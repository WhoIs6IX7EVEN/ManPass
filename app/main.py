import os
import sys
import json
import uuid
import time
import asyncio
import sqlite3
import secrets
import string
import tempfile
import threading
import ctypes
import atexit
import webbrowser
from pathlib import Path
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse

import flet as ft
from updater import RELEASES_URL, fetch_stable_update

try:
    import pystray
except ImportError:
    pystray = None
from PIL import Image, ImageDraw

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_INSTANCE_HANDLE = None
_INSTANCE_FILE = None
_INSTANCE_EVENT = None

def claim_single_instance():
    """Prevent a second GUI from writing to the same vault concurrently."""
    global _INSTANCE_HANDLE, _INSTANCE_FILE
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\ManPass-Single-Instance")
        if not handle:
            raise OSError("Невозможно создать блокировку экземпляра")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            # Signal the already-running application instead of launching
            # a second GUI against the same SQLite database.
            kernel32.OpenEventW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.OpenEventW.restype = ctypes.c_void_p
            kernel32.SetEvent.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            event_name = "Local\\ManPass-Show-Window"
            for _ in range(20):
                existing = kernel32.OpenEventW(0x0002, False, event_name)  # EVENT_MODIFY_STATE
                if existing:
                    kernel32.SetEvent(existing)
                    kernel32.CloseHandle(existing)
                    return False
                time.sleep(0.1)
            raise RuntimeError("ManPass уже запущен, но его окно не ответило.")
        _INSTANCE_HANDLE = handle
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        event = kernel32.CreateEventW(None, True, False, "Local\\ManPass-Show-Window")
        if not event:
            kernel32.CloseHandle(handle)
            raise OSError("Не удалось настроить восстановление окна")
        global _INSTANCE_EVENT
        _INSTANCE_EVENT = event
        atexit.register(lambda: kernel32.CloseHandle(event))
        atexit.register(lambda: kernel32.CloseHandle(handle))
        return True
    else:
        import fcntl
        path = Path.home() / ".manpass_instance.lock"
        f = open(path, "a+b")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise RuntimeError("ManPass уже запущен")
        _INSTANCE_FILE = f
        atexit.register(f.close)
        return True


def windows_idle_seconds():
    """System-wide keyboard/mouse idle duration. None outside Windows."""
    if sys.platform != "win32":
        return None
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    # DWORD tick values wrap; subtraction modulo 2**32 handles wrapping.
    now = kernel32.GetTickCount64()
    return ((int(now) & 0xFFFFFFFF) - info.dwTime) % (2**32) / 1000.0


# =====================================================
# MANPASS 3.1
# Powered by 6IX7EVEN
# =====================================================

APP_NAME = "ManPass"
APP_VERSION = "3.6.2"

APP_DIR = Path.home() / "PasswordVault"
APP_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = APP_DIR / "manpass_config.json"
DEFAULT_DB = APP_DIR / "vault.db"
ICON_PATH = APP_DIR / "manpass.ico"
GUIDE_PATH = APP_DIR / "ManPass_Инструкция.txt"

LOCK_TIMEOUT = 300

CHECK_TEXT = b"PasswordVault verification v1"
AAD_CHECK = b"vault-check-v1"
AAD_ENTRY = b"vault-entry-v1:"
AAD_PROFILE = b"manpass-profile-v1"

CATEGORIES = [
    "Без категории",
    "Сайты",
    "Игры",
    "Соцсети",
    "Почта",
    "Работа",
    "Финансы",
    "Другое"
]

# Colors

BG = "#0D1422"
SIDEBAR = "#151F30"
CARD = "#1B293F"
INPUT = "#24344C"
ACCENT = "#5487EB"
ACCENT_HOVER = "#6A9AF0"
WHITE = "#F2F5FC"
MUTED = "#9AABC7"
BORDER = "#30405A"
SUCCESS = "#72C7A1"
WARNING = "#FFBE71"


# =====================================================
# APP CONFIG
# =====================================================

def read_config():
    if not CONFIG_PATH.exists():
        return {"database": str(DEFAULT_DB)}

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        config = json.load(file)

    return {
        "database": str(
            config.get("database", DEFAULT_DB)
        )
    }


def save_config(config):
    # Записываем настройки атомарно.
    data = json.dumps(
        config,
        ensure_ascii=False,
        indent=2
    )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=APP_DIR,
            suffix=".tmp",
            delete=False
        ) as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            temp_path = Path(temp.name)

        os.replace(temp_path, CONFIG_PATH)

    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


# =====================================================
# ICON
# =====================================================

def create_icon():
    # Создаём иконку щита без внешних изображений.
    if ICON_PATH.exists():
        return

    size = 256

    image = Image.new(
        "RGBA", (size, size), (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(image)

    outer = [
        (128, 18),
        (220, 57),
        (213, 150),
        (182, 201),
        (128, 239),
        (74, 201),
        (43, 150),
        (36, 57)
    ]

    inner = [
        (128, 43),
        (197, 73),
        (191, 143),
        (166, 182),
        (128, 211),
        (90, 182),
        (65, 143),
        (59, 73)
    ]

    draw.polygon(
        outer,
        fill=(73, 139, 244, 255)
    )

    draw.polygon(
        inner,
        fill=(20, 38, 68, 255)
    )

    # Центральная декоративная грань.
    draw.polygon(
        [
            (128, 53),
            (188, 80),
            (182, 140),
            (160, 176),
            (128, 199)
        ],
        fill=(42, 99, 176, 255)
    )

    image.save(
        ICON_PATH,
        format="ICO",
        sizes=[
            (16, 16),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256)
        ]
    )


# =====================================================
# USER GUIDE
# =====================================================

USER_GUIDE = """
MANPASS — РУКОВОДСТВО ПОЛЬЗОВАТЕЛЯ

Версия: 3.1
Powered by 6IX7EVEN

==================================================
1. НАЗНАЧЕНИЕ ПРОГРАММЫ
==================================================

ManPass — локальный менеджер паролей.

Он позволяет хранить:
- название сервиса;
- адрес сайта;
- логин;
- пароль;
- категорию;
- отметку об использовании 2FA;
- заметки;
- избранные записи.

Данные аккаунтов сохраняются в зашифрованном виде
в локальной базе SQLite.

==================================================
2. ПЕРВЫЙ ЗАПУСК
==================================================

При первом запуске необходимо создать мастер-пароль.

Рекомендуется использовать длинную уникальную
парольную фразу.

Мастер-пароль необходим для открытия хранилища.

Если мастер-пароль потерян, восстановление данных
без резервной копии и известного пароля невозможно.

==================================================
3. ВХОД
==================================================

Откройте ManPass.

Введите мастер-пароль.

Нажмите Enter или кнопку «Разблокировать».

Если в профиле сохранено имя, оно будет показано
на экране авторизации.

==================================================
4. ДОБАВЛЕНИЕ АККАУНТА
==================================================

Откройте раздел «Все пароли».

Нажмите кнопку «Добавить».

Заполните:
- название сервиса;
- URL сайта;
- логин;
- пароль;
- категорию;
- отметку 2FA;
- заметку.

Нажмите «Сохранить».

==================================================
5. ГЕНЕРАЦИЯ ПАРОЛЯ
==================================================

В окне редактирования нажмите
«Сгенерировать пароль».

Если пароль уже введён, ManPass запросит
подтверждение его замены.

Новый пароль записывается в базу только после
нажатия «Сохранить».

==================================================
6. ДВУХФАКТОРНАЯ АУТЕНТИФИКАЦИЯ
==================================================

Флажок «2FA включена» предназначен для учёта.

Он не включает двухфакторную аутентификацию
на самом сайте.

Включать 2FA необходимо отдельно в настройках
соответствующего сервиса.

==================================================
7. ПРОФИЛЬ
==================================================

В разделе «Мой профиль» можно:
- изменить отображаемое имя;
- посмотреть статистику;
- создать резервную копию;
- сменить мастер-пароль;
- выбрать папку хранения базы данных.

==================================================
8. ПЕРЕНОС БАЗЫ ДАННЫХ
==================================================

Перед переносом рекомендуется создать
отдельную резервную копию.

В профиле выберите новую папку хранения.

ManPass создаст в ней копию базы, проверит её
и переключит рабочий путь.

Старый файл не удаляется автоматически.

Не запускайте две копии ManPass одновременно.

==================================================
9. РЕЗЕРВНЫЕ КОПИИ
==================================================

Резервная копия содержит зашифрованную базу.

Для открытия копии нужен мастер-пароль,
действовавший на момент её создания.

Храните резервные копии в безопасном месте.

==================================================
10. БЛОКИРОВКА
==================================================

Нажмите «Заблокировать».

Для повторного входа потребуется мастер-пароль.

Программа также предусматривает автоматическую
блокировку при бездействии.

==================================================
11. РЕКОМЕНДАЦИИ ПО БЕЗОПАСНОСТИ
==================================================

Не сообщайте никому мастер-пароль.

Не храните его рядом с базой данных.

Используйте уникальные пароли для разных сайтов.

По возможности включайте 2FA.

Регулярно создавайте резервные копии.

Не используйте программу на заражённом ПК.

==================================================
12. ОГРАНИЧЕНИЯ
==================================================

ManPass является самостоятельным программным
проектом и не проходил независимый аудит.

Программа не защищает от вредоносного ПО,
считывающего экран или оперативную память.

==================================================

Powered by 6IX7EVEN
"""


def create_guide():
    if not GUIDE_PATH.exists():
        GUIDE_PATH.write_text(
            USER_GUIDE.strip(),
            encoding="utf-8"
        )


def open_guide():
    create_guide()

    if sys.platform == "win32":
        os.startfile(str(GUIDE_PATH))
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(GUIDE_PATH)])


# =====================================================
# CRYPTO
# =====================================================

def derive_key(password, salt):
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=32,
        type=Type.ID
    )


def encrypt(key, plaintext, aad):
    nonce = os.urandom(12)

    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext,
        aad
    )

    return nonce + ciphertext


def decrypt(key, blob, aad):
    if not isinstance(blob, bytes) or len(blob) < 29:
        raise ValueError("Повреждённые данные")

    return AESGCM(key).decrypt(
        blob[:12],
        blob[12:],
        aad
    )


def generate_password(length=24):
    alphabet = (
        string.ascii_letters
        + string.digits
        + "!@#$%^&*()-_=+[]{}"
    )

    return "".join(
        secrets.choice(alphabet)
        for _ in range(length)
    )


def validate_url(url):
    url = url.strip()

    if not url:
        return ""

    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            "Ссылка должна начинаться с https:// "
            "или http://"
        )

    if not parsed.hostname:
        raise ValueError("Некорректная ссылка")

    if parsed.username or parsed.password:
        raise ValueError(
            "Ссылки со встроенными логином "
            "или паролем не поддерживаются"
        )

    return url


def analyze_passwords(entries):
    passwords = [
        str(item.get("password", ""))
        for _, item in entries
    ]

    counts = Counter(passwords)

    common = {
        "password",
        "password123",
        "123456",
        "12345678",
        "qwerty",
        "admin",
        "111111",
        "123456789"
    }

    results = []

    for item_id, item in entries:
        password = str(item.get("password", ""))
        reasons = []

        if len(password) < 12:
            reasons.append("Короткий пароль")

        if password.lower() in common:
            reasons.append("Распространённый пароль")

        if password and counts[password] > 1:
            reasons.append("Повторное использование")

        if password and len(set(password)) <= 3:
            reasons.append("Мало различных символов")

        if reasons:
            results.append((
                item_id,
                item.get("site", ""),
                ", ".join(reasons)
            ))

    return results


# =====================================================
# DATABASE
# =====================================================

class VaultDB:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.db = sqlite3.connect(
            self.path,
            timeout=10,
            check_same_thread=False
        )

        self.lock = threading.RLock()

        with self.lock:
            self.db.execute("PRAGMA secure_delete=ON")

            self.db.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    name TEXT PRIMARY KEY,
                    value BLOB NOT NULL
                )
            """)

            self.db.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    encrypted BLOB NOT NULL
                )
            """)

            self.db.commit()

    def meta(self, name):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM metadata WHERE name=?",
                (name,)
            ).fetchone()

        return row[0] if row else None

    def initialized(self):
        return self.meta("salt") is not None

    def setup(self, password):
        if self.initialized():
            raise ValueError(
                "Хранилище уже существует"
            )

        salt = os.urandom(16)
        key = derive_key(password, salt)

        check = encrypt(
            key, CHECK_TEXT, AAD_CHECK
        )

        with self.lock:
            with self.db:
                self.db.execute(
                    "INSERT INTO metadata VALUES (?,?)",
                    ("salt", salt)
                )
                self.db.execute(
                    "INSERT INTO metadata VALUES (?,?)",
                    ("check", check)
                )

        return key

    def unlock(self, password):
        salt = self.meta("salt")
        check = self.meta("check")

        if salt is None or check is None:
            raise ValueError(
                "Хранилище не настроено"
            )

        key = derive_key(password, salt)

        try:
            result = decrypt(
                key, check, AAD_CHECK
            )

            if result != CHECK_TEXT:
                raise ValueError()

        except Exception:
            raise ValueError(
                "Неверный мастер-пароль "
                "или повреждена база"
            ) from None

        return key

    def entries(self, key):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, encrypted FROM entries"
            ).fetchall()

        result = []

        for item_id, blob in rows:
            aad = (
                AAD_ENTRY
                + item_id.encode("ascii")
            )

            raw = decrypt(key, blob, aad)

            item = json.loads(
                raw.decode("utf-8")
            )

            if not isinstance(item, dict):
                raise ValueError(
                    "Повреждён формат записи"
                )

            item.setdefault("site", "")
            item.setdefault("url", "")
            item.setdefault("login", "")
            item.setdefault("password", "")
            item.setdefault("notes", "")
            item.setdefault("category", "Без категории")
            item.setdefault("favorite", False)
            item.setdefault("two_factor", False)

            result.append((item_id, item))

        return result

    def save_entry(self, key, item, item_id=None):
        item_id = item_id or str(uuid.uuid4())

        raw = json.dumps(
            item,
            ensure_ascii=False
        ).encode("utf-8")

        aad = (
            AAD_ENTRY
            + item_id.encode("ascii")
        )

        blob = encrypt(key, raw, aad)

        with self.lock:
            with self.db:
                self.db.execute("""
                    INSERT INTO entries(id, encrypted)
                    VALUES (?,?)
                    ON CONFLICT(id)
                    DO UPDATE SET encrypted=excluded.encrypted
                """, (item_id, blob))

    def delete_entry(self, item_id):
        with self.lock:
            with self.db:
                self.db.execute(
                    "DELETE FROM entries WHERE id=?",
                    (item_id,)
                )

    def profile(self, key):
        blob = self.meta("manpass_profile")

        if blob is None:
            return {"name": ""}

        raw = decrypt(key, blob, AAD_PROFILE)
        data = json.loads(raw.decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Повреждён профиль")

        return data

    def save_profile(self, key, *, name=None, email=None, phone=None):
        """Partial encrypted profile update: never overwrite unrelated fields."""
        profile = dict(self.profile(key))
        if name is not None:
            name = name.strip()
            if len(name) > 60:
                raise ValueError("Имя слишком длинное")
            profile["name"] = name
        if email is not None:
            email = email.strip()
            if len(email) > 254 or (email and ("@" not in email or any(c.isspace() for c in email))):
                raise ValueError("Проверь адрес электронной почты")
            profile["email"] = email
        if phone is not None:
            phone = phone.strip()
            if len(phone) > 40 or (phone and not all(c.isdigit() or c in "+- ()" for c in phone)):
                raise ValueError("Проверь номер телефона")
            profile["phone"] = phone

        raw = json.dumps(profile, ensure_ascii=False).encode("utf-8")
        blob = encrypt(key, raw, AAD_PROFILE)
        with self.lock:
            with self.db:
                self.db.execute("""
                    INSERT INTO metadata(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value
                """, ("manpass_profile", blob))

    def backup_to(self, destination):
        destination = Path(destination).resolve()

        if destination == self.path:
            raise ValueError(
                "Нельзя перезаписать рабочую базу"
            )

        if destination.exists():
            raise FileExistsError(
                "Файл уже существует"
            )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                suffix=".db",
                prefix=".manpass_",
                delete=False
            ) as temp:
                temp_path = Path(temp.name)

            target = sqlite3.connect(temp_path)

            try:
                with self.lock:
                    self.db.backup(target)
            finally:
                target.close()

            # Без перезаписи существующей копии.
            os.link(temp_path, destination)

        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        return destination

    def backup(self):
        directory = APP_DIR / "backups"
        directory.mkdir(
            parents=True,
            exist_ok=True
        )

        name = (
            "ManPass_"
            + datetime.now().strftime("%Y%m%d_%H%M%S")
            + "_"
            + uuid.uuid4().hex[:8]
            + ".db"
        )

        return self.backup_to(directory / name)

    def change_master(self, old_password, new_password):
        if len(new_password) < 14:
            raise ValueError(
                "Новый пароль должен быть "
                "не короче 14 символов"
            )

        old_key = self.unlock(old_password)
        records = self.entries(old_key)

        profile_blob = self.meta("manpass_profile")
        profile_raw = None

        if profile_blob is not None:
            profile_raw = decrypt(
                old_key,
                profile_blob,
                AAD_PROFILE
            )

        backup_path = self.backup()

        new_salt = os.urandom(16)
        new_key = derive_key(
            new_password,
            new_salt
        )

        prepared = []

        for item_id, item in records:
            raw = json.dumps(
                item,
                ensure_ascii=False
            ).encode("utf-8")

            aad = (
                AAD_ENTRY
                + item_id.encode("ascii")
            )

            prepared.append((
                encrypt(new_key, raw, aad),
                item_id
            ))

        new_check = encrypt(
            new_key,
            CHECK_TEXT,
            AAD_CHECK
        )

        new_profile = None

        if profile_raw is not None:
            new_profile = encrypt(
                new_key,
                profile_raw,
                AAD_PROFILE
            )

        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")

                for blob, item_id in prepared:
                    self.db.execute("""
                        UPDATE entries
                        SET encrypted=?
                        WHERE id=?
                    """, (blob, item_id))

                self.db.execute("""
                    UPDATE metadata
                    SET value=?
                    WHERE name='salt'
                """, (new_salt,))

                self.db.execute("""
                    UPDATE metadata
                    SET value=?
                    WHERE name='check'
                """, (new_check,))

                if new_profile is not None:
                    self.db.execute("""
                        UPDATE metadata
                        SET value=?
                        WHERE name='manpass_profile'
                    """, (new_profile,))

                self.db.commit()

            except Exception:
                self.db.rollback()
                raise

        return new_key, backup_path

    def close(self):
        with self.lock:
            self.db.close()


# =====================================================
# MAIN APPLICATION
# =====================================================

async def main(page: ft.Page):
    create_icon()
    create_guide()

    config = read_config()
    db = VaultDB(config["database"])

    page.title = APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.padding = 0

    page.window.width = 1200
    page.window.height = 780
    page.window.min_width = 780
    page.window.min_height = 550

    page.window.title_bar_hidden = True
    page.window.icon = str(ICON_PATH)
    # Always present the main window on a normal application launch.
    # Hiding is reserved for an explicit close-to-tray action.
    page.window.visible = True
    page.window.minimized = False

    picker = ft.FilePicker()

    state = {
        "key": None,
        "items": {},
        "section": "all",
        "category": None,
        "generation": 0,
        "last_activity": time.monotonic(),
        "busy": False,
        "dialogs": [],
        "focus_search": False,
        "update": None,
        "update_error": None,
        "update_checking": False,
        "update_notified": False,
    }

    # The pystray thread only posts commands. All Flet actions run on the
    # asyncio loop used by main(), never on the tray's worker thread.
    ui_loop = asyncio.get_running_loop()
    tray_queue = asyncio.Queue()
    tray_icon = None
    state["exiting"] = False

    def send_tray_command(command):
        if not state["exiting"]:
            ui_loop.call_soon_threadsafe(tray_queue.put_nowait, command)

    async def restore_from_shortcut():
        """Handle Windows signal from a second shortcut launch on the Flet UI loop."""
        if sys.platform != "win32" or _INSTANCE_EVENT is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
        while not state["exiting"]:
            if kernel32.WaitForSingleObject(_INSTANCE_EVENT, 0) == 0:
                kernel32.ResetEvent(_INSTANCE_EVENT)
                send_tray_command("show")
            await asyncio.sleep(0.2)

    def start_tray():
        nonlocal tray_icon
        if sys.platform != "win32" or pystray is None:
            return
        try:
            image = Image.open(ICON_PATH).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("Открыть ManPass", lambda icon, item: send_tray_command("show"), default=True),
                pystray.MenuItem("Заблокировать", lambda icon, item: send_tray_command("lock")),
                pystray.MenuItem("Быстрый поиск", lambda icon, item: send_tray_command("search")),
                pystray.MenuItem("Генератор пароля", lambda icon, item: send_tray_command("generator")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Полностью выйти", lambda icon, item: send_tray_command("exit")),
            )
            tray_icon = pystray.Icon("ManPass", image, "ManPass", menu)
            threading.Thread(target=tray_icon.run, name="ManPassTray", daemon=True).start()
        except Exception as exc:
            print("Не удалось запустить трей:", exc)
            tray_icon = None

    # ================================================
    # UI HELPERS
    # ================================================

    def txt(value, size=14, color=WHITE, bold=False):
        return ft.Text(
            str(value),
            size=size,
            color=color,
            weight=(
                ft.FontWeight.W_600
                if bold else ft.FontWeight.NORMAL
            )
        )

    def panel(content, padding=20):
        return ft.Container(
            bgcolor=CARD,
            padding=padding,
            border_radius=16,
            content=content
        )

    def btn(label, callback, icon=None):
        controls = []

        if icon is not None:
            controls.append(
                ft.Icon(icon, size=17)
            )

        controls.append(
            txt(label, 13, bold=True)
        )

        return ft.Button(
            content=ft.Row(
                controls,
                spacing=8,
                tight=True
            ),
            on_click=callback
        )

    def field(label, value="", password=False):
        return ft.TextField(
            label=label,
            value=value,
            password=password,
            can_reveal_password=password,
            bgcolor=INPUT,
            text_size=14,
            border=ft.OutlineInputBorder(
                border_radius=10,
                side=ft.BorderSide(
                    width=1,
                    color=BORDER
                )
            )
        )

    def activity(e=None):
        state["last_activity"] = time.monotonic()

    def show_dialog(dialog):
        activity()
        state["dialogs"].append(dialog)
        page.show_dialog(dialog)

    def close_dialog():
        if state["dialogs"]:
            dialog = state["dialogs"].pop()
            # Do not leave references to secret controls in retained dialogs.
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dialog.open = False
                    page.update()
                except Exception:
                    pass
        activity()

    def close_all_dialogs():
        while state["dialogs"]:
            close_dialog()

    def info(title, description):
        dialog = ft.AlertDialog(
            modal=True,
            title=txt(title, 18, bold=True),
            content=txt(description, 13, MUTED),
            actions=[
                ft.TextButton(
                    "Понятно",
                    on_click=lambda e: close_dialog()
                )
            ]
        )
        show_dialog(dialog)

    def show_guide(e=None):
        try:
            open_guide()
        except Exception as error:
            info("Ошибка", str(error))

    def clickable_url(raw_url):
        """Clickable validated HTTP(S) link; no automatic navigation."""
        if not raw_url:
            return ft.Container(height=0)
        try:
            safe_url = validate_url(str(raw_url))
        except ValueError:
            return txt("Некорректная ссылка", 12, WARNING)
        return ft.TextButton(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.OPEN_IN_NEW, size=14, color=ACCENT),
                    ft.Text(
                        safe_url,
                        size=12,
                        color=ACCENT,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        max_lines=1,
                    ),
                ],
                tight=True,
                spacing=5,
            ),
            tooltip=safe_url,
            action=ft.OpenUrl(safe_url, target=ft.UrlTarget.BLANK),
        )

    # ================================================
    # CUSTOM WINDOW BAR
    # ================================================

    async def minimize_window(e):
        page.window.minimized = True
        page.update()

    async def maximize_window(e):
        page.window.maximized = (
            not page.window.maximized
        )
        page.update()

    async def exit_app():
        if state["exiting"]:
            return
        state["exiting"] = True
        state["items"].clear()
        state["key"] = None
        if tray_icon is not None:
            await asyncio.to_thread(tray_icon.stop)
        db.close()
        await page.window.close()

    async def close_window(e):
        # The close button minimizes to tray, and always locks first.
        if tray_icon is None:
            await exit_app()
            return
        if state["busy"]:
            return
        if state["key"] is not None:
            lock()
        page.window.visible = False
        page.update()

    async def tray_worker():
        while not state["exiting"]:
            command = await tray_queue.get()
            if command == "exit":
                await exit_app()
                break
            if command == "lock":
                if state["key"] is not None and not state["busy"]:
                    lock()
                continue
            page.window.visible = True
            page.window.minimized = False
            page.update()
            if command == "search" and state["key"] is not None:
                state["section"] = "all"
                state["focus_search"] = True
                render_app()
            elif command == "generator":
                open_tray_generator()

    def open_tray_generator():
        # No password is pushed to a tray menu, toast or clipboard.
        if state["key"] is None:
            render_login()
            return
        generated = generate_password(24)
        secret_field = field("Сгенерированный пароль", generated, password=True)
        secret_field.read_only = True
        show_dialog(ft.AlertDialog(
            modal=True, title=txt("Генератор паролей", 18, bold=True),
            content=ft.Container(width=390, content=secret_field),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: close_dialog())],
        ))

    def open_official_release(e=None):
        # URL is independently checked by updater.py when fetched.
        result = state.get("update")
        url = result["url"] if result else RELEASES_URL
        webbrowser.open(url)

    def show_update_notification():
        result = state.get("update")
        if not result or state["key"] is None or state["update_notified"]:
            return
        if state["dialogs"]:
            return
        state["update_notified"] = True
        show_dialog(ft.AlertDialog(
            modal=True,
            title=txt("Доступно обновление ManPass", 19, bold=True),
            content=ft.Container(width=390, content=ft.Column([
                txt(f"Новая стабильная версия: {result['version']}", 15, bold=True),
                txt(f"Установлена: {APP_VERSION}", 13, MUTED),
                txt("Скачай установщик только с официальной страницы релиза GitHub. Текущая база данных не изменяется.", 13, MUTED),
            ], spacing=12, tight=True)),
            actions=[
                ft.TextButton("Позже", on_click=lambda e: close_dialog()),
                ft.TextButton("Открыть релиз", on_click=lambda e: (close_dialog(), open_official_release())),
            ]
        ))

    async def check_updates(manual=False):
        if state["update_checking"] or state["exiting"]:
            return
        state["update_checking"] = True
        state["update_error"] = None
        if manual and state["key"] is not None and state["section"] == "updates":
            render_app()
        try:
            result = await asyncio.to_thread(fetch_stable_update, APP_VERSION)
            state["update"] = result
            # Update sidebar as soon as a newer stable release is confirmed.
            if state["key"] is not None and not state["dialogs"] and result:
                render_app()
            if result and state["key"] is not None and not manual:
                show_update_notification()
        except Exception as exc:
            # Network failure never prevents authentication or accessing the vault.
            state["update_error"] = "Не удалось проверить обновления. Проверь подключение к интернету или доступность GitHub."
        finally:
            state["update_checking"] = False
            if state["key"] is not None and state["section"] == "updates":
                render_app()
            if manual and state["key"] is not None and state["section"] != "updates":
                info("Проверка обновлений", state["update_error"] or (f"Доступна версия {state['update']['version']}" if state["update"] else "Установлена последняя стабильная версия."))

    def updates_page():
        result = state["update"]
        if state["update_checking"]:
            status = "Проверяем GitHub Releases..."
        elif state["update_error"]:
            status = state["update_error"]
        elif result:
            status = f"Доступна новая стабильная версия: {result['version']}"
        else:
            status = "Новых стабильных версий не найдено (по последней проверке)."
        return ft.Column([
            txt("Обновления", 27, bold=True),
            panel(ft.Column([
                txt(f"Текущая версия: {APP_VERSION}", 17, bold=True),
                txt("Канал: стабильный · Проверка при запуске включена", 13, MUTED),
                txt(status, 14, MUTED),
                ft.Row([
                    btn("Проверить обновления", lambda e: page.run_task(check_updates, True), ft.Icons.REFRESH),
                    btn("Официальные релизы", open_official_release, ft.Icons.OPEN_IN_NEW),
                ], spacing=12, wrap=True),
                txt("Обновления не устанавливаются автоматически. Мастер-пароль и содержимое базы не отправляются на GitHub.", 12, MUTED),
            ], spacing=17)),
        ], spacing=20, expand=True, scroll=ft.ScrollMode.AUTO)

    def title_bar():
        drag_area = ft.WindowDragArea(
            expand=True,
            content=ft.Container(
                height=44,
                padding=ft.Padding.symmetric(
                    horizontal=15
                ),
                content=ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.SHIELD_OUTLINED,
                            color=ACCENT,
                            size=20
                        ),
                        txt(
                            "ManPass",
                            14,
                            bold=True
                        ),
                    ],
                    spacing=9
                )
            )
        )

        return ft.Container(
            bgcolor=SIDEBAR,
            content=ft.Row(
                [
                    drag_area,
                    ft.IconButton(
                        ft.Icons.REMOVE,
                        tooltip="Свернуть",
                        on_click=minimize_window
                    ),
                    ft.IconButton(
                        ft.Icons.CROP_SQUARE,
                        tooltip="Развернуть",
                        on_click=maximize_window
                    ),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        tooltip="Закрыть",
                        on_click=close_window
                    )
                ],
                spacing=3
            )
        )

    # ================================================
    # STATE / LOGIN
    # ================================================

    def load_entries():
        state["items"] = dict(
            db.entries(state["key"])
        )

    def lock(e=None):
        if state["busy"] or state["key"] is None:
            return

        close_all_dialogs()

        state["generation"] += 1
        state["items"].clear()
        state["key"] = None
        state["section"] = "all"
        state["category"] = None

        render_login()

    def render_login():
        page.clean()

        first = not db.initialized()

        # Имя хранится зашифрованным.
        # До ввода мастер-пароля его нельзя
        # прочитать без отдельного хранения
        # в открытом виде.
        password = field(
            "Мастер-пароль",
            password=True
        )
        password.autofocus = True

        confirm = (
            field("Повторите пароль", password=True)
            if first else None
        )

        def login(e):
            value = password.value or ""

            if first:
                if len(value) < 14:
                    info(
                        "Ошибка",
                        "Минимум 14 символов."
                    )
                    return

                if value != confirm.value:
                    info(
                        "Ошибка",
                        "Пароли не совпадают."
                    )
                    return

            try:
                key = (
                    db.setup(value)
                    if first
                    else db.unlock(value)
                )

                records = db.entries(key)
                profile = db.profile(key)

                state["key"] = key
                state["items"] = dict(records)
                state["generation"] += 1

                password.value = ""

                if confirm:
                    confirm.value = ""

                activity()
                render_app()
                show_update_notification()

            except Exception as error:
                info(
                    "Ошибка авторизации",
                    str(error)
                )

        password.on_submit = login

        if confirm:
            confirm.on_submit = login

        controls = [
            ft.Icon(
                ft.Icons.SHIELD_OUTLINED,
                size=55,
                color=ACCENT
            ),
            txt("ManPass", 30, bold=True),
            txt(
                "Создание хранилища"
                if first
                else "Введите мастер-пароль",
                14,
                MUTED
            ),
            ft.Container(height=10),
            password
        ]

        if confirm:
            controls.append(confirm)

        controls.extend([
            btn(
                "Создать хранилище"
                if first
                else "Разблокировать",
                login,
                ft.Icons.LOCK_OPEN
            ),
            ft.TextButton(
                "Инструкция пользователя",
                on_click=show_guide
            ),
            txt(
                "Powered by 6IX7EVEN",
                11,
                MUTED
            )
        ])

        page.add(
            title_bar(),
            ft.Container(
                expand=True,
                alignment=ft.Alignment.CENTER,
                content=ft.Container(
                    width=430,
                    padding=32,
                    bgcolor=CARD,
                    border_radius=20,
                    content=ft.Column(
                        controls,
                        spacing=17,
                        tight=True,
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        )
                    )
                )
            )
        )

        page.update()

    # ================================================
    # EDITOR
    # ================================================

    def show_editor(item_id=None):
        if state["key"] is None:
            return

        activity()
        generation = state["generation"]

        item = (
            dict(state["items"][item_id])
            if item_id
            else {}
        )

        site = field(
            "Название сервиса",
            item.get("site", "")
        )

        url = field(
            "Ссылка на сервис",
            item.get("url", "")
        )

        login = field(
            "Логин",
            item.get("login", "")
        )

        secret = field(
            "Пароль",
            item.get("password", ""),
            password=True
        )

        category_value = item.get(
            "category",
            "Без категории"
        )

        options = list(CATEGORIES)

        if category_value not in options:
            options.append(category_value)

        category = ft.Dropdown(
            label="Категория",
            value=category_value,
            options=[
                ft.DropdownOption(
                    key=c,
                    text=c
                )
                for c in options
            ],
            bgcolor=INPUT
        )

        favorite = ft.Checkbox(
            label="Добавить в избранное",
            value=bool(
                item.get("favorite", False)
            )
        )

        two_factor = ft.Checkbox(
            label="На сервисе включена 2FA",
            value=bool(
                item.get("two_factor", False)
            )
        )

        notes = field(
            "Заметка",
            item.get("notes", "")
        )

        def apply_generated():
            if (
                state["key"] is None
                or state["generation"] != generation
            ):
                return

            secret.value = generate_password(24)
            secret.update()

        def generate(e):
            activity()

            if not secret.value:
                apply_generated()
                return

            def confirmed(e):
                close_dialog()
                apply_generated()

            show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=txt(
                        "Заменить пароль?",
                        18,
                        bold=True
                    ),
                    content=txt(
                        "В поле уже введён пароль. "
                        "Заменить его новым случайным?",
                        14,
                        MUTED
                    ),
                    actions=[
                        ft.TextButton(
                            "Отмена",
                            on_click=lambda e:
                            close_dialog()
                        ),
                        ft.TextButton(
                            "Заменить",
                            on_click=confirmed
                        )
                    ]
                )
            )

        def save(e):
            if (
                state["key"] is None
                or state["generation"] != generation
            ):
                return

            if not (site.value or "").strip():
                info(
                    "Ошибка",
                    "Укажи название сервиса."
                )
                return

            if not secret.value:
                info(
                    "Ошибка",
                    "Пароль не может быть пустым."
                )
                return

            try:
                valid_url = validate_url(
                    url.value or ""
                )

                updated = dict(item)

                updated.update({
                    "site": site.value.strip(),
                    "url": valid_url,
                    "login": login.value or "",
                    "password": secret.value,
                    "category": category.value,
                    "favorite": bool(favorite.value),
                    "two_factor": bool(two_factor.value),
                    "notes": notes.value or ""
                })

                db.save_entry(
                    state["key"],
                    updated,
                    item_id
                )

                close_dialog()
                load_entries()
                render_app()

            except Exception as error:
                info(
                    "Ошибка сохранения",
                    str(error)
                )

        editor = ft.AlertDialog(
            modal=True,
            title=txt(
                "Редактирование аккаунта"
                if item_id
                else "Новый аккаунт",
                19,
                bold=True
            ),
            content=ft.Container(
                width=420,
                height=490,
                content=ft.Column(
                    [
                        site,
                        url,
                        login,
                        secret,
                        btn(
                            "Сгенерировать пароль",
                            generate,
                            ft.Icons.AUTO_AWESOME
                        ),
                        category,
                        favorite,
                        two_factor,
                        notes
                    ],
                    spacing=12,
                    scroll=ft.ScrollMode.AUTO
                )
            ),
            actions=[
                ft.TextButton(
                    "Отмена",
                    on_click=lambda e: close_dialog()
                ),
                btn(
                    "Сохранить",
                    save,
                    ft.Icons.SAVE
                )
            ]
        )

        show_dialog(editor)

    # ================================================
    # DELETE / VIEW
    # ================================================

    def delete_entry(item_id):
        if state["key"] is None:
            return

        generation = state["generation"]

        def confirmed(e):
            if (
                state["key"] is None
                or generation != state["generation"]
            ):
                return

            try:
                db.delete_entry(item_id)
                close_dialog()
                load_entries()
                render_app()
            except Exception as error:
                info("Ошибка", str(error))

        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=txt(
                    "Удалить аккаунт?",
                    18,
                    bold=True
                ),
                content=txt(
                    "Запись будет удалена "
                    "из хранилища.",
                    14,
                    MUTED
                ),
                actions=[
                    ft.TextButton(
                        "Отмена",
                        on_click=lambda e: close_dialog()
                    ),
                    ft.TextButton(
                        "Удалить",
                        on_click=confirmed
                    )
                ]
            )
        )

    def view_password(item_id):
        if state["key"] is None or item_id not in state["items"]:
            return
        activity()
        item = state["items"][item_id]
        generation = state["generation"]
        site_name = str(item.get("site", "Без названия"))
        site_url = str(item.get("url", ""))
        site_login = str(item.get("login", ""))
        password_field = field("Пароль", str(item.get("password", "")), password=True)
        password_field.read_only = True

        def dismiss(e):
            password_field.value = ""
            if state["generation"] == generation:
                close_dialog()

        def copy_button(value, label):
            return ft.IconButton(
                ft.Icons.CONTENT_COPY,
                tooltip=f"Скопировать {label.lower()}",
                disabled=not bool(value),
                action=ft.CopyToClipboard(value),
            )

        content = ft.Column(
            controls=[
                txt("СЕРВИС", 12, ACCENT, True),
                ft.Row(
                    controls=[
                        ft.Container(content=txt(site_name, 15, WHITE, True), expand=True),
                        copy_button(site_name, "название сервиса"),
                    ], spacing=8,
                ),
                txt("ЛОГИН", 12, ACCENT, True),
                ft.Row(
                    controls=[
                        ft.Container(content=txt(site_login or "Не указан", 15, WHITE if site_login else MUTED), expand=True),
                        copy_button(site_login, "логин"),
                    ], spacing=8,
                ),
                txt("АДРЕС САЙТА", 12, ACCENT, True),
                clickable_url(site_url) if site_url else txt(
                    "Ссылка не указана", 13, MUTED
                ),
                ft.Container(height=5),
                password_field,
            ],
            tight=True,
            spacing=8,
        )
        show_dialog(ft.AlertDialog(
            modal=True,
            title=txt("Данные аккаунта", 19, bold=True),
            content=ft.Container(width=420, content=content),
            actions=[ft.TextButton("Закрыть", on_click=dismiss)],
        ))

    # ================================================
    # NAVIGATION
    # ================================================

    def switch(section, category=None):
        if state["key"] is None:
            return

        activity()
        state["section"] = section
        state["category"] = category

        render_app()

    def nav(label, icon, section, category=None):
        selected = (
            state["section"] == section
            and (
                section != "category"
                or state["category"] == category
            )
        )

        return ft.Container(
            bgcolor=(
                "#2E4D7B"
                if selected
                else SIDEBAR
            ),
            border_radius=10,
            padding=11,
            on_click=lambda e: switch(
                section,
                category
            ),
            content=ft.Row(
                [
                    ft.Icon(
                        icon,
                        size=19,
                        color=(
                            WHITE if selected
                            else MUTED
                        )
                    ),
                    txt(
                        label,
                        13,
                        WHITE if selected else MUTED,
                        selected
                    )
                ],
                spacing=12
            )
        )

    def stat(icon, number, label):
        return ft.Container(
            bgcolor=CARD,
            border_radius=15,
            padding=20,
            expand=True,
            content=ft.Column(
                [
                    ft.Icon(
                        icon,
                        color=ACCENT,
                        size=24
                    ),
                    txt(
                        number,
                        29,
                        bold=True
                    ),
                    txt(
                        label,
                        12,
                        MUTED
                    )
                ],
                spacing=9
            )
        )

    # ================================================
    # VAULT PAGE
    # ================================================

    def vault_page():
        section = state["section"]
        selected_category = state["category"]

        title = (
            "Все пароли"
            if section == "all"
            else "Избранное"
            if section == "favorites"
            else selected_category
        )

        search = field("Поиск по аккаунтам")
        if state.pop("focus_search", False):
            search.autofocus = True

        cards = ft.Column(spacing=10, expand=True, scroll=ft.ScrollMode.AUTO)
        visible_limit = [100]
        search_serial = [0]
        search_epoch = state["generation"]

        def populate(e=None):
            query = (
                search.value or ""
            ).casefold()

            cards.controls.clear()

            entries = sorted(
                state["items"].items(),
                key=lambda pair: str(
                    pair[1].get("site", "")
                ).casefold()
            )

            matches = 0
            for item_id, item in entries:
                site = str(item.get("site", ""))
                username = str(item.get("login", ""))
                url = str(item.get("url", ""))

                category = item.get(
                    "category",
                    "Без категории"
                )

                favorite = bool(
                    item.get("favorite", False)
                )

                enabled_2fa = bool(
                    item.get("two_factor", False)
                )

                if query not in (
                    site + " " + username + " " + url
                ).casefold():
                    continue

                if (
                    section == "favorites"
                    and not favorite
                ):
                    continue

                if (
                    section == "category"
                    and selected_category != category
                ):
                    continue

                extra = []

                if enabled_2fa:
                    extra.append("2FA включена")

                extra.append(category)

                subtitle = (
                    username
                    + "  ·  "
                    + "  ·  ".join(extra)
                )

                matches += 1
                if matches > visible_limit[0]:
                    continue
                cards.controls.append(
                    panel(
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.LANGUAGE,
                                    color=ACCENT,
                                    size=25
                                ),
                                ft.Column(
                                    [
                                        txt(
                                            site,
                                            15,
                                            bold=True
                                        ),
                                        txt(
                                            subtitle,
                                            12,
                                            MUTED
                                        ),
                                        clickable_url(url) if url else
                                        ft.Container(height=0)
                                    ],
                                    spacing=5,
                                    expand=True
                                ),
                                ft.IconButton(
                                    ft.Icons.VISIBILITY_OUTLINED,
                                    tooltip="Посмотреть пароль",
                                    on_click=lambda e, i=item_id:
                                    view_password(i)
                                ),
                                ft.IconButton(
                                    ft.Icons.EDIT_OUTLINED,
                                    tooltip="Редактировать",
                                    on_click=lambda e, i=item_id:
                                    show_editor(i)
                                ),
                                ft.IconButton(
                                    ft.Icons.DELETE_OUTLINE,
                                    tooltip="Удалить",
                                    on_click=lambda e, i=item_id:
                                    delete_entry(i)
                                )
                            ],
                            spacing=12
                        ),
                        padding=15
                    )
                )

            if matches > visible_limit[0]:
                remaining = matches - visible_limit[0]
                def show_more(e):
                    activity()
                    visible_limit[0] += 100
                    populate()
                    cards.update()
                cards.controls.append(
                    btn(f"Показать ещё ({remaining})", show_more, ft.Icons.EXPAND_MORE)
                )

            if not cards.controls:
                cards.controls.append(
                    ft.Container(
                        padding=40,
                        alignment=ft.Alignment.CENTER,
                        content=txt(
                            "Здесь пока нет записей",
                            15,
                            MUTED
                        )
                    )
                )

        populate()
        async def delayed_search(serial, generation):
            await asyncio.sleep(0.2)
            if serial != search_serial[0] or generation != state["generation"]:
                return
            visible_limit[0] = 100
            populate()
            cards.update()

        def schedule_search(e):
            activity()
            search_serial[0] += 1
            page.run_task(delayed_search, search_serial[0], search_epoch)

        search.on_change = schedule_search

        return ft.Column(
            [
                ft.Row(
                    [
                        txt(title, 27, bold=True),
                        ft.Container(expand=True),
                        btn(
                            "Добавить",
                            lambda e: show_editor(),
                            ft.Icons.ADD
                        )
                    ]
                ),
                search,
                cards
            ],
            spacing=20,
            expand=True
        )

    # ================================================
    # DATABASE LOCATION
    # ================================================

    async def choose_db_folder(e):
        nonlocal db, config

        if state["key"] is None:
            return

        activity()

        folder = await picker.get_directory_path(
            dialog_title="Выберите папку для базы ManPass"
        )

        if not folder:
            return

        destination = (
            Path(folder).expanduser().resolve()
            / "vault.db"
        )

        if destination == db.path:
            info(
                "Информация",
                "База уже находится в этой папке."
            )
            return

        if destination.exists():
            info(
                "Файл существует",
                "В выбранной папке уже есть vault.db. "
                "Для безопасности он не будет заменён."
            )
            return

        state["busy"] = True

        new_db = None

        try:
            # Убеждаемся, что текущие данные
            # полностью расшифровываются.
            db.entries(state["key"])
            db.profile(state["key"])

            # Создаём согласованную копию SQLite.
            db.backup_to(destination)

            # Проверяем новую базу.
            new_db = VaultDB(destination)

            result = new_db.db.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if result is None or result[0] != "ok":
                raise ValueError(
                    "Проверка целостности не пройдена"
                )

            new_db.entries(state["key"])
            new_db.profile(state["key"])

            # Меняем путь только после проверки.
            new_config = dict(config)
            new_config["database"] = str(destination)

            save_config(new_config)

            old_db = db
            db = new_db
            config = new_config
            new_db = None

            old_db.close()

            render_app()

            info(
                "База перенесена",
                "Новое расположение:\n"
                + str(destination)
                + "\n\nИсходная база сохранена."
            )

        except Exception as error:
            if new_db is not None:
                new_db.close()

            info(
                "Ошибка переноса",
                str(error)
            )

        finally:
            state["busy"] = False

    # ================================================
    # MASTER PASSWORD
    # ================================================

    def change_master_dialog(e):
        if state["key"] is None:
            return

        old = field(
            "Текущий мастер-пароль",
            password=True
        )

        new = field(
            "Новый мастер-пароль",
            password=True
        )

        confirm = field(
            "Повторите новый пароль",
            password=True
        )

        generation = state["generation"]

        def submit(e):
            if (
                state["key"] is None
                or generation != state["generation"]
            ):
                return

            if new.value != confirm.value:
                info(
                    "Ошибка",
                    "Новые пароли не совпадают."
                )
                return

            if old.value == new.value:
                info(
                    "Ошибка",
                    "Новый пароль должен отличаться."
                )
                return

            state["busy"] = True

            try:
                new_key, backup_path = db.change_master(
                    old.value or "",
                    new.value or ""
                )

                state["key"] = new_key
                state["generation"] += 1
                activity()

                old.value = ""
                new.value = ""
                confirm.value = ""

                close_dialog()
                load_entries()
                render_app()

                info(
                    "Мастер-пароль изменён",
                    "Резервная копия:\n"
                    + str(backup_path)
                )

            except Exception as error:
                info(
                    "Ошибка",
                    str(error)
                )

            finally:
                state["busy"] = False

        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=txt(
                    "Смена мастер-пароля",
                    18,
                    bold=True
                ),
                content=ft.Container(
                    width=390,
                    content=ft.Column(
                        [
                            old,
                            new,
                            confirm,
                            txt(
                                "Перед изменением будет "
                                "создана резервная копия.",
                                12,
                                MUTED
                            )
                        ],
                        spacing=12,
                        tight=True
                    )
                ),
                actions=[
                    ft.TextButton(
                        "Отмена",
                        on_click=lambda e: close_dialog()
                    ),
                    btn(
                        "Изменить",
                        submit,
                        ft.Icons.LOCK_RESET
                    )
                ]
            )
        )

    # ================================================
    # PROFILE
    # ================================================

    def profile_page():
        profile = db.profile(state["key"])

        name = str(
            profile.get("name", "")
        )

        records = list(
            state["items"].items()
        )

        total = len(records)

        favorites = sum(
            bool(item.get("favorite", False))
            for _, item in records
        )

        categories = len({
            item.get("category", "Без категории")
            for _, item in records
        })

        issues = len(
            analyze_passwords(records)
        )

        two_factor_count = sum(
            bool(item.get("two_factor", False))
            for _, item in records
        )

        name_field = field("Имя пользователя", name)
        email_field = field("Электронная почта", str(profile.get("email", "")))
        phone_field = field("Номер телефона", str(profile.get("phone", "")))

        def save_name(e):
            if state["key"] is None:
                return
            try:
                db.save_profile(state["key"], name=name_field.value or "")
                activity()
                render_app()
                info("Профиль", "Имя сохранено")
            except Exception as error:
                info("Ошибка", str(error))

        def save_contacts(e):
            if state["key"] is None:
                return
            try:
                db.save_profile(
                    state["key"],
                    email=email_field.value or "",
                    phone=phone_field.value or "",
                )
                activity()
                render_app()
                info("Контакты", "Почта и телефон сохранены в зашифрованной базе")
            except Exception as error:
                info("Ошибка", str(error))

        def backup(e):
            try:
                db.entries(state["key"])
                db.profile(state["key"])

                path = db.backup()

                info(
                    "Резервная копия создана",
                    str(path)
                )

            except Exception as error:
                info("Ошибка", str(error))

        welcome = panel(
            ft.Row(
                [
                    ft.CircleAvatar(
                        radius=30,
                        bgcolor="#365585",
                        content=ft.Icon(
                            ft.Icons.PERSON,
                            color=WHITE,
                            size=30
                        )
                    ),
                    ft.Column(
                        [
                            txt(
                                f"Привет, {name}!"
                                if name else "Привет!",
                                23,
                                bold=True
                            ),
                            txt(
                                "Твоё личное защищённое пространство",
                                13,
                                MUTED
                            )
                        ],
                        spacing=6
                    )
                ],
                spacing=20
            ),
            padding=24
        )

        settings = ft.ResponsiveRow(
            controls=[
                ft.Container(
                    content=panel(ft.Column([
                        txt("Настройки профиля", 19, bold=True),
                        name_field,
                        btn("Сохранить профиль", save_name, ft.Icons.SAVE_OUTLINED),
                    ], spacing=14)),
                    col={"xs": 12, "md": 6},
                ),
                ft.Container(
                    content=panel(ft.Column([
                        txt("Контактные данные", 19, bold=True),
                        email_field,
                        phone_field,
                        txt("Контакты сохраняются зашифрованными в вашей базе.", 12, MUTED),
                        btn("Сохранить контакты", save_contacts, ft.Icons.SAVE_OUTLINED),
                    ], spacing=14)),
                    col={"xs": 12, "md": 6},
                ),
            ],
            spacing=12,
            run_spacing=12,
        )

        statistics = ft.Column(
            [
                ft.Row(
                    [
                        stat(
                            ft.Icons.KEY_OUTLINED,
                            total,
                            "Всего паролей"
                        ),
                        stat(
                            ft.Icons.STAR_OUTLINE,
                            favorites,
                            "Избранное"
                        )
                    ],
                    spacing=12
                ),
                ft.Row(
                    [
                        stat(
                            ft.Icons.FOLDER_OUTLINED,
                            categories,
                            "Категории"
                        ),
                        stat(
                            ft.Icons.SHIELD_OUTLINED,
                            issues,
                            "Предупреждения"
                        )
                    ],
                    spacing=12
                ),
                panel(
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.VERIFIED_USER_OUTLINED,
                                color=SUCCESS
                            ),
                            txt(
                                f"Записей с включённой 2FA: "
                                f"{two_factor_count}",
                                14
                            )
                        ]
                    )
                )
            ],
            spacing=12
        )

        database_card = panel(
            ft.Column(
                [
                    txt(
                        "Расположение базы данных",
                        18,
                        bold=True
                    ),
                    txt(
                        str(db.path),
                        12,
                        MUTED
                    ),
                    txt(
                        "При выборе новой папки исходная "
                        "база останется на месте.",
                        12,
                        MUTED
                    ),
                    btn(
                        "Выбрать другую папку",
                        choose_db_folder,
                        ft.Icons.FOLDER_OPEN_OUTLINED
                    )
                ],
                spacing=14
            )
        )

        security = panel(
            ft.Column(
                [
                    txt(
                        "Безопасность",
                        19,
                        bold=True
                    ),
                    ft.Row(
                        [
                            btn(
                                "Сменить мастер-пароль",
                                change_master_dialog,
                                ft.Icons.LOCK_RESET
                            ),
                            btn(
                                "Резервная копия",
                                backup,
                                ft.Icons.BACKUP_OUTLINED
                            )
                        ],
                        spacing=10,
                        wrap=True
                    )
                ],
                spacing=15
            )
        )

        database_card.height = 238
        security.height = 238

        return ft.Column(
            [
                txt(
                    "Мой профиль",
                    27,
                    bold=True
                ),
                welcome,
                settings,
                txt(
                    "Обзор хранилища",
                    20,
                    bold=True
                ),
                statistics,
                ft.ResponsiveRow(
                    controls=[
                        ft.Container(
                            content=database_card,
                            col={"xs": 12, "md": 6},
                        ),
                        ft.Container(
                            content=security,
                            col={"xs": 12, "md": 6},
                        ),
                    ],
                    spacing=12,
                    run_spacing=12,
                ),
            ],
            spacing=18,
            expand=True,
            scroll=ft.ScrollMode.AUTO
        )

    # ================================================
    # ANALYSIS
    # ================================================

    def analysis_page():
        problems = analyze_passwords(
            list(state["items"].items())
        )

        rows = []

        for _, site, reason in problems:
            rows.append(
                panel(
                    ft.Column(
                        [
                            txt(
                                site,
                                15,
                                bold=True
                            ),
                            txt(
                                reason,
                                13,
                                WARNING
                            )
                        ],
                        spacing=7
                    )
                )
            )

        if not rows:
            rows.append(
                panel(
                    txt(
                        "Простых проблем не найдено.",
                        14,
                        SUCCESS
                    )
                )
            )

        return ft.Column(
            [
                txt(
                    "Анализ безопасности",
                    27,
                    bold=True
                ),
                txt(
                    f"Проблемных записей: {len(problems)}",
                    14,
                    MUTED
                ),
                ft.Column(
                    rows,
                    spacing=12,
                    expand=True,
                    scroll=ft.ScrollMode.AUTO
                )
            ],
            spacing=20,
            expand=True
        )

    # ================================================
    # APP RENDERING
    # ================================================

    def render_app():
        if state["key"] is None:
            render_login()
            return

        page.clean()

        profile = db.profile(state["key"])
        name = profile.get("name", "")

        sidebar = ft.Container(
            width=215,
            bgcolor=SIDEBAR,
            padding=16,
            content=ft.Column(
                [
                    ft.Container(height=15),
                    txt(
                        "ХРАНИЛИЩЕ",
                        11,
                        MUTED
                    ),
                    nav(
                        "Все пароли",
                        ft.Icons.KEY_OUTLINED,
                        "all"
                    ),
                    nav(
                        "Избранное",
                        ft.Icons.STAR_OUTLINE,
                        "favorites"
                    ),
                    ft.Container(height=8),
                    txt(
                        "КАТЕГОРИИ",
                        11,
                        MUTED
                    ),
                    *[
                        nav(
                            category,
                            ft.Icons.FOLDER_OUTLINED,
                            "category",
                            category
                        )
                        for category in CATEGORIES
                    ],
                    ft.Container(height=10),
                    nav(
                        "Анализ",
                        ft.Icons.SHIELD_OUTLINED,
                        "analysis"
                    ),
                    nav(
                        "Профиль",
                        ft.Icons.PERSON_OUTLINE,
                        "profile"
                    ),
                    nav(
                        "Обновления",
                        ft.Icons.SYSTEM_UPDATE,
                        "updates"
                    ),
                    ft.Container(expand=True),
                    *([ft.Container(
                        padding=ft.Padding.symmetric(horizontal=5, vertical=4),
                        content=ft.TextButton(
                            content=ft.Row([
                                ft.Icon(ft.Icons.SYSTEM_UPDATE, size=17, color=ACCENT),
                                ft.Column([
                                    txt("Доступно обновление", 12, WHITE, True),
                                    txt(f"ManPass {state['update']['version']}", 11, ACCENT),
                                ], spacing=2, tight=True),
                            ], spacing=8, tight=True),
                            on_click=lambda e: switch("updates"),
                            tooltip="Открыть раздел обновлений",
                        ),
                        bgcolor=CARD,
                        border_radius=11,
                    )] if state.get("update") else []),
                    ft.TextButton(
                        "Инструкция пользователя",
                        on_click=show_guide
                    ),
                    ft.Container(
                        padding=ft.Padding.only(left=12, top=8, bottom=4),
                        content=ft.Column([
                            txt("О программе", 12, WHITE, True),
                            txt(f"ManPass · версия {APP_VERSION}", 11, MUTED),
                            txt("Powered by 6IX7EVEN", 10, MUTED),
                        ], spacing=5, tight=True),
                    ),
                    btn(
                        "Заблокировать",
                        lock,
                        ft.Icons.LOCK_OUTLINE
                    )
                ],
                spacing=7,
                expand=True,
                scroll=ft.ScrollMode.AUTO
            )
        )

        topbar = ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=24,
                vertical=13
            ),
            content=ft.Row(
                [
                    txt(
                        "Твои пароли под контролем",
                        13,
                        MUTED
                    ),
                    ft.Container(expand=True),
                    btn(
                        name or "Мой профиль",
                        lambda e: switch("profile"),
                        ft.Icons.ACCOUNT_CIRCLE_OUTLINED
                    )
                ]
            )
        )

        section = state["section"]

        if section in (
            "all",
            "favorites",
            "category"
        ):
            current_page = vault_page()

        elif section == "profile":
            current_page = profile_page()

        elif section == "updates":
            current_page = updates_page()

        else:
            current_page = analysis_page()

        content = ft.Column(
            [
                topbar,
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.TOP_CENTER,
                    padding=ft.Padding.symmetric(
                        horizontal=24,
                        vertical=20
                    ),
                    content=ft.Container(
                        width=960,
                        expand=True,
                        content=current_page
                    )
                )
            ],
            spacing=0,
            expand=True
        )

        page.add(
            title_bar(),
            ft.Row(
                [
                    sidebar,
                    content
                ],
                spacing=0,
                expand=True,
                vertical_alignment=(
                    ft.CrossAxisAlignment.STRETCH
                )
            )
        )

        page.update()

    # ================================================
    # SESSION WATCHDOG
    # ================================================

    async def watchdog():
        while True:
            await asyncio.sleep(2)

            if state["key"] is None:
                continue

            if state["busy"]:
                continue

            elapsed = time.monotonic() - state["last_activity"]
            # On Windows consider physical keyboard/mouse activity even when
            # the UI library doesn't dispatch mouse movement or clicks.
            system_idle = windows_idle_seconds()
            # Application idle is authoritative: external Windows activity
            # must NOT keep a forgotten unlocked vault alive indefinitely.
            # GetLastInputInfo independently catches inactivity following sleep.
            if elapsed >= LOCK_TIMEOUT or (system_idle is not None and system_idle >= LOCK_TIMEOUT):
                lock()

    # Обновляем таймер при взаимодействии
    # через элементы приложения.
    page.on_keyboard_event = activity

    # Display the login window before starting the background tray thread.
    # This avoids a start-up race observed in packaged Windows builds.
    render_login()
    page.window.visible = True
    page.window.minimized = False
    page.update()

    page.run_task(watchdog)
    page.run_task(check_updates)
    page.run_task(tray_worker)
    if sys.platform == "win32":
        page.run_task(restore_from_shortcut)
    start_tray()

    # Give the desktop window one more chance to become visible after
    # Flutter/Pystray initialization. This runs only during startup and
    # does NOT override deliberate hiding later in the session.
    await asyncio.sleep(0.35)
    if not state["exiting"]:
        page.window.visible = True
        page.window.minimized = False
        page.update()


if __name__ == "__main__":
    try:
        first_instance = claim_single_instance()
        if not first_instance:
            raise SystemExit(0)
    except RuntimeError as exc:
        print(exc)
        raise SystemExit(1)
    ft.run(main)

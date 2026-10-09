"""Read-only, unauthenticated stable update discovery. No vault data is sent."""
import json
import re
from urllib.request import Request, urlopen
from urllib.parse import urlparse

REPOSITORY = "WhoIs6IX7EVEN/ManPass"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

def parse_version(value):
    match = _VERSION.fullmatch(str(value).strip())
    if not match:
        raise ValueError("Некорректный номер версии")
    return tuple(int(x) for x in match.groups())

def trusted_release_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Недоверенный адрес релиза")
    if not parsed.path.startswith(f"/{REPOSITORY}/releases/tag/"):
        raise ValueError("Адрес не принадлежит релизам ManPass")
    if parsed.query or parsed.fragment:
        raise ValueError("Некорректный адрес релиза")
    return value

def fetch_stable_update(current_version, opener=urlopen):
    current = parse_version(current_version)
    request = Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ManPass-Desktop-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with opener(request, timeout=5) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("Слишком большой ответ сервера")
    data = json.loads(payload.decode("utf-8"))
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name", "")
    remote = parse_version(tag)
    if remote <= current:
        return None
    url = trusted_release_url(str(data.get("html_url", "")))
    return {"version": f"{remote[0]}.{remote[1]}.{remote[2]}", "url": url}

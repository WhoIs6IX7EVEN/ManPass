from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_version_and_publisher():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    installer = (ROOT / "installer/ManPass.iss").read_text(encoding="utf-8")
    assert 'APP_VERSION = "3.6.1"' in source
    assert 'Powered by 6IX7EVEN' in source
    assert '#define MyAppVersion "3.6.1"' in installer
    assert 'AppPublisher=6IX7EVEN' in installer

def test_no_previous_branding_in_text_sources():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".py", ".md", ".txt", ".iss", ".ps1", ".yml", ".yaml"}:
            assert "Andrey" + " Pesterev" not in path.read_text(encoding="utf-8"), str(path)

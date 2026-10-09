import sys
from pathlib import Path
import io
import json
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from updater import fetch_stable_update, parse_version, trusted_release_url

class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()

def fake(data):
    def open_url(request, timeout):
        assert timeout == 5
        assert "api.github.com" in request.full_url
        return Response(json.dumps(data).encode("utf-8"))
    return open_url

BASE = {"tag_name": "v3.7.0", "html_url": "https://github.com/WhoIs6IX7EVEN/ManPass/releases/tag/v3.7.0", "draft": False, "prerelease": False}

def test_new_stable_release():
    result = fetch_stable_update("3.6.2", fake(BASE))
    assert result == {"version": "3.7.0", "url": BASE["html_url"]}

def test_no_update():
    assert fetch_stable_update("3.7.0", fake(BASE)) is None
    assert fetch_stable_update("3.8.0", fake(BASE)) is None

def test_prerelease_ignored():
    assert fetch_stable_update("3.6.2", fake({**BASE, "prerelease": True})) is None

def test_reject_malicious_link():
    with pytest.raises(ValueError):
        fetch_stable_update("3.6.2", fake({**BASE, "html_url":"https://evil.example/run.exe"}))

def test_reject_bad_versions():
    for version in ("v3.7.0-beta", "3.7", "3.7.0.1", "hello"):
        with pytest.raises(ValueError): parse_version(version)

def test_reject_oversized_payload():
    with pytest.raises(ValueError): fetch_stable_update("3.6.2", lambda req,timeout:Response(b"a"*65537))

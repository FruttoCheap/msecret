"""Google Drive: everything that runs without Google. The API calls need a real account."""

import io
import json
import os
import threading
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from urllib.request import urlopen

from test_cli_json import run

from mcp_secret import drive

ID = "1AbCdEfGhIjKlMnOp_-q"


def test_folder_links_and_names():
    for link in (ID, f" https://drive.google.com/drive/folders/{ID}?usp=sharing ",
                 f"https://drive.google.com/drive/u/1/folders/{ID}", f"https://drive.google.com/open?id={ID}"):
        assert drive.folder_id(link) == ID
    assert drive.folder_id("https://example.com/x") is None and drive.folder_id("' or 1") is None
    # names come from whoever shares the folder: never a path out of private/drive/
    assert [drive._safe(n) for n in ("..", ".", " ", "../../x", "a/b\0c", "Fatture")] == [
        "_", "_", "_", ".._.._x", "a_b_c", "Fatture"]


def test_login_takes_only_its_own_redirect(monkeypatch):
    """Google's side stubbed: the browser and the token endpoint."""
    sent = {}

    def browser(url):
        q = dict(parse_qsl(urlsplit(url).query))

        def back():  # a local page guessing the port first, then Google's redirect
            urlopen(f"{q['redirect_uri']}/?code=evil&state=guess").read()
            urlopen(f"{q['redirect_uri']}/?code=good&state={q['state']}").read()

        sent["challenge"] = q["code_challenge"]
        threading.Thread(target=back).start()

    monkeypatch.setattr(drive.webbrowser, "open", browser)
    monkeypatch.setattr(drive, "_post", lambda url, data: sent.update(data) or {"refresh_token": "r"})
    assert drive.login({"client_id": "c", "client_secret": "s"}) == "r"
    pkce = urlsafe_b64encode(sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert sent["code"] == "good" and sent["challenge"] == pkce


def test_mirror(tmp_path):
    base, events, fetched = tmp_path / "drive", [], []
    a, b, gone = base / "Clienti" / "a.pdf", base / "Clienti" / "Sub" / "b.docx", base / "old.pdf"
    gone.parent.mkdir(parents=True)
    gone.write_text("deleted on Drive")
    (base / "Clienti").mkdir()
    (base / "Clienti" / ".a.pdf.part").write_text("a killed download")

    def fetch(url):
        fetched.append(url)
        if url == "fail":
            raise OSError("offline")
        return io.BytesIO(url.encode())

    plan = {"a": (a, "A1", 10**18), "b": (b, "B1", 2 * 10**18)}
    drive.mirror(base, plan, fetch, events.append)
    assert (a.read_text(), b.read_text(), a.stat().st_mtime_ns) == ("A1", "B1", 10**18)
    assert sorted(p.name for p in base.rglob("*") if p.is_file()) == ["a.pdf", "b.docx"]
    assert events == [{"event": "file", "path": str(a), "status": "downloaded", "error": None},
                      {"event": "file", "path": str(b), "status": "downloaded", "error": None}]

    # unchanged on Drive: not fetched; changed: fetched again; failed: the old copy stays
    events.clear()
    drive.mirror(base, {"a": (a, "A2", 10**18), "b": (b, "fail", 3 * 10**18)}, fetch, events.append)
    assert fetched == ["A1", "B1", "fail"] and (a.read_text(), b.read_text()) == ("A1", "B1")
    assert [(e["status"], e["error"]) for e in events] == [("unchanged", None), ("error", "OSError")]
    drive.mirror(base, {}, fetch, events.append)  # every folder removed: nothing left
    assert not [p for p in base.rglob("*") if p.is_file()]


def test_drive_offline(tmp_path):
    """The `drive` command up to the first request to Google."""
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path)}
    none = {"event": "drive", "client": False, "connected": False, "folders": []}
    assert run(env, "drive") == [none]
    assert run(env, "drive", "login", code=1)[0]["error"] == "no_client"
    (tmp_path / "web.json").write_text('{"web": {"client_id": "x", "client_secret": "y"}}')
    assert run(env, "drive", "login", str(tmp_path / "web.json"), code=1)[0]["error"] == "no_client"
    for args in (("sync",), ("add", ID)):
        assert run(env, "drive", *args, code=1)[0]["error"] == "not_connected"
    assert run(env, "drive", "add", "not a link", code=1)[0]["error"] == "no_folder"
    p = tmp_path / "private" / "drive.json"
    p.write_text(json.dumps({"folders": [{"id": ID, "name": "Clienti", "dir": "Clienti"}]}))
    assert run(env, "drive", "remove", "1Zzzzzzzzzzzz", code=1)[0]["error"] == "no_folder"
    assert run(env, "drive", "remove", f"https://drive.google.com/drive/folders/{ID}") == [none]
    assert run(env, "drive", "logout") == [none] and oct(p.stat().st_mode & 0o777) == "0o600"
    assert not Path(tmp_path / "private" / "drive").exists()  # nothing synced, nothing written

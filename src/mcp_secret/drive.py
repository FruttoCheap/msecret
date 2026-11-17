"""Google Drive: the folders you pick, mirrored into private/drive/ for ingest. The only module
that talks to Google, with read-only scope. It runs outside the sandbox (it needs the network)
and never parses a document: ingest, offline and sandboxed, does that."""

import contextlib
import json
import os
import re
import secrets
import shutil
import sys
import unicodedata
import webbrowser
from base64 import urlsafe_b64encode
from datetime import datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

from mcp_secret.ingest import SUFFIXES

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
_OOXML = "application/vnd.openxmlformats-officedocument."
# Docs, Sheets and Slides have no file to download: exported to formats ingest parses.
# ponytail: files.export stops at 10 MB (an error event); exportLinks if bigger ones matter
EXPORT = {"application/vnd.google-apps.document": (".docx", _OOXML + "wordprocessingml.document"),
          "application/vnd.google-apps.spreadsheet": (".xlsx", _OOXML + "spreadsheetml.sheet"),
          "application/vnd.google-apps.presentation": (".pptx", _OOXML + "presentationml.presentation")}


class Error(Exception):
    """(code, message): an `error` event of docs/cli-json.md."""


def _post(url: str, data: dict) -> dict:
    with urlopen(Request(url, urlencode(data).encode()), timeout=60) as r:
        return json.loads(r.read() or b"{}")


def _get(url: str, token: str):
    return urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=60)


def _json(url: str, token: str) -> dict:
    with _get(url, token) as r:
        return json.load(r)


def client(path: Path) -> dict:
    """client_id and client_secret of a Google Cloud OAuth client of type "Desktop app"."""
    try:
        c = json.loads(path.read_text(encoding="utf-8-sig"))["installed"]
        return {"client_id": c["client_id"], "client_secret": c["client_secret"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise Error("no_client", "not the JSON file of a Desktop app OAuth client") from None


def login(c: dict) -> str:
    """Consent in the browser, back on a loopback port (PKCE, state): the refresh token."""
    verifier, state, got = secrets.token_urlsafe(64), secrets.token_urlsafe(16), {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(parse_qsl(urlsplit(self.path).query))
            if q.get("state") == state:  # anything else (a favicon, a stray page) is ignored
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"mcp-secret: done, you can close this tab.")

        def log_message(self, *_):  # the request line holds the code
            pass

    with HTTPServer(("127.0.0.1", 0), Back) as srv:
        redirect = f"http://127.0.0.1:{srv.server_port}"
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = AUTH + "?" + urlencode({
            "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPE, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
        print(f"Allow access in the browser; if none opened, visit:\n{url}", file=sys.stderr)
        webbrowser.open(url)
        srv.timeout = 600  # per request: the wait ends 10 minutes after the last one
        srv.handle_timeout = lambda: got.setdefault("error", "timeout")
        while not got:
            srv.handle_request()
    if "code" not in got:
        raise Error("not_connected", f"login failed: {got.get('error')}")
    return _post(TOKEN, {**c, "code": got["code"], "code_verifier": verifier,
                         "grant_type": "authorization_code", "redirect_uri": redirect})["refresh_token"]


def revoke(token: str | None) -> None:
    """Best effort: offline, the token is forgotten here all the same."""
    if token:
        with contextlib.suppress(OSError):
            _post(REVOKE, {"token": token})


def _access(conf: dict) -> str:
    """A fresh access token (valid one hour) from the stored refresh token."""
    # ponytail: one per command; a sync longer than an hour fails its last files (401)
    if "refresh_token" not in conf:
        raise Error("not_connected", "not connected: `mcp-secret drive login` first")
    try:
        return _post(TOKEN, {**conf["client"], "refresh_token": conf["refresh_token"],
                             "grant_type": "refresh_token"})["access_token"]
    except HTTPError as e:
        if e.code in (400, 401):  # invalid_grant: revoked, or the client was deleted
            raise Error("not_connected", "Google refused the stored login: log in again") from None
        raise


def folder_id(link: str) -> str | None:
    """The id in a folder link (…/folders/<id>, …?id=<id>) or the id itself."""
    m = re.search(r"(?:/folders/|[?&]id=|^)([\w-]{10,})", link.strip())
    return m and m[1]


def folder(conf: dict, link: str) -> dict:
    """The folder a link points to, with the local directory it will be mirrored into."""
    if not (fid := folder_id(link)):
        raise Error("no_folder", "not a Google Drive folder link or id")
    q = urlencode({"fields": "id,name,mimeType", "supportsAllDrives": "true"})
    try:
        f = _json(f"{API}/{fid}?{q}", _access(conf))
    except HTTPError as e:
        if e.code == 404:
            raise Error("no_folder", "no folder you can open has that id") from None
        raise
    if f["mimeType"] != FOLDER:
        raise Error("no_folder", "that is a file, not a folder")
    d, taken = _safe(f["name"]), {_key(x["dir"]) for x in conf.get("folders", [])}
    return {"id": f["id"], "name": f["name"], "dir": f"{d} ({f['id'][:6]})" if _key(d) in taken else d}


def _safe(name: str) -> str:
    """A Drive name (anyone who shares a folder chooses them) as one path component."""
    name = name.replace("/", "_").replace("\0", "_").strip()
    return "_" if name in ("", ".", "..") else name


def _key(path) -> str:
    """How APFS compares names: case- and normalization-insensitive."""
    return unicodedata.normalize("NFC", str(path)).casefold()


def _walk(token: str, folder_id: str, d: Path, plan: dict) -> None:
    """Add every file under the folder that ingest reads: plan[key] = (path, url, mtime_ns)."""
    q = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
         "orderBy": "name,createdTime", "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
         "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
    while True:
        page = _json(f"{API}?{urlencode(q)}", token)
        for f in page["files"]:
            name, mime = _safe(f["name"]), f["mimeType"]
            if mime == FOLDER:  # ponytail: shortcuts, forms, drawings are skipped
                _walk(token, f["id"], d / name, plan)
                continue
            if mime in EXPORT:
                ext, to = EXPORT[mime]
                name = name if name.lower().endswith(ext) else name + ext
                url = f"{API}/{f['id']}/export?{urlencode({'mimeType': to})}"
            elif Path(name).suffix.lower() in SUFFIXES:
                url = f"{API}/{f['id']}?alt=media&supportsAllDrives=true"
            else:
                continue  # ingest would skip it: not downloaded
            p = d / name
            if _key(p) in plan:  # Drive allows two "a.pdf" in a folder
                p = p.with_stem(f"{p.stem} ({f['id'][:6]})")
            ms = datetime.fromisoformat(f["modifiedTime"]).timestamp() * 1000
            plan[_key(p)] = (p, url, round(ms) * 1_000_000)
        if "nextPageToken" not in page:
            return
        q["pageToken"] = page["nextPageToken"]


def mirror(base: Path, plan: dict, fetch, emit) -> None:
    """Make `base` hold the plan's files and nothing else. A file is fetched when missing or its
    mtime is not Drive's; one that fails keeps its old copy. Deleted files need no event: the
    ingest that follows reports their documents removed."""
    keep = set()
    for p, url, ns in plan.values():
        keep.add(p)
        status, error = "unchanged", None
        if not (p.is_file() and p.stat().st_mtime_ns == ns):
            tmp = p.with_name(f".{p.name}.part")
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with fetch(url) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f)
                os.utime(tmp, ns=(ns, ns))
                tmp.replace(p)
                status = "downloaded"
            except OSError as e:  # HTTPError, URLError, a full disk: this file only
                status, error = "error", type(e).__name__
        emit({"event": "file", "path": str(p), "status": status, "error": error})
    for p in list(base.rglob("*")):
        if p.is_file() and p not in keep:
            p.unlink()


def sync(conf: dict, base: Path, emit) -> None:
    """Mirror every folder into `base`, listing them all before touching a file: a listing that
    fails changes nothing."""
    token, plan = _access(conf), {}
    for f in conf.get("folders", []):
        _walk(token, f["id"], base / f["dir"], plan)
    emit({"event": "start", "files": len(plan)})
    mirror(base, plan, lambda url: _get(url, token), emit)


def _print(ev: dict) -> None:
    if ev["event"] == "file":
        print(f"{ev['error'] or ev['status']}\t{ev['path']}", flush=True)

"""mcp-secret: index documents pseudonymized, serve them to an AI agent over MCP."""

import argparse
import json
import os
import pwd
import re
import shutil
import signal
import site
import socket
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

SANDBOX = "/usr/bin/sandbox-exec"
DEFAULT_HOME = "~/Library/Application Support/mcp-secret"
NO_INDEX = "no index yet: run `mcp-secret ingest <path>` first"
_json = None  # --json: writes one JSON object per line to the real stdout


def home() -> Path:
    """Data root: private/ (ingest only) and public/ (what serve may read)."""
    return Path(os.environ.get("MCP_SECRET_HOME", DEFAULT_HOME)).expanduser().resolve()


def _q(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _any(kind: str, paths) -> str:
    return " ".join(f"({kind} {_q(p)})" for p in paths)


def profile(root: Path | None = None, writable: Path | None = None) -> str:
    """sandbox-exec profile: no network. With `root` (serve): no reads of $HOME, the data root,
    other users, volumes and temp dirs except public/ and the Python runtime; no file writes
    anywhere (inherited stdio fds still work); no exec but Python itself; no mach services; no
    process info or signals but its own. With `writable` (export), that folder is also readable and
    writable."""
    sbpl = "(version 1)(allow default)(deny network*)"
    if root is None:
        return sbpl
    # the account's home, not $HOME: whoever launches serve sets the environment
    pub, me = root / "public", Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    # not sys.path: it may hold the cwd or PYTHONPATH, i.e. anything under $HOME
    runtime = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, *site.getsitepackages(),
                                             Path(__file__).parents[1])}
    # ponytail: where documents usually live; (deny default) if one turns up elsewhere (/opt)
    docs = [me, root, "/Users", "/Volumes", "/private/tmp", "/private/var/folders"]
    return sbpl + "".join([
        f"(deny file-read* {_any('subpath', docs)})",
        # a write outside the box (a binary, a shell rc) runs later unsandboxed
        "(deny file-write*)",
        '(allow file-write-data (literal "/dev/null") (literal "/dev/dtracehelper"))',
        # SQLite lstat()s every path component when opening public.db
        f"(allow file-read-metadata {_any('literal', [*pub.parents, *(writable.parents if writable else [])])})",
        f"(allow file-read* {_any('subpath', [pub, *sorted(runtime)])})",
        f"(allow file-read* file-write* {_any('subpath', [writable])})" if writable else "",
        # sandbox-exec execs the interpreter itself, after the profile applies
        "(deny process-exec*)",
        f"(allow process-exec {_any('literal', {sys.executable, os.path.realpath(sys.executable)})})",
        "(deny mach-lookup)",
        # not "(target others)": it does not match an unsandboxed parent. ponytail: no SBPL rule
        # gates sysctl KERN_PROCARGS2: same-user argv, and the environment of processes without
        # hardened runtime, stay readable to a compromised serve (README, residual risks).
        "(deny process-info* signal)(allow process-info* signal (target self))",
    ])


def sandbox_cmd(prof: str, *args: str) -> list[str]:
    return [SANDBOX, "-p", prof, sys.executable, "-I", "-B", *args]  # -B: no .pyc writes


def _denied(op) -> bool:
    try:
        op()
    except PermissionError:
        return True
    except OSError:
        pass
    return False


def _connect_out() -> None:
    with socket.socket() as s:
        s.connect(("127.0.0.1", 9))


def _sandboxed(args: argparse.Namespace, root: Path | None, *must_fail, writable: Path | None = None) -> None:
    """Re-exec under sandbox-exec with profile(root, writable), then prove it. Fails closed."""
    if not args.sandboxed:  # exec raises if sandbox-exec is missing; a bad profile exits non-zero
        cmd = sandbox_cmd(profile(root, writable), "-m", "mcp_secret.cli", "--sandboxed", *sys.argv[1:])
        os.execv(SANDBOX, cmd)
    if not all(_denied(op) for op in (_connect_out, *must_fail)):
        sys.exit("mcp-secret: sandbox not active, refusing to run")


def _jsonl():
    """--json: stdout carries JSON lines only. Anything else written to fd 1 (a library's print,
    a native log) goes to stderr instead."""
    stream = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)

    def emit(ev: dict) -> None:
        stream.write(json.dumps(ev) + "\n")  # ASCII: anything else as \u escapes
        stream.flush()

    emit.fd = stream.fileno()  # the real stdout, for a command that execs another (drive sync)
    return emit


def out(ev: dict, line: str | None = None) -> None:
    """One result: a JSON line with --json, else `line` (if any) for humans."""
    if _json:
        _json(ev)
    elif line is not None:
        print(line, flush=True)


def fail(code: str, message: str) -> NoReturn:
    """End the command: an error line with --json, the message on stderr, exit code 1."""
    if _json:
        _json({"event": "error", "error": code, "message": message})
    sys.exit(message)


def _private(priv: Path) -> None:
    os.umask(0o077)  # private/ 0700, its files 0600
    priv.mkdir(parents=True, exist_ok=True)
    priv.chmod(0o700)


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)


def _replace(path: Path, text: str, mode: int) -> None:
    """Write `path` atomically (a crash leaves the old file or the new one, never half)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def _models() -> bool:
    """Docling's layout/table models and the PII detector are in the local caches."""
    from docling.datamodel.settings import settings

    from mcp_secret import pii

    try:
        pii._snapshot()  # local lookup, no network
    except OSError:
        return False
    # ponytail: presence, not completeness; an interrupted download shows up at ingest
    return any((settings.cache_dir / "models").glob("*"))


def status(root: Path) -> None:
    priv, pub = root / "private" / "private.db", root / "public" / "public.db"
    index = priv.exists() and pub.exists()
    docs = chunks = 0
    if index:  # read-write: rolls back what a killed ingest left, so read-only opens work again
        docs = sqlite3.connect(priv).execute("SELECT count(*) FROM docs").fetchone()[0]
        chunks = sqlite3.connect(pub).execute("SELECT count(*) FROM chunks").fetchone()[0]
    ev = {"event": "status", "root": str(root), "models": _models(), "index": index, "docs": docs,
          "chunks": chunks}
    out(ev, "\n".join(f"{k}\t{v}" for k, v in list(ev.items())[1:]))


def list_docs(root: Path) -> None:
    from mcp_secret import categories
    from mcp_secret.ingest import digest

    priv, pub = root / "private" / "private.db", root / "public" / "public.db"
    if not (priv.exists() and pub.exists()):
        return
    priv_con, pub_con = _ro(priv), _ro(pub)
    n = dict(pub_con.execute("SELECT doc_id, count(*) FROM chunks GROUP BY doc_id"))
    cats = categories.of_docs(priv_con, pub_con, root / "private")
    for doc_id, path, sha in priv_con.execute("SELECT id, path, sha256 FROM docs ORDER BY path"):
        now = digest(Path(path))  # ponytail: hashes every file per call; keep size+mtime if slow
        ev = {"event": "doc", "doc_id": doc_id, "path": path, "chunks": n.get(doc_id, 0),
              "exists": now is not None, "stale": now not in (None, sha),
              "categories": cats.get(doc_id, [])}
        state = "missing" if now is None else "changed" if now != sha else "ok"
        out(ev, "\t".join([doc_id, f"{ev['chunks']} chunks", state, path, *ev["categories"]]))


def _index(root: Path, write: bool = False) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """private.db (read-only unless `write`) and public.db (read-only)."""
    priv, pub = root / "private" / "private.db", root / "public" / "public.db"
    if not (priv.exists() and pub.exists()):
        fail("no_index", NO_INDEX)
    return sqlite3.connect(priv) if write else _ro(priv), _ro(pub)


def cat(root: Path, action: str, doc_ids: list[str], name: str) -> None:
    """Add a category to documents by hand, or take one away (also an automatic one, for good).
    One event for each document, in order; an unknown id changes none."""
    from mcp_secret import categories
    from mcp_secret.ingest import CATS

    if not (name := categories.name(name)):
        fail("bad_value", "empty category")
    priv_con, pub_con = _index(root, write=True)
    before = categories.of_docs(priv_con, pub_con, root / "private")
    for doc_id in doc_ids:
        if doc_id not in before:
            fail("no_document", f"no document {doc_id}")
    add = action == "add"
    with priv_con:
        priv_con.execute(CATS)  # an index from before categories
        for doc_id in doc_ids:
            if add or name in before[doc_id]:  # nothing to take away: no decision to remember
                priv_con.execute("INSERT OR REPLACE INTO cats VALUES (?, ?, ?)", (doc_id, name, not add))
    for doc_id in doc_ids:
        out({"event": "cat", "action": action, "changed": (name in before[doc_id]) != add})


def forget(root: Path, doc_ids: list[str]) -> None:
    """Take documents out of the index, for good: the files stay where they are. One `removed`
    doc event for each; an id that is not indexed is skipped."""
    from mcp_secret import ingest

    _index(root)
    con = ingest.connect(root)
    for doc_id in dict.fromkeys(doc_ids):
        if row := con.execute("SELECT path FROM docs WHERE id = ?", (doc_id,)).fetchone():
            ingest.forget(con, doc_id)
            ev = ingest._doc(doc_id, row[0], 0, "removed")
            out(ev, f"{doc_id}\tremoved\t{row[0]}")


def rules(priv: Path, name: str | None, terms: list[str]) -> None:
    """Keyword rules of the automatic categories; with `name`, set its terms first (none: drop it)."""
    from mcp_secret import categories

    if name is not None:
        if not (name := categories.name(name)) or ":" in name:
            fail("bad_value", "empty category or one with ':'")
        terms = list(dict.fromkeys(t for a in terms for t in map(categories.name, a.split(",")) if t))
        _private(priv)
        _replace(priv / "categories.txt", categories.set_rule(priv, name, terms), 0o600)
    for n, ts in categories.rules(priv).items():
        out({"event": "rule", "name": n, "terms": ts}, f"{n}\t{', '.join(ts)}")


def tokens(root: Path, types: list[str]) -> None:
    from mcp_secret import categories

    priv_con, pub_con = _index(root)
    for ev in categories.tokens(priv_con, pub_con, set(types)):
        out(ev, "\t".join([ev["token"], str(len(ev["doc_ids"])), ev["value"] or ""]))


def values(root: Path, name: str) -> None:
    """private/<name>.txt, each value with the documents that publish it in clear."""
    from mcp_secret import categories
    from mcp_secret.ingest import _lines, norm

    db = root / "public" / "public.db"
    vs = list(dict.fromkeys(map(norm, _lines(root / "private" / f"{name}.txt"))))
    for ev in categories.in_clear(_ro(db) if db.exists() else None, vs):
        out(ev, "\t".join([ev["value"], *ev["doc_ids"]]))


def show(db: Path, doc_id: str) -> None:
    if not db.exists():
        fail("no_index", NO_INDEX)
    rows = _ro(db).execute("SELECT rowid, text FROM chunks WHERE doc_id = ? ORDER BY seq",
                           (doc_id,)).fetchall()
    if not rows:
        fail("no_document", f"no document {doc_id}")
    for rowid, text in rows:
        out({"event": "chunk", "chunk_id": rowid, "text": text}, f"--- chunk {rowid} ---\n{text}\n")


def export(root: Path, dest: Path) -> None:
    """The published documents as `dest/pseudonymized/<doc_id>.md`, and a guide for the agent, for
    a folder it works in (Claude Cowork). Reads public.db only. The subfolder is made to match it:
    what is no longer indexed (a scrubbed or removed document) goes. Only files named like ours are
    ever deleted or replaced: the folder may be a project that has files of its own."""
    from mcp_secret.server import TOKENS, names

    db = root / "public" / "public.db"
    if not db.exists():
        fail("no_index", NO_INDEX)
    con = _ro(db)
    docs: dict[str, list[str]] = {}
    for doc_id, text in con.execute("SELECT doc_id, text FROM chunks ORDER BY doc_id, seq"):
        docs.setdefault(doc_id, []).append(text)
    for doc_id, name in names(con).items():  # its first line: the agent cites it by name
        if doc_id in docs:
            docs[doc_id].insert(0, name)
    os.umask(0o022)  # _private() set 0077: the agent may read through another uid (a VM)
    folder = dest / "pseudonymized"
    folder.mkdir(parents=True, exist_ok=True)
    for doc_id, parts in docs.items():
        _replace(folder / f"{doc_id}.md", "\n\n".join(parts) + "\n", 0o644)
    keep = {f"{doc_id}.md" for doc_id in docs}
    ours = re.compile(r"[0-9a-f]{16}\.md(\.tmp)?")  # the .tmp a killed export left, too
    stale = [p for p in folder.iterdir() if ours.fullmatch(p.name) and p.name not in keep]
    for p in stale:
        p.unlink()
    _replace(dest / "PSEUDONYMIZED.md", f"# Pseudonymized documents\n\n{TOKENS}\n\n"
             "`pseudonymized/<id>.md` is one document each (random id); its first line is the file "
             "name, pseudonymized too: cite documents by that name. Search "
             "a token across the folder to find every document about that entity. `mcp-secret "
             "export` rewrites `pseudonymized/`: put your own files elsewhere.\n", 0o644)
    out({"event": "export", "dir": str(dest), "docs": len(docs), "removed": len(stale)},
        f"{len(docs)} documents exported to {dest}")


def _review(root: Path, doc_id: str) -> dict:
    """The review event of a document, or the error event saying why there is none."""
    from mcp_secret import ingest

    if not (root / "private" / "private.db").exists():
        return {"event": "error", "error": "no_index", "message": NO_INDEX}
    r = ingest.review(root, doc_id)
    if r is None:
        return {"event": "error", "error": "no_document", "message": f"no document {doc_id}"}
    return {"event": "review", **r}


def _review_text(ev: dict) -> str:
    if ev["event"] == "error":
        return ev["message"]
    state = "missing" if not ev["exists"] else "changed" if ev["stale"] else "ok"
    return "\n".join([f"{ev['doc_id']}\t{state}\t{ev['path']}", *(
        f"{s['start']}\t{s['end']}\t{s['token']}\t{ev['text'][s['start']:s['end']]}"
        for s in ev["spans"])])


def review(root: Path, doc_id: str) -> None:
    """`review <doc_id>`; or `review -`, a worker answering each doc_id read on stdin with one
    line, until stdin closes: the models load once, before the first request, not on each."""
    if doc_id != "-":
        ev = _review(root, doc_id)
        if ev["event"] == "error":
            fail(ev["error"], ev["message"])
        return out(ev, _review_text(ev))
    from mcp_secret import pii

    pii.detect("warm up")
    for line in sys.stdin:
        if not (doc_id := line.strip()):
            continue
        try:
            ev = _review(root, doc_id)
        except Exception as e:  # noqa: BLE001 - one unreadable file never stops the worker
            ev = {"event": "error", "error": "failed", "message": type(e).__name__}
        out(ev, _review_text(ev))


def edit(priv: Path, name: str, value: str, add: bool) -> bool:
    """Add `value` to, or remove it from, private/<name>.txt, comparing lines the way ingest
    reads them. True if the file changed."""
    from mcp_secret.ingest import clean, norm

    p = priv / f"{name}.txt"
    lines = p.read_text().splitlines() if p.exists() else []
    keep = [s for s in lines if norm(clean(s)) != value]
    if (len(keep) < len(lines)) == add:  # already there, or nothing to remove
        return False
    _replace(p, "".join(f"{s}\n" for s in keep + ([value] if add else [])), 0o600)
    return True


def _claude() -> tuple[Path, Path]:
    """Claude Desktop's config file and the log it keeps of the mcp-secret server."""
    log = "mcp-server-mcp-secret.log"
    if sys.platform == "win32":
        d = Path(os.environ["APPDATA"]) / "Claude"
        return d / "claude_desktop_config.json", d / "logs" / log
    return (Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
            Path.home() / "Library/Logs/Claude" / log)


def desktop(root: Path, install: bool, uninstall: bool) -> None:
    """Show, add or remove mcpServers["mcp-secret"] in Claude Desktop's config; nothing else
    in it changes, and the previous file is kept as .bak."""
    cfg, log = _claude()
    try:
        conf = json.loads(cfg.read_text(encoding="utf-8-sig")) if cfg.exists() else {}
    except ValueError:
        conf = None
    if not isinstance(conf, dict) or not isinstance(conf.get("mcpServers", {}), dict):
        fail("bad_config", f"not a Claude Desktop config: {cfg}")
    before = json.dumps(conf)
    if install:
        exe = Path(sys.executable).parent / ("mcp-secret.exe" if os.name == "nt" else "mcp-secret")
        entry = {"command": str(exe), "args": ["serve"]}
        if root != Path(DEFAULT_HOME).expanduser().resolve():
            entry["env"] = {"MCP_SECRET_HOME": str(root)}
        conf.setdefault("mcpServers", {})["mcp-secret"] = entry
    elif uninstall:
        conf.get("mcpServers", {}).pop("mcp-secret", None)
    if json.dumps(conf) != before:
        mode = cfg.stat().st_mode & 0o777 if cfg.exists() else 0o600
        if cfg.exists():
            shutil.copy2(cfg, cfg.with_name(cfg.name + ".bak"))
        cfg.parent.mkdir(parents=True, exist_ok=True)
        _replace(cfg, json.dumps(conf, indent=2, ensure_ascii=False) + "\n", mode)
    entry = conf.get("mcpServers", {}).get("mcp-secret")
    command = entry and entry.get("command")
    last = datetime.fromtimestamp(log.stat().st_mtime, UTC) if log.exists() else None
    ev = {"event": "desktop", "config": str(cfg), "configured": entry is not None,
          "command": command, "command_ok": bool(command) and Path(command).is_file(),
          "last_log": last and last.strftime("%Y-%m-%dT%H:%M:%SZ")}
    out(ev, "\n".join(f"{k}\t{v}" for k, v in list(ev.items())[1:]))


def drive_cmd(priv: Path, action: str, arg: str | None) -> None:
    """The Google account and the folders mirrored into private/drive/ (private/drive.json,
    0600). `sync` downloads, then becomes `ingest private/drive`, offline and sandboxed, on the
    same stdout; the other actions end with the `drive` event."""
    from mcp_secret import drive

    _private(priv)
    p = priv / "drive.json"
    conf = json.loads(p.read_text()) if p.exists() else {}
    folders = conf.setdefault("folders", [])

    def save() -> None:
        _replace(p, json.dumps(conf, indent=2) + "\n", 0o600)

    try:
        if action == "login":
            if arg:
                conf["client"] = drive.client(Path(arg))
                save()
            if "client" not in conf:
                fail("no_client", "no OAuth client yet: `drive login <client_secret.json>`")
            conf["refresh_token"] = drive.login(conf["client"])
        elif action == "logout":
            drive.revoke(conf.pop("refresh_token", None))
        elif action == "add":
            f = drive.folder(conf, arg)
            if all(x["id"] != f["id"] for x in folders):
                folders.append(f)
        elif action == "remove":  # its copy and its documents go at the next sync
            conf["folders"] = [x for x in folders if x["id"] != drive.folder_id(arg)]
            if conf["folders"] == folders:
                fail("no_folder", f"no folder {arg} in the list")
        elif action == "sync":
            base = priv / "drive"
            drive.sync(conf, base, _json or drive._print)
            base.mkdir(exist_ok=True)  # no folders yet: ingest still needs one to read
            if _json:
                os.dup2(_json.fd, 1)
            os.execv(sys.executable, [sys.executable, "-m", "mcp_secret.cli",
                                      *(["--json"] if _json else []), "ingest", str(base)])
    except drive.Error as e:
        fail(*e.args)
    if action != "status":
        save()
    ev = {"event": "drive", "client": "client" in conf, "connected": "refresh_token" in conf,
          "folders": [{"id": f["id"], "name": f["name"]} for f in conf["folders"]]}
    out(ev, "\n".join([f"client\t{ev['client']}", f"connected\t{ev['connected']}",
                       *(f"{f['id']}\t{f['name']}" for f in ev["folders"])]))


def main() -> None:
    global _json
    ap = argparse.ArgumentParser(prog="mcp-secret", description=__doc__)
    ap.add_argument("--sandboxed", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per line on stdout (docs/cli-json.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="data root, models, index size")
    sub.add_parser("models", help="download model weights for offline use (needs network)")
    p = sub.add_parser("ingest", help="index files and folders")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--force", action="store_true", help="re-index unchanged files too")
    sub.add_parser("serve", help="MCP server on stdio, sandboxed, read-only")
    sub.add_parser("list", help="indexed documents, with their paths in clear")
    sub.add_parser("show", help="print what the agent sees of a document").add_argument("doc_id")
    sub.add_parser("review", help="a document in clear and what ingest masks in it; "
                   "-: a worker reading doc_ids on stdin").add_argument("doc_id")
    for name, what in (("deny", "always"), ("allow", "never")):
        p = sub.add_parser(name, help=f"{what} mask a value ({name}.txt); no action: list them")
        p.add_argument("action", nargs="?", choices=["add", "remove"])
        p.add_argument("value", nargs="?")
    p = sub.add_parser("cat", help="add or remove a category of documents")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("doc_ids", nargs="+", metavar="doc_id")
    p.add_argument("name")
    sub.add_parser("forget", help="take documents out of the index (the files stay)"
                   ).add_argument("doc_ids", nargs="+", metavar="doc_id")
    p = sub.add_parser("rules", help="keyword rules of the automatic categories (categories.txt)")
    p.add_argument("--set", metavar="NAME", help="set the rule's terms (none: remove it)")
    p.add_argument("terms", nargs="*", help="words, prefix*, or phrases; a document with any of "
                   "them in what the agent sees gets the category")
    sub.add_parser("tokens", help="every published token, values in clear"
                   ).add_argument("--type", action="append", default=[], help="only this type (repeatable)")
    sub.add_parser("export", help="write the pseudonymized documents to a folder an agent works "
                   "in (Claude Cowork)").add_argument("dir", type=Path)
    p = sub.add_parser("desktop-config", help="show, add or remove mcp-secret in Claude Desktop")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    p = sub.add_parser("drive", help="Google Drive folders mirrored into private/drive/ and "
                       "ingested (login, add, remove, sync use the network)")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "login", "logout", "add", "remove", "sync"])
    p.add_argument("arg", nargs="?", help="login: client_secret.json (the first time); "
                   "add, remove: a folder link or id")
    args = ap.parse_args()
    if args.cmd == "rules" and args.terms and args.set is None:
        ap.error("rules: terms need --set NAME")
    if args.cmd in ("deny", "allow") and (args.action is None) != (args.value is None):
        ap.error(f"{args.cmd}: add|remove <value>, or nothing to list them")
    if args.cmd == "drive" and args.action in ("add", "remove") and not args.arg:
        ap.error(f"drive {args.action}: a folder link or id")
    root = home()
    os.environ["MCP_SECRET_HOME"] = str(root)  # absolute for the re-exec (serve chdirs to /)
    priv, db = root / "private", root / "public" / "public.db"

    if args.cmd == "serve":  # stdout is MCP: --json does not apply
        if not db.exists():
            sys.exit(NO_INDEX)
        os.environ["HF_HUB_OFFLINE"] = "1"
        if not args.sandboxed:
            _private(priv)
            os.chdir("/")  # a cwd under $HOME is unreadable in the sandbox (rich calls getcwd)
        _sandboxed(args, root, lambda: os.listdir(priv))
        from mcp_secret import server

        return server.run(db)
    if args.cmd == "export":  # reads public/, writes one folder: boxed like serve
        os.environ["HF_HUB_OFFLINE"] = "1"
        dest = args.dir.expanduser().resolve()
        if root.is_relative_to(dest) or dest.is_relative_to(root):  # the box would open private/
            _json = _jsonl() if args.json else None
            fail("bad_dir", "choose a folder apart from the data root")
        if not args.sandboxed:
            _private(priv)
            sys.argv[-1] = str(dest)  # the re-exec runs from /, where a relative path means another folder
            os.chdir("/")
        _sandboxed(args, root, lambda: os.listdir(priv), writable=dest)
    if args.cmd in ("ingest", "review"):  # parse and detect: no network
        os.environ["HF_HUB_OFFLINE"] = "1"
        if args.cmd == "ingest" and not args.sandboxed:
            _private(priv)
        _sandboxed(args, None)
    if args.json:
        _json = _jsonl()
    try:
        if args.cmd == "status":
            status(root)
        elif args.cmd == "models":
            from docling.utils.model_downloader import download_models

            from mcp_secret import pii

            # ponytail: one event per step; byte progress needs huggingface_hub's tqdm_class
            out({"event": "start", "steps": 2})
            out({"event": "step", "step": 1, "name": "docling"})
            download_models(with_code_formula=False, with_picture_classifier=False,
                            with_rapidocr=False)
            out({"event": "step", "step": 2, "name": "detector"})
            pii.fetch_models()
            out({"event": "done"})
        elif args.cmd == "ingest":
            from mcp_secret import ingest

            # a cancel (SIGTERM) rolls the open transaction back: no hot journal left behind
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            sys.exit(ingest.run(root, args.paths, args.force, _json or ingest._print))
        elif args.cmd == "list":
            list_docs(root)
        elif args.cmd == "show":
            show(db, args.doc_id)
        elif args.cmd == "review":
            review(root, args.doc_id)
        elif args.cmd in ("deny", "allow") and args.action is None:
            values(root, args.cmd)
        elif args.cmd in ("deny", "allow"):
            from mcp_secret.ingest import clean, norm

            if not (value := norm(clean(args.value))):
                fail("bad_value", "empty value")
            _private(priv)
            changed = edit(priv, args.cmd, value, args.action == "add")
            if args.action == "add":  # the latest decision wins
                edit(priv, "allow" if args.cmd == "deny" else "deny", value, False)
            out({"event": args.cmd, "action": args.action, "changed": changed})
        elif args.cmd == "cat":
            cat(root, args.action, args.doc_ids, args.name)
        elif args.cmd == "forget":
            forget(root, args.doc_ids)
        elif args.cmd == "rules":
            rules(priv, args.set, args.terms)
        elif args.cmd == "tokens":
            tokens(root, args.type)
        elif args.cmd == "export":
            export(root, dest)
        elif args.cmd == "drive":
            drive_cmd(priv, args.action, args.arg)
        else:
            desktop(root, args.install, args.uninstall)
    except Exception as e:
        if _json:
            _json({"event": "error", "error": "failed", "message": type(e).__name__})
        raise


if __name__ == "__main__":
    main()

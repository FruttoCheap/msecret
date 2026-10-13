"""The --json contract (docs/cli-json.md), through the real CLI in a temporary data root."""

import json
import os
import signal
import subprocess
from pathlib import Path

from test_leak import CLI

from mcp_secret.ingest import _apply, chunk

CONTRACT = ("Contratto tra Mario Rossi (CF RSSMRA80A01H501U) e la ditta Alfa.\n\n"
            "Pagamento su IBAN IT60X0542811101000000123456 entro marzo.\n")
MINUTES = "Verbale: presente Giulia Bianchi, telefono 3331234567, dopo la frittata.\n"


def run(env: dict, *args: str, code: int = 0) -> list[dict]:
    """The CLI's stdout as JSON objects: a line that is anything else fails json.loads."""
    r = subprocess.run([CLI, "--json", *args], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == code, r.stderr
    return [json.loads(line) for line in r.stdout.splitlines()]


def text(env: dict, doc_id: str) -> str:
    return "".join(c["text"] for c in run(env, "show", doc_id))


def test_ingest_list_show_review_deny_allow(tmp_path):
    root, docs = tmp_path / "data", tmp_path / "docs"
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    assert run(env, "status") == [{"event": "status", "root": str(root.resolve()), "models": True,
                                   "index": False, "docs": 0, "chunks": 0}]
    assert run(env, "list") == []
    assert run(env, "show", "x", code=1)[0]["error"] == "no_index"

    docs.mkdir()
    (docs / "a.txt").write_text(CONTRACT)
    (docs / "b.txt").write_text(MINUTES)
    (docs / "c.txt").write_text("x")
    (docs / "c.txt").chmod(0)  # unreadable: an error event, the other files go on
    ev = run(env, "ingest", str(docs), code=1)
    assert ev[0] == {"event": "start", "files": 3} and ev[-1] == {"event": "done"}
    a, b, c = ev[1:-1]
    assert c == {"event": "doc", "doc_id": None, "path": str((docs / "c.txt").resolve()),
                 "chunks": None, "status": "error", "error": "PermissionError"}
    assert a["status"] == b["status"] == "indexed" and a["chunks"] == 1 and a["error"] is None
    (docs / "c.txt").unlink()
    ev = run(env, "ingest", str(docs))
    assert [(e["doc_id"], e["chunks"], e["status"]) for e in ev[1:-1]] == [
        (a["doc_id"], 1, "unchanged"), (b["doc_id"], b["chunks"], "unchanged")]
    [st] = run(env, "status")
    assert st["index"] and (st["docs"], st["chunks"]) == (2, a["chunks"] + b["chunks"])

    # review: the text in clear and its spans; masked and chunked, exactly what was published
    shown = run(env, "show", a["doc_id"])
    files = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    [r] = run(env, "review", a["doc_id"])
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == files  # wrote nothing
    assert (r["event"], r["doc_id"], r["path"], r["exists"], r["stale"], r["text"]) == (
        "review", a["doc_id"], a["path"], True, False, CONTRACT)
    assert {"PERSONA", "CF", "IBAN"} <= {s["type"] for s in r["spans"]}
    assert all(s["token"].startswith(f"[{s['type']}_") for s in r["spans"])
    masked = _apply(r["text"], [(s["start"], s["end"], s["token"]) for s in r["spans"]])
    assert chunk(masked) == [c["text"] for c in shown] and shown[0]["event"] == "chunk"
    for cmd in ("show", "review"):
        assert run(env, cmd, "f" * 16, code=1) == [
            {"event": "error", "error": "no_document", "message": f"no document {'f' * 16}"}]

    # a value detection missed: deny it, re-ingest (unchanged file), gone from what the agent sees
    assert "frittata" in text(env, b["doc_id"])
    assert run(env, "deny", "add", "--", "  Frittata ") == [
        {"event": "deny", "action": "add", "changed": True}]
    deny, allow = root / "private" / "deny.txt", root / "private" / "allow.txt"
    assert deny.read_text() == "frittata\n" and deny.stat().st_mode & 0o777 == 0o600
    assert run(env, "deny", "add", "FRITTATA") == [
        {"event": "deny", "action": "add", "changed": False}]
    run(env, "ingest", str(docs / "b.txt"))
    assert "frittata" not in text(env, b["doc_id"])
    # never mask it: allow wins over the earlier deny, --force re-indexes the unchanged file
    run(env, "allow", "add", "frittata")
    assert (deny.read_text(), allow.read_text()) == ("", "frittata\n")
    run(env, "ingest", "--force", str(docs / "b.txt"))
    assert "frittata" in text(env, b["doc_id"])
    assert run(env, "allow", "remove", "frittata")[0]["changed"] is True
    assert run(env, "allow", "remove", "frittata")[0]["changed"] is False
    assert run(env, "allow", "add", " ", code=1)[0]["error"] == "bad_value"

    # list and review flag files gone or changed since ingest; ingest drops the gone ones
    (docs / "a.txt").unlink()
    (docs / "b.txt").write_text(MINUTES + "Aggiunta.\n")
    assert {Path(d["path"]).name: (d["exists"], d["stale"]) for d in run(env, "list")} == {
        "a.txt": (False, False), "b.txt": (True, True)}
    [r] = run(env, "review", a["doc_id"])
    assert (r["exists"], r["stale"], r["text"], r["spans"]) == (False, False, "", [])
    [r] = run(env, "review", b["doc_id"])
    assert r["stale"] and r["text"].endswith("Aggiunta.\n")
    ev = run(env, "ingest", str(docs))
    assert [(e["doc_id"], e["status"]) for e in ev[1:-1]] == [
        (a["doc_id"], "removed"), (b["doc_id"], "indexed")]


def test_desktop_config(tmp_path):
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path / "data"), "HOME": str(tmp_path)}
    cfg = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
    assert run(env, "desktop-config") == [{"event": "desktop", "config": str(cfg), "configured": False,
                                           "command": None, "command_ok": False, "last_log": None}]
    cfg.parent.mkdir(parents=True)
    other = {"globalShortcut": "Alt+Space", "mcpServers": {"other": {"command": "x", "env": {"K": "é"}}}}
    cfg.write_text(json.dumps(other))
    [st] = run(env, "desktop-config", "--install")
    assert (st["configured"], st["command"], st["command_ok"]) == (True, CLI, True)
    assert json.loads(cfg.with_name(cfg.name + ".bak").read_text()) == other
    conf = json.loads(cfg.read_text())
    assert conf["mcpServers"].pop("mcp-secret") == {
        "command": CLI, "args": ["serve"], "env": {"MCP_SECRET_HOME": str((tmp_path / "data").resolve())}}
    assert conf == other  # everything else intact
    log = tmp_path / "Library/Logs/Claude/mcp-server-mcp-secret.log"
    log.parent.mkdir(parents=True)
    log.touch()
    [st] = run(env, "desktop-config", "--uninstall")
    assert not st["configured"] and st["last_log"].endswith("Z") and json.loads(cfg.read_text()) == other
    cfg.write_text("{broken")
    assert run(env, "desktop-config", "--install", code=1)[0]["error"] == "bad_config"
    assert cfg.read_text() == "{broken"


def test_models_events(tmp_path):
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path), "HF_HUB_OFFLINE": "1"}  # cached: offline
    assert run(env, "models") == [{"event": "start", "steps": 2},
                                  {"event": "step", "step": 1, "name": "docling"},
                                  {"event": "step", "step": 2, "name": "detector"}, {"event": "done"}]


def test_unexpected_failure_is_an_error_line(tmp_path):
    for d in ("private", "public"):
        (tmp_path / d).mkdir()
        (tmp_path / d / f"{d}.db").write_text("not a database")
    env = {**os.environ, "MCP_SECRET_HOME": str(tmp_path)}
    assert run(env, "status", code=1) == [{"event": "error", "error": "failed", "message": "DatabaseError"}]


def test_cancel_rolls_back(tmp_path):
    """The UI cancels an ingest with SIGTERM: the open transaction rolls back, so no hot journal
    blocks the read-only commands, and what was reported indexed is exactly what is there."""
    root, docs = tmp_path / "data", tmp_path / "docs"
    docs.mkdir()
    for i in range(40):  # many new values per document -> long transactions
        (docs / f"{i:03}.txt").write_text("\n\n".join(f"user{i}x{j}@example.org" for j in range(100)))
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    journal = root / "private" / "private.db-journal"
    p = subprocess.Popen([CLI, "--json", "ingest", str(docs)], env=env, stdout=subprocess.PIPE, text=True)
    lines = [p.stdout.readline(), p.stdout.readline()]  # start, first document committed
    while p.poll() is None and not journal.exists():
        pass
    p.send_signal(signal.SIGTERM)
    lines += p.stdout.readlines()
    assert p.wait() == 143 and not list(root.rglob("*-journal"))
    indexed = {e["doc_id"] for e in map(json.loads, lines) if e["event"] == "doc"}
    assert {d["doc_id"] for d in run(env, "list")} == indexed

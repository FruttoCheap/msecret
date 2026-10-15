"""The --json contract (docs/cli-json.md), through the real CLI in a temporary data root."""

import json
import os
import signal
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_leak import CLI

from mcp_secret import ingest, pii
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
    # review -: one worker, models loaded once, a line per doc_id read on stdin until it closes
    w = subprocess.run([CLI, "--json", "review", "-"], env=env, input=f"{a['doc_id']}\n\n{'f' * 16}\n",
                       capture_output=True, text=True, check=True)
    assert [json.loads(line) for line in w.stdout.splitlines()] == [
        r, {"event": "error", "error": "no_document", "message": f"no document {'f' * 16}"}]
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


def test_review_reuses_what_ingest_detected(tmp_path, monkeypatch):
    root, docs = tmp_path / "data", tmp_path / "docs"
    (root / "private").mkdir(parents=True)
    docs.mkdir()
    (docs / "a.txt").write_text(CONTRACT)
    assert ingest.run(root, [docs], emit=lambda ev: None) == 0
    db = root / "private" / "private.db"
    [(doc_id,)] = sqlite3.connect(db).execute("SELECT id FROM docs")
    stored = ingest.review(root, doc_id)
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE detected")  # an index from before: detected again, same answer
    assert ingest.review(root, doc_id) == stored
    assert ingest.run(root, [docs], force=True, emit=lambda ev: None) == 0

    def detect(text):
        raise AssertionError("detected again")

    monkeypatch.setattr(pii, "detect", detect)
    assert ingest.review(root, doc_id) == stored  # same text: the spans stored at ingest
    (docs / "a.txt").write_text(CONTRACT + "Fine.\n")
    with pytest.raises(AssertionError, match="detected again"):  # new text: detected
        ingest.review(root, doc_id)


def test_categories_and_tokens(tmp_path):
    root, docs = tmp_path / "data", tmp_path / "docs"
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    assert run(env, "tokens", code=1)[0]["error"] == "no_index"
    for folder, name, body in (("Clienti", "a.txt", CONTRACT), ("Verbali", "b.txt", MINUTES)):
        (docs / folder).mkdir(parents=True)
        (docs / folder / name).write_text(body)
    run(env, "ingest", str(docs))
    a, b = run(env, "list")
    assert (a["categories"], b["categories"]) == (["Clienti"], ["Verbali"])  # the file's folder

    # rules: matched on what the agent sees; other lines of categories.txt are kept
    rules = root / "private" / "categories.txt"
    rules.write_text("# mine\nAltro: x\n")
    pub = (root / "public" / "public.db").read_bytes()
    assert run(env, "rules", "--set", " Pagamenti ", "pagament*, iban", "pagament*") == [
        {"event": "rule", "name": "Altro", "terms": ["x"]},
        {"event": "rule", "name": "Pagamenti", "terms": ["pagament*", "iban"]}]
    assert rules.read_text() == "# mine\nAltro: x\nPagamenti: pagament*, iban\n"
    assert rules.stat().st_mode & 0o777 == 0o600
    assert run(env, "rules", "--set", "a:b", code=1)[0]["error"] == "bad_value"
    assert [d["categories"] for d in run(env, "list")] == [["Clienti", "Pagamenti"], ["Verbali"]]

    # by hand: add one; take one away, automatic too, for good (a re-index does not restore it)
    assert run(env, "cat", "add", b["doc_id"], "Urgente")[0] == {"event": "cat", "action": "add",
                                                                  "changed": True}
    assert run(env, "cat", "add", b["doc_id"], "Urgente")[0]["changed"] is False
    assert run(env, "cat", "remove", a["doc_id"], "Pagamenti")[0]["changed"] is True
    assert run(env, "cat", "remove", a["doc_id"], "Nessuna")[0]["changed"] is False
    assert run(env, "cat", "add", "f" * 16, "X", code=1)[0]["error"] == "no_document"
    assert run(env, "cat", "add", a["doc_id"], " ", code=1)[0]["error"] == "bad_value"
    assert (root / "public" / "public.db").read_bytes() == pub  # categories live in private/ only
    run(env, "ingest", "--force", str(docs))
    assert [d["categories"] for d in run(env, "list")] == [["Clienti"], ["Urgente", "Verbali"]]
    assert run(env, "rules", "--set", "Pagamenti") == [{"event": "rule", "name": "Altro", "terms": ["x"]}]

    # tokens: in the most documents first, value in clear, the documents; filtered by type
    toks = run(env, "tokens")
    assert all(t["event"] == "token" and t["value"] and t["doc_ids"] for t in toks)
    rossi = next(t for t in toks if t["value"] == "mario rossi")
    assert rossi["type"] == "PERSONA" and rossi["doc_ids"] == [a["doc_id"]]
    assert {t["type"] for t in run(env, "tokens", "--type", "IBAN")} == {"IBAN"}
    assert len(run(env, "tokens", "--limit", "1")) == 1

    # a document gone takes its decisions along
    (docs / "Verbali" / "b.txt").unlink()
    run(env, "ingest", str(docs))
    db = sqlite3.connect(root / "private" / "private.db")
    assert db.execute("SELECT doc_id, name, removed FROM cats").fetchall() == [
        (a["doc_id"], "Pagamenti", 1)]


def test_forget_and_cat_many(tmp_path):
    root, docs = tmp_path / "data", tmp_path / "docs"
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    assert run(env, "forget", "f" * 16, code=1)[0]["error"] == "no_index"
    docs.mkdir()
    (docs / "a.txt").write_text(CONTRACT)
    (docs / "b.txt").write_text(MINUTES)
    run(env, "ingest", str(docs))
    a, b = run(env, "list")
    both = [a["doc_id"], b["doc_id"]]

    # as the app calls them: `--`, then the ids, a name that starts with a dash
    # one category for several documents; an unknown id among them changes none
    assert [e["changed"] for e in run(env, "cat", "add", "--", *both, "-Urgente")] == [True, True]
    assert run(env, "cat", "add", "--", both[0], "f" * 16, "Altra", code=1)[0]["error"] == "no_document"
    assert run(env, "cat", "remove", "--", *both, "-Urgente")[1]["changed"] is True
    assert [d["categories"] for d in run(env, "list")] == [["docs"], ["docs"]]

    # forget: gone from the index, public.db wiped of its text, the file untouched
    run(env, "cat", "add", "--", both[0], "-Urgente")
    pub = root / "public" / "public.db"
    assert b"Pagamento su IBAN" in pub.read_bytes()
    gone = run(env, "forget", "--", both[0], "f" * 16, both[0])  # the unknown id and the repeat: skipped
    assert gone == [{"event": "doc", "doc_id": both[0], "path": str(docs / "a.txt"), "chunks": 0,
                     "status": "removed", "error": None}]
    assert run(env, "show", both[0], code=1)[0]["error"] == "no_document"
    assert [d["doc_id"] for d in run(env, "list")] == [both[1]] and text(env, both[1])
    assert b"Pagamento su IBAN" not in pub.read_bytes() and (docs / "a.txt").exists()
    assert sqlite3.connect(root / "private" / "private.db").execute(
        "SELECT count(*) FROM cats WHERE doc_id = ?", (both[0],)).fetchone() == (0,)
    assert [e["status"] for e in run(env, "ingest", str(docs / "a.txt"))[1:-1]] == ["indexed"]


def test_export(tmp_path):
    root, docs, work = tmp_path / "data", tmp_path / "docs", tmp_path / "work"
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    assert run(env, "export", str(work), code=1)[0]["error"] == "no_index"
    docs.mkdir()
    (docs / "a.txt").write_text(CONTRACT)
    (docs / "b.txt").write_text(MINUTES)
    a, b = run(env, "ingest", str(docs))[1:-1]
    for bad in (root / "public", root.parent):  # inside the data root, or around it
        assert run(env, "export", str(bad), code=1)[0]["error"] == "bad_dir"

    [ev] = run(env, "export", str(work))  # a new folder, made inside the sandbox
    assert ev == {"event": "export", "dir": str(work.resolve()), "docs": 2, "removed": 0}
    files = {p.name: p for p in (work / "pseudonymized").iterdir()}
    assert sorted(files) == sorted(f"{d['doc_id']}.md" for d in (a, b))
    for d in (a, b):  # what `show` prints, a document per file
        shown = "\n\n".join(c["text"] for c in run(env, "show", d["doc_id"])) + "\n"
        assert files[f"{d['doc_id']}.md"].read_text() == shown
    clear = ("RSSMRA80A01H501U", "Mario Rossi", "Giulia Bianchi", "3331234567")
    blob = "".join(p.read_text() for p in work.rglob("*.md"))
    assert not [v for v in clear if v in blob] and "[PERSONA_" in blob
    assert "[PERSONA_3]" in (work / "PSEUDONYMIZED.md").read_text()
    assert (work / "pseudonymized").stat().st_mode & 0o755 == 0o755  # readable through another uid
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in files.values())

    # the folder follows the index: a gone document and a killed export's leftover go; any other
    # file stays
    (work / "notes.md").write_text("mine")
    (work / "pseudonymized" / "mine.md").write_text("mine")
    (work / "pseudonymized" / f"{'0' * 16}.md.tmp").write_text("tmp")
    (docs / "a.txt").unlink()
    run(env, "ingest", str(docs))
    [ev] = run(env, "export", str(work))
    assert (ev["docs"], ev["removed"]) == (1, 2)
    assert sorted(p.name for p in (work / "pseudonymized").iterdir()) == sorted(["mine.md", f"{b['doc_id']}.md"])
    assert (work / "notes.md").read_text() == "mine"

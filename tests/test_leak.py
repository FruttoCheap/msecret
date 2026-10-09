"""Crash consistency of ingest and the merge of overlapping masks."""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from mcp_secret.ingest import _apply

CLI = str(Path(sys.executable).parent / "mcp-secret")


def cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([CLI, *args], env=env, capture_output=True, text=True, check=False)


def test_kill_mid_ingest(tmp_path: Path) -> None:
    root, docs = tmp_path / "data", tmp_path / "docs"
    docs.mkdir()
    for i in range(40):  # many new values per document -> long transactions
        (docs / f"{i:03}.txt").write_text("\n\n".join(f"user{i}x{j}@example.org" for j in range(100)))
    env = {**os.environ, "MCP_SECRET_HOME": str(root)}
    journal = root / "private" / "private.db-journal"
    p = subprocess.Popen([CLI, "ingest", str(docs)], env=env, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline()  # first document committed: the next journal is a document's
    while p.poll() is None and not journal.exists():
        pass
    p.kill()
    p.wait()
    assert journal.exists(), "killed outside a transaction, nothing tested"

    r = cli(env, "ingest", str(docs))
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(root / "private" / "private.db")
    con.execute("ATTACH ? AS pub", (str(root / "public" / "public.db"),))
    docs_ids = {d for (d,) in con.execute("SELECT id FROM docs")}
    assert len(docs_ids) == 40
    assert {d for (d,) in con.execute("SELECT DISTINCT doc_id FROM pub.chunks")} == docs_ids
    for d in docs_ids:
        seqs = [s for (s,) in con.execute("SELECT seq FROM pub.chunks WHERE doc_id = ? ORDER BY seq",
                                          (d,))]
        assert seqs == list(range(len(seqs)))
    used = set(re.findall(r"\[\w+_\d+\]", " ".join(t for (t,) in con.execute(
        "SELECT text FROM pub.chunks"))))
    assert used <= {t for (t,) in con.execute("SELECT token FROM entities")}
    assert len(used) == 40 * 100


def test_overlapping_masks_merge() -> None:
    # a detector span and a known value that overlap must not leave the tail in clear
    assert _apply("Mario Rossi Bianchi ok", [(0, 11, "[P_1]"), (6, 19, "[P_2]")]) == "[P_1] ok"

"""MCP tools over public.db, read-only. The only process the agent talks to."""

import re
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

TOKENS = ("Personal data is replaced by tokens like [PERSONA_3], [IBAN_1], [EMAIL_2]. "
          "Each token is one real entity and is the same in every document: reuse it in "
          "queries to find that entity elsewhere. Original values exist nowhere you can reach.")

app = MCPServer("mcp-secret", instructions=TOKENS)
con: sqlite3.Connection


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA temp_store = MEMORY")  # serve's sandbox denies $TMPDIR
    return con


@app.tool(description=f"""Full-text search (BM25) over the user's pseudonymized documents.
{TOKENS} Query syntax (SQLite FTS5): words, prefix*, "exact phrase", AND, OR, NOT,
tokens without brackets (PERSONA_3). Returns up to k (max 20) chunks, best first.""")
def search(query: str, k: int = 10) -> list[dict]:
    try:
        rows = con.execute("SELECT rowid, doc_id, text, -rank FROM chunks WHERE chunks MATCH ? "
                           "ORDER BY rank LIMIT ?",
                           (re.sub(r"\[(\w+)\]", r"\1", query), max(1, min(k, 20)))).fetchall()
    except sqlite3.OperationalError:
        raise ToolError('invalid query: use words, prefix*, "phrases", AND/OR/NOT') from None
    return [{"chunk_id": i, "doc_id": d, "text": t, "score": s} for i, d, t, s in rows]


@app.tool()
def get_chunk(chunk_id: int, neighbors: int = 0) -> list[dict]:
    """Chunk `chunk_id` plus up to `neighbors` (0-2) chunks before and after it, in document order."""
    n = max(0, min(neighbors, 2))
    rows = con.execute("SELECT c.rowid, c.doc_id, c.text FROM chunks c, chunks x WHERE x.rowid = ? "
                       "AND c.doc_id = x.doc_id AND c.seq BETWEEN x.seq - ? AND x.seq + ? "
                       "ORDER BY c.seq", (chunk_id, n, n))
    return [{"chunk_id": i, "doc_id": d, "text": t} for i, d, t in rows]


@app.tool()
def list_sources() -> list[dict]:
    """Indexed documents: random doc_id and chunk count. Titles and paths are not exported."""
    rows = con.execute("SELECT doc_id, count(*) FROM chunks GROUP BY doc_id ORDER BY doc_id")
    return [{"doc_id": d, "n_chunks": n} for d, n in rows]


def run(db: Path) -> None:
    global con
    con = connect(db)
    app.run()

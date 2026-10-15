"""Document categories and the tokens of the graph, for the app only. Categories live in
private/ (the `cats` table, categories.txt); nothing here writes public.db or reaches `serve`."""

import re
import sqlite3
from pathlib import Path

from mcp_secret.ingest import _TOKEN, _hits, _lookup, _pattern


def name(s: str) -> str:
    return " ".join(s.split())


def _rule(line: str) -> tuple[str, list[str]] | None:
    head, sep, tail = line.partition(":")
    if line.lstrip().startswith("#") or not sep or not (n := name(head)):
        return None
    return n, [t for t in map(name, tail.split(",")) if t]


def rules(priv: Path) -> dict[str, list[str]]:
    """private/categories.txt, one `Name: term, term*, two words` per line, # comments. A
    document whose published text holds any term gets the category."""
    p, out = priv / "categories.txt", {}
    for r in filter(None, map(_rule, p.read_text().splitlines() if p.exists() else [])):
        terms = out.setdefault(r[0], [])
        terms += [t for t in r[1] if t not in terms]
    return out


def set_rule(priv: Path, n: str, terms: list[str]) -> str:
    """categories.txt with rule `n` replaced by `terms` (removed if none); other lines and
    comments stay as they are."""
    p = priv / "categories.txt"
    keep = [s for s in (p.read_text().splitlines() if p.exists() else [])
            if (r := _rule(s)) is None or r[0] != n]
    return "".join(f"{s}\n" for s in keep + ([f"{n}: {', '.join(terms)}"] if terms else []))


def _query(terms: list[str]) -> str:
    """FTS5 OR of quoted terms (no syntax error possible), a trailing * kept as a prefix."""
    return " OR ".join('"' + t.rstrip("*").replace('"', '""') + '"' + "*" * t.endswith("*")
                       for t in terms)


def _table(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'cats'").fetchone() is not None


def of_docs(priv_con: sqlite3.Connection, pub_con: sqlite3.Connection,
            priv: Path) -> dict[str, list[str]]:
    """Each document's categories: the name of its file's folder and every rule its published
    text matches (automatic), plus those added by hand, minus those removed by hand."""
    cats = {d: {Path(p).parent.name} - {""} for d, p in priv_con.execute("SELECT id, path FROM docs")}
    for n, terms in rules(priv).items():
        if terms:
            for (d,) in pub_con.execute("SELECT DISTINCT doc_id FROM chunks WHERE chunks MATCH ?",
                                        (_query(terms),)):
                cats.get(d, set()).add(n)
    if _table(priv_con):
        for d, n, removed in priv_con.execute("SELECT doc_id, name, removed FROM cats"):
            if d in cats:
                (cats[d].discard if removed else cats[d].add)(n)
    return {d: sorted(c, key=str.lower) for d, c in cats.items()}


def _published(pub_con: sqlite3.Connection) -> list[tuple[str, str]]:
    """(doc_id, text) of all the agent sees: the chunks and the masked file names."""
    from mcp_secret.server import names

    return [*pub_con.execute("SELECT doc_id, text FROM chunks"), *names(pub_con).items()]


def tokens(priv_con: sqlite3.Connection, pub_con: sqlite3.Connection, types: set[str]) -> list[dict]:
    """Every published token (of `types`, if any), in the most documents first, each with its
    value in clear and its documents. Those in two documents or more link documents in the graph."""
    # ponytail: reads every chunk per call; keep a doc -> tokens table at ingest if it gets slow
    docs: dict[str, set[str]] = {}
    for d, text in _published(pub_con):
        for t in re.findall(_TOKEN, text):
            docs.setdefault(t, set()).add(d)
    values = dict(priv_con.execute("SELECT token, value FROM entities"))
    found = [(t, t[1:-1].rsplit("_", 1)[0], ds) for t, ds in docs.items()]
    found = sorted((x for x in found if not types or x[1] in types), key=lambda x: (-len(x[2]), x[0]))
    return [{"event": "token", "token": t, "type": ty, "value": values.get(t), "doc_ids": sorted(ds)}
            for t, ty, ds in found]


def in_clear(pub_con: sqlite3.Connection | None, values: list[str]) -> list[dict]:
    """Each value with the documents that publish it in clear, matched the way ingest masks."""
    docs: dict[str, set[str]] = {v: set() for v in values}
    if values and pub_con:
        pat, same = _pattern(values), {v: v for v in values}
        for d, text in _published(pub_con):
            for m in _hits(pat, text):
                docs[_lookup(same, m)].add(d)
    return [{"event": "value", "value": v, "doc_ids": sorted(ds)} for v, ds in docs.items()]

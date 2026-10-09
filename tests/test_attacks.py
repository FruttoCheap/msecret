"""Attacks on the pipeline: values hidden by formatting, separators, Unicode, file formats and
the detector's blind spots must not reach public.db (the only thing `serve` can read)."""

import re
import sqlite3
import unicodedata
from pathlib import Path

from mcp_secret import ingest

CF, IBAN, EMAIL = "RSSMRA80A01H501U", "IT60X0542811101000000123456", "zq.canary@example.org"


def published(tmp_path: Path, docs: dict | None = None, deny: str = "") -> str:
    """Write `docs` (name -> text, or a function writing the file), ingest the folder in-process
    (same code as the CLI, minus the sandbox), return every published chunk."""
    root, d = tmp_path / "data", tmp_path / "docs"
    (root / "private").mkdir(parents=True, exist_ok=True)
    d.mkdir(exist_ok=True)
    (root / "private" / "deny.txt").write_text(deny)
    for name, content in (docs or {}).items():
        content(d / name) if callable(content) else (d / name).write_text(content)
    assert ingest.run(root, [d]) == 0
    con = sqlite3.connect(root / "public" / "public.db")
    return "\n".join(t for (t,) in con.execute("SELECT text FROM chunks ORDER BY doc_id, seq"))


def squash(s: str) -> str:
    """What a reader recovers once markup, separators and compatibility forms are dropped."""
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", s)).casefold()


def leaks(text: str, *values: str) -> list[str]:
    return [v for v in values if squash(v) in squash(text)]


def test_fullwidth_and_invisible_characters(tmp_path):
    fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in CF)
    note = (f"Codice fiscale {fullwidth}.\n\nIBAN IT60X054\u200b2811101000000123456 per il rimborso.\n\n"
            f"Coniuge: RSSMRA80\u00adA01H501U, scrivere a zq.canary\u200b@example.org\n")
    assert leaks(published(tmp_path, {"nota.txt": note}), CF, IBAN, EMAIL) == []


def test_deny_entry_with_dotted_capital_i(tmp_path):
    text = published(tmp_path, {"nota.txt": "Pratica riservata, parola chiave: İnce\n"}, deny="İnce\n")
    assert "İnce" not in text

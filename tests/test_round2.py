"""Round-2 regressions: the token contract, the serve sandbox scope, rule gaps, lone names."""

import re
import sqlite3

from test_attacks import IBAN, published

from mcp_secret import server

TOKEN = re.compile(r"\[[A-Z]+(?:_[A-Z]+)*_\d+\]")


def test_published_tokens_are_never_rewritten(tmp_path):
    docs = {"a.txt": f"Accredito su IBAN {IBAN}, per conferme telefono 3331234567.\n",
            "b.txt": "Rata 34 di 120, telefono fisso.\n"}
    published(tmp_path, docs)
    con = sqlite3.connect(tmp_path / "data" / "private" / "private.db")
    learned = {v for (v,) in con.execute("SELECT value FROM entities")}
    assert not learned & {"iban", "telefono", "34", "120"}  # labels and bare numbers: not entities
    # the words become entities late: the sweep must not touch [IBAN_1] / [TELEFONO_1]
    text = published(tmp_path, {"c.txt": "Allegato: contratto_telefono.pdf\n"}, deny="telefono\nIBAN\n")
    assert [t for t in re.findall(r"\[[\w\[\]]*\]", text) if not TOKEN.fullmatch(t)] == []
    assert "telefono" not in TOKEN.sub("", text).lower()  # still masked when glued to "_"
    server.con = server.connect(tmp_path / "data" / "public" / "public.db")
    assert server.search("IBAN_1") and server.search("[TELEFONO_1]")

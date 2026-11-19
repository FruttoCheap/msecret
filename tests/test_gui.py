"""The tkinter UI, through the real CLI in a temporary data root: open a document, mask a value."""

import ast
import inspect
import re
import time
import tkinter as tk

import pytest
from test_cli_json import CONTRACT

from mcp_secret import gui


def test_italian_has_every_string():
    """Each string the UI shows has its Italian, with the same {fields}: a missing one would show
    in English, a wrong field would raise."""
    def texts(n: ast.expr) -> set:  # a literal, a module constant, or one of two
        if isinstance(n, ast.IfExp):
            return texts(n.body) | texts(n.orelse)
        return {n.value if isinstance(n, ast.Constant) else getattr(gui, getattr(n, "id", ""), None)}

    shown = {s for c in ast.walk(ast.parse(inspect.getsource(gui)))
             if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "tr" for s in texts(c.args[0])}
    shown.discard(None)  # a local variable: tr(state), "missing" or "changed"
    assert {gui.FIND, "Original", "{n} documents"} <= shown  # the walk does find them
    assert shown <= set(gui.IT), shown - set(gui.IT)
    for en, it in gui.IT.items():
        assert sorted(re.findall(r"{\w*}", en)) == sorted(re.findall(r"{\w*}", it)), en


def test_open_then_always_mask(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as e:  # no display; uv's macOS Python in a venv also wants TCL_LIBRARY
        pytest.skip(f"no Tk: {e}")
    monkeypatch.setenv("MCP_SECRET_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(gui, "LANG", gui.IT)  # whatever the system's: the Italian runs for real
    errors = []  # a modal dialog would block the loop below
    for dialog in ("showerror", "showwarning"):
        monkeypatch.setattr(gui.messagebox, dialog, lambda *a: errors.append(a))
    (doc := tmp_path / "a.txt").write_text(CONTRACT)
    app = gui.App(root)

    def until(cond) -> None:
        end = time.monotonic() + 180
        while not cond() and not errors:
            assert time.monotonic() < end
            root.update()
            time.sleep(0.05)
        assert not errors, errors

    def agent() -> str:
        return app.agent.get("1.0", "end")

    def masked() -> list[str]:  # the values tinted by type in the original
        r = [i for ty in gui.COLORS for i in app.clear.tag_ranges(ty)]
        return [app.clear.get(a, b) for a, b in zip(r[::2], r[1::2])]

    app.ingest(str(doc))
    until(lambda: app.docs)
    assert app.status.get() == "1 documento"
    app.tree.selection_set(next(iter(app.docs)))
    until(lambda: "[PERSONA_" in agent() and masked())
    assert {"Mario Rossi", "RSSMRA80A01H501U"} <= set(masked()) and "Mario" not in agent()
    assert len(app.clear.tag_ranges("chip")) == 2 * len(masked())  # a token tag after each value
    assert app.agent.tag_ranges("PERSONA") and app.agent.tag_ranges("IBAN")  # tokens colored
    app._find("[CF_1]")
    assert app.agent.get(*app.agent.tag_ranges("hit")[:2]) == "[CF_1]"

    at = app.clear.search("Mario Rossi", "1.0")  # a value and its token tag: the value alone
    app.clear.tag_add("sel", at, app.clear.tag_nextrange("chip", at)[1])
    assert app.selected() == "Mario Rossi"
    app.clear.tag_remove("sel", "1.0", "end")
    at = app.clear.search("marzo", "1.0")
    app.clear.tag_add("sel", at, f"{at}+5c")
    app.mask("deny")
    until(lambda: "marzo" not in agent() and "marzo" in masked())

    out = tmp_path / "out"  # Export…: what the agent sees, straight in the folder
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_: str(out))
    monkeypatch.setattr(gui, "launch", lambda p: None)
    app.export(app.cur)
    until(lambda: list(out.glob("*.md")))
    assert "[PERSONA_" in next(out.glob("*.md")).read_text(encoding="utf-8")
    app.worker.stdin.close()
    app.worker.wait()
    root.destroy()

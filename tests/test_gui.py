"""The tkinter UI, through the real CLI in a temporary data root: open a document, mask a value."""

import time
import tkinter as tk

import pytest
from test_cli_json import CONTRACT

from mcp_secret import gui


def test_open_then_always_mask(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as e:  # no display; uv's macOS Python in a venv also wants TCL_LIBRARY
        pytest.skip(f"no Tk: {e}")
    monkeypatch.setenv("MCP_SECRET_HOME", str(tmp_path / "data"))
    errors = []  # a modal dialog would block the loop below
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a: errors.append(a))
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

    def masked() -> list[str]:
        r = app.clear.tag_ranges("pii")
        return [app.clear.get(a, b) for a, b in zip(r[::2], r[1::2])]

    app.ingest(str(doc))
    until(lambda: app.docs)
    app.tree.selection_set(next(iter(app.docs)))
    until(lambda: "[PERSONA_" in agent())
    assert {"Mario Rossi", "RSSMRA80A01H501U"} <= set(masked()) and "Mario" not in agent()
    assert "marzo" in agent()

    at = app.clear.search("marzo", "1.0")
    app.clear.tag_add("sel", at, f"{at}+5c")
    app.mask("deny")
    until(lambda: "marzo" not in agent())
    assert "marzo" in masked()
    app.worker.stdin.close()
    app.worker.wait()
    root.destroy()

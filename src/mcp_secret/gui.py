"""Desktop UI where the Mac app (apps/macos) does not run: Windows, Linux. tkinter, stdlib only.
Like the Mac app it only runs the CLI with --json (docs/cli-json.md): it never opens the
databases, never starts serve, never loads a model. Paths, text and values stay in memory."""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

EXE = str(Path(sys.executable).parent / ("mcp-secret.exe" if os.name == "nt" else "mcp-secret"))


def spawn(*args: str, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    # stderr may quote documents: dropped, as the Mac app does. No console window on Windows.
    return subprocess.Popen([EXE, "--json", *args], stdin=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class App:
    def __init__(self, root: tk.Tk):
        self.root, self.docs, self.cur, self.worker = root, {}, None, None
        # ponytail: one CLI call at a time (writes must not overlap), so a document opened
        # during an ingest waits for it; a second queue if reads should run alongside
        self.jobs, self.ui = queue.Queue(), queue.Queue()
        threading.Thread(target=self._run_jobs, daemon=True).start()
        root.title("mcp-secret")
        root.geometry("1200x700")
        bar = ttk.Frame(root, padding=4)
        bar.pack(fill="x")
        for label, cmd in (("Add files…", self.add_files), ("Add folder…", self.add_folder),
                           ("Re-index all", self.reindex), ("Connect Claude Desktop", self.desktop)):
            ttk.Button(bar, text=label, command=cmd).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=8)
        panes = ttk.PanedWindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(panes, columns=("state",), selectmode="browse")
        self.tree.heading("#0", text="Document")
        self.tree.heading("state", text="State")
        self.tree.column("state", width=90, stretch=False)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.open())
        panes.add(self.tree, weight=1)
        self.clear = self._pane(panes, "Original: hover a highlight for its token", (
            ("Always mask selection", lambda: self.mask("deny")),
            ("Never mask selection", lambda: self.mask("allow"))))
        self.clear.tag_configure("pii", background="#ffe08a", foreground="black")
        self.agent = self._pane(panes, "What the agent sees", ())
        self._poll()
        self.job(lambda: self.cli("status"), self._started)

    def _pane(self, panes, title, buttons) -> tk.Text:
        f = ttk.Frame(panes)
        head = ttk.Frame(f)
        head.pack(fill="x")
        ttk.Label(head, text=title).pack(side="left")
        for label, cmd in buttons:
            ttk.Button(head, text=label, command=cmd).pack(side="right")
        t = tk.Text(f, wrap="word", width=50, state="disabled")
        s = ttk.Scrollbar(f, command=t.yview)
        t["yscrollcommand"] = s.set
        s.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        panes.add(f, weight=2)
        return t

    # CLI calls run on one thread; what they hand back runs on the UI thread (tkinter's own)

    def job(self, work, then) -> None:
        self.jobs.put((work, then))

    def _run_jobs(self) -> None:
        while True:
            work, then = self.jobs.get()
            try:
                r = work()
                self.ui.put(lambda r=r, then=then: then(r))
            except Exception as e:  # noqa: BLE001 - shown; the next job still runs
                m = f"{type(e).__name__}: {e}"
                self.ui.put(lambda m=m: messagebox.showerror("mcp-secret", m))

    def _poll(self) -> None:
        while not self.ui.empty():
            self.ui.get_nowait()()
        self.root.after(50, self._poll)

    def cli(self, *args: str, each=None) -> list[dict]:
        """One CLI command's events; `each` gets every one on the UI thread as it comes."""
        p, evs = spawn(*args), []
        for line in p.stdout:
            evs.append(ev := json.loads(line))
            if each:
                self.ui.put(lambda ev=ev: each(ev))
        p.wait()
        return self._check(evs)

    def review(self, doc_id: str) -> dict:
        """`review -` stays up: the models load once, then each document opens in under a second."""
        if self.worker is None or self.worker.poll() is not None:
            self.worker = spawn("review", "-", stdin=subprocess.PIPE)
        self.worker.stdin.write(doc_id + "\n")
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        return self._check([json.loads(line) if line else
                            {"event": "error", "error": "failed", "message": "review stopped"}])[0]

    def _check(self, evs: list[dict]) -> list[dict]:
        for ev in evs:
            if ev["event"] == "error":
                self.ui.put(lambda m=f"{ev['error']}: {ev['message']}": messagebox.showerror("mcp-secret", m))
        return evs

    # actions

    def _started(self, evs: list[dict]) -> None:
        if evs and not evs[0]["models"] and messagebox.askyesno(
                "mcp-secret", "Download the models now? About 2 GB, once: the only network use."):
            self.status.set("Downloading models…")
            self.job(lambda: self.cli("models", each=self._step), lambda _: self.status.set("Models ready"))
        self.refresh()

    def _step(self, ev: dict) -> None:
        if ev["event"] == "step":
            self.status.set(f"Downloading models {ev['step']}/2…")

    def refresh(self) -> None:
        self.job(lambda: self.cli("list"), self._listed)

    def _listed(self, evs: list[dict]) -> None:
        self.docs = {ev["doc_id"]: ev for ev in evs if ev["event"] == "doc"}
        self.tree.delete(*self.tree.get_children())
        for d in self.docs.values():
            state = "file missing" if not d["exists"] else "changed" if d["stale"] else f"{d['chunks']} chunks"
            self.tree.insert("", "end", iid=d["doc_id"], text=Path(d["path"]).name, values=(state,))
        self.status.set(f"{len(self.docs)} documents")
        if self.cur in self.docs:
            self.tree.selection_set(self.cur)  # opens it again

    def add_files(self) -> None:
        self.ingest(*filedialog.askopenfilenames(title="Add files"))

    def add_folder(self) -> None:
        if folder := filedialog.askdirectory(title="Add a folder"):
            self.ingest(folder)

    def reindex(self) -> None:
        self.ingest(*(d["path"] for d in self.docs.values() if d["exists"]), force=True)

    def ingest(self, *paths: str, force: bool = False) -> None:
        if not paths:
            return
        n = [0]

        def each(ev: dict) -> None:
            if ev["event"] == "doc" and ev["status"] != "removed":
                n[0] += 1
                self.status.set(f"Indexing {n[0]}…")

        def done(evs: list[dict]) -> None:
            bad = [f"{Path(e['path']).name}: {e['error']}" for e in evs if e.get("status") == "error"]
            if bad:
                messagebox.showwarning("Not indexed", "\n".join(bad))
            self.refresh()

        self.status.set("Indexing…")
        self.job(lambda: self.cli("ingest", *paths, *(["--force"] if force else []), each=each), done)

    def open(self) -> None:
        if not (sel := self.tree.selection()):
            return
        self.cur = doc_id = sel[0]
        self.status.set("Opening… (the first one loads the models)")
        self.job(lambda: (self.review(doc_id), self.cli("show", doc_id)), lambda r: self._show(*r))

    def _show(self, rev: dict, chunks: list[dict]) -> None:
        for t in (self.clear, self.agent):
            t["state"] = "normal"
            t.delete("1.0", "end")
        if rev["event"] == "review":
            text, at = rev["text"], 0
            for i, s in enumerate(rev["spans"]):  # inserted piecewise: no offset arithmetic
                self.clear.insert("end", text[at:s["start"]])
                self.clear.insert("end", text[s["start"]:s["end"]], ("pii", f"s{i}"))
                self.clear.tag_bind(f"s{i}", "<Enter>", lambda _, t=s["token"]: self.status.set(t))
                at = s["end"]
            self.clear.insert("end", text[at:])
        self.agent.insert("end", "\n\n".join(c["text"] for c in chunks if c["event"] == "chunk"))
        for t in (self.clear, self.agent):
            t["state"] = "disabled"
        self.status.set("")

    def mask(self, cmd: str) -> None:
        """deny (always mask) or allow (never mask) the selected text, then index this document
        again; other documents keep an allowed value masked until Re-index all."""
        try:
            value = self.clear.get("sel.first", "sel.last").strip()
        except tk.TclError:
            value = ""
        if not value or self.cur not in self.docs:
            return self.status.set("Select a value in the original first")
        self.job(lambda: self.cli(cmd, "add", "--", value), lambda _: None)
        self.ingest(self.docs[self.cur]["path"], force=cmd == "allow")

    def desktop(self) -> None:
        self.job(lambda: self.cli("desktop-config", "--install"),
                 lambda evs: evs[-1:] and evs[-1]["event"] == "desktop" and messagebox.showinfo(
                     "mcp-secret", "Added to Claude Desktop: restart Claude."))


def main() -> None:
    if os.name == "nt":  # sharp text on scaled displays instead of a blurry bitmap
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

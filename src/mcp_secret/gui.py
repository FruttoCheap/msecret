"""Desktop UI where the Mac app (apps/macos) does not run: Windows, Linux. tkinter, stdlib only.
Like the Mac app it only runs the CLI with --json (docs/cli-json.md): it never opens the
databases, never starts serve, never loads a model. Paths, text and values stay in memory."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

EXE = str(Path(sys.executable).parent / ("mcp-secret.exe" if os.name == "nt" else "mcp-secret"))
TOKEN = re.compile(r"\[([A-Z]+(?:_[A-Z]+)*)_\d+\]")
COLORS = {  # by type, as in the Mac app (DocumentView.swift)
    "PERSONA": "#007aff", "ORG": "#af52de", "LUOGO": "#34c759", "INDIRIZZO": "#30b0c7",
    "CF": "#ff9500", "PIVA": "#a2845e", "IBAN": "#ff3b30", "EMAIL": "#ff2d55",
    "TELEFONO": "#5856d6", "TARGA": "#ffcc00", "DOCUMENTO": "#00c7be", "DATA_NASCITA": "#32ade6",
}
INK, MUTED, LINE = "#1d1d1f", "#6e6e73", "#d2d2d7"


def tint(color: str, a: float) -> str:
    """`color` at opacity `a` over white."""
    return "#" + "".join(f"{round(255 - (255 - int(color[i:i + 2], 16)) * a):02x}" for i in (1, 3, 5))


def spawn(*args: str, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL) -> subprocess.Popen:
    # stderr may quote documents: dropped, or kept in memory. No console window on Windows.
    return subprocess.Popen([EXE, "--json", *args], stdin=stdin, stdout=subprocess.PIPE,
                            stderr=stderr, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class App:
    def __init__(self, root: tk.Tk):
        self.root, self.docs, self.cur, self.worker, self.busy = root, {}, None, None, 0
        self.log = deque(maxlen=15)  # the review worker's last stderr lines, shown on its errors
        # ponytail: one CLI call at a time (writes must not overlap), so a document opened
        # during an ingest waits for it; a second queue if reads should run alongside
        self.jobs, self.ui = queue.Queue(), queue.Queue()
        threading.Thread(target=self._run_jobs, daemon=True).start()
        self.font, self.bold, self.small, self.big = (tkfont.nametofont("TkDefaultFont").copy()
                                                      for _ in range(4))
        self.font.configure(size=11)
        self.bold.configure(size=10, weight="bold")
        self.small.configure(size=8, weight="bold")
        self.big.configure(size=14, weight="bold")
        ttk.Style().configure("Treeview", rowheight=self.font.metrics("linespace") + 10)
        root.title("mcp-secret")
        root.geometry("1280x800")
        if os.name == "nt":
            root.state("zoomed")

        bar = ttk.Frame(root, padding=(10, 8))
        bar.pack(fill="x")
        for label, cmd in (("Add files…", self.add_files), ("Add folder…", self.add_folder),
                           ("Re-index all", self.reindex), ("Connect Claude Desktop", self.desktop)):
            ttk.Button(bar, text=label, command=cmd).pack(side="left", padx=(0, 6))
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, foreground=MUTED).pack(side="right")
        self.spin = ttk.Progressbar(bar, mode="indeterminate", length=100)
        ttk.Separator(root).pack(fill="x")

        split = ttk.PanedWindow(root, orient="horizontal")
        split.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(split, columns=("state",), selectmode="browse")
        self.tree.heading("#0", text="Document", anchor="w")
        self.tree.heading("state", text="State", anchor="w")
        self.tree.column("state", width=110, stretch=False)
        self.tree.tag_configure("missing", foreground="#c62828")
        self.tree.tag_configure("changed", foreground="#e65100")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.open())
        split.add(self.tree, weight=1)

        right = ttk.Frame(split, padding=(14, 10))
        split.add(right, weight=4)
        head = ttk.Frame(right)
        head.pack(fill="x")
        self.name = ttk.Label(head, text="No document open", font=self.big)
        self.name.pack(side="left")
        ttk.Button(head, text="Open file", command=self.open_file).pack(side="right")
        ttk.Button(head, text="Re-index", command=self.reindex_one).pack(side="right", padx=6)
        self.note = ttk.Label(head, foreground=MUTED)  # the path, or what is wrong with it
        self.note.pack(side="left", padx=10)
        self.legend = ttk.Frame(right)
        self.legend.pack(fill="x", pady=(8, 0))
        panes = ttk.PanedWindow(right, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=8)
        self.clear = self._pane(panes, "Original (click a value to find its token)")
        self.agent = self._pane(panes, "What the agent sees")
        acts = ttk.Frame(right)
        acts.pack(fill="x")
        ttk.Label(acts, text="Select text in the original, then:", foreground=MUTED).pack(side="left")
        ttk.Button(acts, text="Always mask", command=lambda: self.mask("deny")).pack(side="left", padx=6)
        ttk.Button(acts, text="Never mask…", command=lambda: self.mask("allow")).pack(side="left")

        self._fill(self.clear, "Add files or a folder, then pick a document on the left.")
        self._poll()
        self.job(lambda: self.cli("status"), self._started)

    def _pane(self, panes, title: str) -> tk.Text:
        f = ttk.Frame(panes)
        ttk.Label(f, text=title, font=self.bold).pack(anchor="w", pady=(0, 4))
        t = tk.Text(f, wrap="word", width=40, state="disabled", font=self.font, padx=14, pady=12,
                    spacing1=2, spacing3=6, relief="flat", background="white", foreground=INK,
                    highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE,
                    selectbackground="#b3d4fc", selectforeground=INK,
                    inactiveselectbackground="#b3d4fc", cursor="arrow")
        s = ttk.Scrollbar(f, command=t.yview)
        t["yscrollcommand"] = s.set
        s.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        for ty, c in COLORS.items():
            t.tag_configure(ty, background=tint(c, .25))
        t.tag_configure("hint", foreground=MUTED)
        t.tag_configure("head", foreground=MUTED, font=self.small, spacing1=8)
        t.tag_configure("hit", font=self.bold, underline=True, relief="solid", borderwidth=1)
        panes.add(f, weight=1)
        return t

    # CLI calls run on one thread; what they hand back runs on the UI thread (tkinter's own)

    def job(self, work, then) -> None:
        if not self.busy:
            self.spin.pack(side="right", padx=10)
            self.spin.start(12)
        self.busy += 1
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
            finally:
                self.ui.put(self._idle)

    def _idle(self) -> None:
        self.busy -= 1
        if not self.busy:
            self.spin.stop()
            self.spin.pack_forget()

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
        for ev in evs:
            if ev["event"] == "error":
                m = f"{ev['error']}: {ev['message']}"
                self.ui.put(lambda m=m: messagebox.showerror("mcp-secret", m))
        return evs

    def review(self, doc_id: str) -> dict:
        """`review -` stays up: the models load once, then each document opens in under a second."""
        if self.worker is None or self.worker.poll() is not None:
            self.worker = spawn("review", "-", stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=self.log.extend, args=(self.worker.stderr,), daemon=True).start()
        self.worker.stdin.write(doc_id + "\n")
        self.worker.stdin.flush()
        line = self.worker.stdout.readline()
        return json.loads(line) if line else {"event": "error", "error": "failed",
                                              "message": f"review exited ({self.worker.wait()})"}

    # documents

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
            state = "missing" if not d["exists"] else "changed" if d["stale"] else ""
            self.tree.insert("", "end", iid=d["doc_id"], text=Path(d["path"]).name, tags=(state,),
                             values=(state or f"{d['chunks']} chunk{'s' * (d['chunks'] != 1)}",))
        self.status.set(f"{len(self.docs)} documents")
        if self.cur in self.docs:
            self.tree.selection_set(self.cur)  # opens it again

    def open(self) -> None:
        if not (sel := self.tree.selection()):
            return
        self.cur = doc_id = sel[0]
        d = self.docs[doc_id]
        self.name["text"] = Path(d["path"]).name
        self.note.configure(**(
            {"text": "File missing: only what the agent sees is left.", "foreground": "#c62828"}
            if not d["exists"] else
            {"text": "Changed since indexed: the agent still sees the old text. Re-index it.",
             "foreground": "#e65100"} if d["stale"] else {"text": d["path"], "foreground": MUTED}))
        self._fill(self.clear, "Reading the original… the first one also loads the models, "
                               "up to a minute.")
        if d["chunks"]:
            self.job(lambda: self.cli("show", doc_id), lambda evs: self._agent(doc_id, evs))
        else:
            self._fill(self.agent, "Nothing published: the file has no text.")
        self.job(lambda: self.review(doc_id), lambda rev: self._original(doc_id, rev))

    def _fill(self, t: tk.Text, hint: str = "") -> None:
        """Empty a pane (and its token labels), with a gray `hint` in it."""
        for w in t.winfo_children():
            w.destroy()
        t["state"] = "normal"
        t.delete("1.0", "end")
        t.insert("end", hint, "hint")
        t["state"] = "disabled"

    def _original(self, doc_id: str, rev: dict) -> None:
        if doc_id != self.cur:
            return
        t = self.clear
        for w in self.legend.winfo_children():  # a document opened again: it comes back here
            w.destroy()
        if rev["event"] == "error":  # stderr is only shown here, never written anywhere
            return self._fill(t, f"Can't read the original: {rev['error']}, {rev['message']}.\n\n"
                                 + "".join(self.log))
        if not rev["exists"]:
            return self._fill(t, "File missing: only what the agent sees is left.")
        self._fill(t)
        t["state"] = "normal"
        text, at = rev["text"], 0
        for s in rev["spans"]:  # inserted piecewise: no offset arithmetic
            t.insert("end", text[at:s["start"]])
            t.insert("end", text[s["start"]:s["end"]], (s["type"], s["token"]))
            t.tag_bind(s["token"], "<Button-1>", lambda _, k=s["token"]: self._find(k))
            # ponytail: a label per span; a document with thousands of them opens slowly
            chip = tk.Label(t, text=s["token"], font=self.small, foreground=INK, padx=3, pady=0,
                            background=tint(COLORS.get(s["type"], "#8e8e93"), .5), cursor="hand2")
            chip.bind("<Button-1>", lambda _, k=s["token"]: self._find(k))
            t.window_create("end", window=chip, padx=2)
            at = s["end"]
        t.insert("end", text[at:])
        t["state"] = "disabled"
        types = {s["type"] for s in rev["spans"]}
        for ty in (ty for ty in COLORS if ty in types):
            tk.Label(self.legend, text=ty, font=self.small, foreground=INK, padx=6, pady=1,
                     background=tint(COLORS[ty], .5)).pack(side="left", padx=(0, 6))

    def _agent(self, doc_id: str, evs: list[dict]) -> None:
        if doc_id != self.cur:
            return
        t = self.agent
        self._fill(t)
        t["state"] = "normal"
        for c in (ev for ev in evs if ev["event"] == "chunk"):
            t.insert("end", f"CHUNK {c['chunk_id']}\n", "head")
            text, at = c["text"].strip("\n"), 0
            for m in TOKEN.finditer(text):
                t.insert("end", text[at:m.start()])
                t.insert("end", m[0], (m[1], m[0]))  # its type's color; its own tag, for _find
                t.tag_bind(m[0], "<Button-1>", lambda _, k=m[0]: self._find(k))
                at = m.end()
            t.insert("end", text[at:] + "\n\n")
        t["state"] = "disabled"

    def _find(self, token: str) -> None:
        """Mark every occurrence of `token` in what the agent sees, scroll to the first."""
        a = self.agent
        a.tag_remove("hit", "1.0", "end")
        r = a.tag_ranges(token)
        for i in range(0, len(r), 2):
            a.tag_add("hit", r[i], r[i + 1])
        if r:
            a.see(r[0])
        self.status.set(f"{token}: {len(r) // 2} in what the agent sees")

    # actions

    def add_files(self) -> None:
        self.ingest(*filedialog.askopenfilenames(title="Add files"))

    def add_folder(self) -> None:
        if folder := filedialog.askdirectory(title="Add a folder"):
            self.ingest(folder)

    def reindex(self) -> None:
        self.ingest(*(d["path"] for d in self.docs.values() if d["exists"]), force=True)

    def reindex_one(self) -> None:
        if self.cur in self.docs:
            self.ingest(self.docs[self.cur]["path"], force=True)

    def open_file(self) -> None:
        """The original in its own app: the PDF as a PDF, not as parsed text."""
        if self.cur not in self.docs or not self.docs[self.cur]["exists"]:
            return
        path = self.docs[self.cur]["path"]
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

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

    def mask(self, cmd: str) -> None:
        """deny (always mask) or allow (never mask) the selected text, then index this document
        again; other documents keep an allowed value masked until Re-index all."""
        try:
            value = self.clear.get("sel.first", "sel.last").strip()  # token labels left out
        except tk.TclError:
            value = ""
        if not value or self.cur not in self.docs:
            return self.status.set("Select text in the original first")
        if cmd == "allow" and not messagebox.askyesno(
                "Publish this value in clear?", f"“{value}” becomes visible to the agent in this "
                "document. Other documents keep it masked until Re-index all."):
            return
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

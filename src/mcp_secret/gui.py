"""Desktop UI where the Mac app (apps/macos) does not run: Windows, Linux. tkinter, with sv-ttk
for the Windows 11 look. Like the Mac app it only runs the CLI with --json (docs/cli-json.md): it
never opens the databases, never starts serve, never loads a model. Paths, text and values stay
in memory."""

import json
import locale
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

import sv_ttk

EXE = str(Path(sys.executable).parent / ("mcp-secret.exe" if os.name == "nt" else "mcp-secret"))
TOKEN = re.compile(r"\[([A-Z]+(?:_[A-Z]+)*)_\d+\]")
COLORS = {  # by type, as in the Mac app (DocumentView.swift)
    "PERSONA": "#007aff", "ORG": "#af52de", "LUOGO": "#34c759", "INDIRIZZO": "#30b0c7",
    "CF": "#ff9500", "PIVA": "#a2845e", "IBAN": "#ff3b30", "EMAIL": "#ff2d55",
    "TELEFONO": "#5856d6", "TARGA": "#ffcc00", "DOCUMENTO": "#00c7be", "DATA_NASCITA": "#32ade6",
}
# sv-ttk's light ink and accent; Windows 11's secondary text, card stroke, critical and caution
INK, MUTED, LINE, PAPER, ACCENT = "#1c1c1c", "#5f5f5f", "#e5e5e5", "#ffffff", "#005fb8"
RED, ORANGE = "#c42b1c", "#9d5d00"
BODY, STRONG, CAPTION = "SunValleyBodyFont", "SunValleyBodyStrongFont", "SunValleyCaptionFont"
FIND = "Click a highlighted value in the original to find its token here."


def italian() -> bool:
    """The system speaks Italian: Windows by its display language, elsewhere by the locale."""
    if os.name == "nt":
        import ctypes

        return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x10  # LANG_ITALIAN
    return (locale.getlocale()[0] or "").startswith("it")


IT = {  # the Mac app's terms (apps/macos/Sources/MCPSecret/Localizable.xcstrings)
    "Add files…": "Aggiungi file…",
    "Add folder…": "Aggiungi cartella…",
    "Re-index all": "Re-indicizza tutto",
    "Export all for Cowork…": "Esporta tutto per Cowork…",
    "Connect Claude Desktop": "Collega Claude Desktop",
    "Document": "Documento",
    "State": "Stato",
    "No document open": "Nessun documento aperto",
    "Add files or a folder to start.": "Aggiungi file o una cartella per iniziare.",
    "Pick a document on the left.": "Scegli un documento a sinistra.",
    "Open file": "Apri file",
    "Re-index": "Re-indicizza",
    "Export…": "Esporta…",
    "Original": "Originale",
    "Click a value to find its token. Select text to mask it.":
        "Fai clic su un valore per trovarne il token. Seleziona del testo per mascherarlo.",
    FIND: "Fai clic su un valore evidenziato nell'originale per trovare qui il suo token.",
    "What the agent sees": "Cosa vede l'agente",
    "Always mask": "Maschera sempre",
    "Never mask…": "Non mascherare mai…",
    "the text selected in the original (or right-click it)":
        "il testo selezionato nell'originale (o con il clic destro)",
    "Download the models now? About 2 GB, once: the only network use.":
        "Scaricare ora i modelli? Circa 2 GB, una volta sola: l'unico uso della rete.",
    "Downloading models…": "Download dei modelli…",
    "Downloading models {step}/2…": "Download dei modelli {step}/2…",
    "Models ready": "Modelli pronti",
    "missing": "mancante",
    "changed": "cambiato",
    "{n} chunk": "{n} frammento",
    "{n} chunks": "{n} frammenti",
    "{n} document": "{n} documento",
    "{n} documents": "{n} documenti",
    "File missing: only what the agent sees is left.":
        "File mancante: resta solo ciò che vede l'agente.",
    "Changed since indexed: the agent still sees the old text. Re-index it.":
        "Cambiato dopo l'indicizzazione: l'agente vede ancora il testo vecchio. Re-indicizzalo.",
    "Reading the original… the first one also loads the models, up to a minute.":
        "Lettura dell'originale… il primo carica anche i modelli, fino a un minuto.",
    "Loading…": "Caricamento…",
    "Nothing published: the file has no text.": "Niente di pubblicato: il file non ha testo.",
    "Can't read the original: {error}, {message}.":
        "Impossibile leggere l'originale: {error}, {message}.",
    "Chunk {n}": "Frammento {n}",
    "{token}: {n} occurrence": "{token}: {n} occorrenza",
    "{token}: {n} occurrences": "{token}: {n} occorrenze",
    "Add files": "Aggiungi file",
    "Add a folder": "Aggiungi una cartella",
    "Export to": "Esporta in",
    "Exported {n} document": "{n} documento esportato",
    "Exported {n} documents": "{n} documenti esportati",
    "Exporting…": "Esportazione…",
    "Indexing…": "Indicizzazione…",
    "Indexing {n}…": "Indicizzazione {n}…",
    "Not indexed": "Indicizzazione non riuscita",
    "Select text in the original first": "Prima seleziona del testo nell'originale",
    "Publish this value in clear?": "Pubblicare questo valore in chiaro?",
    "“{value}” becomes visible to the agent in this document. Other documents keep it masked "
    "until Re-index all.": "“{value}” diventa visibile all'agente in questo documento. Negli "
    "altri documenti resta mascherato fino a Re-indicizza tutto.",
    "Added to Claude Desktop: restart Claude.": "Aggiunto a Claude Desktop: riavvia Claude.",
}
LANG = IT if italian() else {}  # ponytail: Italian or English, like the Mac app; no gettext


def tr(en: str, **kw) -> str:
    """`en` in the system's language; `kw` fills its {fields}. CLI codes and messages stay as is."""
    return LANG.get(en, en).format(**kw)


def tint(color: str, a: float) -> str:
    """`color` at opacity `a` over white."""
    return "#" + "".join(f"{round(255 - (255 - int(color[i:i + 2], 16)) * a):02x}" for i in (1, 3, 5))


def launch(path: str) -> None:
    """A file in its own app, a folder in the file manager."""
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


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
        sv_ttk.set_theme("light", root)
        root.update_idletasks()  # sv-ttk recolors plain tk widgets once, now: before ours exist
        # sv-ttk's fonts and our sizes are pixels at 100%; Windows at 150% has 144 dpi, so 1.5
        scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.px = px = lambda n: round(n * scale)
        for name in (n for n in tkfont.names(root) if n.startswith("SunValley")):
            f = tkfont.nametofont(name, root)
            f["size"] = px(f["size"])  # negative: pixels
        style = ttk.Style(root)
        style.configure("Treeview", rowheight=tkfont.nametofont(BODY, root).metrics("linespace")
                        + px(14))
        for b in ("TButton", "Accent.TButton"):
            style.configure(b, padding=(px(8), px(2), px(8), px(3)))
        root.title("mcp-secret")
        root.geometry(f"{px(1280)}x{px(800)}")
        root.minsize(px(800), px(500))
        if os.name == "nt":
            root.state("zoomed")

        bar = ttk.Frame(root, padding=(px(12), px(10)))
        bar.pack(fill="x")
        ttk.Button(bar, text=tr("Add files…"), style="Accent.TButton",
                   command=self.add_files).pack(side="left")
        ttk.Button(bar, text=tr("Add folder…"), command=self.add_folder).pack(side="left", padx=px(8))
        ttk.Button(bar, text=tr("Re-index all"), command=self.reindex).pack(side="left")
        ttk.Button(bar, text=tr("Export all for Cowork…"), command=self.export).pack(side="left",
                                                                                 padx=px(8))
        ttk.Button(bar, text=tr("Connect Claude Desktop"), command=self.desktop).pack(side="right")
        ttk.Separator(root).pack(fill="x")
        foot = ttk.Frame(root, padding=(px(12), px(5)))  # the status bar
        foot.pack(side="bottom", fill="x")
        ttk.Separator(root).pack(side="bottom", fill="x")
        self.status = tk.StringVar()
        ttk.Label(foot, textvariable=self.status, font=CAPTION, foreground=MUTED).pack(side="left")
        self.spin = ttk.Progressbar(foot, mode="indeterminate", length=px(140))

        split = ttk.PanedWindow(root, orient="horizontal")
        split.pack(fill="both", expand=True)
        side = ttk.Frame(split, padding=(px(12), px(12), 0, px(12)))
        split.add(side, weight=0)  # keeps its width: the document gets the rest
        # a flat list: headings only, no tree column and its indent
        self.tree = ttk.Treeview(side, columns=("name", "state"), show="headings",
                                 selectmode="browse")
        self.tree.heading("name", text=tr("Document"), anchor="w")
        self.tree.heading("state", text=tr("State"), anchor="w")
        self.tree.column("name", width=px(210))
        self.tree.column("state", width=px(90), stretch=False)
        self.tree.tag_configure("missing", foreground=RED)
        self.tree.tag_configure("changed", foreground=ORANGE)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.open())
        s = ttk.Scrollbar(side, command=self.tree.yview)
        self.tree["yscrollcommand"] = s.set
        s.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        right = ttk.Frame(split, padding=(px(20), px(14), px(20), px(12)))
        split.add(right, weight=1)
        self.hello = ttk.Frame(right)  # until a document is open
        self.hello.place(relx=.5, rely=.4, anchor="center")
        ttk.Label(self.hello, text=tr("No document open"), font="SunValleySubtitleFont").pack()
        self.tip = ttk.Label(self.hello, text=tr("Add files or a folder to start."), foreground=MUTED)
        self.tip.pack(pady=px(6))
        self.view = ttk.Frame(right)  # the open document
        head = ttk.Frame(self.view)
        head.pack(fill="x")
        self.opener = ttk.Button(head, text=tr("Open file"), command=self.open_file)
        self.opener.pack(side="right")
        self.again = ttk.Button(head, text=tr("Re-index"), command=self.reindex_one)
        self.again.pack(side="right", padx=px(8))
        ttk.Button(head, text=tr("Export…"), command=lambda: self.export(self.cur)).pack(side="right")
        self.name = ttk.Label(head, font="SunValleySubtitleFont")
        self.name.pack(side="left")
        self.note = ttk.Label(self.view, font=CAPTION)  # the path, or what is wrong with it
        self.note.pack(fill="x", pady=(px(2), 0))
        self.view.bind("<Configure>", lambda e: self.note.configure(wraplength=e.width))
        self.legend = ttk.Frame(self.view)
        self.legend.pack(fill="x", pady=(px(12), 0))
        panes = ttk.PanedWindow(self.view, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=px(12))
        self.clear = self._pane(panes, tr("Original"), tr(
            "Click a value to find its token. Select text to mask it."), (0, px(8)))
        self.found = tk.StringVar(value=tr(FIND))
        self.agent = self._pane(panes, tr("What the agent sees"), self.found, (px(8), 0))
        acts = ttk.Frame(self.view)
        acts.pack(fill="x")
        self.deny = ttk.Button(acts, text=tr("Always mask"), command=lambda: self.mask("deny"))
        self.deny.pack(side="left")
        self.allow = ttk.Button(acts, text=tr("Never mask…"), command=lambda: self.mask("allow"))
        self.allow.pack(side="left", padx=px(8))
        ttk.Label(acts, text=tr("the text selected in the original (or right-click it)"),
                  foreground=MUTED).pack(side="left")
        self.clear.bind("<<Selection>>", self._selection)
        self._selection()
        menu = tk.Menu(root, tearoff=False)
        menu.add_command(label=tr("Always mask"), command=lambda: self.mask("deny"))
        menu.add_command(label=tr("Never mask…"), command=lambda: self.mask("allow"))
        self.clear.bind("<Button-2>" if sys.platform == "darwin" else "<Button-3>",
                        lambda e: self.clear.tag_ranges("sel") and menu.tk_popup(e.x_root, e.y_root))

        self._poll()
        self.job(lambda: self.cli("status"), self._started)

    def _pane(self, panes, title: str, hint: str | tk.StringVar, padx: tuple) -> tk.Text:
        px, f = self.px, ttk.Frame(panes, padding=(padx[0], 0, padx[1], 0))
        ttk.Label(f, text=title, font=STRONG).pack(anchor="w")
        ttk.Label(f, font=CAPTION, foreground=MUTED, **{
            "textvariable" if isinstance(hint, tk.StringVar) else "text": hint}).pack(
            anchor="w", pady=(0, px(6)))
        t = tk.Text(f, wrap="word", width=40, height=1, state="disabled", font=BODY,
                    padx=px(16), pady=px(12), spacing2=px(3), spacing3=px(3), relief="flat",
                    borderwidth=0, background=PAPER, foreground=INK, highlightthickness=1,
                    highlightbackground=LINE, highlightcolor=ACCENT, selectbackground=ACCENT,
                    selectforeground=PAPER, inactiveselectbackground=ACCENT)
        s = ttk.Scrollbar(f, command=t.yview)
        t["yscrollcommand"] = s.set
        s.pack(side="right", fill="y", padx=(px(2), 0))
        t.pack(fill="both", expand=True)
        for ty, c in COLORS.items():
            for tag in (ty, ty + "-chip"):  # a value, its token tag: the same tint, one pill
                t.tag_configure(tag, background=tint(c, .25))
                t.tag_bind(tag, "<Enter>", lambda _: t.configure(cursor="hand2"))
                t.tag_bind(tag, "<Leave>", lambda _: t.configure(cursor="xterm"))
        t.tag_configure("chip", font=CAPTION, foreground=MUTED)
        t.tag_configure("hint", foreground=MUTED)
        t.tag_configure("head", foreground=MUTED, font=CAPTION, spacing1=px(10), spacing3=px(4))
        t.tag_configure("hit", font=STRONG, underline=True, background="#ffd54f")  # find
        t.tag_raise("sel")  # a selection shows over the tints
        panes.add(f, weight=1)
        return t

    def _selection(self, _=None) -> None:
        """The mask buttons work while text is selected in the original."""
        on = "!disabled" if self.clear.tag_ranges("sel") else "disabled"
        self.deny.state([on])
        self.allow.state([on])

    def selected(self) -> str:
        """The text selected in the original, its token tags left out: they are not in the file."""
        t = self.clear
        t.tag_configure("chip", elide=True)  # get -displaychars skips elided text
        try:
            return t.tk.call(t, "get", "-displaychars", "sel.first", "sel.last").strip()
        except tk.TclError:  # nothing selected
            return ""
        finally:
            t.tag_configure("chip", elide=False)

    # CLI calls run on one thread; what they hand back runs on the UI thread (tkinter's own)

    def job(self, work, then) -> None:
        if not self.busy:
            self.spin.pack(side="right")
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
                "mcp-secret", tr("Download the models now? About 2 GB, once: the only network use.")):
            self.status.set(tr("Downloading models…"))
            self.job(lambda: self.cli("models", each=self._step),
                     lambda _: self.status.set(tr("Models ready")))
        self.refresh()

    def _step(self, ev: dict) -> None:
        if ev["event"] == "step":
            self.status.set(tr("Downloading models {step}/2…", step=ev["step"]))

    def refresh(self) -> None:
        self.job(lambda: self.cli("list"), self._listed)

    def _listed(self, evs: list[dict]) -> None:
        self.docs = {ev["doc_id"]: ev for ev in evs if ev["event"] == "doc"}
        self.tree.delete(*self.tree.get_children())
        for d in self.docs.values():
            state = "missing" if not d["exists"] else "changed" if d["stale"] else ""
            self.tree.insert("", "end", iid=d["doc_id"], tags=(state,), values=(
                Path(d["path"]).name, tr(state) if state else
                tr("{n} chunk" if d["chunks"] == 1 else "{n} chunks", n=d["chunks"])))
        n = len(self.docs)
        self.status.set(tr("{n} document" if n == 1 else "{n} documents", n=n))
        if self.docs:
            self.tip["text"] = tr("Pick a document on the left.")
        if self.cur in self.docs:
            self.tree.selection_set(self.cur)  # opens it again

    def open(self) -> None:
        if not (sel := self.tree.selection()):
            return
        self.cur = doc_id = sel[0]
        d = self.docs[doc_id]
        self.hello.place_forget()
        self.view.pack(fill="both", expand=True)
        self.name["text"] = Path(d["path"]).name
        self.note.configure(**(
            {"text": tr("File missing: only what the agent sees is left."), "foreground": RED}
            if not d["exists"] else
            {"text": tr("Changed since indexed: the agent still sees the old text. Re-index it."),
             "foreground": ORANGE} if d["stale"] else {"text": d["path"], "foreground": MUTED}))
        self.again["style"] = "Accent.TButton" if d["stale"] else "TButton"
        self.opener.state(["!disabled" if d["exists"] else "disabled"])
        self.found.set(tr(FIND))
        self._fill(self.clear, tr(
            "Reading the original… the first one also loads the models, up to a minute."))
        self._fill(self.agent, tr("Loading…") if d["chunks"] else
                   tr("Nothing published: the file has no text."))
        if d["chunks"]:
            self.job(lambda: self.cli("show", doc_id), lambda evs: self._agent(doc_id, evs))
        self.job(lambda: self.review(doc_id), lambda rev: self._original(doc_id, rev))

    def _fill(self, t: tk.Text, hint: str = "") -> None:
        """Empty a pane, with a gray `hint` in it; the original takes its legend along."""
        for w in self.legend.winfo_children() if t is self.clear else ():
            w.destroy()
        t["state"] = "normal"
        t.delete("1.0", "end")
        t.insert("end", hint, "hint")
        t["state"] = "disabled"

    def _original(self, doc_id: str, rev: dict) -> None:
        if doc_id != self.cur:
            return
        t = self.clear
        if rev["event"] == "error":  # stderr is only shown here, never written anywhere
            return self._fill(t, tr("Can't read the original: {error}, {message}.",
                                    error=rev["error"], message=rev["message"])
                              + "\n\n" + "".join(self.log))
        if not rev["exists"]:
            return self._fill(t, tr("File missing: only what the agent sees is left."))
        self._fill(t)
        t["state"] = "normal"
        text, at = rev["text"], 0
        for s in rev["spans"]:  # inserted piecewise: no offset arithmetic
            t.insert("end", text[at:s["start"]])
            # the value tinted by type, then its token in small type ("chip": never selected)
            t.insert("end", text[s["start"]:s["end"]], (s["type"], s["token"]),
                     " " + s["token"], ("chip", s["type"] + "-chip", s["token"]))
            t.tag_bind(s["token"], "<Button-1>", lambda _, k=s["token"]: self._find(k))
            at = s["end"]
        t.insert("end", text[at:])
        t["state"] = "disabled"
        types = {s["type"] for s in rev["spans"]}
        for ty in (ty for ty in COLORS if ty in types):  # tinted as in the text
            tk.Label(self.legend, text=ty, font=CAPTION, foreground=INK, padx=self.px(8),
                     pady=self.px(2), background=tint(COLORS[ty], .25)).pack(
                side="left", padx=(0, self.px(6)))

    def _agent(self, doc_id: str, evs: list[dict]) -> None:
        if doc_id != self.cur:
            return
        t = self.agent
        self._fill(t)
        t["state"] = "normal"
        for c in (ev for ev in evs if ev["event"] == "chunk"):
            t.insert("end", tr("Chunk {n}", n=c["chunk_id"]) + "\n", "head")
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
        n = len(r) // 2
        self.found.set(tr("{token}: {n} occurrence" if n == 1 else "{token}: {n} occurrences",
                           token=token, n=n))

    # actions

    def add_files(self) -> None:
        self.ingest(*filedialog.askopenfilenames(title=tr("Add files")))

    def add_folder(self) -> None:
        if folder := filedialog.askdirectory(title=tr("Add a folder")):
            self.ingest(folder)

    def reindex(self) -> None:
        self.ingest(*(d["path"] for d in self.docs.values() if d["exists"]), force=True)

    def reindex_one(self) -> None:
        if self.cur in self.docs:
            self.ingest(self.docs[self.cur]["path"], force=True)

    def open_file(self) -> None:
        """The original in its own app: the PDF as a PDF, not as parsed text."""
        if self.cur in self.docs and self.docs[self.cur]["exists"]:
            launch(self.docs[self.cur]["path"])

    def export(self, *doc_ids: str) -> None:
        """One document straight in a folder, as its original format with tokens for values
        (`export --doc`); none: every document and the guide, for Claude Cowork."""
        if not (folder := filedialog.askdirectory(title=tr("Export to"))):
            return
        docs = [a for d in doc_ids for a in ("--doc", d)]

        def done(evs: list[dict]) -> None:
            if ev := next((e for e in evs if e["event"] == "export"), None):
                self.status.set(tr("Exported {n} document" if ev["docs"] == 1 else
                                   "Exported {n} documents", n=ev["docs"]))
                launch(folder)

        self.status.set(tr("Exporting…"))
        self.job(lambda: self.cli("export", *docs, "--", folder), done)

    def ingest(self, *paths: str, force: bool = False) -> None:
        if not paths:
            return
        n = [0]

        def each(ev: dict) -> None:
            if ev["event"] == "doc" and ev["status"] != "removed":
                n[0] += 1
                self.status.set(tr("Indexing {n}…", n=n[0]))

        def done(evs: list[dict]) -> None:
            bad = [f"{Path(e['path']).name}: {e['error']}" for e in evs if e.get("status") == "error"]
            if bad:
                messagebox.showwarning(tr("Not indexed"), "\n".join(bad))
            self.refresh()

        self.status.set(tr("Indexing…"))
        self.job(lambda: self.cli("ingest", *paths, *(["--force"] if force else []), each=each), done)

    def mask(self, cmd: str) -> None:
        """deny (always mask) or allow (never mask) the selected text, then index this document
        again; other documents keep an allowed value masked until Re-index all."""
        value = self.selected()
        if not value or self.cur not in self.docs:
            return self.status.set(tr("Select text in the original first"))
        if cmd == "allow" and not messagebox.askyesno(
                tr("Publish this value in clear?"), tr(
                    "“{value}” becomes visible to the agent in this document. Other documents "
                    "keep it masked until Re-index all.", value=value)):
            return
        self.job(lambda: self.cli(cmd, "add", "--", value), lambda _: None)
        self.ingest(self.docs[self.cur]["path"], force=cmd == "allow")

    def desktop(self) -> None:
        self.job(lambda: self.cli("desktop-config", "--install"),
                 lambda evs: evs[-1:] and evs[-1]["event"] == "desktop" and messagebox.showinfo(
                     "mcp-secret", tr("Added to Claude Desktop: restart Claude.")))


def main() -> None:
    if os.name == "nt":  # sharp text on scaled displays instead of a blurry bitmap: App scales
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

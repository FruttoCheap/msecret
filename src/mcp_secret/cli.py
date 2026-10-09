"""mcp-secret: index documents pseudonymized, serve them to an AI agent over MCP."""

import argparse
import os
import site
import socket
import sys
from pathlib import Path

SANDBOX = "/usr/bin/sandbox-exec"


def home() -> Path:
    """Data root: private/ (ingest only) and public/ (what serve may read)."""
    root = os.environ.get("MCP_SECRET_HOME", "~/Library/Application Support/mcp-secret")
    return Path(root).expanduser().resolve()


def _q(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(root: Path | None = None) -> str:
    """sandbox-exec profile: no network. With `root` (serve): no reads of $HOME or of the
    data root except public/ and the Python runtime, no writes there at all."""
    sbpl = "(version 1)(allow default)(deny network*)"
    if root is None:
        return sbpl
    pub, me = root / "public", Path.home().resolve()
    # not sys.path: it may hold the cwd or PYTHONPATH, i.e. anything under $HOME
    runtime = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, *site.getsitepackages(),
                                             Path(__file__).parents[1])}
    return sbpl + "".join([
        f"(deny file-read* file-write* (subpath {_q(me)}) (subpath {_q(root)}))",
        # SQLite lstat()s every path component when opening public.db
        f"(allow file-read-metadata {' '.join(f'(literal {_q(a)})' for a in pub.parents)})",
        f"(allow file-read* {' '.join(f'(subpath {_q(p)})' for p in [pub, *sorted(runtime)])})",
    ])


def sandbox_cmd(prof: str, *args: str) -> list[str]:
    return [SANDBOX, "-p", prof, sys.executable, "-I", *args]


def _denied(op) -> bool:
    try:
        op()
    except PermissionError:
        return True
    except OSError:
        pass
    return False


def _connect_out() -> None:
    with socket.socket() as s:
        s.connect(("127.0.0.1", 9))


def _sandboxed(args: argparse.Namespace, prof: str, *must_fail) -> None:
    """Re-exec under sandbox-exec, then prove it. Fails closed: no sandbox, no run."""
    if not args.sandboxed:  # exec raises if sandbox-exec is missing; a bad profile exits non-zero
        os.execv(SANDBOX, sandbox_cmd(prof, "-m", "mcp_secret.cli", "--sandboxed", *sys.argv[1:]))
    if not all(_denied(op) for op in (_connect_out, *must_fail)):
        sys.exit("mcp-secret: sandbox not active, refusing to run")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcp-secret", description=__doc__)
    ap.add_argument("--sandboxed", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="download model weights for offline use (needs network)")
    sub.add_parser("ingest", help="index files and folders").add_argument("paths", nargs="+", type=Path)
    sub.add_parser("serve", help="MCP server on stdio, sandboxed, read-only")
    sub.add_parser("show", help="print what the agent sees of a document").add_argument("doc_id")
    args = ap.parse_args()
    root = home()
    db = root / "public" / "public.db"
    if args.cmd in ("show", "serve") and not db.exists():
        sys.exit("no index yet: run `mcp-secret ingest <path>` first")

    if args.cmd == "models":
        from docling.utils.model_downloader import download_models

        from mcp_secret import pii

        download_models(with_code_formula=False, with_picture_classifier=False, with_rapidocr=False)
        pii.fetch_models()
    elif args.cmd == "show":
        from mcp_secret.server import connect

        rows = connect(db).execute("SELECT rowid, text FROM chunks WHERE doc_id = ? ORDER BY seq",
                                   (args.doc_id,)).fetchall()
        for rowid, text in rows:
            print(f"--- chunk {rowid} ---\n{text}\n")
        if not rows:
            sys.exit(f"no document {args.doc_id}")
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
        priv = root / "private"
        if not args.sandboxed:
            os.umask(0o077)  # private/ 0700, its files 0600
            priv.mkdir(parents=True, exist_ok=True)
            priv.chmod(0o700)
            if args.cmd == "serve":
                os.chdir("/")  # a cwd under $HOME is unreadable in the sandbox (rich calls getcwd)
        if args.cmd == "ingest":
            _sandboxed(args, profile())
            from mcp_secret import ingest

            sys.exit(ingest.run(root, args.paths))
        _sandboxed(args, profile(root), lambda: os.listdir(priv))
        from mcp_secret import server

        server.run(db)


if __name__ == "__main__":
    main()

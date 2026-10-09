"""mcp-secret: index documents pseudonymized, serve them to an AI agent over MCP."""

import argparse
import os
import socket
import sys
from pathlib import Path

SANDBOX = "/usr/bin/sandbox-exec"


def home() -> Path:
    """Data root: private/ (ingest only) and public/ (what serve may read)."""
    root = os.environ.get("MCP_SECRET_HOME", "~/Library/Application Support/mcp-secret")
    return Path(root).expanduser().resolve()


def profile() -> str:
    """sandbox-exec profile: no network."""
    return "(version 1)(allow default)(deny network*)"


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
    args = ap.parse_args()
    root = home()

    if args.cmd == "models":
        from docling.utils.model_downloader import download_models

        from mcp_secret import pii

        download_models(with_code_formula=False, with_picture_classifier=False, with_rapidocr=False)
        pii.fetch_models()
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
        priv = root / "private"
        if not args.sandboxed:
            os.umask(0o077)  # private/ 0700, its files 0600
            priv.mkdir(parents=True, exist_ok=True)
            priv.chmod(0o700)
        _sandboxed(args, profile())
        from mcp_secret import ingest

        sys.exit(ingest.run(root, args.paths))


if __name__ == "__main__":
    main()

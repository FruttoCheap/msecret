"""The Windows box, proven from inside: what serve gets is what its profile names, nothing else.
Windows only; run it on a Windows machine: `uv run pytest tests/test_winbox.py -s`."""

import json
import socket
import sqlite3
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="AppContainer is Windows")

# Runs inside the box. Each key is True when the operation was refused; anything else says
# what happened instead, so a failure shows its cause.
PROBE = r"""
import json, os, pathlib, socket, sqlite3, sys
root, loose, port, udp = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
from mcp_secret import winbox

def refused(op):
    try:
        op()
    except (PermissionError, TimeoutError):  # loopback to a listener is dropped, not refused
        return True
    except OSError as e:
        return repr(e)
    return "allowed"

def opens(path):
    try:
        open(path, "r+b").close()
    except OSError as e:
        return repr(e)
    return True

def imports():  # the stack ingest loads: the first operation the box refuses shows here
    os.devnull = winbox.devnull()  # as cli._sandboxed does
    try:
        import docling.document_converter, gliner  # noqa
    except Exception as e:
        return repr(e)
    return True

def connect(host, port):
    def op():
        with socket.socket() as s:
            s.settimeout(3)
            s.connect((host, port))
    return op

def write(path):
    return lambda: open(os.path.join(path, "x"), "w").close()

pub = os.path.join(root, "public")
try:  # the test checks that nothing arrives: a datagram does not fail at the sender
    socket.socket(type=socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", udp))
except OSError:
    pass
print(json.dumps({
    "network": refused(connect("127.0.0.1", port)),  # a listener of the test, not a closed port
    "external": refused(connect("1.1.1.1", 53)),
    "contained": winbox.contained(),
    "devnull": opens(winbox.devnull()),
    "imports": imports(),
    "private": refused(lambda: os.listdir(os.path.join(root, "private"))),
    "loose_file": refused(lambda: open(os.path.join(loose, "doc.txt")).read()),
    "write_public": refused(write(pub)),
    "write_root": refused(write(root)),
    "reads_public": sqlite3.connect(pathlib.Path(pub, "public.db").as_uri() + "?mode=ro",
                                    uri=True).execute("select 1").fetchone() == (1,),
}))
"""


def test_serve_box(tmp_path):
    root, loose = tmp_path / "root", tmp_path / "loose"
    for d in (root / "private", root / "public", loose):
        d.mkdir(parents=True)
    (root / "private" / "secret.txt").write_text("x")
    (loose / "doc.txt").write_text("x")
    sqlite3.connect(root / "public" / "public.db").execute("create table t(x)").connection.close()

    # the launcher is a process of its own, as in the CLI: it execs nothing, it waits
    launcher = (
        "import sys, json; from pathlib import Path; from mcp_secret import winbox\n"
        "root = Path(sys.argv[1])\n"
        "argv = [sys.executable, '-I', '-B', '-c', sys.argv[5], *sys.argv[1:5]]\n"
        "sys.exit(winbox.run(argv, 'test', root / '.winbox-test.json',\n"
        "                    read=[root / 'public', *winbox.runtime()]))\n")
    with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(1)
        r = subprocess.run([sys.executable, "-c", launcher, str(root), str(loose),
                            str(tcp.getsockname()[1]), str(udp.getsockname()[1]), PROBE],
                           capture_output=True, text=True, check=False)
        with pytest.raises(TimeoutError):
            udp.recvfrom(16)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"network": True, "external": True, "contained": True,
                                    "devnull": True, "imports": True, "private": True, "loose_file": True, "write_public": True,
                                    "write_root": True, "reads_public": True}

"""Windows sandbox: the child runs in an AppContainer with no capabilities. No network (loopback
included) and no file whose ACL does not name the profile's SID: the data root, $HOME and the
documents stay closed unless `run` was told to open them. One profile per command, so what
`ingest` may write is never what `serve` may read.

Spike: tests/test_winbox.py is the check, run on Windows."""

import ctypes
import json
import os
import site
import subprocess
import sys
from ctypes import wintypes as w
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
ue = ctypes.WinDLL("userenv", use_last_error=True)

ALREADY_EXISTS = 0x800700B7
SECURITY_CAPABILITIES_ATTR, HANDLE_LIST_ATTR = 0x20009, 0x20002
EXTENDED_STARTUPINFO_PRESENT, CREATE_UNICODE_ENVIRONMENT = 0x80000, 0x400


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", w.LPVOID), ("Capabilities", w.LPVOID),
                ("CapabilityCount", w.DWORD), ("Reserved", w.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD),
                ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD), ("wShowWindow", w.WORD),
                ("cbReserved2", w.WORD), ("lpReserved2", w.LPVOID), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", w.LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                ("dwThreadId", w.DWORD)]


def _sig(fn, restype, *argtypes):  # without argtypes ctypes passes handles and pointers as 32-bit
    fn.restype, fn.argtypes = restype, list(argtypes)


PSID = w.LPVOID
_sig(ue.CreateAppContainerProfile, ctypes.c_long, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPVOID,
     w.DWORD, ctypes.POINTER(PSID))
_sig(ue.DeriveAppContainerSidFromAppContainerName, ctypes.c_long, w.LPCWSTR, ctypes.POINTER(PSID))
_sig(ue.GetAppContainerFolderPath, ctypes.c_long, w.LPCWSTR, ctypes.POINTER(w.LPWSTR))
_sig(adv.ConvertSidToStringSidW, w.BOOL, PSID, ctypes.POINTER(w.LPWSTR))
_sig(k32.GetStdHandle, w.HANDLE, w.DWORD)
_sig(k32.SetHandleInformation, w.BOOL, w.HANDLE, w.DWORD, w.DWORD)
_sig(k32.InitializeProcThreadAttributeList, w.BOOL, w.LPVOID, w.DWORD, w.DWORD,
     ctypes.POINTER(ctypes.c_size_t))
_sig(k32.UpdateProcThreadAttribute, w.BOOL, w.LPVOID, w.DWORD, ctypes.c_size_t, w.LPVOID,
     ctypes.c_size_t, w.LPVOID, w.LPVOID)
_sig(k32.CreateProcessW, w.BOOL, w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
     w.LPVOID, w.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
_sig(k32.WaitForSingleObject, w.DWORD, w.HANDLE, w.DWORD)
_sig(k32.GetExitCodeProcess, w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD))
_sig(k32.GetCurrentProcess, w.HANDLE)
_sig(adv.OpenProcessToken, w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
_sig(adv.GetTokenInformation, w.BOOL, w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD))


def contained() -> bool:
    """True in an AppContainer token that holds no capability: the proof `serve` runs. Loopback
    is not refused there, its connections to a listener are dropped (a timeout), so unlike on
    macOS there is no socket call that fails fast: the token is what the kernel enforces."""
    tok, n, flag = w.HANDLE(), w.DWORD(), w.DWORD()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 8, ctypes.byref(tok)):  # TOKEN_QUERY
        return False
    if not adv.GetTokenInformation(tok, 29, ctypes.byref(flag), 4, ctypes.byref(n)):  # IsAppContainer
        return False
    caps = ctypes.create_string_buffer(4096)
    if not adv.GetTokenInformation(tok, 30, caps, ctypes.sizeof(caps), ctypes.byref(n)):  # Capabilities
        return False
    return flag.value == 1 and int.from_bytes(caps.raw[:4], "little") == 0  # TOKEN_GROUPS.GroupCount


def devnull() -> str:
    """The box cannot open NUL, and dill (torch) and subprocess do at import: an empty file of the
    box's own TEMP stands in. The caller sets os.devnull to it before the heavy imports."""
    p = Path(os.environ["TEMP"]) / "devnull"
    p.touch()
    return str(p)


def runtime() -> set[str]:
    """What a Python needs to start and import mcp_secret. Not sys.path: it may hold the cwd."""
    return {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, *site.getsitepackages(),
                                          Path(__file__).parents[1])}


def _profile(name: str) -> tuple[int, str, Path]:
    """The profile's SID, the SID as a string, and the folder the box may write (its TEMP)."""
    sid = PSID()
    hr = ue.CreateAppContainerProfile(name, name, name, None, 0, ctypes.byref(sid))
    if hr & 0xFFFFFFFF == ALREADY_EXISTS:
        hr = ue.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if hr:
        raise OSError(f"AppContainer profile {name}: HRESULT {hr & 0xFFFFFFFF:#x}")
    text = w.LPWSTR()
    adv.ConvertSidToStringSidW(sid, ctypes.byref(text))
    folder = w.LPWSTR()
    if ue.GetAppContainerFolderPath(text.value, ctypes.byref(folder)):
        raise OSError("AppContainer folder")
    tmp = Path(folder.value) / "AC" / "Temp"
    tmp.mkdir(parents=True, exist_ok=True)
    return sid.value, text.value, tmp


def _grant(sid: str, paths, rights: str, state: Path) -> int:
    """icacls: `rights` (RX, M) for the profile on each path, inherited by what is inside a
    folder. `state` remembers what was done, so a big runtime is walked once. Returns the paths
    it could not open: the box then fails on them, closed. ponytail: an ACE stays after the
    run; the folder's mtime is part of the key, so an install walks again, but a file moved into
    a subfolder keeps its own ACL: delete `state` after a manual ACL reset or such a move."""
    done = set(json.loads(state.read_text())) if state.exists() else set()
    failed = 0
    for p in map(Path, paths):
        if not p.exists():
            continue
        key = f"{rights}|{p}|{p.stat().st_mtime_ns}"  # a folder that changed is walked again: uv sync
        if key in done:  # hard-links packages, and a hard link keeps the ACL of its cache file
            continue
        flags = "(OI)(CI)" if p.is_dir() else ""
        r = subprocess.run(["icacls", str(p), "/grant", f"*{sid}:{flags}{rights}", "/Q"],
                           capture_output=True, check=False)
        if r.returncode:
            failed += 1
        else:
            done.add(key)
    state.write_text(json.dumps(sorted(done)))
    return failed


def run(argv: list[str], name: str, state: Path, read=(), write=()) -> int:
    """Run `argv` in the AppContainer `mcp-secret.<name>` and wait: the exit code. The child
    inherits stdin, stdout and stderr (an MCP client talks to it directly) and nothing else.
    ponytail: no job object, so a killed launcher leaves the child until its stdin closes."""
    sid, text, tmp = _profile(f"mcp-secret.{name}")
    bad = _grant(text, read, "RX", state) + _grant(text, write, "M", state)
    if bad:
        print(f"mcp-secret: {bad} paths without access for the sandbox", file=sys.stderr)

    std = [k32.GetStdHandle(n) for n in (-10, -11, -12)]  # stdin, stdout, stderr
    for h in filter(None, std):
        k32.SetHandleInformation(h, 1, 1)  # HANDLE_FLAG_INHERIT: the handle list needs it
    handles = list(dict.fromkeys(filter(None, std)))
    hl = (w.HANDLE * len(handles))(*handles)
    caps = SECURITY_CAPABILITIES(sid, None, 0, 0)

    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))  # sizing call: fails
    attrs = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    for attr, val, n in ((SECURITY_CAPABILITIES_ATTR, caps, ctypes.sizeof(caps)),
                         (HANDLE_LIST_ATTR, hl, ctypes.sizeof(hl))):
        if not k32.UpdateProcThreadAttribute(attrs, 0, attr, ctypes.byref(val), n, None, None):
            raise ctypes.WinError(ctypes.get_last_error())

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(si)
    si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = std
    si.lpAttributeList = ctypes.addressof(attrs)

    env = {**os.environ, "TEMP": str(tmp), "TMP": str(tmp)}  # the box cannot write the user's
    block = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper()))
    raw = (block + "\0").encode("utf-16-le")
    env_buf = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
    cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    pi = PROCESS_INFORMATION()
    # cwd is the box's own folder: the user's may be closed to it
    if not k32.CreateProcessW(argv[0], cmd, None, None, True,
                              EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                              env_buf, str(tmp), ctypes.byref(si), ctypes.byref(pi)):
        raise ctypes.WinError(ctypes.get_last_error())
    k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
    code = w.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    return code.value

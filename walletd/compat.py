"""The few places where the operating system shows through.

walletd was written against Linux: a Unix-domain socket, `flock`, `/proc`, directory `fsync`. None of
those exist on native Windows, where Hermes also runs, and there the plugin used to fail at import
(`No module named 'fcntl'`) before a single tool was registered. Everything platform-specific lives
here so the rest of the tree can stay boring:

- **Transport.** A Unix socket where the platform has one. Elsewhere the daemon listens on a
  loopback TCP port it picks itself and writes `tcp://127.0.0.1:<port>` into the file where the
  socket would have been — so "does `<home>/walletd.sock` exist" keeps meaning the same thing, and
  every caller keeps passing one path around. The capability tokens authenticate either transport;
  the peer-uid check is a Unix-socket extra that has no loopback equivalent.
- **The daemon lock.** `flock` or `msvcrt.locking`, one non-blocking exclusive lock per wallet home.
- **Processes.** Reading another process's argv and asking whether it is alive, without `/proc`.
  (`os.kill(pid, 0)` is not a probe on Windows — it terminates the process.)
- **Durability and privacy.** Directory `fsync` and `chmod 0600` are best-effort where the platform
  has no such thing; on Windows the wallet home inherits the user profile's ACL instead.

Standard library only: the plugin half imports this module too, and it must not pull daemon code.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
HAS_UNIX_SOCKETS = hasattr(socket, "AF_UNIX") and not IS_WINDOWS

#: what the endpoint file holds on a platform without Unix sockets
TCP_PREFIX = "tcp://"
LOOPBACK = "127.0.0.1"


# ---------- durability and privacy ----------

def fsync_dir(path: Path) -> None:
    """Flush a directory entry to disk where the platform can; a no-op where it cannot."""
    if IS_WINDOWS:
        return                      # no directory handles to fsync; NTFS journals the rename
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def make_private(path: Path, mode: int = 0o600) -> None:
    """`chmod` that never raises: on Windows the mode bits mean almost nothing, and the home
    directory's ACL (the user's own profile) is what keeps the file private."""
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


def enforces_modes() -> bool:
    """True where `chmod 0600` is a real statement about who can read a file."""
    return not IS_WINDOWS


def replace(src: Path, dst: Path) -> None:
    """`os.replace`, patient on Windows.

    There a rename fails with PermissionError while another process holds the destination open — an
    indexer, a scanner, the pane reading the file. It is a matter of milliseconds, so wait them out
    rather than lose the write.
    """
    import time

    for attempt in range(6):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 5:
                raise
            time.sleep(0.03 * (attempt + 1))


def write_json_atomic(path: Path, payload: object) -> None:
    """temp file + fsync + rename, private. A reader sees the old file or the new one, never half.

    Truncating a state file in place meant a crash mid-write left it empty or cut off, and every
    loader reads an unparseable file as "nothing here": pending requests, the idempotency keys that
    stop a retry paying twice, standing autopays and the deposit accounts all vanished that way.
    """
    import json
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        replace(Path(tmp), path)
        make_private(path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private_text(path: Path, text: str) -> None:
    """temp file + fsync + rename + 0600, for a small secret that is not JSON (the device key)."""
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace(Path(tmp), path)
        make_private(path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- the daemon lock ----------

def try_lock(fd: int) -> bool:
    """Take an exclusive, non-blocking lock on an open file. False when someone else holds it."""
    if IS_WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd: int) -> None:
    if IS_WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


# ---------- transport ----------

def parse_endpoint(text: str) -> Optional[Tuple[str, int]]:
    """`tcp://127.0.0.1:1234` -> ("127.0.0.1", 1234). Anything that is not loopback is refused: the
    endpoint file is a file, and a file can be edited to point the client somewhere else."""
    text = (text or "").strip()
    if not text.startswith(TCP_PREFIX):
        return None
    host, _, port = text[len(TCP_PREFIX):].rpartition(":")
    if host != LOOPBACK or not port.isdigit():
        return None
    number = int(port)
    return (host, number) if 0 < number < 65536 else None


def read_endpoint(path: Path) -> Optional[Tuple[str, int]]:
    try:
        if not Path(path).is_file():
            return None
        return parse_endpoint(Path(path).read_text(encoding="ascii", errors="replace")[:64])
    except OSError:
        return None


def write_endpoint(path: Path, port: int) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{TCP_PREFIX}{LOOPBACK}:{int(port)}\n", encoding="ascii")
    make_private(tmp)
    os.replace(tmp, path)


def connect(path: Path, timeout: float) -> socket.socket:
    """Open a connection to the daemon that serves `path`.

    Raises FileNotFoundError / ConnectionRefusedError when nothing is listening, the same two
    exceptions a Unix socket raises, so callers need one `except` for both transports.
    """
    path = Path(path)
    if HAS_UNIX_SOCKETS:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(timeout)
            conn.connect(str(path))
        except BaseException:
            conn.close()
            raise
        return conn
    if not path.exists():
        raise FileNotFoundError(str(path))
    endpoint = read_endpoint(path)
    if endpoint is None:
        raise ConnectionRefusedError(f"{path} does not name a loopback endpoint")
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout)
        conn.connect(endpoint)
    except BaseException:
        conn.close()
        raise
    return conn


# ---------- processes ----------

def _split_windows_cmdline(line: str) -> List[str]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        return line.split()
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def process_cmdline(pid: int) -> Optional[List[str]]:
    """The argv of a running process, or None when it is gone or cannot be read."""
    pid = int(pid)
    if pid <= 0:
        return None
    if not IS_WINDOWS and Path("/proc/self/cmdline").exists():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        return [part.decode("utf-8", "replace") for part in raw if part]
    try:
        import psutil  # type: ignore

        try:
            return list(psutil.Process(pid).cmdline())
        except Exception:  # noqa: BLE001 - gone, or not ours to read
            return None
    except ImportError:
        pass
    try:
        if IS_WINDOWS:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line = (done.stdout or "").strip()
            return _split_windows_cmdline(line) if line else None
        done = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        line = (done.stdout or "").strip()
        if done.returncode != 0 or not line:
            return None
        import shlex

        return shlex.split(line)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    pid = int(pid)
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate(pid: int) -> None:
    """Ask a process to stop (SIGTERM; on Windows that is TerminateProcess)."""
    import signal

    os.kill(int(pid), signal.SIGTERM)


def is_walletd_serving(argv: Optional[List[str]], home: Path) -> bool:
    """Is this argv `python -m walletd --home <home> serve`? Compared as paths, so a different
    spelling of the same directory (case on Windows, a trailing slash) still matches."""
    if not argv or len(argv) < 5 or argv[1:4] != ["-m", "walletd", "--home"] or "serve" not in argv[4:]:
        return False
    try:
        return os.path.normcase(os.path.abspath(argv[4])) == os.path.normcase(os.path.abspath(str(home)))
    except (OSError, ValueError):
        return False


def detach_kwargs() -> dict:
    """Popen arguments that let a spawned daemon outlive the process that started it."""
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def platform_tag() -> str:
    """`linux-x86_64`, `win32-amd64`, `darwin-arm64` — what a prebuilt binary has to match."""
    import platform

    return f"{sys.platform}-{platform.machine().lower()}"

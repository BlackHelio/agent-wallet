"""Supervise the walletd process: start it if it is not listening, never if it is locked down.

The plugin spawns the daemon as an argv list (no shell). If the daemon dies, tools fail with a
clear message instead of silently signing somewhere else.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .client import socket_path, wait_ready, wallet_home

_PROC: Optional[subprocess.Popen] = None


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_running(home: Optional[Path] = None) -> bool:
    return socket_path(home).exists() and wait_ready(home, seconds=1.0)


def _service_managed(home: Optional[Path] = None) -> bool:
    """True when a unit exists *for this wallet home*, so systemd owns the process, not us."""
    try:
        from . import service

        return service._user_systemd() and service.managed_by_service(home)
    except Exception:  # noqa: BLE001 - a broken service probe must not block a manual start
        return False


def _held_by_another(home: Path) -> bool:
    """True when a live process holds this wallet home's daemon lock."""
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(home / "walletd.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _unit_name(home: Path) -> str:
    from . import service

    return service.unit_name(home)


def start(home: Optional[Path] = None) -> dict:
    global _PROC
    home = home or wallet_home()
    if is_running(home):
        return {"ok": True, "already_running": True, "socket": str(socket_path(home))}
    if _held_by_another(home):
        # The lock is the truth: a daemon may be mid-boot, or wedged. Never spawn a second one —
        # its token rotation would invalidate the live daemon's tokens.
        ready = wait_ready(home, seconds=8.0)
        return {"ok": ready, "already_running": True, "manager": "flock",
                "socket": str(socket_path(home)),
                "message": "" if ready else "another walletd holds this wallet home but is not answering"}
    if _service_managed(home):
        # One owner only: ask systemd, never spawn a second daemon that would fight it for the socket.
        unit = _unit_name(home)
        subprocess.run(["systemctl", "--user", "reset-failed", unit],
                       capture_output=True, text=True, timeout=15)
        subprocess.run(["systemctl", "--user", "start", unit],
                       capture_output=True, text=True, timeout=20)
        ready = wait_ready(home, seconds=8.0)
        return {"ok": ready, "manager": "systemd-user", "unit": unit,
                "socket": str(socket_path(home)), "already_running": False}
    if not (home / "keystore.json").exists():
        return {"ok": False, "error": "no_keystore",
                "message": f"create a wallet first: python -m walletd --home {home} init"}

    root = _plugin_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # No starter lock here: the daemon itself holds an exclusive flock for its lifetime and refuses
    # to steal a live socket, so the parent only has to spawn it.

    log = home / "walletd.log"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log.open("ab") as handle:
        _PROC = subprocess.Popen(  # noqa: S603 — argv list, no shell
            [sys.executable, "-m", "walletd", "--home", str(home), "serve"],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    ready = wait_ready(home, seconds=8.0)
    return {"ok": ready, "pid": _PROC.pid, "socket": str(socket_path(home)),
            "log": str(log), "already_running": False}


def stop(home: Optional[Path] = None) -> dict:
    global _PROC
    if _PROC is not None and _PROC.poll() is None:
        _PROC.terminate()
        try:
            _PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _PROC.kill()
        _PROC = None
        return {"ok": True, "stopped": "supervised_child"}
    return {"ok": False, "stopped": None,
            "message": "no child of this process; stop the daemon where it was started"}

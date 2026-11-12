"""Supervise the walletd process: start it if it is not listening, never if it is locked down.

The plugin spawns the daemon as an argv list (no shell). If the daemon dies, tools fail with a
clear message instead of silently signing somewhere else.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import sys
from pathlib import Path
from typing import Optional

from .client import compat, socket_path, wait_ready, wallet_home

_PROC: Optional[subprocess.Popen] = None
_PROC_HOME: Optional[Path] = None


def _same_home(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (OSError, ValueError):
        return False


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


#: The only environment names a `walletd` child inherits. Everything else the agent's process holds —
#: every provider key, every forge token — used to reach the daemon through `dict(os.environ)`, for a
#: process whose whole job is to import this plugin and answer on a loopback socket.
_CHILD_ENV_NAMES = frozenset({
    "PATH", "HOME", "TMPDIR", "PYTHONPATH",
    # where the wallet is: the daemon resolves its home from these, and dropping them would silently
    # serve a different (default) home than the parent asked about
    "HERMES_HOME", "HERMES_PROFILE", "AGENT_WALLET_HOME",
    "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
})

#: Prefixes that are this wallet's own configuration rather than a third party's secret: node
#: overrides (`AGENT_WALLET_RPC_*`, `AGENT_WALLET_ESPLORA_*`, `AGENT_WALLET_MONERO_*`) and the
#: daemon's own knobs (`WALLETD_LOCK_TIMEOUT`, `WALLETD_DISABLE_CHAINS`).
_CHILD_ENV_PREFIXES = ("AGENT_WALLET_", "WALLETD_")

#: One-shot secrets for `init` only: they must never be inherited by a long-lived `serve` child.
_ONE_SHOT_ENV = frozenset({"WALLETD_PASSPHRASE", "WALLETD_PIN"})


def child_env(extra: Optional[dict] = None) -> dict:
    """An explicit, minimal environment for a `walletd` child.

    Built by allow-list, the way `wallet.service` builds its unit's environment: a child gets the
    wallet's own configuration and the paths it needs, and nothing else. `dict(os.environ)` handed the
    daemon (and anything it later spawns) every key the agent was holding — an exfiltration surface
    with no upside, and the reason a compromised daemon could read provider credentials it never uses.
    """
    env = {name: value for name, value in os.environ.items()
           if (name in _CHILD_ENV_NAMES or name.startswith(_CHILD_ENV_PREFIXES))
           and name not in _ONE_SHOT_ENV}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    if not env.get("HOME"):
        try:
            env["HOME"] = str(Path.home())
        except (RuntimeError, KeyError):   # no HOME and no passwd entry: leave it out
            pass
    root = str(_plugin_root())
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if extra:
        env.update(extra)
    return env


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
        if not compat().try_lock(fd):
            return True
        compat().unlock(fd)
        return False
    finally:
        os.close(fd)


def _unit_name(home: Path) -> str:
    from . import service

    return service.unit_name(home)


def bootstrap(home: Path, passphrase: str, pin: str = "") -> dict:
    """Create the keystore in an empty home, offline.

    `start()` refuses without `keystore.json`, so every first-run command — `demo`, `init` — used to
    dead-end on `no_keystore` even though the bootstrap code sat three lines away. The passphrase
    travels in the child's environment, never argv.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = _plugin_root()
    # The passphrase travels in the child's environment, never argv — and that environment carries
    # nothing but this wallet's own configuration.
    env = child_env({"WALLETD_PASSPHRASE": passphrase})
    if pin:
        env["WALLETD_PIN"] = pin
    proc = subprocess.run(  # noqa: S603 — argv list, no shell
        [sys.executable, "-m", "walletd", "--home", str(home), "init"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        # `init` on a home that already has a keystore is the *desired* state for a bootstrap, not a
        # failure: `demo` and `init` both call this before doing anything else, and a second run used
        # to surface a Python traceback as `bootstrap_failed`.
        if "already_initialized" in detail:
            return {"ok": True, "already_initialized": True}
        return {"ok": False, "error": "bootstrap_failed", "message": detail}
    try:
        return {"ok": True, **json.loads(proc.stdout or "{}")}
    except json.JSONDecodeError:
        return {"ok": True, "output": (proc.stdout or "").strip()[:300]}


def start(home: Optional[Path] = None) -> dict:
    global _PROC, _PROC_HOME
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
                "message": f"no wallet here yet ({home}) — create one: `hermes wallet init` "
                           f"(it asks for a passphrase, then a six-digit PIN)"}

    root = _plugin_root()
    # Explicit minimal env (see `child_env`): the daemon needs this plugin, its home and its own
    # settings — not the agent's provider keys.
    env = child_env()
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
            close_fds=True,
            **compat().detach_kwargs(),
        )
    _PROC_HOME = Path(home)
    ready = wait_ready(home, seconds=8.0)
    return {"ok": ready, "pid": _PROC.pid, "socket": str(socket_path(home)),
            "log": str(log), "already_running": False}


def stop(home: Optional[Path] = None) -> dict:
    global _PROC, _PROC_HOME
    home = Path(home) if home is not None else wallet_home()
    # Only the child this process started *for this home*: one module-level handle used to be stopped
    # for whichever home was asked about, so `down` on profile B could kill profile A's daemon.
    if _PROC is not None and _PROC.poll() is None and _same_home(_PROC_HOME, home):
        _PROC.terminate()
        try:
            _PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _PROC.kill()
        _PROC = None
        _PROC_HOME = None
        if not compat().HAS_UNIX_SOCKETS:
            # a terminated process on Windows runs no cleanup: drop its pointer files for it
            (home / "walletd.pid").unlink(missing_ok=True)
            socket_path(home).unlink(missing_ok=True)
        return {"ok": True, "stopped": "supervised_child"}
    # A CLI invocation is not the process that started the daemon, so the supervised-child check
    # nearly always missed and `down` told the user to go find it themselves. The pidfile is
    # authoritative — but only kill it after confirming it is *our* daemon for *this* home.
    pid_file = home / "walletd.pid"
    pid: Optional[int] = None
    platform = compat()
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        # older daemons never wrote one; find ours by argv rather than telling the user to hunt
        pid = _find_daemon_pid(home)
        if pid is None:
            return {"ok": False, "stopped": None,
                    "message": f"no daemon is running for {home}"}
    args = platform.process_cmdline(pid)
    if args is None:
        if platform.process_alive(pid):
            # alive, but its argv cannot be read (another user's process that inherited a recycled
            # pid): not provably ours, so it is not ours to signal
            return {"ok": False, "stopped": None,
                    "message": f"pid {pid} cannot be identified as this wallet's daemon; refusing to signal it"}
        pid_file.unlink(missing_ok=True)
        return {"ok": False, "stopped": None, "message": "the daemon is not running (stale pidfile)"}
    if not platform.is_walletd_serving(args, home):
        return {"ok": False, "stopped": None,
                "message": f"pid {pid} is not this wallet's daemon; refusing to signal it"}
    try:
        platform.terminate(pid)
    except ProcessLookupError:
        pass                                    # it exited between the check and the signal
    except OSError as exc:
        return {"ok": False, "stopped": None, "message": f"could not stop pid {pid}: {exc}"}
    stopped = False
    for _ in range(50):
        if not platform.process_alive(pid):
            stopped = True
            break
        time.sleep(0.1)
    if not stopped:
        # Saying "stopped" over a daemon that is still holding the keys is the wrong kind of optimism.
        return {"ok": False, "stopped": None, "pid": pid,
                "message": f"pid {pid} did not exit within 5s; it may be mid-payment — try again"}
    pid_file.unlink(missing_ok=True)
    if not platform.HAS_UNIX_SOCKETS:
        # A terminated process on Windows runs no cleanup, so its endpoint file would keep pointing
        # at a port nobody serves.
        socket_path(home).unlink(missing_ok=True)
    return {"ok": True, "stopped": "daemon", "pid": pid}


def _find_daemon_pid(home: Path) -> Optional[int]:
    """The pid of `python -m walletd --home <home> serve`, found by argv. Linux only (/proc): on other
    platforms every daemon this code can meet writes a pidfile."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    platform = compat()
    for name in names:
        if name.isdigit() and platform.is_walletd_serving(platform.process_cmdline(int(name)), home):
            return int(name)
    return None

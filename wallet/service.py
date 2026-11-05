"""Install walletd as a background service so nobody has to think about it.

- Linux with a user session: a `systemd --user` unit with Restart=always.
- macOS: a LaunchAgent plist.
- Anything without a service manager (containers): a clear message and the on-demand
  auto-start the plugin already does.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict

from .client import socket_path, wallet_home

UNIT_NAME = "agent-wallet.service"      # the default profile keeps the historical name


def unit_name(home: Path) -> str:
    """One unit per wallet home. A single shared unit baked in whichever profile installed it, so a
    second profile's start targeted the first profile's wallet and timed out.
    """
    home = Path(home).expanduser().resolve()
    # The historical single-unit name belongs to the profile whose home this is (HERMES_HOME when
    # set, else ~/.hermes); every other profile gets a unit of its own.
    try:
        from ..walletd import home as home_mod
    except ImportError:
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from walletd import home as home_mod
    default = home_mod.resolve_home().resolve()
    if home == default:
        return UNIT_NAME
    digest = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:8]
    return f"agent-wallet-{digest}.service"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _user_systemd() -> bool:
    if not _have("systemctl"):
        return False
    try:
        done = subprocess.run(["systemctl", "--user", "is-system-running"],
                              capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # "degraded" still means a working user manager; only a hard failure counts as absent.
    return done.returncode == 0 or "degraded" in (done.stdout or "").lower()


def _unit_path(home: Path | None = None) -> Path:
    home = home or wallet_home()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "systemd" / "user" / unit_name(home)


def _pick_python() -> str:
    """An interpreter that can import PyNaCl — the service must not depend on which python ran us."""
    candidates = [sys.executable, shutil.which("python3") or "", "/usr/bin/python3", "/usr/local/bin/python3"]
    seen: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen or not Path(candidate).exists():
            continue
        seen.append(candidate)
        try:
            # every import the daemon needs at runtime, not just the keystore's: an interpreter with
            # PyNaCl but no eth-account boots fine and then reports BTC/EVM/XRP as unavailable
            probe = ("from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt, "
                     "crypto_aead_xchacha20poly1305_ietf_decrypt\n"
                     "import eth_account, eth_keys, embit")
            # Probe under the SAME conditions the unit runs in: PYTHONPATH is the plugin root and
            # nothing else. Probing with the CLI's inherited env passes on an interpreter whose cffi
            # comes from the Hermes site dir, then fails the moment systemd starts it.
            unit_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()),
                        "PYTHONPATH": str(_plugin_root())}
            done = subprocess.run([candidate, "-c", probe], capture_output=True, timeout=15, env=unit_env)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return candidate
    return sys.executable


def _unit_text(home: Path | None = None) -> str:
    home = home or wallet_home()
    return f"""[Unit]
Description=agent-wallet signer daemon for {home} (Hermes agent-wallet plugin)
Documentation=https://forgejo.lan/BlackHelio/agent-wallet
# After=default.target with WantedBy=default.target is an ordering cycle; systemd drops the job.
ConditionPathExists={home}/keystore.json

[Service]
Type=simple
Environment=PYTHONPATH={_plugin_root()}
# Optional per-machine secrets/config for the signer (wallet file name, password, RPC). Kept out
# of the unit so `hermes wallet service install` can be re-run without losing them.
EnvironmentFile=-{home}/wallet.env
ExecStart={_pick_python()} -m walletd --home {home} serve
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


def write_env(values: Dict[str, str]) -> Path:
    """Write `<wallet home>/wallet.env` (0600) with the given KEY=VALUE pairs."""
    home = wallet_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "wallet.env"
    lines = [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def install() -> Dict[str, object]:
    if _user_systemd():
        home = wallet_home()
        path = _unit_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_unit_text(home), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
        done = subprocess.run(["systemctl", "--user", "enable", "--now", unit_name(home)],
                              capture_output=True, text=True, timeout=30)
        detail = (done.stderr or done.stdout or "").strip()[:400]
        return {"ok": done.returncode == 0, "manager": "systemd-user", "unit": str(path),
                "detail": detail, "error": "" if done.returncode == 0 else detail,
                "hint": "run `loginctl enable-linger $USER` so the wallet starts before you log in",
                "socket": str(socket_path())}
    return {"ok": False, "manager": None,
            "message": "no user service manager here (container?); the daemon still starts on demand "
                       "from the plugin — `hermes wallet up` forces it"}


def status() -> Dict[str, object]:
    if _user_systemd() and _unit_path().exists():
        done = subprocess.run(["systemctl", "--user", "is-active", unit_name(wallet_home())],
                              capture_output=True, text=True, timeout=10)
        return {"manager": "systemd-user", "active": done.stdout.strip() == "active",
                "unit": str(_unit_path()), "socket": str(socket_path())}
    return {"manager": None, "active": socket_path().exists(), "socket": str(socket_path())}


def uninstall() -> Dict[str, object]:
    if _user_systemd() and _unit_path().exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", unit_name(wallet_home())],
                       capture_output=True, text=True, timeout=30)
        _unit_path().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
        return {"ok": True, "removed": str(_unit_path())}
    return {"ok": False, "message": "no unit installed"}


def managed_by_service(home: Path | None = None) -> bool:
    """True only when a unit exists AND it runs *this* wallet home.

    Checking existence alone meant profile B treated profile A's unit as its own, asked systemd to
    start it, and then waited for a socket that would never appear.
    """
    home = Path(home or wallet_home()).expanduser().resolve()
    unit = _unit_path(home)
    if not unit.exists():
        return False
    try:
        text = unit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return f"--home {home}" in text

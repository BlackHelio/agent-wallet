"""Thin client for walletd's UDS. The plugin half never imports daemon code."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional

AGENT = "agent"
OPERATOR = "operator"


def wallet_home() -> Path:
    """One wallet per Hermes profile. The rule lives in walletd/home.py so every surface agrees."""
    try:
        from ..walletd import home as home_mod      # loaded as hermes_plugins.<id>.wallet.client
    except ImportError:  # standalone
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from walletd import home as home_mod
    return home_mod.resolve_home()


def current_profile() -> str:
    """Which Hermes profile this process is acting for (used by the sandbox ACL)."""
    name = os.environ.get("HERMES_PROFILE") or ""
    if name:
        return name
    parts = wallet_home().parts
    if "profiles" in parts:
        index = parts.index("profiles")
        if index + 1 < len(parts):
            return parts[index + 1]
    return "default"


def socket_path(home: Optional[Path] = None) -> Path:
    return (home or wallet_home()) / "walletd.sock"


def read_token(scope: str, home: Optional[Path] = None) -> Optional[str]:
    path = (home or wallet_home()) / "tokens" / f"{scope}.token"
    try:
        return path.read_text(encoding="ascii").strip() or None
    except FileNotFoundError:
        return None


class WalletUnavailable(RuntimeError):
    pass


def call(method: str, params: Optional[Dict[str, Any]] = None, *, scope: str = AGENT,
         home: Optional[Path] = None, timeout: float = 60.0) -> Dict[str, Any]:
    home = home or wallet_home()
    path = socket_path(home)
    token = read_token(scope, home)
    if token is None:
        raise WalletUnavailable("walletd has not been started (no token); run `hermes wallet up`")
    payload: Dict[str, Any] = {"id": 1, "method": method, "params": params or {}, "token": token}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(path))
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise WalletUnavailable("walletd is not listening; run `hermes wallet up`") from exc
    except socket.timeout as exc:
        raise WalletUnavailable("walletd did not answer in time") from exc
    if not buffer.strip():
        raise WalletUnavailable("walletd closed the connection without answering")
    reply = json.loads(buffer.decode("utf-8"))
    if "error" in reply:
        code = str(reply["error"].get("code", ""))
        if code == "unknown_method":
            # a daemon that predates this caller. Saying "unknown method" sends people hunting for a
            # policy problem; the fix is always to restart the daemon.
            raise WalletUnavailable(
                f"stale_walletd: this daemon is an older build and does not know {method!r}; "
                f"restart it (hermes wallet down && hermes wallet up)")
        raise WalletUnavailable(f"{code}: {reply['error'].get('message')}")
    return reply.get("result") or {}


def wait_ready(home: Optional[Path] = None, seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            call("status", home=home, timeout=2)
            return True
        except WalletUnavailable:
            time.sleep(0.15)
    return False

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

#: monotonic time of the last time a wallet call showed work waiting for the human. The per-turn
#: brief reads this so it can speak up when a payment is pending even if the user's word does not
#: look like money ("can you handle this for me"). Set on any call that mentions an approval.
PENDING_SEEN_AT = 0.0


def _walletd(name: str):
    """One of walletd's dependency-free helper modules (`home`, `compat`) — never daemon code."""
    import importlib

    try:
        # loaded as hermes_plugins.<id>.wallet.client
        return importlib.import_module(f"..walletd.{name}", __package__)
    except (ImportError, ValueError, TypeError):  # standalone: `wallet` is a top-level package
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        return importlib.import_module(f"walletd.{name}")


def compat():
    """The platform layer (transport, locks, processes); see walletd/compat.py."""
    return _walletd("compat")


def wallet_home() -> Path:
    """One wallet per Hermes profile. The rule lives in walletd/home.py so every surface agrees."""
    return _walletd("home").resolve_home()


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
        with compat().connect(path, timeout) as conn:
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
    except OSError as exc:
        # a reset, a broken pipe, a socket path that is not a socket: all of them used to escape as a
        # raw traceback out of whichever tool happened to be calling
        raise WalletUnavailable(f"walletd is not listening ({type(exc).__name__}); "
                                f"run `hermes wallet up`") from exc
    if not buffer.strip():
        raise WalletUnavailable("walletd closed the connection without answering")
    try:
        reply = json.loads(buffer.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WalletUnavailable("walletd sent an answer that is not JSON (cut off mid-reply?)") from exc
    if not isinstance(reply, dict):
        raise WalletUnavailable("walletd sent an answer that is not an object")
    if "error" in reply:
        code = str(reply["error"].get("code", ""))
        if code == "unknown_method":
            # a daemon that predates this caller. Saying "unknown method" sends people hunting for a
            # policy problem; the fix is always to restart the daemon.
            raise WalletUnavailable(
                f"stale_walletd: this daemon is an older build and does not know {method!r}; "
                f"restart it (hermes wallet down && hermes wallet up)")
        raise WalletUnavailable(f"{code}: {reply['error'].get('message')}")
    result = reply.get("result") or {}
    _note_pending(result)
    return result


def _note_pending(result: Any) -> None:
    """Remember that a human is needed, cheaply. No probe, no extra RPC."""
    global PENDING_SEEN_AT
    try:
        if not isinstance(result, dict):
            return
        pending = result.get("pending_requests") or result.get("approval_id")
        decision = result.get("decision") or {}
        if pending or (isinstance(decision, dict) and decision.get("code") == "needs_pin"):
            import time as _time
            PENDING_SEEN_AT = _time.monotonic()
    except Exception:                                        # noqa: BLE001 - never break a call
        return


def wait_ready(home: Optional[Path] = None, seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            call("status", home=home, timeout=2)
            return True
        except WalletUnavailable:
            time.sleep(0.15)
    return False

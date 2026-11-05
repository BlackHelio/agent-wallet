"""Unix-domain socket JSON-RPC with scope enforcement and peer-uid checking.

Line-delimited JSON in, one JSON object out. The socket is 0600 inside a 0700 directory, and a
connection from another uid is closed before it can say anything.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .state import State, WalletError, build_methods
from .tokens import AGENT, OPERATOR, TokenStore

MAX_LINE = 1 << 20


def _peer_uid(conn: socket.socket) -> Optional[int]:
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (AttributeError, OSError):
        return None


class _Handler(socketserver.StreamRequestHandler):
    server: "WalletdServer"

    def handle(self) -> None:
        uid = _peer_uid(self.request)
        allowed = self.server.allow_uids
        if allowed is not None and uid not in allowed:
            self._write({"id": None, "error": {"code": "peer_uid_denied", "message": "unexpected peer uid"}})
            return
        for raw in self.rfile:
            if len(raw) > MAX_LINE:
                self._write({"id": None, "error": {"code": "too_large", "message": "request too large"}})
                return
            line = raw.strip()
            if not line:
                continue
            self._write(self.server.dispatch(line))

    def _write(self, payload: Dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n")
        self.wfile.flush()


class WalletdServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    # The default backlog of 5 makes a burst of concurrent tool calls fail with EAGAIN before the
    # server ever sees them; the agent can easily fire more than five at once.
    request_queue_size = 128

    def __init__(self, socket_path: str, state: State, tokens: TokenStore, allow_uids: Optional[set[int]] = None):
        self.state = state
        self.tokens = tokens
        self.methods = build_methods()
        self.allow_uids = {os.getuid()} if allow_uids is None else allow_uids
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            # Refuse to steal a socket that a live daemon is answering on — two signers on one
            # wallet is how budgets get charged twice and wallets open with the wrong password.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(path))
                probe.close()
                raise SystemExit(f"another walletd is already serving {path}; refusing to start a second one")
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
                pass
            finally:
                try:
                    probe.close()
                except OSError:
                    pass
            path.unlink()
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)

    def dispatch(self, line: bytes) -> Dict[str, Any]:
        request_id = None
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"id": None, "error": {"code": "bad_json", "message": "request is not JSON"}}
        if not isinstance(message, dict):
            return {"id": None, "error": {"code": "bad_request", "message": "request must be an object"}}
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return {"id": request_id, "error": {"code": "bad_params", "message": "params must be an object"}}

        entry = self.methods.get(method)
        if entry is None:
            return {"id": request_id, "error": {"code": "unknown_method", "message": f"no such method {method!r}"}}
        required_scope = entry[0]
        handler = entry[1]

        presented_scope = self.tokens.scope_for(message.get("token"))
        if presented_scope is None:
            return {"id": request_id, "error": {"code": "unauthorized", "message": "missing or invalid token"}}
        if required_scope == OPERATOR and presented_scope != OPERATOR:
            return {"id": request_id, "error": {"code": "forbidden",
                                                "message": f"{method} requires the operator scope"}}

        # handlers that need to know how they were called (e.g. an operator-only demo affordance)
        # read this instead of trusting a caller-supplied field
        params = {**params, "_scope": presented_scope}
        # One global mutex keeps the wallet consistent, but a slow chain call inside it used to make
        # *every* method wait - the desktop app saw 30s timeouts on /status, gave up on the plugin and
        # dropped its surfaces. Queue, but refuse to queue for ever.
        lock_timeout = float(os.environ.get("WALLETD_LOCK_TIMEOUT", "8"))
        acquired = self.state.mutex.acquire(timeout=lock_timeout)
        if not acquired:
            return {"id": request_id,
                    "error": {"code": "busy",
                              "message": f"walletd is busy (a slow chain call holds the lock); "
                                         f"retry {method} in a moment"}}
        try:
            result = handler(self.state, params)
        except WalletError as exc:
            return {"id": request_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # never leak a traceback or a path over the socket
            return {"id": request_id, "error": {"code": "internal_error", "message": type(exc).__name__}}
        finally:
            self.state.mutex.release()
        return {"id": request_id, "result": result}


class ServerThread:
    """Start a walletd socket in a background thread (tests, embedded use)."""

    def __init__(self, socket_path: str, state: State, tokens: TokenStore):
        self.server = WalletdServer(socket_path, state, tokens)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "ServerThread":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def call(socket_path: str, method: str, params: Optional[Dict[str, Any]] = None,
         token: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
    """One request/response. Raises RuntimeError carrying the server's error code."""
    payload = {"id": 1, "method": method, "params": params or {}}
    if token is not None:
        payload["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(socket_path)
        conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
    if not buffer.strip():
        raise RuntimeError("empty_response")
    reply = json.loads(buffer.decode("utf-8"))
    if "error" in reply:
        raise WalletError(reply["error"]["code"], reply["error"]["message"])
    return reply["result"]

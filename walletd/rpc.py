"""Local JSON-RPC with scope enforcement and peer-uid checking.

Line-delimited JSON in, one JSON object out. On a platform with Unix sockets the socket is 0600
inside a 0700 directory, and a connection from another uid is closed before it can say anything.
Where there are none (native Windows) the daemon listens on a loopback TCP port of its own choosing
and writes the endpoint into the same path (see walletd/compat.py); the capability tokens are the
authentication on both, and a loopback connection that does not speak the protocol is dropped at its
first malformed line.
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

from . import compat
from .state import State, WalletError, build_methods
from .chains.base import ChainError
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
        while True:
            # readline with a bound: iterating the file would buffer an endless line whole before the
            # size check ever ran, so one client could make the daemon hold as much memory as it sent
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except OSError:                      # the peer went away mid-line
                return
            if not raw:
                return
            if len(raw) > MAX_LINE:
                self._write({"id": None, "error": {"code": "too_large", "message": "request too large"}})
                return
            line = raw.strip()
            if not line:
                continue
            reply = self.server.dispatch(line)
            self._write(reply)
            if self.server.loopback and (reply.get("error") or {}).get("code") in _NOT_OUR_PROTOCOL:
                # On loopback TCP anything on the machine can connect, a browser tab included. A peer
                # whose first words are not this protocol (an HTTP request line, say) gets one error
                # and no second line to try with.
                return

    def _write(self, payload: Dict[str, Any]) -> None:
        try:
            self.wfile.write(json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n")
            self.wfile.flush()
        except OSError:                          # the peer hung up before the answer; nothing to tell
            pass


#: replies that mean "this peer is not a wallet client"
_NOT_OUR_PROTOCOL = {"bad_json", "bad_request"}

_ServerBase = (socketserver.ThreadingUnixStreamServer if compat.HAS_UNIX_SOCKETS
               else socketserver.ThreadingTCPServer)


class WalletdServer(_ServerBase):
    daemon_threads = True
    # A Unix socket path is unlinked before binding, so "reuse" is harmless there. On Windows
    # SO_REUSEADDR lets a second process bind a port that is already being served, so it is off.
    allow_reuse_address = compat.HAS_UNIX_SOCKETS
    # The default backlog of 5 makes a burst of concurrent tool calls fail with EAGAIN before the
    # server ever sees them; the agent can easily fire more than five at once.
    request_queue_size = 128

    def __init__(self, socket_path: str, state: State, tokens: TokenStore, allow_uids: Optional[set[int]] = None):
        self.state = state
        self.tokens = tokens
        self.methods = build_methods()
        self.loopback = not compat.HAS_UNIX_SOCKETS
        if allow_uids is None:
            # no peer credentials on a TCP connection: the tokens are the whole authentication there
            allow_uids = {os.getuid()} if compat.HAS_UNIX_SOCKETS else None
        self.allow_uids = allow_uids
        path = Path(socket_path)
        self.endpoint_path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            # Refuse to steal a socket that a live daemon is answering on — two signers on one
            # wallet is how budgets get charged twice and wallets open with the wrong password.
            answered = False
            try:
                compat.connect(path, 1.0).close()
                answered = True
            except OSError:                      # refused, missing, timed out: nobody is there
                pass
            if answered:
                raise SystemExit(f"another walletd is already serving {path}; refusing to start a second one")
            path.unlink()
        if compat.HAS_UNIX_SOCKETS:
            super().__init__(str(path), _Handler)
            compat.make_private(path)
        else:
            super().__init__((compat.LOOPBACK, 0), _Handler)
            compat.write_endpoint(path, self.server_address[1])

    def server_bind(self) -> None:
        if self.loopback and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self) -> None:
        super().server_close()
        if self.loopback:
            # the endpoint file is only a pointer; leaving it behind would point at a dead (or, later,
            # somebody else's) port
            try:
                if compat.read_endpoint(self.endpoint_path) == (compat.LOOPBACK, self.server_address[1]):
                    self.endpoint_path.unlink()
            except OSError:
                pass

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
        except ChainError as exc:
            # An adapter refusal is an ANSWER, not a malfunction: `no_swap_route`, `gas_floor` and
            # `unknown_operator` are the codes a caller acts on, and folding them into
            # `internal_error` told the client nothing. ChainError is deliberately not a WalletError
            # (the chain layer must not import the state layer), so the dispatch knows about both.
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
    with compat.connect(Path(socket_path), timeout) as conn:
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

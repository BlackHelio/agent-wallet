"""`python -m walletd <cmd>` — init, serve, status, verify, requests, mandates."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import threading
import time
import json
import os
import sys
from pathlib import Path

from . import keystore
from .ledger import verify_file
from .rpc import WalletdServer
from . import home as home_mod
from .state import State
from .tokens import TokenStore


def default_home() -> Path:
    """Where this wallet lives. One rule, shared by every component (see walletd/home.py)."""
    return home_mod.resolve_home()


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home).expanduser() if args.home else default_home()


def _passphrase(prompt: str, from_env: str = "WALLETD_PASSPHRASE") -> str:
    value = os.environ.get(from_env)
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit("no passphrase: set WALLETD_PASSPHRASE or run on a TTY")
    return getpass.getpass(prompt)


def cmd_init(args: argparse.Namespace) -> int:
    state = State(_home(args))
    passphrase = _passphrase("New keystore passphrase: ")
    if args.passphrase_stdin:
        passphrase = sys.stdin.readline().rstrip("\n")
    result = state.initialize(passphrase)
    print(json.dumps({"home": str(state.home), **result}, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    home = _home(args)
    state = State(home)
    if not state.exists():
        print(json.dumps({"error": "not_initialized", "home": str(home)}), file=sys.stderr)
        return 2
    runtime = Path(args.socket).expanduser() if args.socket else home / "walletd.sock"

    # One daemon per wallet home, enforced by a lock held for the life of the process. This must
    # happen BEFORE tokens are rotated: a second starter that rotated first would invalidate the
    # live daemon's tokens and every call would come back "unauthorized" while it kept serving.
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(home / "walletd.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"error": "already_running", "home": str(home),
                          "message": "another walletd holds this wallet home"}), file=sys.stderr)
        return 3

    tokens = TokenStore(home / "tokens")
    tokens.rotate_all()
    server = WalletdServer(str(runtime), state, tokens)
    def _autolock() -> None:
        """Lock on the clock, not on traffic.

        Auto-lock used to happen only when a call happened to check the TTL, so an idle daemon kept
        the keys loaded (the XMR spend key inside the signer) for as long as nobody asked it
        anything. This thread enforces the window and, through lock(), stops the signer too.
        """
        while True:
            time.sleep(5)
            try:
                with state.mutex:
                    state.is_unlocked()
            except Exception:  # noqa: BLE001 - a lock failure must not kill the daemon
                pass

    threading.Thread(target=_autolock, name="walletd-autolock", daemon=True).start()

    def _reaper() -> None:
        """One-time sandboxes must die on the clock even if nobody calls the daemon.

        It only acts while unlocked: a sweep needs the signer, and destroying a sandbox that still
        holds money would strand it.
        """
        while True:
            time.sleep(20)
            try:
                with state.mutex:
                    state.reap_sandboxes()
            except Exception:  # noqa: BLE001 - a reap failure must not kill the daemon
                pass

    threading.Thread(target=_reaper, name="walletd-sandbox-reaper", daemon=True).start()
    print(json.dumps({"listening": str(runtime), "human_gate": state.settings.human_gate,
                      "autolock_minutes": state.settings.autolock_minutes}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = State(_home(args))
    print(json.dumps(state.status(), indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    home = _home(args)
    ledger_path = home / "ledger.jsonl"
    pub_path = ledger_path.with_suffix(ledger_path.suffix + ".pub")
    if not pub_path.exists():
        print(json.dumps({"ok": False, "message": "no ledger public key"}), file=sys.stderr)
        return 2
    pubkey = pub_path.read_text(encoding="ascii").strip()
    ok, message, count = verify_file(ledger_path, pubkey)
    print(json.dumps({"ok": ok, "message": message, "entries": count, "pubkey": pubkey}, indent=2))
    return 0 if ok else 1


def cmd_tail(args: argparse.Namespace) -> int:
    home = _home(args)
    path = home / "ledger.jsonl"
    if not path.exists():
        print("[]")
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in lines[-args.limit:]:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="walletd", description="agent-wallet signing daemon")
    parser.add_argument("--home", help="state directory (default: ~/.local/state/agent-wallet)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create an encrypted keystore")
    p_init.add_argument("--passphrase-stdin", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="serve the JSON-RPC socket")
    p_serve.add_argument("--socket", help="socket path (default: <home>/walletd.sock)")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="print state without unlocking").set_defaults(func=cmd_status)
    sub.add_parser("verify", help="verify the receipt ledger offline").set_defaults(func=cmd_verify)

    p_tail = sub.add_parser("tail", help="print the last receipt entries")
    p_tail.add_argument("-n", "--limit", type=int, default=20)
    p_tail.set_defaults(func=cmd_tail)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

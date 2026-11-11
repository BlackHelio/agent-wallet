"""`python -m walletd <cmd>` — init, serve, status, verify, requests, mandates."""

from __future__ import annotations

import argparse
import atexit
import getpass
import threading
import time
import json
from typing import Any, Dict
import os
import signal
import sys
from pathlib import Path

from . import compat, keystore
from .ledger import verify_file
from .rpc import WalletdServer
from . import home as home_mod
from .state import State, WalletError
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
    # --passphrase-stdin is checked first: asking the environment/TTY before looking at the flag made
    # the flag useless in exactly the place it exists for (a pipe, no TTY, no variable).
    if args.passphrase_stdin:
        passphrase = sys.stdin.readline().rstrip("\r\n")
        if not passphrase:
            raise SystemExit("no passphrase on stdin")
    else:
        passphrase = _passphrase("New keystore passphrase: ")
    # The six-digit secret typed before every payment. `WALLETD_PIN` for scripts (the demo uses it);
    # a human at a TTY is asked twice. Without one the wallet can hold funds but cannot approve.
    pin = os.environ.get("WALLETD_PIN") or ""
    if not pin and sys.stdin.isatty() and not args.passphrase_stdin:
        first = getpass.getpass("New PIN for payments (six digits): ")
        if getpass.getpass("Again: ") != first:
            print(json.dumps({"error": "pin_mismatch"}), file=sys.stderr)
            return 2
        pin = first
    try:
        result = state.initialize(passphrase, pin=pin or None)
    except WalletError as exc:
        # `init` is the first command anyone types, and it is the one most likely to be typed twice.
        # A traceback here reads as "the wallet is broken"; it is not, it is already there.
        if exc.code == "already_initialized":
            # stdout, because `wallet/daemon.bootstrap()` parses this to tell "created" from
            # "already there"; a caller reading only stdout must still see the truth
            print(json.dumps({"ok": True, "already_initialized": True, "home": str(state.home)}))
            return 0
        print(json.dumps({"error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    print(json.dumps({"home": str(state.home), "pin_set": bool(pin), **result}, indent=2))
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
    if not compat.try_lock(lock_fd):
        os.close(lock_fd)
        print(json.dumps({"error": "already_running", "home": str(home),
                          "message": "another walletd holds this wallet home"}), file=sys.stderr)
        return 3

    # One authority for "which process serves this home": the daemon writes its own pid, so `down`
    # works from any shell, systemd or not. Removed on the way out (and by `down` itself).
    pid_file = home / "walletd.pid"
    pid_file.write_text(str(os.getpid()))
    compat.make_private(pid_file)

    def _drop_pid(*_: object) -> None:
        try:
            pid_file.unlink()
        except OSError:
            pass

    atexit.register(_drop_pid)
    for _sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None),
                 getattr(signal, "SIGBREAK", None)):
        if _sig is None:                 # SIGHUP is POSIX-only, SIGBREAK Windows-only
            continue
        try:
            signal.signal(_sig, lambda *a: (_drop_pid(), sys.exit(0)))
        except (ValueError, OSError):
            pass

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
        ticks = 0
        while True:
            time.sleep(20)
            ticks += 1
            try:
                with state.mutex:
                    state.reap_sandboxes()
            except Exception:  # noqa: BLE001 - a reap failure must not kill the daemon
                pass
            # Deposit addresses merge on a slower clock: a merge costs a fee, and incoming Monero
            # unlocks ten blocks after it is mined, so checking every 20 s would pay a fee the moment
            # money became spendable and never give the operator a chance to see it arrive.
            if ticks % 15 == 0:
                try:
                    with state.mutex:
                        if state.is_unlocked():
                            state.deposits_auto_merge()
                            # paid order accounts are the same shape: their money belongs in the
                            # main balance, not in a per-order account nobody reads
                            state.orders_merge()
                except Exception:  # noqa: BLE001
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
        _drop_pid()
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
    # The chain alone cannot notice a truncated tail or a deleted file — that is what the anchor is
    # for, and this command used to ignore it, so `walletd verify` said "ok" over a wiped ledger.
    anchor: Dict[str, Any] = {"anchored": False}
    anchor_path = home / "ledger.anchor"
    if anchor_path.exists():
        try:
            saved = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved, ok = {}, False
            message = "anchor is unreadable"
        last: Dict[str, Any] = {}
        try:
            lines = [line for line in ledger_path.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        except OSError:
            # The ledger is gone but its anchor is not: the exact case the anchor exists for. Reading
            # the missing file used to end this command in a traceback instead of the verdict.
            lines = []
        if lines:
            try:
                last = json.loads(lines[-1])
            except json.JSONDecodeError:
                last = {}
        anchor = {"anchored": True, "seq": saved.get("seq"), "sig": saved.get("sig")}
        if count < int(saved.get("seq") or 0) or last.get("sig") != saved.get("sig"):
            ok = False
            message = f"ledger has {count} entries but {saved.get('seq')} were recorded"
    elif count > 0:
        ok = False
        message = "anchor file is missing — receipts may have been truncated"
    print(json.dumps({"ok": ok, "message": message, "entries": count, "pubkey": pubkey,
                      "anchor": anchor}, indent=2))
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

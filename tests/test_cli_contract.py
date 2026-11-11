"""The `hermes wallet` CLI contract: what --help promises, what the tables say, how it fails.

Each test here pins a promise an operator acts on: a command advertised in --help works, `status`
and `balances` show ETH and USDC on Base, a six-digit secret typed at a passphrase prompt reaches
the daemon, and a refusal is one line, never a traceback. No daemon and no network: the daemon is
stubbed at `client.call`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from unittest import mock

from wallet import cli, client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wallet")
    cli.setup(parser)
    return parser


BASE = "0x0c04f9534b74587275D1d28eE20A56A88E9423c5"

ACCOUNTS = [
    {"kind": "monero-mainnet", "asset": "XMR", "balance": "0", "available": False,
     "state": "signer", "error": "the signer is not answering — `hermes wallet signer start`"},
    {"kind": "bitcoin-mainnet", "asset": "BTC", "balance": "0.00012", "available": True,
     "address": "bc1qwq0y0q2gddlufk4urmrwygzm0843dhkuukxy97"},
    {"kind": "base-mainnet", "asset": "ETH", "balance": "0.0042", "available": True,
     "address": BASE, "provider": "https://mainnet.base.org"},
    {"kind": "base-mainnet", "asset": "USDC", "balance": "12.5", "available": True,
     "address": BASE, "provider": "https://mainnet.base.org"},
    {"kind": "polygon-mainnet", "asset": "USDC", "balance": "0", "available": True, "address": BASE},
]

STATUS = {
    "locked": False, "home": "/tmp/w", "assets": ["XMR", "BTC", "ETH", "USDC"],
    "chains": ["monero-mainnet", "bitcoin-mainnet", "base-mainnet", "polygon-mainnet"],
    "chain_assets": {"monero-mainnet": ["XMR"], "bitcoin-mainnet": ["BTC"],
                     "base-mainnet": ["ETH", "USDC"], "polygon-mainnet": ["USDC"]},
    "caps": {"per_tx": {"ETH": "0.05", "USDC": "25"}}, "human_gate": True,
    "pending_requests": 0, "active_mandates": 0, "ledger_archives": 0,
}


def fake_daemon(method_calls=None):
    """Answer the handful of RPCs the CLI makes, and record every call."""
    def call(method, params=None, **kwargs):
        if method_calls is not None:
            method_calls.append((method, params or {}))
        if method == "status":
            return dict(STATUS)
        if method == "balances":
            return {"accounts": [dict(row) for row in ACCOUNTS]}
        if method == "unlock":
            return {"ok": True}
        return {"ok": True}
    return call


class _NoDaemonStart:
    """Keep every test off the real wallet: nothing here may spawn a daemon or read /opt/data."""

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.calls = []
        self.stack.enter_context(mock.patch.object(client, "call", fake_daemon(self.calls)))
        self.stack.enter_context(mock.patch.object(cli, "_ensure_daemon", lambda: None))
        self.stack.enter_context(mock.patch.object(cli, "_warn_home_override", lambda: None))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def _is_command_row(line: str) -> bool:
    """True for a command row in `--help`: four spaces, a single-space name, then the help text."""
    if not line.startswith("    ") or line.startswith("     ") or not line.strip():
        return False
    body = line[4:]
    if " " not in body:
        return False
    name = body.split(" ", 1)[0]
    return not name.startswith(("{", "-"))


class HelpHonestyTests(unittest.TestCase):
    def test_incoming_and_orders_are_not_advertised(self) -> None:
        text = build_parser().format_help()
        self.assertNotIn("incoming,orders", text)            # the usage choice list
        self.assertNotIn("==SUPPRESS==", text)               # argparse's own leak
        rows = [line.split()[0] for line in text.splitlines() if _is_command_row(line)]
        self.assertNotIn("incoming", rows)
        self.assertNotIn("orders", rows)
        self.assertNotIn("incoming (orders)", text)

    def test_a_hidden_command_still_parses(self) -> None:
        # older scripts call it; hiding it from help must not break them
        parser = build_parser()
        for name in ("incoming", "orders"):
            args = parser.parse_args([name])
            self.assertIs(args.func.__wrapped__, cli.cmd_incoming)

    def test_every_advertised_command_parses(self) -> None:
        parser = build_parser()
        text = parser.format_help()
        # a command row is "    <name>, <help>" (never two spaces after the name; wrapped help and
        # the `{a,b,c}` metavar line are indented further and are not rows)
        rows = [line.split()[0] for line in text.splitlines() if _is_command_row(line)]
        self.assertIn("status", rows)
        self.assertGreater(len(rows), 40)
        for name in rows:
            # its own --help must resolve: an advertised command that argparse cannot build is dead
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    parser.parse_args([name, "--help"])
            self.assertEqual(exited.exception.code, 0, name)


class StatusAndBalancesTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with _NoDaemonStart():
            with contextlib.redirect_stdout(out):
                code = cli.run(build_parser().parse_args(argv))
        return code, out.getvalue()

    def test_status_prints_a_per_chain_table_with_base(self) -> None:
        code, out = self.run_cli(["status"])
        self.assertEqual(code, 0)
        base_rows = [line for line in out.splitlines() if line.strip().startswith("Base")]
        self.assertEqual(len(base_rows), 2, out)
        self.assertTrue(any("ETH" in line for line in base_rows), out)
        self.assertTrue(any("USDC" in line for line in base_rows), out)
        self.assertIn("chain", out)            # a table, not a dump
        # one line per coin per chain: no address sneaking onto a second line
        self.assertNotIn("\n       ", out)

    def test_balances_shows_base_eth_and_usdc(self) -> None:
        code, out = self.run_cli(["balances"])
        self.assertEqual(code, 0)
        self.assertIn("Base", out)
        self.assertRegex(out, r"Base\s+◆ ETH")
        self.assertRegex(out, r"Base\s+\$ USDC")
        self.assertIn("0.0042 ETH", out)
        self.assertIn("12.5 USDC", out)

    def test_columns_line_up_despite_the_wide_emblems(self) -> None:
        _, out = self.run_cli(["balances"])
        lines = [line for line in out.splitlines() if line.strip()]
        header = next(line for line in lines if "balance" in line)
        column = header.index("balance")
        header_width = cli._cell_width(header[:column])
        for line in lines:
            if "balance" in line or not line.startswith("  "):
                continue
            # every row's amount ends where the header's does, counting the emblems as two columns
            self.assertEqual(cli._cell_width(line[:column]), header_width, repr(line))

    def test_locked_balance_is_not_drawn_as_zero(self) -> None:
        locked = [{"kind": "base-mainnet", "asset": "ETH", "balance": "0", "available": False,
                   "state": "locked", "error": "locked", "chain_assets": ["ETH", "USDC"]}]
        text = cli._balance_table(locked)
        self.assertIn("locked", text)
        self.assertNotIn("0 ETH", text)

    def test_json_is_still_json_from_either_side_of_the_command(self) -> None:
        for argv in (["status", "--json"], ["--json", "status"]):
            code, out = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertIn("status", json.loads(out), argv)

    def test_json_on_stdout_for_balances(self) -> None:
        code, out = self.run_cli(["--json", "balances"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["accounts"]), len(ACCOUNTS))


class UnlockSecretTests(unittest.TestCase):
    def test_six_digits_at_the_passphrase_prompt_reach_the_daemon(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["unlock", "--passphrase-stdin"])
        calls = []
        with mock.patch.object(client, "call", fake_daemon(calls)), \
                mock.patch.object(cli, "_ensure_daemon", lambda: None), \
                mock.patch.object(cli.sys, "stdin", io.StringIO("123456\n")), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.run(args)
        unlock = [params for method, params in calls if method == "unlock"]
        # the value the daemon's own six-digit fallback expects; not rejected here as "too short"
        self.assertEqual(unlock, [{"passphrase": "123456"}])

    def test_pin_stdin_is_not_length_checked_client_side(self) -> None:
        args = build_parser().parse_args(["unlock", "--pin-stdin"])
        calls = []
        with mock.patch.object(client, "call", fake_daemon(calls)), \
                mock.patch.object(cli, "_ensure_daemon", lambda: None), \
                mock.patch.object(cli.sys, "stdin", io.StringIO("123456\n")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run(args), 0)
        self.assertEqual([p for m, p in calls if m == "unlock"], [{"passphrase": "123456"}])

    def test_the_passphrase_prompt_names_the_pin(self) -> None:
        args = build_parser().parse_args(["unlock", "--passphrase-stdin"])
        seen = {}

        def fake_getpass(prompt):
            seen["prompt"] = prompt
            return "a-long-passphrase"

        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(cli.getpass, "getpass", fake_getpass):
            args.passphrase_stdin = False
            self.assertEqual(cli._passphrase(args), "a-long-passphrase")
        self.assertIn("PIN", seen["prompt"])


class NeverATracebackTests(unittest.TestCase):
    def test_a_refusal_is_one_line_and_nonzero(self) -> None:
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli._fail("bad_passphrase: passphrase does not match the keystore")
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue().strip().splitlines(),
                         ["error: bad_passphrase: passphrase does not match the keystore"])
        self.assertEqual(out.getvalue(), "")

    def test_an_unexpected_exception_is_one_line_and_nonzero(self) -> None:
        def boom(_args):
            return {}["missing"]

        parser = build_parser()
        args = parser.parse_args(["status"])
        args.func = cli._bound(boom)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.run(args)
        self.assertEqual(code, 2)
        text = err.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertTrue(text.startswith("error: internal_error: KeyError"), text)

    def test_daemon_refusal_is_one_line(self) -> None:
        def refuse(_args):
            raise client.WalletUnavailable("locked: wallet is locked; an operator must unlock it")

        args = build_parser().parse_args(["incoming"])
        args.func = cli._bound(refuse)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.run(args)
        self.assertEqual(code, 2)
        self.assertIn("error: locked: wallet is locked", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_broken_pipe_is_not_an_error(self) -> None:
        def boom(_args):
            raise BrokenPipeError(32, "Broken pipe")

        args = build_parser().parse_args(["status"])
        args.func = cli._bound(boom)
        with mock.patch.object(cli.os, "dup2", lambda *a: None), \
                mock.patch.object(cli.os, "open", lambda *a: 1):
            self.assertEqual(cli.run(args), 0)


if __name__ == "__main__":
    unittest.main()

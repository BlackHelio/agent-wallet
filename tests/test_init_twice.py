"""`init` twice is the most likely thing a new user does, and it must not look like a crash.

It used to: `state.initialize()` raised `WalletError("already_initialized")` straight out of `main()`,
so the second run printed a Python traceback and exited 1 — and `wallet/daemon.bootstrap()`, which
calls `init` before `demo` does anything, surfaced the same traceback as `bootstrap_failed`. Both are
covered here, against the real entry point rather than a mock.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = "init-twice-passphrase"
PIN = "123456"


def run_init(home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, WALLETD_PASSPHRASE=PASS, WALLETD_PIN=PIN, PYTHONPATH=str(ROOT))
    env.pop("AGENT_WALLET_HOME", None)
    return subprocess.run([sys.executable, "-m", "walletd", "--home", str(home), "init"],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)


class InitTwiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "wallet"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_first_init_creates_the_keystore(self) -> None:
        first = run_init(self.home)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.home / "keystore.json").exists())
        self.assertTrue(json.loads(first.stdout)["ok"])

    def test_the_second_init_says_so_instead_of_raising(self) -> None:
        self.assertEqual(run_init(self.home).returncode, 0)
        second = run_init(self.home)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("Traceback", second.stderr + second.stdout)
        self.assertIn("already_initialized", second.stderr + second.stdout)

    def test_bootstrap_treats_an_existing_wallet_as_success(self) -> None:
        # `wallet demo` and `wallet init` both bootstrap first; on an existing home that must be a
        # no-op, not `bootstrap_failed`
        sys.path.insert(0, str(ROOT))
        from wallet import daemon

        self.assertEqual(run_init(self.home).returncode, 0)
        result = daemon.bootstrap(self.home, PASS, PIN)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("already_initialized"), result)


if __name__ == "__main__":
    unittest.main()

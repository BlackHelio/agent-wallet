"""One wallet home, resolved the same way by every surface.

Five components used to work out the path themselves and three of them disagreed, which is how a
wallet gets created in a directory the daemon never reads. These tests pin the order and the
agreeing behaviour, so a future refactor cannot re-introduce a private copy of the rule.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wallet import client
from walletd import home as home_mod


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("AGENT_WALLET_HOME", "HERMES_HOME", "XDG_STATE_HOME", "HOME")}
    env.update(extra)
    if "HOME" in extra:
        for name in ("USERPROFILE", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"):
            env.pop(name, None)
        # Windows resolves the user's home from USERPROFILE and the default Hermes home from
        # LOCALAPPDATA; a test that moves HOME means all three
        env.setdefault("USERPROFILE", extra["HOME"])
        env.setdefault("LOCALAPPDATA", str(Path(extra["HOME"]) / "AppData" / "Local"))
    return env


class HomeResolverTests(unittest.TestCase):
    def test_an_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(AGENT_WALLET_HOME=tmp, HERMES_HOME="/should/not/matter")
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(home_mod.resolve_home(), Path(tmp))
                self.assertEqual(client.wallet_home(), Path(tmp))

    def test_hermes_home_is_the_second_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(HERMES_HOME=tmp)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(home_mod.resolve_home(), Path(tmp) / "wallet")
                self.assertEqual(client.wallet_home(), Path(tmp) / "wallet")

    def test_without_hermes_it_falls_back_to_xdg_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(HOME=tmp, XDG_STATE_HOME=str(Path(tmp) / "xdg"))
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(home_mod.resolve_home(), Path(tmp) / "xdg" / "agent-wallet")

    def test_a_second_wallet_elsewhere_is_reported(self) -> None:
        """Creating a wallet while another one exists on the same machine is how a user funds
        the wrong address; the resolver must be able to say so."""
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "xdg"
            stray = xdg / "agent-wallet"
            stray.mkdir(parents=True)
            (stray / "keystore.json").write_text("{}", encoding="utf-8")
            served = Path(tmp) / "served"
            env = _clean_env(HOME=str(Path(tmp) / "home"), XDG_STATE_HOME=str(xdg))
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.dict(os.environ, {"HERMES_HOME": str(served)}, clear=False):
                    self.assertEqual(home_mod.stray_wallets(), [stray])

    def test_no_second_wallet_when_only_one_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(HOME=tmp, HERMES_HOME=str(Path(tmp) / "h"))
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(home_mod.stray_wallets(), [])


if __name__ == "__main__":
    unittest.main()

"""What a child process inherits: the wallet's own configuration, and nothing else.

A daemon (and the vendor signer it supervises) is a process the agent's runtime starts, so anything
it inherits is a credential the agent was holding. These tests pin the allow-list, the one-shot init
secrets, and the signer's option file — the login must not be an argument.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wallet import daemon
from walletd import signer

PROVIDER_KEY = "OPENROUTER_API_KEY"


class _FakeProc:
    def __init__(self, pid: int = 4242, exited: bool = False) -> None:
        self.pid = pid
        self._exited = exited

    def poll(self):
        return 1 if self._exited else None

    def terminate(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return 0


class ChildEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="wallet-env-"))

    def test_no_provider_key_reaches_the_daemon(self) -> None:
        with mock.patch.dict(os.environ, {PROVIDER_KEY: "sk-canary", "GITHUB_TOKEN": "gh-canary"}):
            env = daemon.child_env()
        self.assertNotIn(PROVIDER_KEY, env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_the_wallets_own_configuration_survives(self) -> None:
        overrides = {"AGENT_WALLET_RPC_MONERO_MAINNET": "https://node.example:18081",
                     "AGENT_WALLET_MONERO_WALLET": "cold.xmr",
                     "WALLETD_LOCK_TIMEOUT": "3",
                     "HERMES_HOME": "/tmp/hermes-home",
                     "AGENT_WALLET_HOME": str(self.home)}
        with mock.patch.dict(os.environ, {**overrides, PROVIDER_KEY: "sk-canary"}):
            env = daemon.child_env()
        for name, value in overrides.items():
            self.assertEqual(env[name], value)
        self.assertTrue(env["PYTHONPATH"].startswith(str(daemon._plugin_root())))

    def test_one_shot_init_secrets_are_not_inherited_by_serve(self) -> None:
        with mock.patch.dict(os.environ, {"WALLETD_PASSPHRASE": "leak", "WALLETD_PIN": "123456"}):
            env = daemon.child_env()
        self.assertNotIn("WALLETD_PASSPHRASE", env)
        self.assertNotIn("WALLETD_PIN", env)

    def test_start_passes_the_minimal_env_to_walletd(self) -> None:
        (self.home / "keystore.json").write_text("{}")
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            captured["argv"] = argv
            return _FakeProc()

        with mock.patch.dict(os.environ, {PROVIDER_KEY: "sk-canary"}), \
                mock.patch.object(daemon, "is_running", lambda home=None: False), \
                mock.patch.object(daemon, "_held_by_another", lambda home: False), \
                mock.patch.object(daemon, "_service_managed", lambda home=None: False), \
                mock.patch.object(daemon, "wait_ready", lambda home=None, seconds=0: False), \
                mock.patch.object(daemon.subprocess, "Popen", fake_popen):
            daemon.start(self.home)

        self.assertNotIn(PROVIDER_KEY, captured["env"])
        self.assertEqual(captured["argv"][-1], "serve")
        self.assertIn(str(daemon._plugin_root()), captured["env"]["PYTHONPATH"])

    def test_bootstrap_hands_the_passphrase_to_init_only(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")

        with mock.patch.dict(os.environ, {PROVIDER_KEY: "sk-canary"}), \
                mock.patch.object(daemon.subprocess, "run", fake_run):
            daemon.bootstrap(self.home, "correct horse", pin="123456")

        self.assertEqual(captured["env"]["WALLETD_PASSPHRASE"], "correct horse")
        self.assertEqual(captured["env"]["WALLETD_PIN"], "123456")
        self.assertNotIn(PROVIDER_KEY, captured["env"])
        self.assertEqual(captured["argv"][-1], "init")


class SignerChildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="wallet-signer-"))
        binary = signer.binary_path(self.home)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 1\n")
        binary.chmod(0o755)

    def test_spawn_passes_only_a_config_file_and_a_minimal_env(self) -> None:
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return _FakeProc(exited=True)          # exits at once: spawn must fail, we keep the call

        with mock.patch.dict(os.environ, {PROVIDER_KEY: "sk-canary"}), \
                mock.patch.object(signer.subprocess, "Popen", fake_popen):
            with self.assertRaises(signer.SignerError):
                signer.spawn(self.home, "https://node.example:18081", port=38111)

        argv = captured["argv"]
        self.assertEqual(argv[:2], [str(signer.binary_path(self.home)), "--config-file"])
        conf = Path(argv[2])
        self.assertEqual(conf, signer.config_path(self.home))
        self.assertEqual(os.stat(conf).st_mode & 0o777, 0o600, "the login file must be 0600")
        self.assertEqual(sorted(captured["env"]), ["HOME", "PATH"])
        self.assertNotIn(PROVIDER_KEY, captured["env"])

    def test_the_login_is_in_the_file_and_never_in_argv(self) -> None:
        creds = signer.credentials(self.home)
        conf = signer._write_config(self.home, "https://node.example:18081", 38112,
                                   self.home / "monero", creds)
        text = conf.read_text()
        self.assertIn(f"rpc-login={creds['user']}:{creds['password']}", text)
        self.assertIn("daemon-address=https://node.example:18081", text)
        self.assertIn("rpc-bind-port=38112", text)
        self.assertNotIn(creds["password"], " ".join(
            [str(signer.binary_path(self.home)), "--config-file", str(conf)]))

    def test_a_wider_mode_on_an_old_file_is_tightened(self) -> None:
        creds = signer.credentials(self.home)
        conf = signer.config_path(self.home)
        conf.write_text("stale")
        conf.chmod(0o644)
        signer._write_config(self.home, "https://node.example:18081", 38113,
                             self.home / "monero", creds)
        self.assertEqual(os.stat(conf).st_mode & 0o777, 0o600)

    def test_preflight_does_not_hand_the_binary_the_agent_env(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return mock.Mock(returncode=1, stdout="", stderr="nope")

        with mock.patch.dict(os.environ, {PROVIDER_KEY: "sk-canary"}), \
                mock.patch.object(signer.subprocess, "run", fake_run):
            signer.preflight(self.home)

        self.assertEqual(sorted(captured["env"]), ["HOME", "PATH"])


if __name__ == "__main__":
    unittest.main()

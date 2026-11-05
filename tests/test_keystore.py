from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from walletd import keystore


class KeystoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keystore.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_roundtrip(self) -> None:
        payload = keystore.new_payload()
        keystore.write_atomic(self.path, keystore.seal(payload, "correct horse battery"))
        reopened = keystore.open_sealed(keystore.load(self.path), "correct horse battery")
        self.assertEqual(reopened["master"], payload["master"])
        self.assertEqual(len(base64.b64decode(reopened["master"])), 32)

    def test_wrong_passphrase_fails_closed(self) -> None:
        payload = keystore.new_payload()
        keystore.write_atomic(self.path, keystore.seal(payload, "passphrase-one"))
        with self.assertRaises(keystore.InvalidPassphrase):
            keystore.open_sealed(keystore.load(self.path), "passphrase-two")
        os.remove(self.path)
        with self.assertRaises(keystore.CorruptKeystore):
            keystore.load(self.path)

    def test_ciphertext_tamper_detected(self) -> None:
        payload = keystore.new_payload()
        envelope = keystore.seal(payload, "passphrase-one")
        raw = bytearray(base64.b64decode(envelope["cipher"]["ct"]))
        raw[5] ^= 0x01
        envelope["cipher"]["ct"] = base64.b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(keystore.InvalidPassphrase):
            keystore.open_sealed(envelope, "passphrase-one")

    def test_envelope_has_no_plaintext(self) -> None:
        payload = keystore.new_payload()
        envelope = keystore.seal(payload, "passphrase-one")
        blob = json.dumps(envelope)
        self.assertNotIn(payload["master"], blob)
        self.assertNotIn("passphrase-one", blob)

    def test_file_mode_is_0600(self) -> None:
        keystore.write_atomic(self.path, keystore.seal(keystore.new_payload(), "passphrase-one"))
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_empty_passphrase_refused(self) -> None:
        with self.assertRaises(keystore.InvalidPassphrase):
            keystore.seal({"a": 1}, "")


if __name__ == "__main__":
    unittest.main()

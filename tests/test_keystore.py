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
        """Tampering with the payload is *corruption*, not a wrong passphrase: the wrapper opened fine,
        and telling the operator their passphrase is wrong would send them looking in the wrong place."""
        payload = keystore.new_payload()
        envelope = keystore.seal(payload, "passphrase-one")
        raw = bytearray(base64.b64decode(envelope["cipher"]["ct"]))
        raw[5] ^= 0x01
        envelope["cipher"]["ct"] = base64.b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(keystore.CorruptKeystore):
            keystore.open_sealed(envelope, "passphrase-one")

    def test_wrapper_tamper_reads_as_a_wrong_passphrase(self) -> None:
        payload = keystore.new_payload()
        envelope = keystore.seal(payload, "passphrase-one")
        raw = bytearray(base64.b64decode(envelope["unlocks"]["passphrase"]["cipher"]["ct"]))
        raw[3] ^= 0x01
        envelope["unlocks"]["passphrase"]["cipher"]["ct"] = base64.b64encode(bytes(raw)).decode("ascii")
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
        from tests import assert_private
        assert_private(self, self.path)

    def test_empty_passphrase_refused(self) -> None:
        with self.assertRaises(keystore.InvalidPassphrase):
            keystore.seal({"a": 1}, "")


class TwoUnlocksTests(unittest.TestCase):
    """One payload, one data key, two wrappers. Either secret opens the same wallet, and neither can be
    derived from the other."""

    def setUp(self) -> None:
        self.payload = keystore.new_payload()
        self.envelope, self.data_key = keystore.seal_and_key(self.payload, "a-long-passphrase")
        self.envelope = keystore.with_pin(self.envelope, "123456", self.data_key)

    def test_either_secret_opens_the_same_payload(self) -> None:
        by_pass, key_a = keystore.open_full(self.envelope, "a-long-passphrase")
        by_pin, key_b = keystore.open_with_pin(self.envelope, "123456")
        self.assertEqual(by_pass["master"], self.payload["master"])
        self.assertEqual(by_pin["master"], self.payload["master"])
        self.assertEqual(key_a, key_b)

    def test_a_wrong_pin_is_its_own_refusal(self) -> None:
        with self.assertRaises(keystore.InvalidPin):
            keystore.open_with_pin(self.envelope, "654321")

    def test_the_pin_cannot_open_a_keystore_that_has_no_pin(self) -> None:
        plain, _ = keystore.seal_and_key(self.payload, "a-long-passphrase")
        with self.assertRaises(keystore.NoPinSlot):
            keystore.open_with_pin(plain, "123456")

    def test_rotating_the_passphrase_leaves_the_pin_working(self) -> None:
        rotated = keystore.with_passphrase(self.envelope, "a-different-passphrase", self.data_key)
        with self.assertRaises(keystore.InvalidPassphrase):
            keystore.open_full(rotated, "a-long-passphrase")
        self.assertEqual(keystore.open_full(rotated, "a-different-passphrase")[0]["master"],
                         self.payload["master"])
        self.assertEqual(keystore.open_with_pin(rotated, "123456")[0]["master"],
                         self.payload["master"])

    def test_rotating_the_pin_leaves_the_passphrase_working(self) -> None:
        rotated = keystore.with_pin(self.envelope, "999999", self.data_key)
        with self.assertRaises(keystore.InvalidPin):
            keystore.open_with_pin(rotated, "123456")
        self.assertEqual(keystore.open_with_pin(rotated, "999999")[0]["master"],
                         self.payload["master"])
        self.assertEqual(keystore.open_full(rotated, "a-long-passphrase")[0]["master"],
                         self.payload["master"])

    def test_resealing_a_changed_payload_keeps_both_wrappers(self) -> None:
        """The reason for the data key: the payload is written far more often than the secrets change."""
        changed = dict(self.payload, accounts={"xmr": 7})
        again = keystore.reseal(self.envelope, changed, self.data_key)
        self.assertEqual(keystore.open_with_pin(again, "123456")[0]["accounts"], {"xmr": 7})
        self.assertEqual(keystore.open_full(again, "a-long-passphrase")[0]["accounts"], {"xmr": 7})

    def test_the_pin_wrapper_is_expensive(self) -> None:
        """Six digits only mean something because each guess costs ~128 MB and about a second."""
        slot = self.envelope["unlocks"]["pin"]["kdf"]
        self.assertGreaterEqual(int(slot["n"]), keystore.PIN_KDF_N)
        self.assertGreaterEqual(keystore.PIN_KDF_N, 1 << 17)

    def test_the_wrappers_do_not_share_a_salt_or_a_nonce(self) -> None:
        pin, pw = self.envelope["unlocks"]["pin"], self.envelope["unlocks"]["passphrase"]
        self.assertNotEqual(pin["kdf"]["salt"], pw["kdf"]["salt"])
        self.assertNotEqual(pin["cipher"]["nonce"], pw["cipher"]["nonce"])


class DeviceBoundPinTests(unittest.TestCase):
    """The PIN slot is bound to the machine that set it: 32 random bytes kept *outside* the wallet
    directory, mixed into the key. A copy of keystore.json is then not a guessable target at all —
    even the correct PIN does not open it."""

    def setUp(self) -> None:
        self.payload = keystore.new_payload()
        self.envelope, self.data_key = keystore.seal_and_key(self.payload, "a-long-passphrase")
        self.device = b"\x11" * 32
        self.envelope = keystore.with_pin(self.envelope, "123456", self.data_key, pepper=self.device)

    def test_the_slot_says_it_is_bound(self) -> None:
        self.assertTrue(keystore.pin_is_bound(self.envelope))
        self.assertTrue(self.envelope["unlocks"]["pin"].get("peppered"))

    def test_the_pin_plus_the_device_key_opens_it(self) -> None:
        payload, key = keystore.open_with_pin(self.envelope, "123456", pepper=self.device)
        self.assertEqual(payload["master"], self.payload["master"])
        self.assertEqual(key, self.data_key)

    def test_the_correct_pin_without_the_device_key_does_not(self) -> None:
        with self.assertRaises(keystore.NoDeviceKey):
            keystore.open_with_pin(self.envelope, "123456")

    def test_a_different_device_key_is_a_wrong_pin(self) -> None:
        """Indistinguishable on purpose: an attacker learns nothing about which half was wrong."""
        with self.assertRaises(keystore.InvalidPin):
            keystore.open_with_pin(self.envelope, "123456", pepper=b"\x22" * 32)

    def test_the_passphrase_is_not_bound_and_still_opens_it(self) -> None:
        self.assertEqual(keystore.open_full(self.envelope, "a-long-passphrase")[0]["master"],
                         self.payload["master"])

    def test_an_unbound_pin_slot_still_opens(self) -> None:
        """0.9.0 shipped unpeppered slots for about an hour; they must keep working."""
        envelope, data_key = keystore.seal_and_key(self.payload, "a-long-passphrase")
        plain = keystore.with_pin(envelope, "123456", data_key)
        self.assertFalse(keystore.pin_is_bound(plain))
        self.assertEqual(keystore.open_with_pin(plain, "123456")[0]["master"], self.payload["master"])

    def test_the_kdf_is_expensive_enough_to_be_worth_measuring(self) -> None:
        slot = self.envelope["unlocks"]["pin"]["kdf"]
        self.assertGreaterEqual(int(slot["n"]), 1 << 19)


if __name__ == "__main__":
    unittest.main()

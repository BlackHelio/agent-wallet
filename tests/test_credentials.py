"""Changing the two secrets a person owns: the passphrase that decrypts the wallet and the PIN that
authorises a payment.

The passphrase is not a setting — it is the key to `keystore.json`. Re-sealing that file must leave
every derived thing inside it untouched (master key, ledger seed, chain keys, Monero wallet password),
and the old passphrase must stop working at once. These tests open a *fresh* state after the change,
because the in-memory object cannot prove anything about what landed on disk.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState, xmr_addr
from walletd import keystore

PIN = "123456"
NEW_PASS = "a-much-longer-passphrase"


class PassphraseChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _change(self, current=PASS, new=NEW_PASS, confirm=None) -> dict:
        return self.state.change_passphrase({
            "passphrase": current,
            "new_passphrase": new,
            "confirm": new if confirm is None else confirm,
        })

    # ---------- the refusals ----------
    def test_the_current_passphrase_is_required(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._change(current="")
        self.assertEqual(caught.exception.code, "passphrase_required")

    def test_a_wrong_current_passphrase_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._change(current="not-the-passphrase")
        self.assertEqual(caught.exception.code, "bad_passphrase")
        # and the file is untouched: the original still opens it
        fresh = WalletState(self.home)
        fresh.fake = FakeAdapter()
        fresh.unlock(PASS)

    def test_a_short_passphrase_is_refused(self) -> None:
        """`123456` is the PIN. Six characters as a passphrase is what made the pane refuse to unlock."""
        with self.assertRaises(WalletError) as caught:
            self._change(new="123456")
        self.assertEqual(caught.exception.code, "bad_passphrase_format")

    def test_a_mismatch_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._change(new=NEW_PASS, confirm=NEW_PASS + "x")
        self.assertEqual(caught.exception.code, "passphrase_mismatch")

    def test_the_same_passphrase_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._change(new=PASS)
        self.assertEqual(caught.exception.code, "passphrase_unchanged")

    def test_it_needs_an_unlocked_wallet(self) -> None:
        locked = WalletState(self.home)
        with self.assertRaises(WalletError) as caught:
            locked.change_passphrase({"passphrase": PASS, "new_passphrase": NEW_PASS})
        self.assertEqual(caught.exception.code, "locked")

    # ---------- the change ----------
    def test_the_old_passphrase_stops_working_and_the_new_one_opens_the_wallet(self) -> None:
        self._change()
        fresh = WalletState(self.home)
        with self.assertRaises(WalletError) as caught:
            fresh.unlock(PASS)
        self.assertEqual(caught.exception.code, "bad_passphrase")
        again = WalletState(self.home)
        again.fake = FakeAdapter()
        again.unlock(NEW_PASS)
        self.assertTrue(again.is_unlocked())

    def test_nothing_inside_the_wallet_moves(self) -> None:
        """Same addresses, same master key, same PIN — only the key that opens the file changed."""
        before = WalletState(self.home)
        before.fake = FakeAdapter()      # `_build_adapters` reads it; without it unlock dies on AttributeError
        before.unlock(PASS)
        address = None
        if before.adapters:
            adapter = before.adapters["monero-stagenet"]
            address = adapter.address_for(0) if hasattr(adapter, "address_for") else None
        master_before = before.master

        self._change()

        after = WalletState(self.home)
        after.fake = FakeAdapter()
        after.unlock(NEW_PASS)
        self.assertEqual(after.master, master_before)
        self.assertTrue(after.pin_status()["set"], "the PIN lives inside the payload and must survive")
        if address and after.adapters:
            self.assertEqual(after.adapters["monero-stagenet"].address_for(0), address)

    def test_the_change_is_receipted_without_the_secret(self) -> None:
        self._change()
        entries = [e for e in self.state.ledger.tail(50) if e.get("kind") == "wallet"
                   and e.get("decision") == "passphrase_changed"]
        self.assertEqual(len(entries), 1)
        details = entries[0].get("details") or {}
        self.assertNotIn(NEW_PASS, str(details))
        self.assertNotIn(PASS, str(details))
        self.assertEqual(details.get("length"), len(NEW_PASS))

    def test_the_agent_scope_cannot_reach_it(self) -> None:
        """A token alone must not be able to re-key the wallet or change what authorises a payment."""
        from walletd.state import AGENT, build_methods
        methods = build_methods()
        self.assertIn("change_passphrase", methods)
        self.assertNotEqual(methods["change_passphrase"][0], AGENT)
        self.assertNotEqual(methods["set_pin"][0], AGENT)


class PinUnlockTests(unittest.TestCase):
    """The PIN opens the wallet, and it is the same six digits that authorise a payment.

    This is a deliberate design decision by the operator: one secret he types, both jobs. What makes it
    survivable is that the PIN wraps the keystore's data key behind ~128 MB of scrypt (a million guesses
    is weeks of work) and that the counter slows a live attacker — not that six digits are strong.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fresh(self) -> WalletState:
        fresh = WalletState(self.home)
        fresh.fake = FakeAdapter()
        return fresh

    def test_the_pin_opens_the_wallet(self) -> None:
        fresh = self._fresh()
        self.assertFalse(fresh.is_unlocked())
        fresh.unlock_pin(PIN)
        self.assertTrue(fresh.is_unlocked())
        self.assertIsNotNone(fresh.data_key)

    def test_a_wrong_pin_is_counted_and_refused(self) -> None:
        fresh = self._fresh()
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin("654321")
        self.assertEqual(caught.exception.code, "bad_pin")
        self.assertIn("left", str(caught.exception))
        self.assertFalse(fresh.is_unlocked())

    def test_repeated_wrong_pins_lock_the_pin_path(self) -> None:
        fresh = self._fresh()
        code = ""
        for _ in range(6):
            try:
                fresh.unlock_pin("654321")
            except WalletError as exc:
                code = exc.code
        self.assertEqual(code, "pin_locked")
        # and the right PIN is refused while locked, or the lockout would be decoration
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin(PIN)
        self.assertEqual(caught.exception.code, "pin_locked")

    def test_a_wallet_with_no_pin_says_so(self) -> None:
        home = Path(self.tmp.name) / "nopin"
        state = WalletState(home)
        state.fake = FakeAdapter()
        state.initialize(PASS)          # no PIN
        fresh = WalletState(home)
        fresh.fake = FakeAdapter()
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin(PIN)
        self.assertEqual(caught.exception.code, "pin_not_set")

    def test_setting_a_pin_makes_pin_unlock_available(self) -> None:
        home = Path(self.tmp.name) / "later"
        state = WalletState(home)
        state.fake = FakeAdapter()
        state.initialize(PASS)
        state.set_pin({"pin": PIN, "confirm": PIN, "passphrase": PASS})
        fresh = WalletState(home)
        fresh.fake = FakeAdapter()
        fresh.unlock_pin(PIN)
        self.assertTrue(fresh.is_unlocked())

    def test_the_pin_survives_a_passphrase_change(self) -> None:
        self.state.change_passphrase({"passphrase": PASS, "new_passphrase": NEW_PASS, "confirm": NEW_PASS})
        fresh = self._fresh()
        fresh.unlock_pin(PIN)           # the six digits still open it
        self.assertTrue(fresh.is_unlocked())
        other = self._fresh()
        other.unlock(NEW_PASS)          # and so does the new passphrase
        self.assertTrue(other.is_unlocked())

    def test_the_passphrase_survives_a_pin_change(self) -> None:
        self.state.set_pin({"pin": "999999", "confirm": "999999", "passphrase": PASS})
        fresh = self._fresh()
        with self.assertRaises(WalletError):
            fresh.unlock_pin(PIN)
        again = self._fresh()
        again.unlock_pin("999999")
        self.assertTrue(again.is_unlocked())
        third = self._fresh()
        third.unlock(PASS)
        self.assertTrue(third.is_unlocked())

    def test_the_receipt_says_which_secret_opened_it(self) -> None:
        fresh = self._fresh()
        fresh.unlock_pin(PIN)
        entries = [e for e in fresh.ledger.tail(20)
                   if e.get("kind") == "wallet" and e.get("decision") == "unlocked"]
        self.assertTrue(entries)
        self.assertEqual((entries[-1].get("details") or {}).get("how"), "pin")

    def test_locking_forgets_the_data_key(self) -> None:
        fresh = self._fresh()
        fresh.unlock_pin(PIN)
        self.assertIsNotNone(fresh.data_key)
        fresh.lock()
        self.assertIsNone(fresh.data_key)
        self.assertIsNone(fresh.payload)


def write_v2_keystore(home: Path, payload: dict, passphrase: str) -> None:
    """A keystore exactly as 0.8.x wrote it: one scrypt key straight over the payload."""
    import base64
    import hashlib
    import json
    import os

    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

    salt, nonce = os.urandom(16), os.urandom(24)
    key = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=1 << 15, r=8, p=1, dklen=32,
                         maxmem=128 * (1 << 15) * 8 + 32 * 1024 * 1024)
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = {
        "v": 2,
        "kdf": {"alg": "scrypt", "n": 1 << 15, "r": 8, "p": 1,
                "salt": base64.b64encode(salt).decode("ascii")},
        "cipher": {"alg": "xchacha20-poly1305-ietf",
                   "nonce": base64.b64encode(nonce).decode("ascii"),
                   "ct": base64.b64encode(crypto_aead_xchacha20poly1305_ietf_encrypt(
                       message, b"agent-wallet/keystore/v2", nonce, key)).decode("ascii")},
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "keystore.json").write_text(json.dumps(envelope), encoding="utf-8")


class KeystoreMigrationTests(unittest.TestCase):
    """A 0.8.x keystore must open, upgrade, and keep working. The PIN cannot be carried over — a hash is
    not a PIN — so the wallet says so in a receipt instead of pretending the PIN still unlocks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        payload = keystore.new_payload()
        payload["pin"] = keystore.make_pin(PIN)
        write_v2_keystore(self.home, payload, PASS)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _state(self) -> WalletState:
        state = WalletState(self.home)
        state.fake = FakeAdapter()
        return state

    def test_the_passphrase_still_opens_it_and_the_file_becomes_v3(self) -> None:
        state = self._state()
        state.unlock(PASS)
        self.assertTrue(state.is_unlocked())
        on_disk = keystore.load(self.home / "keystore.json")
        self.assertEqual(on_disk["v"], keystore.VERSION)
        self.assertIn("unlocks", on_disk)

    def test_the_upgrade_is_receipted_and_says_the_pin_must_be_set_again(self) -> None:
        state = self._state()
        state.unlock(PASS)
        entries = [e for e in state.ledger.tail(20)
                   if e.get("kind") == "wallet" and e.get("decision") == "keystore_upgraded"]
        self.assertEqual(len(entries), 1)
        details = entries[0].get("details") or {}
        self.assertTrue(details.get("pin_was_set"))
        self.assertFalse(details.get("pin_slot"))

    def test_the_pin_cannot_unlock_until_it_is_set_again(self) -> None:
        self._state().unlock(PASS)
        fresh = self._state()
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin(PIN)
        self.assertEqual(caught.exception.code, "pin_not_set")
        # and the status line tells the pane exactly that, so it can ask for the passphrase. While
        # locked, `set` can only mean "the PIN opens this file" — the hash is inside the payload.
        self.assertFalse(fresh.pin_status()["set"])
        self.assertFalse(fresh.pin_status()["unlocks"])
        unlocked = self._state()
        unlocked.unlock(PASS)
        self.assertTrue(unlocked.pin_status()["set"], "the hash survived the upgrade")
        self.assertFalse(unlocked.pin_status()["unlocks"], "but it cannot open the file yet")

    def test_setting_the_pin_again_restores_pin_unlock(self) -> None:
        self._state().unlock(PASS)
        again = self._state()
        again.unlock(PASS)
        again.set_pin({"pin": PIN, "confirm": PIN, "passphrase": PASS})
        third = self._state()
        third.unlock_pin(PIN)
        self.assertTrue(third.is_unlocked())


class PinStatusWhileLockedTests(unittest.TestCase):
    def test_a_locked_wallet_reports_whether_the_pin_opens_it(self) -> None:
        """The pane shows the unlock bar only while locked, so this is the one moment it needs the
        answer — and whether a PIN slot exists is a key in the file, not a secret."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = WalletState(home)
            state.fake = FakeAdapter()
            state.initialize(PASS, pin=PIN)
            state.lock()
            status = state.pin_status()
            self.assertTrue(status["wallet_locked"])
            self.assertTrue(status["set"])
            self.assertTrue(status["unlocks"])
            self.assertIsNone(status["attempts_left"])


class PinHardeningTests(unittest.TestCase):
    """What makes six digits survivable: the PIN slot is bound to this machine, the KDF is deliberately
    expensive, and repeated lockouts close the PIN path until the passphrase is proved."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "wallet"
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        state = WalletState(self.home)
        state.fake = FakeAdapter()
        state.initialize(PASS, pin=PIN)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fresh(self, home: Path | None = None) -> WalletState:
        fresh = WalletState(home or self.home)
        fresh.fake = FakeAdapter()
        return fresh

    def test_the_device_key_lives_outside_the_wallet_directory_and_is_private(self) -> None:
        key_path = self.root / ".wallet-device.key"
        self.assertTrue(key_path.exists(), "the key must sit beside the wallet, not in it")
        self.assertEqual(len(bytes.fromhex(key_path.read_text().strip())), 32)
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / ".wallet-device.key").exists())

    def test_a_stolen_keystore_cannot_be_bruteforced_because_the_right_pin_does_not_open_it(self) -> None:
        """The whole point: copy the wallet somewhere else and the six digits are not even a guess —
        the 256-bit device key is missing, so there is nothing to brute-force."""
        stolen = self.root / "stolen"
        stolen.mkdir()
        (stolen / "keystore.json").write_bytes((self.home / "keystore.json").read_bytes())
        thief = self._fresh(stolen)
        with self.assertRaises(WalletError) as caught:
            thief.unlock_pin(PIN)              # the *correct* PIN
        self.assertEqual(caught.exception.code, "pin_needs_device")

    def test_the_passphrase_is_the_way_in_on_a_new_machine(self) -> None:
        stolen = self.root / "moved"
        stolen.mkdir()
        for name in ("keystore.json",):
            (stolen / name).write_bytes((self.home / name).read_bytes())
        moved = self._fresh(stolen)
        moved.unlock(PASS)
        self.assertTrue(moved.is_unlocked())

    def test_three_lockouts_close_the_pin_path(self) -> None:
        fresh = self._fresh()
        fresh._save_pin_unlock_state({"fails": 5, "lockouts": 3, "locked_until": "", "disabled": True})
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin(PIN)
        self.assertEqual(caught.exception.code, "pin_disabled")

    def test_the_ladder_closes_the_pin_after_seven_guesses(self) -> None:
        """Drive the real ladder instead of seeding the counter. Five wrong PINs lock it for 30 s, and
        every further wrong PIN after that is a new lockout — so the PIN path shuts on the **seventh**
        guess, not the fifteenth. Pinning the number matters: it is the only control that makes the
        online path finite, and a test that seeds `disabled=True` proves only that the flag is read."""
        fresh = self._fresh()
        codes = []
        for guess in range(1, 9):
            try:
                fresh.unlock_pin("000000" if PIN != "000000" else "111111")
                codes.append("accepted")
            except WalletError as exc:
                codes.append(exc.code)
            row = fresh._pin_unlock_state()
            if row["locked_until"]:  # the window elapsed, as waiting would make it
                row["locked_until"] = ""
                fresh._save_pin_unlock_state(row)
        self.assertEqual(codes[:4], ["bad_pin"] * 4)
        self.assertEqual(codes[4], "pin_locked")      # the 5th guess starts the 30 s window
        self.assertEqual(codes[5], "pin_locked")      # 6th: second lockout (2 min)
        self.assertEqual(codes[6], "pin_disabled")    # 7th: third lockout closes the PIN path
        self.assertEqual(codes[7], "pin_disabled")
        # and the right PIN is refused too, until the passphrase is proved
        with self.assertRaises(WalletError) as caught:
            fresh.unlock_pin(PIN)
        self.assertEqual(caught.exception.code, "pin_disabled")
        fresh.unlock(PASS)
        self.assertFalse(fresh._pin_unlock_state()["disabled"])

    def test_proving_the_passphrase_re_arms_the_pin(self) -> None:
        fresh = self._fresh()
        fresh._save_pin_unlock_state({"fails": 5, "lockouts": 3, "locked_until": "", "disabled": True})
        fresh.unlock(PASS)
        self.assertEqual(fresh._pin_unlock_state()["lockouts"], 0)
        self.assertFalse(fresh._pin_unlock_state()["disabled"])
        again = self._fresh()
        again.unlock_pin(PIN)
        self.assertTrue(again.is_unlocked())

    def test_doctor_reports_the_binding(self) -> None:
        fresh = self._fresh()
        ok, note, status = fresh._pin_binding_state()
        self.assertTrue(ok)
        self.assertIn("this machine", note)
        self.assertEqual(status, "")

    def test_doctor_fails_a_pin_that_opens_with_digits_alone(self) -> None:
        """A slot from 0.9.0: still works, but it is six digits in a file — say so."""
        from walletd import keystore as ks
        envelope = ks.load(self.home / "keystore.json")
        _, data_key = ks.open_full(envelope, PASS)
        payload, _ = ks.open_full(envelope, PASS)
        ks.write_atomic(self.home / "keystore.json",
                        ks.with_pin(ks.reseal(envelope, payload, data_key), PIN, data_key))
        ok, note, _ = self._fresh()._pin_binding_state()
        self.assertFalse(ok)
        self.assertIn("six digits alone", note)

    def test_doctor_fails_a_pin_bound_to_another_machine(self) -> None:
        (self.root / ".wallet-device.key").unlink()
        ok, note, _ = self._fresh()._pin_binding_state()
        self.assertFalse(ok)
        self.assertIn("another machine", note)

    def test_the_pin_slot_is_bound_and_the_kdf_is_expensive(self) -> None:
        from walletd import keystore as ks
        envelope = ks.load(self.home / "keystore.json")
        self.assertTrue(ks.pin_is_bound(envelope))
        self.assertGreaterEqual(int(envelope["unlocks"]["pin"]["kdf"]["n"]), 1 << 19)


if __name__ == "__main__":
    unittest.main()

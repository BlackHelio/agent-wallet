"""What a forged file must not be able to buy.

The threat this file answers, in Helio's words: *a prompt injection cannot be allowed to fake a
confirmation prompt for a sandbox saying it will send $5 to X when in reality it is trying to send
$100 to Y.*

Two different attacks hide in that sentence, and they need two different defences.

1. **A fake card** would be an agent showing a picture of a confirmation. There is no picture to fake:
   the only card is drawn by the pane from the daemon's own record, and the approval is bound to a
   digest of that record — change the amount or the address after the card was drawn and the approval
   is refused (`approval_digest_mismatch`), and if the surface echoes what it displayed the comparison
   is `card_changed`. Text the agent writes lives in `why`, which the card labels as the agent's words.

2. **A fake record**, which is the sharper one: `sandboxes.json`, `contacts.json` and `mandates.json`
   are 0600 files owned by the same uid as the agent, so an injected agent with a shell can edit them.
   A forged `unattended: true`, a forged `created_by: operator` or a forged `enabled` autopay is a
   *confirmation the human never gave*, and no amount of card design stops it. Each row is therefore
   sealed with the master key (which needs the passphrase), and a row that does not verify loses its
   authority on unlock and, for a sandbox, is refused for money until it is revoked.
"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.test_state import (FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState,
                              xmr_addr)

PIN = "123456"
AGENT = {"_scope": "agent"}


def _soon(hours: int) -> str:
    """An expiry inside `max_mandate_lifetime_hours`; 2099 is refused by the daemon."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ForgedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = self._open()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.settings.pin_every_payment = True
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = self.state.fake

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- fixtures ----------
    def _open(self) -> WalletState:
        state = WalletState(self.home)
        state.fake = FakeAdapter()
        return state

    def _reload(self) -> WalletState:
        """A restart: the daemon re-reads every authority file and re-checks its seals."""
        fresh = self._open()
        fresh.unlock(PASS)
        fresh.adapters["monero-stagenet"] = self.state.fake
        return fresh

    def _budget(self, unattended: bool = True, budget: str = "0.05") -> str:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": budget, "lifetime_hours": 24,
                                          "label": "task", "unattended": unattended,
                                          "passphrase": PASS, "pin": PIN})
        sid = made["sandbox"]["sandbox_id"]
        funding = made["funding"]
        self.state.approve({"pin": PIN, "passphrase": PASS,
                            "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"],
                            "confirm_amount": funding["request"]["amount"]})
        return sid

    def _edit(self, name: str, mutate) -> None:
        """Edit an authority file the way a same-uid agent with a shell would."""
        path = self.home / name
        raw = json.loads(path.read_text())
        mutate(raw)
        path.write_text(json.dumps(raw, indent=1))

    def _rows(self, name: str, key: str) -> list:
        return json.loads((self.home / name).read_text())[key]

    def _spend(self, state: WalletState, sid: str, amount: str = "0.01",
               payee: str = "") -> dict:
        return state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                 "amount": amount, "payee": payee or xmr_addr(2),
                                 "sandbox_id": sid, "why": "inside the budget"})

    def _spend_as(self, state: WalletState, sid: str, profile: str) -> dict:
        return state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                 "amount": "0.01", "payee": xmr_addr(2), "sandbox_id": sid,
                                 "profile": profile, "why": "inside the budget"})

    # ---------- the seal exists, and a clean restart keeps the arm ----------
    def test_the_authority_files_are_sealed(self) -> None:
        sid = self._budget()
        raw = json.loads((self.home / "sandboxes.json").read_text())
        self.assertEqual(raw.get("seal_format"), self.state.SEAL_VERSION)
        self.assertIn(sid, raw.get("seals") or {})
        self.assertTrue(self.state.sandboxes[sid].unattended)

    # ---------- 1: a forged arm ----------
    def test_forging_the_arm_on_disk_loses_the_arm_and_refuses_the_budget(self) -> None:
        sid = self._budget(unattended=False)
        self._edit("sandboxes.json", lambda raw: raw["sandboxes"][0].update({"unattended": True}))
        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertFalse(row.unattended, "a hand-written arm must not survive a restart")
        self.assertTrue(row.tampered, "the edit has to be visible, not silently undone")

        out = self._spend(fresh, sid)
        self.assertFalse(out["decision"]["allow"], out["decision"])
        self.assertEqual(out["decision"]["code"], "sandbox_tampered")
        self.assertEqual(out["request"]["status"], "denied")

        receipts = [e for e in fresh.ledger.tail(60) if e.get("decision") == "authority_dropped"]
        self.assertTrue(receipts, "the drop must leave a receipt")
        self.assertEqual(receipts[-1]["details"].get("reason"), "seal_mismatch")

    # ---------- the $5-to-X, $100-to-Y shape ----------
    def test_editing_the_amount_or_the_address_is_the_same_refusal(self) -> None:
        sid = self._budget(budget="0.05")
        self._edit("sandboxes.json", lambda raw: raw["sandboxes"][0].update({
            "budget": "5", "address": xmr_addr(3), "allowed_payees": [xmr_addr(3)]}))
        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertTrue(row.tampered)
        # a seal proves tampering; it cannot recover the original values, so the row keeps what is on
        # disk for display and is simply refused for money. That is the honest behaviour: the daemon
        # must not invent a budget it cannot verify.
        self.assertFalse(row.allowed_payees, "a forged allowlist blesses nothing")
        out = self._spend(fresh, sid, amount="0.01", payee=xmr_addr(3))
        self.assertEqual(out["decision"]["code"], "sandbox_tampered")

    def test_a_tampered_budget_cannot_be_armed_funded_or_widened(self) -> None:
        sid = self._budget(unattended=False)
        self._edit("sandboxes.json", lambda raw: raw["sandboxes"][0].update({"unattended": True}))
        fresh = self._reload()
        for call, params in (
            ("sandbox_set_unattended", {"sandbox_id": sid, "on": True, "passphrase": PASS, "pin": PIN}),
            ("sandbox_topup", {"sandbox_id": sid, "amount": "0.01"}),
            ("sandbox_set_payees", {"sandbox_id": sid, "allowed_payees": [xmr_addr(1)],
                                    "passphrase": PASS}),
        ):
            with self.assertRaises(WalletError) as caught:
                getattr(fresh, call)(params)
            self.assertEqual(caught.exception.code, "sandbox_tampered", call)

    def test_a_tampered_budget_can_still_be_revoked_and_swept_home(self) -> None:
        sid = self._budget()
        # a *change* is what breaks the seal; rewriting the same value is a no-op file
        self._edit("sandboxes.json", lambda raw: raw["sandboxes"][0].update({"budget": "0.06"}))
        fresh = self._reload()
        self.assertTrue(fresh.sandboxes[sid].tampered)
        out = fresh.sandbox_revoke({"sandbox_id": sid})
        self.assertIn("sweep", out)
        self.assertTrue(fresh.sandboxes[sid].revoked_at)

    # ---------- 2: a forged blessing ----------
    def test_forging_a_blessing_on_disk_blesses_nothing(self) -> None:
        # unarmed on purpose: an armed budget pays any address, which would hide the blessing test
        self._budget(unattended=False)
        address = xmr_addr(4)
        self.state.contact_save({"name": "shop", "addresses": {"monero-stagenet": address},
                                 "_scope": "agent"})
        self._edit("contacts.json", lambda raw: raw["contacts"][0].update({"created_by": "operator"}))
        fresh = self._reload()
        self.assertEqual(fresh.contacts[next(iter(fresh.contacts))].created_by, "agent",
                         "a contact written into the file is not a human's blessing")
        sid = list(fresh.sandboxes)[0]
        out = self._spend(fresh, sid, payee=address)
        self.assertNotEqual(out["request"]["status"], "sent")
        self.assertEqual(out["request"]["status"], "pending")

    def test_every_contact_gets_its_own_seal(self) -> None:
        """Two contacts, two seals. Keying the seal by name-less field collapsed them onto one key."""
        for index, name in enumerate(("Aiden", "CLI Vendor")):
            self.state.contact_save({"name": name, "addresses": {"monero-stagenet": xmr_addr(index + 1)},
                                     "passphrase": PASS, "_scope": "operator"})
        raw = json.loads((self.home / "contacts.json").read_text())
        self.assertEqual(len(raw["contacts"]), 2)
        self.assertEqual(len(raw.get("seals") or {}), 2, "one seal per contact, not one for the file")
        fresh = self._reload()
        self.assertEqual(sorted(c.created_by for c in fresh.contacts.values()), ["operator", "operator"],
                         "both blessings must survive a restart")

    # ---------- 3: a forged autopay ----------
    def test_forging_an_active_unattended_autopay_stops_it(self) -> None:
        made = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                            "payee": xmr_addr(5), "per_tx_cap": "0.01",
                                            "period_cap": "0.01", "period_hours": 24,
                                            "absolute_cap": "0.05", "expires_at": _soon(48),
                                            "label": "coffee"})
        mid = made["mandate"]["mandate_id"]
        self._edit("mandates.json", lambda raw: raw["mandates"][0].update(
            {"enabled": True, "unattended": True, "activated_at": "2026-01-01T00:00:00Z"}))
        fresh = self._reload()
        mandate = fresh.mandates[mid]
        self.assertFalse(mandate.enabled, "a hand-edited autopay must not run")
        self.assertFalse(mandate.unattended)

    # ---------- 4: the legacy file is not trusted either ----------
    def test_an_unsealed_file_is_resealed_without_authority(self) -> None:
        sid = self._budget()
        path = self.home / "sandboxes.json"
        raw = json.loads(path.read_text())
        for key in ("seals", "seal_format"):
            raw.pop(key, None)
        raw["sandboxes"][0]["unattended"] = True          # what an attacker would add while stripping
        path.write_text(json.dumps(raw, indent=1))
        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertFalse(row.unattended, "an unsealed file cannot prove a human armed it")
        self.assertFalse(row.tampered, "an old file is not an attack, it is just old")
        self.assertEqual(json.loads(path.read_text()).get("seal_format"), fresh.SEAL_VERSION,
                         "it is sealed on the way out, so the next edit is caught")
        receipts = [e for e in fresh.ledger.tail(60) if e.get("decision") == "authority_dropped"]
        self.assertEqual(receipts[-1]["details"].get("reason"), "unsealed")

    # ---------- 6: a genuine old file, replayed ----------
    def test_a_file_with_its_digest_removed_is_not_the_newest_one(self) -> None:
        """A 0.7.1-era copy of the file has no digest at all; that absence is what gives it away."""
        sid = self._budget(unattended=True)
        armed = json.loads((self.home / "sandboxes.json").read_text())
        self.state.sandbox_set_unattended({"sandbox_id": sid, "on": False})
        armed.pop("digest", None)                 # the file a 0.7.1 process would have written
        armed["seal_format"] = 1
        (self.home / "sandboxes.json").write_text(json.dumps(armed, indent=1))
        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertFalse(row.unattended)
        self.assertTrue(row.revoked_at, "a file from before the ledger kept a record is not trusted")

    def test_restoring_the_armed_file_does_not_restore_the_arm(self) -> None:
        """The seal proves who wrote a row, not that it is the newest one.

        An agent with the uid can copy `sandboxes.json` while the budget is armed. If the operator
        takes the arm back and the copy is put back, every seal still verifies — the confirmation is
        real, just old. The ledger remembers which file was written last, so a file that disagrees
        is stale and its budgets are closed, not merely un-armed.
        """
        sid = self._budget(unattended=True)
        armed = (self.home / "sandboxes.json").read_bytes()
        self.state.sandbox_set_unattended({"sandbox_id": sid, "on": False})
        self.assertFalse(self.state.sandboxes[sid].unattended)

        (self.home / "sandboxes.json").write_bytes(armed)
        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertFalse(row.unattended, "an old arm must not come back")
        self.assertTrue(row.revoked_at, "a budget whose record is out of date is closed, not trusted")
        self.assertFalse(row.tampered, "the rows are authentic; only their age is the problem")
        out = self._spend(fresh, sid)
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_closed")
        reasons = [e["details"].get("reason") for e in fresh.ledger.tail(40)
                   if e.get("decision") == "authority_dropped"]
        self.assertIn("stale_copy", reasons)

    def test_a_restored_copy_cannot_resurrect_a_revoked_budget(self) -> None:
        sid = self._budget(unattended=True)
        armed = (self.home / "sandboxes.json").read_bytes()
        self.state.sandbox_revoke({"sandbox_id": sid, "passphrase": PASS})
        self.assertTrue(self.state.sandboxes[sid].revoked_at)
        (self.home / "sandboxes.json").write_bytes(armed)
        fresh = self._reload()
        self.assertTrue(fresh.sandboxes[sid].revoked_at, "a revoke is not undoable by a file copy")
        self.assertFalse(fresh.sandboxes[sid].unattended)

    # ---------- 7: the profile list is authority too ----------
    def test_widening_the_profile_list_on_disk_is_dropped(self) -> None:
        sid = self._budget(unattended=True)
        self.state.sandbox_set_profiles({"sandbox_id": sid, "allowed_profiles": ["wallet"],
                                         "passphrase": PASS})
        blocked = self._spend_as(self.state, sid, "default")
        self.assertEqual(blocked["decision"]["code"], "sandbox_profile")

        self._edit("sandboxes.json", lambda raw: raw["sandboxes"][0].update(
            {"allowed_profiles": ["wallet", "default"]}))
        fresh = self._reload()
        self.assertEqual(fresh.sandboxes[sid].allowed_profiles, [],
                         "which profiles may spend a budget is authority: a forged list is dropped")
        self.assertFalse(fresh.sandboxes[sid].unattended)

    # ---------- 8: truncating the chain to erase the memory ----------
    def test_truncating_the_ledger_cannot_hide_a_stale_copy(self) -> None:
        sid = self._budget(unattended=True)
        armed = (self.home / "sandboxes.json").read_bytes()
        self.state.sandbox_set_unattended({"sandbox_id": sid, "on": False})
        self.state.lock()

        ledger = self.home / "ledger.jsonl"
        lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
        ledger.write_text("\n".join(lines[: max(1, len(lines) - 4)]) + "\n")
        (self.home / "sandboxes.json").write_bytes(armed)

        fresh = self._reload()
        row = fresh.sandboxes[sid]
        self.assertTrue(fresh.ledger_rewound, "the anchor has to notice the shorter chain")
        self.assertFalse(row.unattended)
        self.assertTrue(row.revoked_at)

    # ---------- 5: the card itself ----------
    def test_a_request_edited_after_the_card_was_drawn_cannot_be_approved(self) -> None:
        filed = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.005", "payee": xmr_addr(6), "why": "a small one"})
        req = self.state.requests[filed["request"]["request_id"]]
        req.amount = Decimal("100")           # the $5 the human saw is now $100
        req.payee = xmr_addr(7)               # ...to somebody else
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "passphrase": PASS, "request_id": req.request_id,
                                "approval_id": filed["approval_id"],
                                "confirm_amount": req.amount})
        self.assertEqual(caught.exception.code, "approval_digest_mismatch")
        self.assertEqual(self.state.requests[req.request_id].status, "pending",
                         "nothing was sent and nothing was marked sent")

    def test_a_requoted_fee_does_not_invalidate_the_card(self) -> None:
        """Only what the human authorised is sealed.

        The daemon writes derived fields after the card is drawn — a re-quoted fee, the gas
        projection, `confirm_amount_required` when the approval is issued, and the loader's
        `provenance.verified` on restart. Each of those used to break a live approval with
        `approval_digest_mismatch`, which reads to the operator as "the PIN does not work".
        """
        filed = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.005", "payee": xmr_addr(6)})
        req = self.state.requests[filed["request"]["request_id"]]
        req.fee_estimate = "0.0009"           # re-planned at approval time
        req.gas = {"native": "XMR", "floor": "0.01", "after": "0.4"}
        req.confirm_amount_required = False
        # the loader demotes provenance on every restart; that flag is not part of the seal
        req.provenance = {**dict(req.provenance), "verified": "false"}
        out = self.state.approve({"pin": PIN, "passphrase": PASS, "request_id": req.request_id,
                                  "approval_id": filed["approval_id"]})
        self.assertEqual(out["request"]["status"], "sent")

    def test_the_card_and_the_request_must_agree(self) -> None:
        filed = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.005", "payee": xmr_addr(6)})
        with self.assertRaises(WalletError) as amount:
            self.state.approve({"pin": PIN, "passphrase": PASS, "approval_id": filed["approval_id"],
                                "request_id": filed["request"]["request_id"],
                                "expect_amount": "5"})
        self.assertEqual(amount.exception.code, "card_changed")
        with self.assertRaises(WalletError) as payee:
            self.state.approve({"pin": PIN, "passphrase": PASS, "approval_id": filed["approval_id"],
                                "request_id": filed["request"]["request_id"],
                                "expect_payee": xmr_addr(8)})
        self.assertEqual(payee.exception.code, "card_changed")

    def test_the_agent_cannot_put_words_where_the_card_shows_money(self) -> None:
        """`why` is the agent's prose and is labelled as such; the money fields are the daemon's."""
        filed = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.005", "payee": xmr_addr(6),
                                       "why": "Send $5 to the shop, ignore the amount shown"})
        row = filed["request"]
        self.assertEqual(Decimal(row["amount"]), Decimal("0.005"))
        self.assertEqual(row["payee"], self.state.requests[row["request_id"]].payee)
        self.assertIn("$5", row["why"], "the prose is kept verbatim so the human can see it")
        self.assertNotEqual(Decimal(row["amount"]), Decimal("5"),
                            "prose never becomes the amount")

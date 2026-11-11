"""Prompt-injection battery: can an agent make money leave to an address nobody blessed?

Every test here is written from the *adversary's* seat. The agent's model has been injected, it can
call any agent-scope method with any parameters it likes, and it wants a payment to its own address.
The invariant under test is one sentence:

    Funds may only leave to a destination a human approved for that purpose.

The tests run against a real `State` with the in-memory chain fixture, and they drive agent-scope
calls the way the RPC does (`_scope: "agent"`, injected server-side from the token). The same
ground truth as the live daemon, without the network.
"""

import os
import tempfile
import unittest
from unittest import mock
from decimal import Decimal
from pathlib import Path

from tests.test_state import (FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState,
                              xmr_addr)

PIN = "123456"   # the per-payment secret; every approval needs it

#: A valid stagenet address that no human blessed. The attack is not "an invalid address" (that is
#: refused on format); it is a perfectly payable address the operator never approved.
ATTACKER = xmr_addr(97)
#: a valid *testnet* address: the fixture chains are testnets, and a mainnet bech32 is refused on
#: format (which would make the test pass for the wrong reason)
ATTACKER_BTC = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
AGENT = {"_scope": "agent"}


class InjectionTests(unittest.TestCase):
    def _approve_with_pin(self, out: dict) -> dict:
        """Finish a payment the way the pane does: read the card, type the PIN."""
        return self.state.approve({"pin": PIN, "request_id": out["request"]["request_id"],
                                   "approval_id": out["approval_id"],
                                   "confirm_amount": out["request"]["amount"]})

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
        # prefer-Monero ships off now; these injection tests still exercise it, so turn it on
        self.state.settings.prefer_monero = True
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = self.state.fake

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- fixture: a funded budget, the way a human would create one ----------
    def funded_sandbox(self, budget: str = "0.2", payees=None) -> str:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": budget, "lifetime_hours": 24,
                                          "label": "budget", "allowed_payees": payees or []})
        funding = made["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": budget})
        return made["sandbox"]["sandbox_id"]

    # ---------- the attack: redirect a budget ----------
    def test_agent_cannot_bless_its_own_destination_when_asking_for_a_budget(self) -> None:
        made = self.state.sandbox_propose({"kind": "monero-stagenet", "asset": "XMR",
                                           "budget": "0.2", "label": "research",
                                           "allowed_payees": [ATTACKER],
                                           "allowed_profiles": ["anything"]})
        sandbox = self.state.sandboxes[made["sandbox"]["sandbox_id"]]
        self.assertEqual(sandbox.allowed_payees, [])
        self.assertEqual(sandbox.allowed_profiles, [])
        self.assertIn("allowed_payees", made.get("ignored", []))

    def test_agent_cannot_launder_its_payee_through_the_address_book(self) -> None:
        """Save the attacker as "Aiden" (agent scope), then spend: still needs the human."""
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": ATTACKER},
                                 **AGENT})
        sandbox_id = self.funded_sandbox()
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        self.assertFalse(out.get("auto"), out)
        self.assertEqual(out["decision"]["code"], "needs_operator")

    def test_agent_cannot_overwrite_a_human_contact_address(self) -> None:
        """The swap attack: the contact is real, the address is replaced by the agent."""
        self.state.contact_save({"name": "Vendor", "addresses": {"monero-stagenet": xmr_addr(9)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Vendor",
                                     "addresses": {"monero-stagenet": ATTACKER}, **AGENT})
        self.assertIn(caught.exception.code, ("passphrase_required", "bad_contact"))

    def test_agent_cannot_spend_a_budget_to_an_unblessed_address(self) -> None:
        sandbox_id = self.funded_sandbox()
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        self.assertFalse(out.get("auto"), out)
        self.assertNotEqual(out["decision"].get("rule"), "sandbox")

    def test_agent_can_still_spend_to_a_blessed_destination(self) -> None:
        """The control: the same call succeeds when a human allowed the address."""
        sandbox_id = self.funded_sandbox(payees=[ATTACKER])
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        # blessed, but still a payment: the pane draws the card and asks for the PIN
        self.assertEqual(out["decision"]["code"], "needs_pin")
        sent = self._approve_with_pin(out)
        self.assertEqual(sent["request"]["status"], "sent")
        self.assertEqual(sent["request"]["authorised_by"], "pin")

    # ---------- the attack: fund somebody else's address ----------
    def test_agent_cannot_use_funding_for_to_pay_an_attacker(self) -> None:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 24})
        sid = made["sandbox"]["sandbox_id"]
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.2",
                                   "payee": ATTACKER, "funding_for": sid, **AGENT})
        self.assertEqual(caught.exception.code, "funding_mismatch")

    def test_funding_must_be_the_whole_budget(self) -> None:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 24})
        sandbox = self.state.sandboxes[made["sandbox"]["sandbox_id"]]
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.001",
                                   "payee": sandbox.address, "funding_for": sandbox.sandbox_id,
                                   **AGENT})
        self.assertEqual(caught.exception.code, "funding_mismatch")

    def test_only_the_operator_can_bless_a_destination_after_funding(self) -> None:
        """The proposal may not name payees, so the operator needs a setter — with the passphrase."""
        sandbox_id = self.funded_sandbox()
        with self.assertRaises(WalletError) as caught:
            self.state.sandbox_set_payees({"sandbox_id": sandbox_id, "allowed_payees": [ATTACKER]})
        self.assertEqual(caught.exception.code, "passphrase_required")
        blessed = self.state.sandbox_set_payees({"sandbox_id": sandbox_id,
                                                 "allowed_payees": [ATTACKER],
                                                 "passphrase": PASS})
        self.assertEqual(blessed["sandbox"]["allowed_payees"], [ATTACKER])
        # and now the agent may pay it without asking, which is the grant the human just made
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        self.assertEqual(out["decision"]["code"], "needs_pin",
                         "even a blessed destination asks for the PIN")

    def test_clearing_the_allowlist_needs_no_passphrase(self) -> None:
        sandbox_id = self.funded_sandbox(payees=[ATTACKER])
        cleared = self.state.sandbox_set_payees({"sandbox_id": sandbox_id, "allowed_payees": []})
        self.assertEqual(cleared["sandbox"]["allowed_payees"], [])

    def test_a_mistyped_sandbox_id_cannot_spend_the_main_balance(self) -> None:
        """An id that names no budget must refuse, not fall through to account 0.

        Returning "no sandbox decision" dropped the budget and the destination allowlist and planned
        the payment from the wallet's main account: the same 0.10 was denied under the real id
        (budget 0.05) and accepted under a one-character typo.
        """
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                                     "payee": ATTACKER, "sandbox_id": "sbx_does_not_exist", **AGENT})
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "unknown_sandbox")
        self.assertFalse(out.get("auto"))
        self.assertFalse(out.get("approval_id"), "an unknown sandbox must not even be filed")

    def test_an_agent_edit_does_not_demote_a_human_blessed_contact(self) -> None:
        """A blessing belongs to the human who made it; adding a chain must not revoke it."""
        # blessing costs the passphrase now: creating an operator contact is the blessing itself
        self.state.contact_save({"name": "Mum", "addresses": {"monero-stagenet": ATTACKER},
                                 "_scope": "operator", "passphrase": PASS})
        saved = [c for c in self.state.contacts.values() if c.name == "Mum"][0]
        self.assertEqual(saved.created_by, "operator")
        # re-saving the same address with a new note needs no passphrase, and that was enough to
        # demote it — the address never changed, so nothing looked dangerous
        self.state.contact_save({"name": "Mum", "addresses": {"monero-stagenet": ATTACKER},
                                 "notes": "note", **AGENT})
        again = [c for c in self.state.contacts.values() if c.name == "Mum"][0]
        self.assertEqual(again.created_by, "operator",
                         "an agent edit demoted a human contact, silently disabling the blessing")
        # and it still blesses: a sandbox with no allowlist may pay it without asking
        sandbox_id = self.funded_sandbox()
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        self.assertNotEqual(out["request"]["status"], "denied", "the blessing still applies")
        self.assertEqual(out["decision"]["code"], "needs_pin")

    def test_an_agent_cannot_claim_it_verified_the_page(self) -> None:
        """The card prints "address read from <host>". Only `pay_url`, which fetched the page, may
        set that — otherwise an injected agent writes "openrouter.ai" onto its own request."""
        out = self.state.request_tx({
            "kind": "monero-stagenet", "asset": "XMR", "amount": "0.01", "payee": ATTACKER,
            "provenance": {"url": "https://openrouter.ai/credits", "host": "openrouter.ai",
                           "title": "Credits", "method": "402", "verified": "true"},
            **AGENT})
        self.assertEqual(out["request"]["provenance"]["verified"], "false")

    def test_provenance_read_from_disk_is_not_trusted(self) -> None:
        """requests.json is plain JSON under a writable directory: a stored `verified: true` is a claim
        nobody signed, so a restart must not turn it into fact on the card."""
        import json
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": ATTACKER, **AGENT})
        request_id = out["request"]["request_id"]
        self.state._save_requests()
        rows = json.loads(self.state.requests_path.read_text())
        for row in rows if isinstance(rows, list) else rows.get("requests", []):
            if row.get("request_id") == request_id:
                row["provenance"] = {"url": "https://shop.example/x", "host": "shop.example",
                                     "verified": "true"}
        self.state.requests_path.write_text(json.dumps(rows))
        again = WalletState(self.home)
        again.fake = FakeAdapter()
        again.unlock(PASS)
        again._load_requests()
        loaded = again.requests[request_id]
        self.assertEqual(loaded.provenance.get("verified"), "false")

    # ---------- the attack: end the budget, or revive it ----------
    def test_agent_cannot_sweep_a_budget_home(self) -> None:
        sandbox_id = self.funded_sandbox()
        own = self.state.fake.address()
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                   "payee": own, "sandbox_id": sandbox_id, "sweep_of": sandbox_id,
                                   **AGENT})
        self.assertEqual(caught.exception.code, "sweep_operator_only")

    def test_a_revoked_budget_cannot_be_revived_or_spent(self) -> None:
        sandbox_id = self.funded_sandbox()
        self.state.sandbox_revoke({"sandbox_id": sandbox_id, "sweep": False})
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": ATTACKER, "sandbox_id": sandbox_id, **AGENT})
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_closed")

    def test_an_expired_budget_cannot_be_funded_after_the_fact(self) -> None:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 1})
        sandbox = self.state.sandboxes[made["sandbox"]["sandbox_id"]]
        sandbox.expires_at = "2000-01-01T00:00:00Z"
        funding = made["funding"]
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                                "approval_id": funding["approval_id"], "passphrase": PASS,
                                "confirm_amount": "0.2"})
        self.assertEqual(caught.exception.code, "sandbox_expired")
        self.assertFalse(sandbox.enabled)

    # ---------- the attack: reach money by another door ----------
    def test_agent_scope_cannot_reach_any_authorising_method(self) -> None:
        """The structural guard: money-moving authorisation is not on the agent's surface at all."""
        from walletd.state import build_methods
        methods = build_methods()
        must_be_operator = ("approve", "deny", "unlock", "lock", "config_set", "export_seed",
                            "create_keystore", "wallet_import", "sandbox_create", "sandbox_revoke",
                            "sandbox_destroy", "sandbox_sweep", "sandbox_set_profiles",
                            "mandates_create", "mandates_update", "mandates_activate",
                            "mandates_delete", "set_human_gate", "ledger_archive", "signer_install",
                            "signer_start", "signer_stop", "sandbox_set_unattended", "sandbox_arm",
                            "sandbox_set_payees")
        from walletd.tokens import AGENT, OPERATOR
        # an equality, not a subset: a new money-moving method added to the agent surface fails here
        agent_surface = {name for name, (scope, _) in methods.items() if scope == AGENT}
        self.assertEqual(agent_surface, {
            "status", "chain_status", "balances", "nodes", "config_get", "requests_list",
            "request_tx", "pay_url", "receive_address", "incoming", "orders_close", "spend_totals",
            "ledger_tail", "ledger_csv", "ledger_verify", "contacts_list", "contact_save",
            "merchants", "mandates_list", "mandates_propose", "sandboxes_list", "sandbox_propose",
            "cancel_request", "verify_payment", "readiness", "request_operator_prompt",
            "signer_status", "doctor", "pin_status", "deposits_list",
            # removing authority is always allowed to the agent: revoking its own autopay only ever
            # takes power away. Granting one (`mandates_propose`) leaves it inert until a human
            # activates it with the passphrase.
            "mandates_revoke",
            # asking for more money on a budget it already has: files a funding request and moves
            # nothing. Arming (`sandbox_set_unattended`) is operator-only and is in the list above.
            "sandbox_topup",
            # swapping, reviewed: `swap_quote` and `swap_routers` are view calls (a rate and a list of
            # contracts pinned in code — no signing, no filing). `swap_request` files a card and does
            # nothing else: the router is not a parameter it can set, the minimum out comes from the
            # operator's slippage policy, the caps and the gas floor still apply, and only a PIN
            # converts. `suggest_swaps` off refuses this path to the agent outright.
            "swap_quote", "swap_routers", "swap_request",
        }, "the agent surface changed: review what the new method can do")
        for name in must_be_operator:
            self.assertIn(name, methods, f"{name} is not registered at all")
            self.assertEqual(methods[name][0], OPERATOR, f"{name} is reachable with the agent token")

    def test_agent_cannot_approve_its_own_request(self) -> None:
        filed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                       "payee": ATTACKER, "why": "invoice", **AGENT})
        # the approval method is operator-scope, so an agent call is refused by the RPC, not by this
        # method's body. Assert both: the scope table, and that the approval token alone is not enough
        # without the passphrase.
        # the approval token alone buys nothing: the daemon wants the PIN the human types
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"], "confirm_amount": "0.01"})
        self.assertEqual(caught.exception.code, "pin_required")

    def test_a_payee_from_another_chain_is_refused(self) -> None:
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": ATTACKER_BTC, "why": "x", **AGENT})
        self.assertFalse(out["decision"]["allow"])
        self.assertIn(out["decision"]["code"], ("invalid_payee", "payee_format"))

    def test_an_identical_retry_cannot_pay_twice(self) -> None:
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": ATTACKER, "why": "invoice", "idempotency_key": "same-key", **AGENT}
        first = self.state.request_tx(dict(params))
        second = self.state.request_tx(dict(params))
        self.assertTrue(first["decision"]["allow"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["request"]["request_id"], first["request"]["request_id"])
        pending = [r for r in self.state.requests.values() if r.status == "pending"]
        self.assertEqual(len(pending), 1)

    def test_prefer_monero_cannot_be_used_to_dodge_the_reason(self) -> None:
        """An injected agent paying in another coin still has to say why — and the why is recorded."""
        out = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC", "amount": "0.0001",
                                     "payee": ATTACKER_BTC, **AGENT})
        self.assertEqual(out["decision"]["code"], "prefer_monero_needs_reason")
        with_reason = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                             "amount": "0.0001", "payee": ATTACKER_BTC,
                                             "why": "this shop only takes BTC", **AGENT})
        self.assertEqual(with_reason["decision"]["code"], "needs_operator")

    def test_pay_url_carries_the_agents_scope_into_policy(self) -> None:
        """A 402 purchase filed by the agent must be judged as agent-initiated.

        `pay_url` builds its own request dict; it used to drop `_scope`, so the policy engine read
        the purchase as operator-initiated and the Monero preference never applied.
        """
        from walletd import pay402

        quote = pay402.Quote(url="https://seller.example/report", scheme="exact",
                             network="base-mainnet", kind="bitcoin-testnet", asset="BTC",
                             amount=Decimal("0.0001"), payee=ATTACKER_BTC, resource="/report",
                             description="a report", digest="d" * 64, raw={"scheme": "exact"})
        with mock.patch.object(pay402, "fetch", return_value=quote):
            out = self.state.pay_url({"url": "https://seller.example/report", **AGENT})
        self.assertEqual(out["decision"]["code"], "prefer_monero_needs_reason")

    # ---------- the attack: point the wallet at an internal service ----------
    def test_pay_url_refuses_private_addresses_for_the_agent(self) -> None:
        """SSRF: the agent must not be able to make the daemon fetch an internal service.

        The `AGENT_WALLET_ALLOW_PRIVATE_402` escape hatch is for the unit tests that serve a 402
        from loopback; a real daemon never sets it. Clear it here so this asserts the shipping
        behaviour rather than whatever the previous test left in the environment.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
            with self.assertRaises(WalletError) as caught:
                self.state.pay_url({"url": "http://127.0.0.1:9119/api/status", **AGENT})
        self.assertEqual(caught.exception.code, "bad_url")

    def test_pay_url_loopback_is_operator_only(self) -> None:
        # the agent asking for loopback is ignored: `_scope` comes from the token, not the caller
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
            with self.assertRaises(WalletError) as caught:
                self.state.pay_url({"url": "http://127.0.0.1:1/x", "allow_loopback": True, **AGENT})
        self.assertEqual(caught.exception.code, "bad_url")


if __name__ == "__main__":
    unittest.main()

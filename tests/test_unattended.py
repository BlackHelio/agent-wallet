"""An unattended budget: the agent acts alone *inside* the money it was given.

The product sentence this file pins down:

    A human fills the budget. Inside it the agent spends freely, to anyone, without a PIN.
    The moment it wants more, a human decides again.

That is the whole point of a budget — otherwise the PIN makes it a checkout form — and it is also
the sharpest thing an operator can hand over, so every test here is a boundary: what the agent can
do inside (spend, to any destination, until the money is gone) and what it must never do (arm
itself, extend itself, raise its own ceiling, or reach past the budget into the main wallet).

The chain is the in-memory fixture, so "sent" means the policy engine allowed the broadcast, not
that a node accepted it.
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.test_state import (FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState,
                              xmr_addr)

PIN = "123456"
AGENT = {"_scope": "agent"}
#: a payable address no human ever blessed — the injected agent's own wallet
ATTACKER = xmr_addr(2)


class UnattendedBudgetTests(unittest.TestCase):
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
        self.state.settings.pin_every_payment = True          # the shipped default
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = self.state.fake

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- fixtures ----------
    def _fund(self, funding: dict) -> dict:
        """Approve a funding transfer the way the pane does: read the card, type the PIN."""
        request = funding["request"]
        self.assertEqual(request["status"], "pending", request)
        return self.state.approve({"pin": PIN, "passphrase": PASS,
                                   "request_id": request["request_id"],
                                   "approval_id": funding["approval_id"],
                                   "confirm_amount": request["amount"]})

    def budget(self, budget: str = "0.2", unattended: bool = True, payees=None) -> str:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": budget, "lifetime_hours": 24,
                                          "label": "task", "allowed_payees": payees or [],
                                          "unattended": unattended,
                                          "passphrase": PASS, "pin": PIN})
        sandbox_id = made["sandbox"]["sandbox_id"]
        self._fund(made["funding"])
        return sandbox_id

    def assertNeedsHuman(self, out: dict) -> None:
        # the daemon reports `allow: true` with `needs_operator` for anything a human may still
        # approve, so the honest assertion is "nothing was sent, it is waiting for a person"
        self.assertNotIn(out["decision"]["code"], ("sandbox_ok", ""), out["decision"])
        self.assertEqual(out["request"]["status"], "pending", out["request"])

    def spend(self, sandbox_id: str, amount: str, payee: str = ATTACKER) -> dict:
        return self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                      "amount": amount, "payee": payee,
                                      "sandbox_id": sandbox_id, "why": "inside the budget"})

    # ---------- what the hand-over buys ----------
    def test_the_agent_spends_inside_an_armed_budget_with_no_pin(self) -> None:
        sandbox_id = self.budget("0.05")
        out = self.spend(sandbox_id, "0.01")
        self.assertTrue(out["decision"]["allow"], out["decision"])
        self.assertEqual(out["request"]["status"], "sent")
        self.assertEqual(out["request"]["authorised_by"], "sandbox")
        self.assertGreaterEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0.01"))

    def test_an_armed_budget_pays_an_address_nobody_blessed(self) -> None:
        """The ceiling is the containment. Requiring a blessing here would make the arm a no-op."""
        sandbox_id = self.budget("0.05")
        out = self.spend(sandbox_id, "0.01", payee=ATTACKER)
        self.assertTrue(out["decision"]["allow"], out["decision"])
        self.assertEqual(out["request"]["status"], "sent")
        self.assertFalse(self.state.contacts, "the test must not rely on a saved contact")

    def test_an_armed_budget_can_be_spent_until_it_is_empty(self) -> None:
        sandbox_id = self.budget("0.05")
        for _ in range(3):
            out = self.spend(sandbox_id, "0.01")
            self.assertEqual(out["request"]["status"], "sent", out["decision"])
        self.assertLess(self.state.sandboxes[sandbox_id].remaining(), Decimal("0.02"),
                        "three payments (and their fees) must have left the budget")

    # ---------- what it does not ----------
    def test_without_the_arm_every_payment_still_asks_for_the_pin(self) -> None:
        sandbox_id = self.budget("0.05", unattended=False)
        self.assertNeedsHuman(self.spend(sandbox_id, "0.01"))

    def test_the_main_wallet_is_untouched_by_the_arm(self) -> None:
        self.budget("0.05")
        out = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.01", "payee": ATTACKER, "why": "errand"})
        self.assertNeedsHuman(out)

    def test_the_agent_cannot_arm_its_own_budget(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.sandbox_propose({"kind": "monero-stagenet", "asset": "XMR",
                                        "budget": "0.1", "label": "mine", "unattended": True})
        self.assertEqual(caught.exception.code, "unattended_needs_operator")

    def test_arming_costs_the_passphrase_and_the_pin(self) -> None:
        sandbox_id = self.budget("0.05", unattended=False)
        with self.assertRaises(WalletError) as no_pass:
            self.state.sandbox_set_unattended({"sandbox_id": sandbox_id, "on": True, "pin": PIN})
        self.assertEqual(no_pass.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as no_pin:
            self.state.sandbox_set_unattended({"sandbox_id": sandbox_id, "on": True,
                                               "passphrase": PASS})
        self.assertEqual(no_pin.exception.code, "pin_required")
        self.assertFalse(self.state.sandboxes[sandbox_id].unattended)
        out = self.state.sandbox_set_unattended({"sandbox_id": sandbox_id, "on": True,
                                                 "passphrase": PASS, "pin": PIN})
        self.assertTrue(out["unattended"])
        self.assertTrue(self.state.sandboxes[sandbox_id].unattended)

    def test_disarming_is_free_and_takes_effect_at_once(self) -> None:
        sandbox_id = self.budget("0.05")
        self.state.sandbox_set_unattended({"sandbox_id": sandbox_id, "on": False})
        self.assertNeedsHuman(self.spend(sandbox_id, "0.01"))

    def test_a_revoked_budget_can_not_be_armed_or_spent(self) -> None:
        sandbox_id = self.budget("0.05")
        self.state.sandbox_revoke({"sandbox_id": sandbox_id})
        self.assertFalse(self.state.sandboxes[sandbox_id].unattended)
        out = self.spend(sandbox_id, "0.01")
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_closed")

    def test_an_expired_budget_is_refused_and_can_not_be_armed(self) -> None:
        sandbox_id = self.budget("0.05", unattended=False)
        self.state.sandboxes[sandbox_id].expires_at = "2020-01-01T00:00:00Z"
        with self.assertRaises(WalletError) as expired:
            self.state.sandbox_set_unattended({"sandbox_id": sandbox_id, "on": True,
                                               "passphrase": PASS, "pin": PIN})
        self.assertEqual(expired.exception.code, "sandbox_expired")
        out = self.spend(sandbox_id, "0.01")
        self.assertEqual(out["decision"]["code"], "sandbox_expired")

    def test_an_exhausted_budget_names_the_remedy_and_sends_nothing(self) -> None:
        sandbox_id = self.budget("0.02")
        out = self.spend(sandbox_id, "0.02")            # all of it, but the fee comes out of it too
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_budget")
        self.assertIn("wallet_request_sandbox", out["decision"]["reason"])
        self.assertEqual(out["request"]["status"], "denied")
        self.assertEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0"))

    def test_a_narrowed_list_still_decides_even_when_armed(self) -> None:
        """`--pay-to` is the operator saying "inside this budget, only these"."""
        blessed = xmr_addr(1)
        sandbox_id = self.budget("0.1", unattended=True, payees=[blessed])
        self.assertNeedsHuman(self.spend(sandbox_id, "0.01", payee=ATTACKER))
        ok = self.spend(sandbox_id, "0.01", payee=blessed)
        self.assertTrue(ok["decision"]["allow"], ok["decision"])

    def test_the_arm_survives_a_restart(self) -> None:
        sandbox_id = self.budget("0.05")
        reloaded = WalletState(self.home)
        reloaded.fake = FakeAdapter()          # the fixture adapter, as every test here builds it
        reloaded.settings.chains_enabled = list(TEST_CHAINS)
        reloaded.settings.assets_enabled = list(TEST_ASSETS)
        reloaded.settings.pin_every_payment = True
        reloaded.adapters["monero-stagenet"] = self.state.fake
        reloaded.unlock(PASS)
        self.assertTrue(reloaded.sandboxes[sandbox_id].unattended,
                        "a restart must not silently re-arm the PIN gate inside a budget")

    def test_a_pooled_budget_says_the_number_is_the_only_containment(self) -> None:
        """Bitcoin has no sub-accounts: arming one is allowed, but nobody may think it is custody.

        Built by hand because the fixture adapter claims `supports_accounts` for every chain, which
        makes `sandbox_create(mode="pooled")` refuse. The row is what a real Bitcoin budget looks
        like: account_index -1, no funding transfer, containment by policy.
        """
        from walletd.state import Sandbox, now_iso
        pooled = Sandbox(sandbox_id="sbx_pooled01", kind="bitcoin-testnet", asset="BTC",
                         account_index=-1, address="", budget=Decimal("0.02"), spent=Decimal("0"),
                         created_at=now_iso(), expires_at="2099-01-01T00:00:00Z", label="pooled",
                         mode="pooled", enabled=True)
        self.state.sandboxes["sbx_pooled01"] = pooled
        self.state._save_sandboxes()
        out = self.state.sandbox_set_unattended({"sandbox_id": "sbx_pooled01", "on": True,
                                                 "passphrase": PASS, "pin": PIN})
        self.assertTrue(out["unattended"])
        self.assertEqual(out["custody"], "pooled")
        self.assertIn("pooled", (out.get("warning") or "").lower())

    # ---------- asking for more ----------
    def test_the_agent_asks_for_more_and_the_human_says_yes(self) -> None:
        sandbox_id = self.budget("0.02")
        asked = self.state.sandbox_topup({**AGENT, "sandbox_id": sandbox_id, "amount": "0.03",
                                          "why": "needed for the second order"})
        self.assertEqual(asked["funding"]["request"]["status"], "pending",
                         "asking must never move money by itself")
        self.assertEqual(self.state.sandboxes[sandbox_id].budget, Decimal("0.02"))
        self._fund(asked["funding"])
        self.assertEqual(self.state.sandboxes[sandbox_id].budget, Decimal("0.05"))
        self.assertEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0"),
                         "a top-up raises the ceiling, it does not count as spending")
        out = self.spend(sandbox_id, "0.04")
        self.assertTrue(out["decision"]["allow"], out["decision"])

    def test_a_top_up_redirected_to_another_address_raises_no_ceiling(self) -> None:
        """requests.json is unsigned, so assume an attacker edits the payee after the card was drawn.

        The approval is bound to the request's digest, so the edit invalidates the card before any
        money moves; and the funding path itself refuses a transfer that is not to the sandbox's own
        address. Either refusal is fine. A raised budget is not.
        """
        sandbox_id = self.budget("0.02")
        asked = self.state.sandbox_topup({**AGENT, "sandbox_id": sandbox_id, "amount": "0.03"})
        req = self.state.requests[asked["funding"]["request"]["request_id"]]
        req.payee = ATTACKER
        with self.assertRaises(WalletError):
            self.state.approve({"pin": PIN, "passphrase": PASS, "request_id": req.request_id,
                                "approval_id": asked["funding"]["approval_id"],
                                "confirm_amount": req.amount})
        self.assertEqual(self.state.sandboxes[sandbox_id].budget, Decimal("0.02"),
                         "a redirected funding transfer must not raise the budget")

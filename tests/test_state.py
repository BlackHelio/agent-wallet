from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from walletd import config, keystore
from walletd import state as state_module
from walletd.chains.base import ChainAdapter, ChainError, TxPlan
from walletd.policy import Mandate
from walletd.state import State, WalletError

PASS = "test-passphrase-123"
# The product now defaults to real chains. Tests are explicit about the nets they assert on, so a
# default change never silently rewrites what the suite is proving.
TEST_CHAINS = ["monero-stagenet", "sim-mainnet", "bitcoin-testnet", "base-sepolia"]
TEST_ASSETS = ["XMR", "BTC", "ETH", "POL", "USDC"]
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class FakeAdapter(ChainAdapter):
    """In-memory chain double: plans and 'broadcasts' without a network.

    Production binds MoneroAdapter; these tests are about policy, approvals and receipts, so the
    chain step is a fixture rather than the subject.
    """

    kind = "monero-stagenet"
    asset = "XMR"
    supports_accounts = True

    def __init__(self) -> None:
        self.broadcast_count = 0
        self.planned: list = []
        self.fee = Decimal("0.00002")

    def available(self) -> bool:
        return True

    def status(self):
        return {"kind": self.kind, "available": True, "height": 1}

    def address(self) -> str:
        # a valid *stagenet* address: the fixture is bound to monero-stagenet, and a mainnet-shaped
        # address here made every sweep fail validation once sandbox spends were policed
        return "57" + "1" * 93

    def balance(self):
        return Decimal("10")

    def plan_transfer(self, amount, payee, account_index=0, asset=""):
        self.planned.append((amount, payee, account_index))
        return TxPlan(kind=self.kind, asset="XMR", amount=amount, payee=payee,
                      fee=self.fee, raw={"tx_blob": "deadbeef", "tx_hash": "aa" * 32,
                                         "tx_key": "bb" * 32})

    def payment_proof(self, txid, tx_key, address):
        return {"received": False, "amount": "0", "confirmations": 0, "in_pool": False,
                "reason": "transaction not found"}

    def broadcast(self, plan):
        self.broadcast_count += 1
        # model the chain: a transfer out of an account lowers that account's balance, which is what
        # "the sweep emptied it" means to the code that decides whether to close a sandbox
        amount, _payee, account = self.planned[-1]
        held = FakeAdapter.balances.get(account)
        if held is not None:
            FakeAdapter.balances[account] = max(Decimal("0"), held - Decimal(str(amount)))
        return {"tx_hash": plan.raw["tx_hash"], "tx_key": plan.raw["tx_key"],
                "fee": str(plan.fee), "confirmations": 0}

    # sub-accounts (sandboxes)
    supports_accounts = True
    _next_account = 1

    def create_account(self, label):
        index = FakeAdapter._next_account
        FakeAdapter._next_account += 1
        # distinct per account, so a test can tell two purchases apart
        # 95 chars: prefix + version nibble + 93 body — a real stagenet shape, distinct per account
        return {"account_index": index, "address": "5" + str(index) + "1" * 93}

    def accounts(self):
        return [{"account_index": 0, "address": "57" + "1" * 93, "balance": "10", "label": "main"}]

    # a per-account balance, so a sweep can be tested against what is really in the account
    balances: dict = {}

    def balance_at(self, account_index):
        return FakeAdapter.balances.get(account_index, Decimal("0"))

    def set_balance(self, account_index, amount):
        FakeAdapter.balances[account_index] = Decimal(str(amount))

    def mnemonic(self):
        return " ".join(["test"] * 25)


class WalletState(State):
    """State whose adapter map always contains the in-memory chain fixture."""

    def _build_adapters(self) -> None:
        super()._build_adapters()
        self.adapters[self.fake.kind] = self.fake


def xmr_addr(offset: int) -> str:
    body = (ALPHABET[offset:] + ALPHABET[:offset]) * 3
    return "57" + body[:93]


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1          # each test starts from a clean account counter
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS)
        # pin the test nets: the product defaults to real chains now, and this suite asserts on stagenet
        self._pin_chains()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pin_chains(self) -> None:
        """Write the test chain list into the *signed* settings.

        `unlock()` reloads settings from disk, so assigning to the in-memory object is not enough —
        the first unlock would put the product defaults back and every request would be denied.
        """
        from walletd import config as config_mod

        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)

    def _chain(self) -> FakeAdapter:
        return self.state.fake

    def _approve(self, request_id: str, approval_id: str, amount: str | None = None) -> dict:
        params = {"request_id": request_id, "approval_id": approval_id, "passphrase": PASS}
        if amount is not None:
            params["confirm_amount"] = amount
        return self.state.approve(params)

    # ---------- basics ----------

    def test_initialize_writes_keystore_and_ledger(self) -> None:
        self.assertTrue((self.home / "keystore.json").exists())
        self.assertTrue((self.home / "ledger.jsonl").exists())
        ok, message, count = self.state.ledger.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 1)

    def test_locked_wallet_refuses_to_spend(self) -> None:
        self.state.lock()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(1)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "unattended_no_operator")

    def test_lock_clears_material(self) -> None:
        self.state.lock()
        self.assertIsNone(self.state.master)
        self.assertIsNone(self.state.payload)
        self.assertFalse(self.state.is_unlocked())

    def test_unlock_rejects_wrong_passphrase(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.unlock("not-the-passphrase")
        self.assertEqual(caught.exception.code, "bad_passphrase")

    # ---------- manual approval path ----------

    def test_manual_flow_approve(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(2)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": addr, "why": "api call"})
        self.assertTrue(result["decision"]["allow"])
        approval_id = result["approval_id"]
        approved = self._approve(result["request"]["request_id"], approval_id)
        self.assertEqual(approved["request"]["status"], "sent")
        self.assertTrue(approved["signed"])
        self.assertEqual(len(approved["tx_hash"]), 64)
        ok, message, _ = self.state.ledger.verify()
        self.assertTrue(ok, message)

    def test_approval_replay_refused(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(3)})
        payload = {"request_id": result["request"]["request_id"], "approval_id": result["approval_id"]}
        self._approve(payload["request_id"], payload["approval_id"])
        with self.assertRaises(WalletError) as caught:
            self._approve(payload["request_id"], payload["approval_id"])
        self.assertEqual(caught.exception.code, "approval_replayed")

    def test_amount_confirmation_required_above_threshold(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.2", "payee": xmr_addr(4)})
        base = {"request_id": result["request"]["request_id"], "approval_id": result["approval_id"]}
        with self.assertRaises(WalletError) as caught:
            self._approve(base["request_id"], base["approval_id"])
        self.assertEqual(caught.exception.code, "confirm_amount_required")
        with self.assertRaises(WalletError) as caught:
            self._approve(base["request_id"], base["approval_id"], "0.02")
        self.assertEqual(caught.exception.code, "confirm_amount_required")
        approved = self._approve(base["request_id"], base["approval_id"], "0.2")
        self.assertEqual(approved["request"]["status"], "sent")

    def test_per_tx_cap_denied(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "5", "payee": xmr_addr(5)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "per_tx_cap")
        last = self.state.ledger.last()
        self.assertEqual(last["decision"], "denied")

    def test_invalid_payee_denied(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "not-an-address"})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "invalid_payee")

    def test_disabled_chain_denied(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.chains_enabled = ["monero-stagenet"]
        result = self.state.request_tx({"kind": "base-sepolia", "asset": "USDC",
                                        "amount": "0.1", "payee": "0x" + "ab" * 20})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "chain_not_enabled")

    # ---------- mandates ----------

    def _mandate(self, addr: str) -> dict:
        proposal = self.state.mandates_propose({
            "kind": "monero-stagenet", "asset": "XMR", "payee": addr,
            "per_tx_cap": "0.2", "period_cap": "0.3", "absolute_cap": "1",
            "period_hours": 24, "label": "api vendor",
        })
        return proposal["mandate"]

    def test_mandate_proposal_is_inert_until_activated(self) -> None:
        addr = xmr_addr(7)
        mandate = self._mandate(addr)
        self.assertEqual(mandate["status"], "revoked")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": addr})
        self.assertIn("approval_id", result)   # manual path, not auto

    def test_activated_mandate_auto_approves_then_hits_period_cap(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(8)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})

        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15", "payee": addr})
        self.assertTrue(first.get("auto"), first)
        self.assertEqual(first["request"]["status"], "sent")
        self.assertTrue(first["signed"])

        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.2", "payee": addr})
        self.assertFalse(second["decision"]["allow"])
        self.assertEqual(second["decision"]["code"], "period_cap")

    def test_mandate_requires_exact_payee(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(9)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})
        lookalike = xmr_addr(10)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": lookalike})
        self.assertFalse(result.get("auto"))
        self.assertIn("approval_id", result)

    def test_revoked_mandate_stops_auto_pay(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(11)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})
        self.state.mandates_revoke({"mandate_id": mandate["mandate_id"], "reason": "vendor changed"})
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": addr})
        self.assertFalse(result.get("auto"))
        self.assertIn("approval_id", result)

    def test_mandate_cannot_exceed_lifetime_horizon(self) -> None:
        with self.assertRaises(ValueError):
            Mandate.parse({"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(12),
                           "per_tx_cap": "0.1", "period_cap": "0.1", "absolute_cap": "0.1",
                           "expires_at": "2099-01-01T00:00:00Z"}, self.state.settings)

    # ---------- unattended ----------

    def test_unattended_run_denies_with_receipt(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(13)}, unattended=True)
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "unattended_no_operator")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    # ---------- chain execution ----------

    def test_chain_error_denies_before_approval(self) -> None:
        from walletd.chains.base import ChainError

        class Broke(FakeAdapter):
            def plan_transfer(self, amount, payee, account_index=0, asset=""):
                raise ChainError("insufficient_funds", "not enough unlocked XMR")

        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = Broke()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(30)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "insufficient_funds")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    def test_broadcast_failure_is_recorded(self) -> None:
        from walletd.chains.base import ChainError
        from walletd.state import WalletError

        class Flaky(FakeAdapter):
            def broadcast(self, plan):
                raise ChainError("rpc_error", "relay_tx failed")

        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = Flaky()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(31)})
        with self.assertRaises(WalletError) as caught:
            self._approve(result["request"]["request_id"], result["approval_id"])
        self.assertEqual(caught.exception.code, "broadcast_failed")
        self.assertEqual(self.state.ledger.last()["details"]["code"], "rpc_error")


    # ---------- the promise: no passphrase, no transaction ----------

    def _pending(self, amount: str = "0.001") -> dict:
        """A request an agent filed and a human has not approved yet."""
        self.state.unlock(PASS)
        return self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": amount, "payee": xmr_addr(70),
                                      "why": "test payment"})

    def test_a_locked_wallet_cannot_sign_anything(self) -> None:
        """The physical guarantee: locked means no key in memory, so nothing can be broadcast."""
        pending = self._pending()
        request_id = pending["request"]["request_id"]
        approval_id = pending["approval_id"]
        before = self.state.fake.broadcast_count
        self.state.lock()
        with self.assertRaises(WalletError) as caught:
            self._approve(request_id, approval_id, "0.001")
        self.assertIn(caught.exception.code, ("locked", "passphrase_required"))
        self.assertEqual(self.state.fake.broadcast_count, before, "a locked wallet signed a transfer")

    def test_approval_without_the_passphrase_never_reaches_the_signer(self) -> None:
        pending = self._pending()
        before = self.state.fake.broadcast_count
        for passphrase in ("", "not-the-passphrase"):
            with self.assertRaises(WalletError) as caught:
                self.state.approve({"request_id": pending["request"]["request_id"],
                                    "approval_id": pending["approval_id"],
                                    "passphrase": passphrase, "confirm_amount": "0.001"})
            self.assertEqual(caught.exception.code, "passphrase_required")
        self.assertEqual(self.state.fake.broadcast_count, before)
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].authorised_by, "")

    def test_broadcast_refuses_a_request_nobody_authorised(self) -> None:
        """Defence in depth: even a direct call to the signing step refuses an unauthorised request."""
        pending = self._pending()
        req = self.state.requests[pending["request"]["request_id"]]
        plan = self.state.plans.get(req.request_id)
        before = self.state.fake.broadcast_count
        with self.assertRaises(WalletError) as caught:
            self.state._broadcast(req, plan, self.state.fake)
        self.assertEqual(caught.exception.code, "not_authorised")
        self.assertEqual(self.state.fake.broadcast_count, before)

    def test_authorised_by_records_which_authority_signed(self) -> None:
        pending = self._pending()
        self._approve(pending["request"]["request_id"], pending["approval_id"], "0.001")
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].authorised_by,
                         "passphrase")

    def test_a_mandate_spend_still_needs_an_unlocked_wallet(self) -> None:
        """Standing authority is not a key: with the wallet locked there is nothing to sign with."""
        self.state.unlock(PASS)
        created = self.state.mandates_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "payee": xmr_addr(71), "per_tx_cap": "0.01",
                                             "period_cap": "0.05", "absolute_cap": "0.05",
                                             "interval": "24h", "passphrase": PASS})
        self.assertEqual(created["mandate"]["status"], "active")
        before = self.state.fake.broadcast_count
        self.state.lock()
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.001",
                                     "payee": xmr_addr(71)})
        self.assertFalse(out.get("auto"), out)
        self.assertEqual(self.state.fake.broadcast_count, before,
                         "a mandate signed a payment while the wallet was locked")
        self.assertEqual((out.get("request") or {}).get("tx_hash", ""), "",
                         "a locked mandate spend produced a transaction hash")

    def test_the_gate_off_still_requires_an_unlocked_wallet(self) -> None:
        """Turning the prompt off removes the approval, not the key: a locked daemon still cannot sign."""
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        pending = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.001", "payee": xmr_addr(72)})
        before = self.state.fake.broadcast_count
        self.state.lock()
        with self.assertRaises(WalletError):
            self.state.approve({"request_id": pending["request"]["request_id"],
                                "approval_id": pending["approval_id"]})
        self.assertEqual(self.state.fake.broadcast_count, before)

    # ---------- sandboxes ----------

    def _funded_sandbox(self, budget: str = "0.2") -> dict:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                            "budget": budget, "lifetime_hours": 24,
                                            "label": "test sandbox"})
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], budget)
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    def test_sandbox_is_unfunded_until_the_transfer_is_approved(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                            "budget": "0.2", "lifetime_hours": 24})
        self.assertEqual(created["sandbox"]["status"], "unfunded")
        spend = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                       "payee": xmr_addr(40), "sandbox_id": created["sandbox"]["sandbox_id"]})
        self.assertFalse(spend["decision"]["allow"])
        self.assertEqual(spend["decision"]["code"], "sandbox_unfunded")

    def test_sandbox_spends_without_approval_and_stops_at_the_budget(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.assertEqual(sandbox["status"], "active")
        self.assertTrue(self.state.sandboxes[sandbox["sandbox_id"]].enabled)

        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15",
                                       "payee": xmr_addr(41), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(first.get("auto"), first)
        self.assertEqual(first["decision"]["rule"], "sandbox")
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].spent, Decimal("0.15"))

        over = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15",
                                      "payee": xmr_addr(42), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(over["decision"]["allow"])
        self.assertEqual(over["decision"]["code"], "sandbox_budget")

    # ---------- one-time sandboxes destroy themselves ----------

    def _one_time(self, budget: str = "0.2", destruct: int = 60) -> dict:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": budget, "lifetime_hours": 24,
                                             "mode": "one_time", "self_destruct_seconds": destruct,
                                             "label": "single payment"})
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], budget)
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    def test_one_time_sandbox_takes_one_payment_and_arms_the_clock(self) -> None:
        sandbox = self._one_time("0.2")
        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                                       "payee": xmr_addr(50), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(first.get("auto"), first)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(live.destroy_at, "the clock must start when the payment settles")
        self.assertEqual(live.spent, Decimal("0.1"))
        self.assertLessEqual(live.destroy_in(), 60)

        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                        "payee": xmr_addr(51), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(second["decision"]["allow"])
        self.assertEqual(second["decision"]["code"], "sandbox_one_time_used")

    def test_budget_sandbox_never_arms_a_destruct_clock(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(52), "sandbox_id": sandbox["sandbox_id"]})
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].destroy_at, "")
        self.assertEqual(self.state.reap_sandboxes(), [])

    def test_self_destruct_waits_while_the_sandbox_still_holds_funds(self) -> None:
        """The single most important guard: keys go last, money goes home first."""
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(53), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0.15")   # still funded
        outcome = self.state.reap_sandboxes()
        self.assertFalse(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.destroyed_at, "")
        self.assertEqual(live.destroy_blocked, "sweep_pending")
        self.assertGreater(self.state.fake.broadcast_count, 0, "it should try to sweep before giving up")

    def test_self_destruct_sweeps_then_destroys_and_records_it(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(54), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0")
        outcome = self.state.reap_sandboxes()
        self.assertTrue(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.status(), "destroyed")
        self.assertFalse(live.enabled)
        receipts = [e for e in self.state.ledger.entries()
                    if e["kind"] == "sandbox" and e["decision"] == "destroyed"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["details"]["sandbox_id"], sandbox["sandbox_id"])
        self.assertIn("keys", receipts[0]["details"])
        # a destroyed sandbox cannot be spent from again
        after = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                       "payee": xmr_addr(55), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(after["decision"]["allow"])

    def test_failed_sweep_keeps_the_sandbox_alive(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(56), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0.15")

        def broke(*args, **kwargs):
            raise ChainError("rpc_unreachable", "node is down")

        self.state.fake.plan_transfer = broke
        outcome = self.state.reap_sandboxes()
        self.assertFalse(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.destroyed_at, "")
        self.assertTrue(live.destroy_blocked.startswith("sweep_failed"), live.destroy_blocked)

    def test_reaper_does_nothing_while_locked(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(57), "sandbox_id": sandbox["sandbox_id"]})
        self.state.sandboxes[sandbox["sandbox_id"]].destroy_at = "2000-01-01T00:00:00Z"
        self.state.lock()
        self.assertEqual(self.state.reap_sandboxes(), [])
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].destroyed_at, "")

    def test_revoked_sandbox_stops_spending(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.15")
        self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"]})
        spend = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                       "payee": xmr_addr(43), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(spend["decision"]["allow"])
        self.assertEqual(spend["decision"]["code"], "sandbox_unfunded")

    def test_sandbox_profile_acl(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        self.assertEqual(created["sandbox"]["allowed_profiles"], ["wallet"])
        blocked = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                         "payee": xmr_addr(50), "sandbox_id": sandbox_id,
                                         "profile": "default"})
        self.assertFalse(blocked["decision"]["allow"])
        self.assertEqual(blocked["decision"]["code"], "sandbox_profile")

    def test_sandbox_profile_acl_allows_the_named_profile_after_funding(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], "0.2")
        allowed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                         "payee": xmr_addr(51), "sandbox_id": sandbox_id,
                                         "profile": "wallet"})
        self.assertTrue(allowed.get("auto"), allowed)

    # ---------- recovery ----------

    def test_seed_export_needs_confirm_and_passphrase(self) -> None:
        from walletd.state import WalletError

        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.export_seed({"passphrase": PASS, "kind": "monero-stagenet"})
        self.assertEqual(caught.exception.code, "confirm_required")

        with self.assertRaises(WalletError) as caught:
            self.state.export_seed({"passphrase": "wrong", "confirm": True, "kind": "monero-stagenet"})
        self.assertEqual(caught.exception.code, "bad_passphrase")

        result = self.state.export_seed({"passphrase": PASS, "confirm": True, "kind": "monero-stagenet"})
        self.assertEqual(result["words"], 25)
        self.assertNotIn("test test", str(self.state.ledger_tail({"limit": 1})))

    def test_backup_roundtrip(self) -> None:
        from walletd import backup

        self.state.unlock(PASS)
        out = Path(self.tmp.name) / "backup.json"
        created = backup.create(self.home, out, "backup-passphrase")
        self.assertTrue(created["ok"])
        self.assertIn("keystore.json", created["files"])

        target = Path(self.tmp.name) / "restored"
        restored = backup.restore(out, target, "backup-passphrase")
        self.assertIn("keystore.json", restored["restored"])

        from walletd import keystore
        payload = keystore.open_sealed(keystore.load(target / "keystore.json"), PASS)
        self.assertIn("master", payload)

    def test_backup_refuses_wrong_passphrase(self) -> None:
        from walletd import backup, keystore

        self.state.unlock(PASS)
        out = Path(self.tmp.name) / "backup.json"
        backup.create(self.home, out, "right-passphrase")
        with self.assertRaises(keystore.InvalidPassphrase):
            backup.restore(out, Path(self.tmp.name) / "x", "wrong-passphrase")


    def test_a_funded_sandbox_blocks_removing_its_coin(self) -> None:
        """Removing the coin hides the adapter, and an unswept budget would be stranded forever."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.2")
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"assets_enabled": ["BTC"]})
        self.assertEqual(caught.exception.code, "sandbox_active")
        self.assertIn("XMR", self.state.settings.assets_enabled)

    def test_sandbox_sweep_returns_the_remainder_and_closes(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(60), "sandbox_id": sandbox["sandbox_id"]})
        # the account really holds the 0.15 that is left: the sweep reads the chain, not the books
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.15")
        result = self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        self.assertEqual(Decimal(result["swept"]), Decimal("0.1495"))   # 0.15 minus the fee reserve
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertFalse(after.enabled)
        self.assertEqual(after.spent, after.budget)
        self.assertTrue(self.state.ledger.verify()[0])


    # ---------- second-review regressions ----------

    def test_a_partial_sweep_does_not_close_the_sandbox(self) -> None:
        """Only an account that is actually empty gets closed; otherwise the rest is stranded."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        # the account keeps its money: the fake does not model the transfer out in this test
        self.state.fake.balance_at = lambda index: Decimal("0.15")
        self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(after.enabled, "a sweep of 0.05 must not close a 0.15 account")
        partial = [e for e in self.state.ledger.entries()
                   if e["kind"] == "sandbox" and e["decision"] == "swept_partial"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0]["details"]["left"], "0.15")

    def test_a_sweep_is_not_policed_as_a_spend(self) -> None:
        """A tightened per-tx cap must not be able to wedge a teardown for ever."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.2")
        self.state.config_set({"per_tx_cap": {"XMR": "0.01", "BTC": "0.001", "ETH": "0.05", "USDC": "25"},
                               "passphrase": PASS})
        result = self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        self.assertIn("swept", result)
        self.assertEqual(result["sweep_rule"], "sweep")      # asserts the key exists, no fallback

    def test_deny_still_works_after_the_approval_window(self) -> None:
        pending = self._pending()
        approval = self.state.approvals[pending["approval_id"]]
        approval.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.state.deny({"request_id": pending["request"]["request_id"],
                         "approval_id": pending["approval_id"], "reason": "too late"})
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].status, "denied")

    def test_adding_a_new_chain_to_a_contact_needs_the_passphrase(self) -> None:
        """A new chain on an existing contact is an address the human has never seen."""
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(90)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden",
                                     "addresses": {"bitcoin-mainnet": "bc1q9wytzayn38z0u0kgms7hanv5vz33y98tspuycq"}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        ok = self.state.contact_save({"name": "Aiden", "passphrase": PASS,
                                      "addresses": {"bitcoin-mainnet": "bc1q9wytzayn38z0u0kgms7hanv5vz33y98tspuycq"}})
        self.assertIn("bitcoin-mainnet", ok["contact"]["addresses"])

    def test_a_proposal_cannot_overwrite_an_existing_mandate(self) -> None:
        self.state.unlock(PASS)
        created = self.state.mandates_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "payee": xmr_addr(91), "per_tx_cap": "0.01",
                                             "period_cap": "0.05", "absolute_cap": "0.05",
                                             "interval": "24h", "passphrase": PASS})
        mandate_id = created["mandate"]["mandate_id"]
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_propose({"mandate_id": mandate_id, "kind": "monero-stagenet",
                                         "asset": "XMR", "payee": xmr_addr(92),
                                         "per_tx_cap": "0.001", "period_cap": "0.001",
                                         "absolute_cap": "0.001"})
        self.assertEqual(caught.exception.code, "mandate_exists")
        self.assertTrue(self.state.mandates[mandate_id].enabled)

    # ---------- third-review regressions ----------

    def test_the_signer_endpoint_cannot_be_repointed_without_the_passphrase(self) -> None:
        """It decides which address the human is told to fund, and it receives the signer's creds."""
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"monero_rpc_url": "http://127.0.0.1:9/json_rpc"})
        self.assertEqual(caught.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"monero_rpc_url": "https://attacker.example/json_rpc",
                                   "passphrase": PASS})
        self.assertEqual(caught.exception.code, "bad_setting")
        self.assertIn("local signer", caught.exception.message)

    def test_an_unreadable_balance_does_not_close_a_sandbox(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]

        def broken(_index):
            raise ChainError("rpc_unreachable", "node is down")

        self.state.fake.balance_at = broken
        self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(after.enabled, "an unreadable balance was treated as 'swept'")
        self.assertNotEqual(after.spent, after.budget)

    def test_a_dead_request_stops_being_approvable(self) -> None:
        pending = self._pending()
        row = pending["request"]
        old = self.state.requests[row["request_id"]]
        old.created_at = "2000-01-01T00:00:00Z"          # well past any approval window
        listed = self.state.requests_list()["requests"]
        found = [r for r in listed if r["request_id"] == row["request_id"]][0]
        self.assertNotIn("approval_id", found,
                         "a request older than the approval window must not be re-issued an approval")

    # ---------- contacts ----------

    def test_contact_save_validates_the_address(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": "nope"}})
        self.assertEqual(caught.exception.code, "bad_contact")

    def test_sending_to_a_contact_name_resolves_the_address(self) -> None:
        self.state.unlock(PASS)
        address = xmr_addr(70)
        saved = self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": address},
                                         "notes": "flatmate"})
        self.assertTrue(saved["created"])
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "aiden"})
        self.assertTrue(result["decision"]["allow"])
        self.assertEqual(result["request"]["payee"], address)
        self.assertIn("Aiden", result["request"]["memo"])

    def test_contact_without_that_chain_is_a_clear_error(self) -> None:
        self.state.unlock(PASS)
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(71)}})
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                   "amount": "0.001", "payee": "Aiden"})
        self.assertEqual(caught.exception.code, "contact_chain_missing")

    def test_unknown_name_is_denied_with_a_receipt(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "nobody"})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "invalid_payee")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    # ---------- coin preference ----------

    def test_monero_only_refuses_other_assets(self) -> None:
        # a real pairing: USDC lives on the EVM chain, not on a Monero one, and the mismatch is now
        # rejected before policy even runs
        self.state.unlock(PASS)
        self.state.settings.monero_only = True
        result = self.state.request_tx({"kind": "base-sepolia", "asset": "USDC",
                                        "amount": "1", "payee": "0x" + "1" * 40})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "monero_only")

    def test_a_coin_that_does_not_belong_to_the_chain_is_refused(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "USDC",
                                   "amount": "1", "payee": xmr_addr(73)})
        self.assertEqual(caught.exception.code, "asset_chain_mismatch")

    def test_the_chain_supplies_the_default_asset(self) -> None:
        """Name a chain and omit the asset: you get that chain's own coin, not prefer_asset."""
        self.state.unlock(PASS)
        self.state.settings.prefer_asset = "XMR"
        out = self.state.request_tx({"kind": "bitcoin-testnet", "amount": "0.0001",
                                     "payee": "bcrt1q" + "1" * 30})
        self.assertEqual(out["request"]["asset"], "BTC")

    def test_asset_auto_uses_the_preference(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.prefer_asset = "XMR"
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "AUTO",
                                        "amount": "0.01", "payee": xmr_addr(73)})
        self.assertEqual(result["request"]["asset"], "XMR")

    def test_queued_approvals_cannot_multiply_the_daily_cap(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.daily_cap = {"XMR": "1.0"}
        self.state.settings.per_tx_cap = {"XMR": "10"}
        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.6", "payee": xmr_addr(80)})
        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.6", "payee": xmr_addr(81)})
        self._approve(first["request"]["request_id"], first["approval_id"], "0.6")
        with self.assertRaises(WalletError) as caught:
            self._approve(second["request"]["request_id"], second["approval_id"], "0.6")
        self.assertEqual(caught.exception.code, "daily_cap")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    def test_sandbox_with_empty_profile_is_denied(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], "0.2")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(82),
                                        "sandbox_id": sandbox_id})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "sandbox_profile")

    def test_mandates_survive_a_restart(self) -> None:
        self.state.unlock(PASS)
        proposed = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                                "payee": xmr_addr(90), "per_tx_cap": "0.01",
                                                "period_cap": "0.1", "period_hours": 24,
                                                "absolute_cap": "1"})
        mandate_id = proposed["mandate"]["mandate_id"]
        self.state.mandates_activate({"mandate_id": mandate_id, "passphrase": PASS})

        revived = State(self.state.home)
        revived.unlock(PASS)
        listed = {m["mandate_id"]: m for m in revived.mandates_list()["mandates"]}
        self.assertIn(mandate_id, listed)
        self.assertEqual(listed[mandate_id]["status"], "active")

    def test_adapters_use_the_keystore_password_not_a_plaintext_file(self) -> None:
        """The wallet-file password lives in the sealed keystore, not in wallet.env.

        It used to be plaintext on disk (0600, but readable by anything running as this user). The
        keystore copy wins, so an env file — stale, copied, or written by an attacker — cannot make
        the wallet open a different wallet file.
        """
        self.state.unlock(PASS)
        stored = self.state._signer_password()
        self.assertTrue(stored, "the keystore should carry a wallet-file password")
        (self.state.home / "wallet.env").write_text("AGENT_WALLET_MONERO_PASSWORD=attacker-value\n",
                                                    encoding="utf-8")
        passwords: list[str] = []

        class Spy:
            def __init__(self, *args, **kwargs):
                passwords.append(args[3] if len(args) > 3 else kwargs.get("wallet_password", ""))
                self.kind = args[0]

            def available(self) -> bool:
                return True

        disabled = os.environ.pop("WALLETD_DISABLE_CHAINS", None)
        original = state_module.XmrAdapter
        state_module.XmrAdapter = Spy  # type: ignore[assignment]
        try:
            self.state._build_adapters()
        finally:
            state_module.XmrAdapter = original  # type: ignore[assignment]
            if disabled is not None:
                os.environ["WALLETD_DISABLE_CHAINS"] = disabled

        self.assertTrue(passwords, "no adapter was built")
        self.assertEqual(set(passwords), {stored})
        self.assertNotIn("attacker-value", passwords)

    def test_each_purchase_gets_its_own_address(self) -> None:
        self.state.unlock(PASS)
        first = self.state.receive_address({"label": "order 1"})
        second = self.state.receive_address({"label": "order 2"})
        self.assertNotEqual(first["address"], second["address"])
        self.assertFalse(first["reused"])
        self.assertEqual([first["account_index"], second["account_index"]], [1, 2])

    def test_incoming_joins_each_order_to_what_arrived(self) -> None:
        """The receipt knows what the address was for; the chain knows the money. Join them."""
        self.state.unlock(PASS)
        partial = self.state.receive_address({"label": "order 1", "expected": "0.5"})
        paid = self.state.receive_address({"label": "order 2"})
        waiting = self.state.receive_address({"label": "order 3", "expected": "1"})
        self.state.fake.accounts = lambda: [
            {"account_index": partial["account_index"], "address": partial["address"],
             "balance": "0.2", "unlocked": "0.2", "label": "order 1"},
            {"account_index": paid["account_index"], "address": paid["address"],
             "balance": "0.01", "unlocked": "0.01", "label": "order 2"},
            {"account_index": waiting["account_index"], "address": waiting["address"],
             "balance": "0", "unlocked": "0", "label": "order 3"},
        ]
        self.state.ledger.append("payment_proof", "checked", "agent",
                                 {"kind": "monero-stagenet", "address": paid["address"],
                                  "txid": "cd" * 32, "tx_hash": "ab" * 32})
        payload = self.state.incoming()
        orders = {row["label"]: row for row in payload["orders"]}
        self.assertEqual(orders["order 1"]["status"], "partial")
        self.assertEqual(orders["order 1"]["received"], "0.2")
        self.assertEqual(orders["order 1"]["expected"], "0.5")
        self.assertEqual(orders["order 2"]["status"], "paid")
        self.assertEqual(orders["order 3"]["status"], "waiting")
        self.assertEqual(orders["order 1"]["asset"], "XMR")
        self.assertEqual(orders["order 1"]["account_index"], partial["account_index"])
        self.assertEqual(orders["order 2"]["last_tx"], "ab" * 32)
        self.assertEqual(orders["order 3"]["last_tx"], "")

    def test_incoming_does_not_hide_an_account_the_chain_no_longer_lists(self) -> None:
        self.state.unlock(PASS)
        opened = self.state.receive_address({"label": "vanished"})
        self.state.fake.accounts = lambda: []
        rows = self.state.incoming()["orders"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "waiting")
        self.assertEqual(rows[0]["address"], opened["address"])

    def test_incoming_degrades_when_the_chain_has_no_subaddresses(self) -> None:
        self.state.unlock(PASS)
        self.state.fake.supports_accounts = False
        self.state.adapters = {self.state.fake.kind: self.state.fake}
        payload = self.state.incoming()
        self.assertEqual(payload["orders"], [])
        self.assertIn("subaddress", payload["note"])

    def test_cancel_withdraws_only_a_pending_request_and_receipts_it(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(97)})
        request_id = made["request"]["request_id"]
        out = self.state.cancel_request({"request_id": request_id, "reason": "changed my mind"})
        self.assertEqual(out["request"]["status"], "cancelled")
        last = list(self.state.ledger.entries())[-1]
        self.assertEqual((last["kind"], last["decision"], last["rule"]),
                         ("spend_request", "cancelled", "agent"))
        self.assertEqual(last["details"]["request_id"], request_id)
        self.assertEqual(last["details"]["code"], "cancelled_by_agent")
        # the cancelled request's approval token is dead, so a card left open cannot approve it
        with self.assertRaises(WalletError) as caught:
            self._approve(request_id, made["approval_id"])
        self.assertIn(caught.exception.code, ("approval_replayed", "request_not_pending"))
        # and cancelling it twice is refused, not a second receipt
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": request_id})
        self.assertEqual(caught.exception.code, "not_cancellable")

    def test_cancelling_an_approved_request_is_refused(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(98)})
        self._approve(made["request"]["request_id"], made["approval_id"])
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": made["request"]["request_id"]})
        self.assertEqual(caught.exception.code, "not_cancellable")

    def test_cancelling_an_unknown_request_is_a_clear_error(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": "nope"})
        self.assertEqual(caught.exception.code, "unknown_request")

    def test_a_cancelled_idempotency_key_does_not_pay_on_retry(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(99), "idempotency_key": "cancel-then-retry"}
        made = self.state.request_tx(dict(params))
        self.state.cancel_request({"request_id": made["request"]["request_id"]})
        again = self.state.request_tx(dict(params))
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(again["request"]["status"], "cancelled")

    def test_a_wrong_tx_key_is_reported_as_not_received(self) -> None:
        self.state.unlock(PASS)
        proof = self.state.verify_payment({"txid": "ab" * 32, "tx_key": "cd" * 32,
                                           "address": "85" + "1" * 93})
        self.assertFalse(proof["received"])
        self.assertEqual(proof["amount"], "0")

    # ---------- what an operator token is not allowed to do ----------

    def test_activating_a_mandate_needs_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                            "payee": xmr_addr(70), "per_tx_cap": "0.01",
                                            "period_cap": "0.1", "period_hours": 24,
                                            "absolute_cap": "1"})
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_activate({"mandate_id": made["mandate"]["mandate_id"]})
        self.assertEqual(caught.exception.code, "passphrase_required")
        # with the passphrase it goes through
        self.state.mandates_activate({"mandate_id": made["mandate"]["mandate_id"], "passphrase": PASS})
        self.assertEqual(self.state.mandates[made["mandate"]["mandate_id"]].status(), "active")

    def test_caps_cannot_be_raised_without_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"per_tx_cap": {"XMR": "999"}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.config_set({"per_tx_cap": {"XMR": "999"}, "passphrase": PASS})
        self.assertEqual(self.state.settings.per_tx_cap["XMR"], "999")
        # a coin toggle is a preference, not policy
        self.state.config_set({"chains_enabled": ["monero-stagenet"]})

    def test_deleting_receipts_is_detected(self) -> None:
        self.state.unlock(PASS)
        for index in range(3):
            self.state.ledger.append("test", "note", "test", {"i": index})
        self.assertTrue(self.state.ledger_verify()["ok"])
        path = self.state.ledger_path
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        result = self.state.ledger_verify()
        self.assertFalse(result["ok"])
        self.assertIn("recorded", result["message"])

    def test_a_sandbox_still_obeys_the_allowlist(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.config_set({"payee_allowlist": [xmr_addr(10)], "passphrase": PASS})
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(61),
                                       "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(result["decision"]["allow"])

    def test_funding_a_sandbox_at_the_wrong_address_does_not_arm_it(self) -> None:
        self.state.unlock(PASS)
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 24})
        sandbox_id = made["sandbox"]["sandbox_id"]
        other = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.2", "payee": xmr_addr(62),
                                       "funding_for": sandbox_id})
        self._approve(other["request"]["request_id"], other["approval_id"], "0.2")
        self.assertFalse(self.state.sandboxes[sandbox_id].enabled)

    def test_a_fee_above_the_cap_is_refused(self) -> None:
        self.state.unlock(PASS)
        self._chain().fee = Decimal("0.5")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(63)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "fee_too_high")

    def test_an_amount_below_the_asset_precision_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                   "amount": "0.0000000000001", "payee": xmr_addr(64)})
        self.assertEqual(caught.exception.code, "bad_amount")

    def test_changing_a_contact_address_needs_the_passphrase(self) -> None:
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden",
                                     "addresses": {"monero-stagenet": xmr_addr(40)}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.contact_save({"name": "Aiden",
                                 "addresses": {"monero-stagenet": xmr_addr(40)},
                                 "passphrase": PASS})
        receipt = self.state.ledger.last()
        self.assertEqual(receipt["decision"], "saved")
        self.assertIn("changed", receipt["details"])

    # ---------- retries must not pay twice ----------

    def test_the_same_idempotency_key_returns_the_same_request(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(90), "idempotency_key": "buy-42"}
        first = self.state.request_tx(dict(params))
        second = self.state.request_tx(dict(params))
        self.assertEqual(first["request"]["request_id"], second["request"]["request_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["approval_id"], first["approval_id"])
        pending = [r for r in self.state.requests.values() if r.status == "pending"]
        self.assertEqual(len(pending), 1)

    def test_a_retry_after_a_restart_still_deduplicates(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(91), "idempotency_key": "buy-43"}
        first = self.state.request_tx(dict(params))

        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        again = revived.request_tx(dict(params))
        self.assertEqual(again["request"]["request_id"], first["request"]["request_id"])
        self.assertTrue(again["duplicate"])

    def test_a_retry_after_payment_does_not_pay_again(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(92), "idempotency_key": "buy-44"}
        made = self.state.request_tx(dict(params))
        self._approve(made["request"]["request_id"], made["approval_id"], "0.01")
        broadcasts = self._chain().broadcast_count
        again = self.state.request_tx(dict(params))
        self.assertEqual(again["request"]["request_id"], made["request"]["request_id"])
        self.assertEqual(self._chain().broadcast_count, broadcasts, "a retry broadcast again")

    def test_different_keys_are_different_payments(self) -> None:
        self.state.unlock(PASS)
        base = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01", "payee": xmr_addr(93)}
        first = self.state.request_tx({**base, "idempotency_key": "a"})
        second = self.state.request_tx({**base, "idempotency_key": "b"})
        self.assertNotEqual(first["request"]["request_id"], second["request"]["request_id"])

    def test_a_backup_carries_every_state_file(self) -> None:
        from walletd import backup

        self.state.unlock(PASS)
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(4),
                                     "per_tx_cap": "0.01", "period_cap": "0.1", "period_hours": 24,
                                     "absolute_cap": "1"})
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                               "payee": xmr_addr(5), "idempotency_key": "backup-1"})
        out = Path(self.tmp.name) / "state-backup.json"
        created = backup.create(self.home, out, "backup-passphrase")
        for name in ("contacts.json", "mandates.json", "requests.json", "idempotency.json",
                     "ledger.anchor"):
            self.assertIn(name, created["files"], f"{name} was left out of the backup")
        restored = Path(self.tmp.name) / "restored-state"
        backup.restore(out, restored, "backup-passphrase")
        for name in ("contacts.json", "mandates.json", "requests.json", "idempotency.json"):
            self.assertTrue((restored / name).exists(), f"{name} was not restored")

    # ---------- autopay lifecycle ----------

    def _mandate_params(self, **over) -> dict:
        params = {"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(70),
                  "per_tx_cap": "0.01", "period_cap": "0.05", "absolute_cap": "0.2",
                  "interval": "1d", "label": "rent", "activate": True, "passphrase": PASS}
        params.update(over)
        return params

    def test_autopay_can_be_created_edited_paused_resumed_and_deleted(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.assertEqual(made["mandate"]["status"], "active")
        self.assertEqual(made["mandate"]["period_hours"], 24)

        edited = self.state.mandates_update({"mandate_id": mandate_id, "interval": "6h",
                                             "period_cap": "0.02", "passphrase": PASS})
        self.assertEqual(edited["mandate"]["period_hours"], 6)
        self.assertEqual(Decimal(edited["mandate"]["period_cap"]), Decimal("0.02"))

        paused = self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})
        self.assertEqual(paused["mandate"]["status"], "revoked")
        resumed = self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True,
                                                   "passphrase": PASS})
        self.assertEqual(resumed["mandate"]["status"], "active")

        # survives a restart, then deletes cleanly
        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        self.assertIn(mandate_id, {m["mandate_id"] for m in revived.mandates_list()["mandates"]})
        revived.mandates_delete({"mandate_id": mandate_id, "passphrase": PASS})
        self.assertNotIn(mandate_id, revived.mandates)
        after = WalletState(self.state.home)
        after.fake = FakeAdapter()
        after.unlock(PASS)
        self.assertNotIn(mandate_id, after.mandates)

    def test_autopay_creation_and_resume_need_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_create(self._mandate_params(passphrase=None))
        self.assertEqual(caught.exception.code, "passphrase_required")

        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True})
        self.assertEqual(caught.exception.code, "passphrase_required")
        # pausing only removes authority, so it needs nothing
        self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})

    def test_autopay_intervals_parse(self) -> None:
        from walletd.policy import parse_interval

        self.assertEqual(parse_interval("6h"), 6)
        self.assertEqual(parse_interval("2d"), 48)
        self.assertEqual(parse_interval("weekly"), 168)
        self.assertEqual(parse_interval(12), 12)
        self.assertEqual(parse_interval(None), 24)
        with self.assertRaises(ValueError):
            parse_interval("every other tuesday")

    def test_autopay_caps_are_validated_on_edit(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_update({"mandate_id": mandate_id, "period_cap": "5",
                                        "absolute_cap": "1", "passphrase": PASS})
        self.assertEqual(caught.exception.code, "bad_mandate")

    def test_an_expired_autopay_cannot_be_resumed(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.state.mandates[mandate_id].expires_at = "2020-01-01T00:00:00Z"
        self.state.mandates[mandate_id].enabled = False
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True,
                                             "passphrase": PASS})
        self.assertEqual(caught.exception.code, "mandate_expired")

    # ---------- clearing the receipt view ----------

    def test_archiving_history_keeps_the_old_chain_verifiable(self) -> None:
        self.state.unlock(PASS)
        for index in range(4):
            self.state.ledger.append("test", "note", "test", {"i": index})
        before = self.state.ledger.last()["seq"]

        result = self.state.ledger_archive({"passphrase": PASS})
        self.assertEqual(result["through_seq"], before)

        # the view is clean, and the new chain starts by pointing at what came before
        self.assertEqual(self.state.ledger.last()["seq"], 1)
        self.assertEqual(self.state.ledger.last()["details"]["archived_through_seq"], before)

        # the archive is on disk and still verifies with the same public key
        archive = self.state.home / result["archived"]
        self.assertTrue(archive.exists())
        from walletd.ledger import verify_file
        ok, reason, count = verify_file(archive, self.state.ledger.pubkey_hex)
        self.assertTrue(ok, reason)
        self.assertEqual(count, before)

        # and the live chain still verifies (anchor included)
        self.assertTrue(self.state.ledger_verify()["ok"])

    def test_archiving_history_needs_no_passphrase(self) -> None:
        """It archives rather than deletes: the record survives, so there is nothing to authorise."""
        before = self.state.ledger.last()["seq"]
        self.state.ledger.append("test", "note", "test", {})      # works while locked, as before
        result = self.state.ledger_archive({})
        self.assertEqual(result["through_seq"], before + 1)
        self.assertEqual(self.state.ledger.last()["kind"], "ledger")

    # ---------- turning the passphrase prompt off ----------

    def test_turning_the_prompt_off_needs_the_passphrase_again(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "confirm": True})
        self.assertEqual(caught.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": "wrong-one"})
        self.assertEqual(caught.exception.code, "bad_passphrase")
        # and it needs the explicit confirmation, even with the right passphrase
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": PASS})
        self.assertEqual(caught.exception.code, "confirm_required")

        result = self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": PASS})
        self.assertFalse(result["human_gate"])
        self.assertFalse(self.state.settings.human_gate)
        self.assertEqual(self.state.ledger.last()["details"]["setting"], "human_gate")

    def test_turning_the_prompt_back_on_removes_authority_so_it_needs_none(self) -> None:
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": PASS})
        result = self.state.set_human_gate({"enabled": True, "confirm": True})
        self.assertTrue(result["human_gate"])
        self.assertTrue(self.state.settings.human_gate)

    # ---------- the passphrase prompt the agent may raise ----------

    def test_an_agent_can_raise_a_prompt_but_never_sees_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(77)})
        request_id = made["request"]["request_id"]

        raised = self.state.request_operator_prompt({"request_id": request_id})
        self.assertEqual(raised["prompt"]["kind"], "approval")
        self.assertEqual(raised["prompt"]["request_id"], request_id)
        self.assertIn("never reaches you", raised["note"])
        self.assertEqual(self.state.status()["prompt"]["request_id"], request_id)

        # approving clears the prompt, and nothing about it carries a secret
        self._approve(request_id, made["approval_id"], "0.01")
        self.assertIsNone(self.state.status()["prompt"])

    def test_a_prompt_for_an_unknown_request_is_refused(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.request_operator_prompt({"request_id": "req_nope"})
        self.assertEqual(caught.exception.code, "unknown_request")

    def test_revoking_a_sandbox_returns_the_leftover_budget(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        sandbox_id = sandbox["sandbox_id"]
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(66), "sandbox_id": sandbox_id})
        live = self.state.sandboxes[sandbox_id]
        self.state.fake.set_balance(live.account_index, "0.15")   # what is really in the account
        out = self.state.sandbox_revoke({"sandbox_id": sandbox_id})
        self.assertTrue(out["sweep"]["attempted"])
        self.assertEqual(Decimal(out["sweep"]["swept"]), Decimal("0.1495"))   # 0.15 minus the fee
        self.assertFalse(out["sandbox"]["enabled"])

    def test_the_sweep_can_be_skipped(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        out = self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"], "sweep": False})
        self.assertFalse(out["sweep"]["attempted"])

    def test_archiving_history_does_not_reset_the_spend_caps(self) -> None:
        """Archiving is a view operation: it must never hand the agent fresh budget."""
        self.state.unlock(PASS)
        from walletd import config as config_mod

        self.state.settings.daily_cap = {"XMR": "2.0"}
        self.state.settings.per_tx_cap = {"XMR": "10"}
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        for offset in (40, 41):
            made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                          "amount": "0.6", "payee": xmr_addr(offset)})
            self._approve(made["request"]["request_id"], made["approval_id"], "0.6")
        self.assertEqual(self.state.ledger.totals("XMR"), Decimal("1.2"))

        self.state.ledger_archive({})
        self.assertEqual(self.state.ledger.totals("XMR"), Decimal("1.2"),
                         "totals lost the archived spend — a cap just reset itself")

        # 1.2 already spent against a 2.0/day cap: another 0.9 would cross it
        next_try = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.9", "payee": xmr_addr(42)})
        self.assertFalse(next_try["decision"]["allow"])
        self.assertEqual(next_try["decision"]["code"], "daily_cap")

    def test_a_pruned_request_still_answers_for_its_idempotency_key(self) -> None:
        """requests.json is pruned to the newest 200; the key must still refuse to pay twice."""
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(95), "idempotency_key": "pruned-1"}
        first = self.state.request_tx(dict(params))

        # simulate the prune that a restart performs on a long-lived wallet
        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        revived.requests.pop(first["request"]["request_id"], None)

        again = revived.request_tx(dict(params))
        self.assertTrue(again.get("duplicate"))
        self.assertNotIn("approval_id", again)
        self.assertEqual(len(revived.requests), 0, "a pruned request's key created a second payment")

    def test_denying_works_while_the_wallet_is_locked(self) -> None:
        """Auto-lock can fire while the human is reading the card; refusing must still work."""
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(96)})
        self.state.lock()
        out = self.state.deny({"request_id": made["request"]["request_id"],
                               "approval_id": made["approval_id"], "reason": "locked but refused"})
        self.assertEqual(out["request"]["status"], "denied")

    def test_deleting_a_contact_needs_the_passphrase(self) -> None:
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_delete({"name": "Aiden"})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.contact_delete({"name": "Aiden", "passphrase": PASS})
        self.assertNotIn("aiden", self.state.contacts)

    def test_a_sandbox_leaves_room_for_its_own_fee(self) -> None:
        """The fee comes out of the sandbox budget, so the last payment must not consume all of it."""
        sandbox = self._funded_sandbox("0.2")
        self.state.settings.fee_cap = {"XMR": "0.0005"}
        # 0.1998 + 0.0005 still fits in 0.2 …
        ok = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1995",
                                    "payee": xmr_addr(67), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(ok["decision"]["allow"], ok["decision"])
        # … but 0.1999 + 0.0005 does not
        refused = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1996",
                                        "payee": xmr_addr(68), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(refused["decision"]["allow"])
        self.assertEqual(refused["decision"]["code"], "sandbox_budget")

    def test_the_wallet_home_is_outside_the_plugin_directory(self) -> None:
        """Removing the plugin must not be able to take the wallet with it."""
        from wallet.client import wallet_home

        plugin_root = Path(__file__).resolve().parents[1]
        home = wallet_home().resolve()
        self.assertFalse(str(home).startswith(str(plugin_root)),
                         "the wallet home lives inside the plugin tree, so `plugins remove` would delete it")

    # ---------- the human gate ----------






    def test_human_gate_cannot_be_disabled_without_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": "wrong", "confirm": True})
        self.assertEqual(caught.exception.code, "bad_passphrase")
        self.assertTrue(self.state.settings.human_gate)

    def test_human_gate_requires_explicit_confirm(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": PASS})
        self.assertEqual(caught.exception.code, "confirm_required")

    def test_human_gate_can_be_disabled_by_operator_with_passphrase(self) -> None:
        self.state.unlock(PASS)
        result = self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        self.assertTrue(result["changed"])
        self.assertFalse(result["human_gate"])
        self.assertTrue(self.state.ledger.verify()[0])

    def test_tampered_config_forces_human_gate_back_on(self) -> None:
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        path = self.home / "config.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["settings"]["human_gate"] = False
        envelope["settings"]["per_tx_cap"]["XMR"] = "10000"
        path.write_text(json.dumps(envelope), encoding="utf-8")

        fresh = State(self.home)
        result = fresh.unlock(PASS)
        self.assertTrue(result["config_tampered"])
        self.assertTrue(fresh.settings.human_gate)
        self.assertEqual(fresh.settings.cap(fresh.settings.per_tx_cap, "XMR"), fresh.settings.cap(config.Settings().per_tx_cap, "XMR"))

    # ---------- the promise: no secrets in responses ----------

    def test_no_secret_material_in_agent_views(self) -> None:
        self.state.unlock(PASS)
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                               "amount": "5", "payee": xmr_addr(14)})
        views = [self.state.status(), self.state.accounts(), self.state.config_get(),
                 self.state.requests_list(), self.state.mandates_list(), self.state.ledger_tail({})]
        allowed = {"ledger_pubkey", "prev", "sig"}
        leaks: list[str] = []

        def walk(node, path="") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in allowed:
                        continue
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str) and len(node) == 64:
                try:
                    int(node, 16)
                except ValueError:
                    return
                leaks.append(f"{path} looks like a hex secret")

        for index, view in enumerate(views):
            walk(view, f"view{index}")
        self.assertEqual(leaks, [], leaks)


if __name__ == "__main__":
    unittest.main()

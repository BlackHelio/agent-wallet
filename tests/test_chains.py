"""In-process signing: the adapters must produce transactions the chain will accept.

No network here. The point is cryptographic: a Bitcoin witness that verifies against the BIP143
sighash, and an EVM transaction whose recovered sender is our own address with the right calldata.
If these pass, "we sign locally" is a fact rather than a claim; only the funding is missing.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from walletd.chains.base import ChainError
from walletd.chains.bitcoin import BtcAdapter, SATS
from walletd.chains.evm import EvmAdapter

SECRET = "11" * 32
OTHER = "0x1234567890abcdef1234567890abcdef12345678"


class BitcoinSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = BtcAdapter("bitcoin-mainnet", SECRET)
        self.adapter._utxos = lambda: [{"txid": "22" * 32, "vout": 1, "value": 100_000, "status": {}}]
        self.adapter._fee_rate = lambda target=6: Decimal("2")

    def test_witness_verifies_against_the_sighash(self) -> None:
        from embit.ec import PublicKey, Signature
        from embit.psbt import PSBT
        from embit.transaction import Transaction, TransactionOutput

        plan = self.adapter.plan_transfer(Decimal("0.0005"), self.adapter.address())
        tx = Transaction.parse(bytes.fromhex(plan.raw["raw_tx"]))
        psbt = PSBT(tx)
        psbt.inputs[0].witness_utxo = TransactionOutput(100_000, self.adapter._script())
        signature, pubkey = tx.vin[0].witness.items
        self.assertTrue(PublicKey.parse(pubkey).verify(Signature.parse(signature[:-1]), psbt.sighash(0)),
                        "the witness does not verify — this transaction would be rejected")

    def test_change_comes_back_and_the_fee_is_believable(self) -> None:
        plan = self.adapter.plan_transfer(Decimal("0.0005"), self.adapter.address())
        from embit.transaction import Transaction

        tx = Transaction.parse(bytes.fromhex(plan.raw["raw_tx"]))
        self.assertEqual(len(tx.vout), 2)
        self.assertEqual(tx.vout[0].value, 50_000)
        self.assertEqual(tx.vout[1].value, 100_000 - 50_000 - int(plan.fee * SATS))
        self.assertLess(plan.fee, Decimal("0.0001"))

    def test_empty_wallet_is_refused_before_signing(self) -> None:
        self.adapter._utxos = lambda: []
        with self.assertRaises(ChainError) as caught:
            self.adapter.plan_transfer(Decimal("0.0005"), self.adapter.address())
        self.assertEqual(caught.exception.code, "insufficient_funds")

    def test_a_bad_address_never_reaches_signing(self) -> None:
        with self.assertRaises(ChainError) as caught:
            self.adapter.plan_transfer(Decimal("0.0005"), "not-an-address")
        self.assertEqual(caught.exception.code, "invalid_payee")


class EvmSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = EvmAdapter("base-sepolia", SECRET)
        # a fake node: right chain id, plenty of gas and tokens
        self.adapter._rpc = lambda method, params: {
            "eth_chainId": hex(84532),
            "eth_blockNumber": hex(1),
            "eth_gasPrice": hex(1_000_000_000),
            "eth_getTransactionCount": hex(7),
            "eth_getBalance": hex(10 ** 18),
            "eth_call": hex(100 * 10 ** 6),
        }.get(method, "0x0")

    def test_native_transfer_recovers_to_our_address(self) -> None:
        from eth_account import Account

        plan = self.adapter.plan_transfer(Decimal("0.001"), OTHER, asset="ETH")
        recovered = Account.recover_transaction(plan.raw["raw_tx"])
        self.assertEqual(recovered.lower(), self.adapter.address().lower())
        self.assertEqual(plan.raw["chain_id"], 84532)

    def test_usdc_transfer_encodes_transfer_calldata(self) -> None:
        from eth_account import Account

        plan = self.adapter.plan_transfer(Decimal("0.025"), OTHER, asset="USDC")
        recovered = Account.recover_transaction(plan.raw["raw_tx"])
        self.assertEqual(recovered.lower(), self.adapter.address().lower())

        # decode the signed payload: to == the token contract, data == transfer(payee, 25000)
        self.assertEqual(self.adapter._token("USDC")[0].lower(), plan.raw["to"].lower())
        calldata = plan.raw["calldata"]
        self.assertTrue(calldata.startswith("a9059cbb"))
        self.assertIn(OTHER.lower().replace("0x", ""), calldata)
        self.assertEqual(int(calldata[-64:], 16), 25_000)

    def test_token_transfer_without_balance_is_refused(self) -> None:
        adapter = EvmAdapter("base-sepolia", SECRET)
        calls = {"eth_call": hex(0), "eth_gasPrice": hex(1_000_000_000),
                 "eth_getTransactionCount": hex(1), "eth_chainId": hex(84532),
                 "eth_getBalance": hex(10 ** 18)}
        adapter._rpc = lambda method, params: calls.get(method, "0x0")
        with self.assertRaises(ChainError) as caught:
            adapter.plan_transfer(Decimal("0.025"), OTHER, asset="USDC")
        self.assertEqual(caught.exception.code, "insufficient_funds")

    def test_gas_repricing_cannot_beat_the_fee_cap(self) -> None:
        """The cap is checked at plan time; this is the second check, at signing time.

        `broadcast` re-signs because a nonce from fifteen minutes ago may be spent. That also means
        it re-reads the gas price - so a busy or hostile endpoint could otherwise make the wallet pay
        far more than the operator allowed, and the receipt would still say "sent".
        """
        plan = self.adapter.plan_transfer(Decimal("0.001"), OTHER, asset="ETH")
        # the node triples the price between planning and sending
        self.adapter._rpc = lambda method, params: {
            "eth_chainId": hex(84532), "eth_blockNumber": hex(2),
            "eth_gasPrice": hex(50_000_000_000), "eth_getTransactionCount": hex(8),
            "eth_getBalance": hex(10 ** 18), "eth_call": hex(100 * 10 ** 6),
        }.get(method, "0x0")
        with self.assertRaises(ChainError) as caught:
            self.adapter.broadcast(plan, fee_cap=Decimal("0.0008"))
        self.assertEqual(caught.exception.code, "fee_too_high")

    def test_a_usdc_payment_proof_is_read_from_the_calldata(self) -> None:
        txid = "0x" + "ab" * 32
        calldata = ("a9059cbb" + "0" * 24 + OTHER[2:] + (25_000).to_bytes(32, "big").hex())
        self.adapter._rpc = lambda method, params: {
            "eth_getTransactionByHash": {"to": self.adapter._token("USDC")[0], "value": "0x0",
                                          "input": "0x" + calldata, "from": OTHER},
            "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": hex(100)},
            "eth_blockNumber": hex(104),
        }.get(method, "0x0")
        proof = self.adapter.payment_proof(txid, OTHER, asset="USDC")
        self.assertTrue(proof["received"])
        self.assertEqual(Decimal(proof["amount"]), Decimal("0.025"))
        self.assertEqual(proof["confirmations"], 5)

    def test_a_reverted_transfer_is_not_a_payment(self) -> None:
        txid = "0x" + "cd" * 32
        self.adapter._rpc = lambda method, params: {
            "eth_getTransactionByHash": {"to": OTHER, "value": hex(10 ** 15), "input": "0x", "from": OTHER},
            "eth_getTransactionReceipt": {"status": "0x0", "blockNumber": hex(90)},
            "eth_blockNumber": hex(95),
        }.get(method, "0x0")
        proof = self.adapter.payment_proof(txid, OTHER, asset="ETH")
        self.assertFalse(proof["received"])
        self.assertFalse(proof["settled"])

    def test_unreachable_endpoints_report_every_attempt(self) -> None:
        adapter = EvmAdapter("polygon-mainnet", SECRET,
                             rpc_url="http://127.0.0.1:1/nothing")
        with self.assertRaises(ChainError) as caught:
            adapter.available()
        self.assertEqual(caught.exception.code, "rpc_unreachable")


if __name__ == "__main__":
    unittest.main()

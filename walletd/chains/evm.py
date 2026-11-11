"""EVM adapter: sign natively, broadcast through a public RPC.

Unlike Monero, EVM signing genuinely fits in-process: the key is 32 bytes, the transaction is a
small RLP payload or an EIP-712 typed structure, and `eth-account` implements both. So there is no
external signer here — `walletd` holds the key inside its encrypted keystore, signs, and hands the
raw transaction to a public RPC. The agent still cannot reach the key: it lives in the daemon's
sealed payload, not in the Hermes process.
"""

from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal
from typing import Any, Dict, Optional

from .base import ChainAdapter, ChainError, TxPlan
from .. import httpdigest

# chain id, native symbol, tokens (address, decimals), explorer
CHAINS: Dict[str, Dict[str, Any]] = {
    "base-mainnet": {
        "chain_id": 8453, "native": "ETH",
        "rpcs": ["https://mainnet.base.org", "https://base.publicnode.com"],
        "explorer": "https://basescan.org/tx/{tx}",
        "tokens": {"USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)},
        "tokens_name": {"USDC": "USD Coin"}, "tokens_version": {"USDC": "2"},
    },
    "base-sepolia": {
        "chain_id": 84532, "native": "ETH",
        "rpcs": ["https://sepolia.base.org", "https://base-sepolia.publicnode.com"],
        "explorer": "https://sepolia.basescan.org/tx/{tx}",
        "tokens": {"USDC": ("0x036CbD53842c5426634e7929541eC2318f3dCF7e", 6)},
        "tokens_name": {"USDC": "USDC"}, "tokens_version": {"USDC": "2"},
    },
    "polygon-mainnet": {
        "chain_id": 137, "native": "POL",
        # polygon-rpc.com now answers "API key disabled" for anonymous callers
        "rpcs": ["https://api.zan.top/polygon-mainnet", "https://polygon-rpc.com"],
        "explorer": "https://polygonscan.com/tx/{tx}",
        "tokens": {"USDC": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6)},
        "tokens_name": {"USDC": "USD Coin"}, "tokens_version": {"USDC": "2"},
    },
    "polygon-amoy": {
        "chain_id": 80002, "native": "POL",
        "rpcs": ["https://api.zan.top/polygon-amoy", "https://rpc-amoy.polygon.technology"],
        "explorer": "https://amoy.polygonscan.com/tx/{tx}",
        "tokens": {"USDC": ("0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582", 6)},
        "tokens_name": {"USDC": "USDC"}, "tokens_version": {"USDC": "2"},
    },
}

GWEI = Decimal(10) ** 9
ERC20_TRANSFER_GAS = 65_000
NATIVE_TRANSFER_GAS = 21_000

#: Wrapped native coin, the hop a V2 route needs when neither side is the native coin.
WETH = {"base-mainnet": "0x4200000000000000000000000000000000000006"}

#: The only contracts this wallet will ever call with swap calldata.
#:
#: A swap is the most dangerous thing a wallet can sign: the destination is a contract, the amount
#: out is not known until the pool answers, and the "fee" includes slippage. So the router list is
#: code, not configuration: both entries are Uniswap-V2 interfaces (quote is a view call, execution
#: is one function with a deadline and a minimum-out), the agent can never name one, and the only
#: thing an operator can do to this list is narrow it. Polygon is deliberately absent: neither V2
#: router answered `getAmountsOut` for its native USDC when probed, and an unverified route is worse
#: than no route.
SWAP_ROUTERS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "base-mainnet": {
        "sushiswap-v2": {"router": "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",
                         "label": "SushiSwap V2", "fee_bps": 30},
        "baseswap-v2": {"router": "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
                        "label": "BaseSwap V2", "fee_bps": 30},
    },
}

#: Function selectors, written out because a typo here would send money to a wrong function.
#: `tests/test_swap.py` recomputes each one with keccak and fails if any is wrong.
SELECTORS = {
    "approve": "095ea7b3",                                   # approve(address,uint256)
    "getAmountsOut": "d06ca61f",                             # getAmountsOut(uint256,address[])
    "swapExactETHForTokens": "7ff36ab5",                     # (uint256,address[],address,uint256)
    "swapExactTokensForETH": "18cbafe5",                     # (uint256,uint256,address[],address,uint256)
    "swapExactTokensForTokens": "38ed1739",                  # (uint256,uint256,address[],address,uint256)
}
SWAP_GAS = 260_000        # ceiling for a V2 swap; the estimate overrides it when the node answers
APPROVE_GAS = 60_000


class EvmAdapter(ChainAdapter):
    supports_accounts = False

    def __init__(self, kind: str, secret_hex: str, rpc_url: str = "", timeout: float = 30.0):
        if kind not in CHAINS:
            raise ChainError("unknown_chain", f"{kind} is not a supported EVM chain")
        self.kind = kind
        self.asset = CHAINS[kind]["native"]
        self.explorer_tx = CHAINS[kind]["explorer"]
        self.secret_hex = secret_hex
        self.candidates = [rpc_url] if rpc_url else list(CHAINS[kind]["rpcs"])
        self.rpc_url = self.candidates[0]
        self.timeout = timeout
        self._acct = None

    # ---------- plumbing ----------

    def _rpc(self, method: str, params: list) -> Any:
        """Call the first endpoint that answers, and remember it.

        Public RPCs go down, rate-limit, or (polygon-rpc.com) switch off anonymous access entirely.
        One endpoint is a single point of failure for a wallet, so the list is walked in order and
        the winner is kept for the rest of the session.
        """
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        errors: list[str] = []
        for url in [self.rpc_url] + [u for u in self.candidates if u != self.rpc_url]:
            status, raw = 0, b""
            for attempt in range(2):
                try:
                    status, raw = httpdigest.post(url, payload, timeout=self.timeout)
                except OSError as exc:
                    errors.append(f"{url}: {exc}")
                    break
                if status == 429 and attempt == 0:
                    time.sleep(1.0)      # free endpoints throttle; one short retry beats failing
                    continue
                break
            if status != 200:
                errors.append(f"{url}: HTTP {status}")
                continue
            body = json.loads(raw.decode("utf-8"))
            if "error" in body:
                errors.append(f"{url}: {str(body['error'].get('message') or body['error'])[:120]}")
                continue
            self.rpc_url = url
            return body.get("result")
        raise ChainError("rpc_unreachable", "; ".join(errors)[:300] or "no EVM endpoint answered")

    def _account(self):
        if self._acct is None:
            try:
                from eth_account import Account
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise ChainError("missing_dependency",
                                 "eth-account is not installed (declared python_dependencies)") from exc
            self._acct = Account.from_key(self.secret_hex)
        return self._acct

    def _token(self, asset: str) -> tuple:
        token = CHAINS[self.kind]["tokens"].get(asset.upper())
        if not token:
            raise ChainError("unsupported_asset", f"{asset} is not a token on {self.kind}")
        return token

    # ---------- reads ----------

    def available(self) -> bool:
        chain_id = int(self._rpc("eth_chainId", []), 16)
        return chain_id == CHAINS[self.kind]["chain_id"]

    def status(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"kind": self.kind, "available": False, "provider": self.rpc_url,
                                "network": CHAINS[self.kind]["native"],
                                "tokens": list(CHAINS[self.kind]["tokens"])}
        try:
            info["available"] = self.available()
            if info["available"]:
                info["address"] = self.address()
                info["height"] = int(self._rpc("eth_blockNumber", []), 16)
        except ChainError as exc:
            info["error"] = exc.message
        return info

    def address(self) -> str:
        return self._account().address

    def balance(self, asset: str = "") -> Decimal:
        asset = (asset or self.asset).upper()
        owner = self.address()
        if asset == CHAINS[self.kind]["native"]:
            wei = int(self._rpc("eth_getBalance", [owner, "latest"]), 16)
            return Decimal(wei) / Decimal(10) ** 18
        token, decimals = self._token(asset)
        # balanceOf(address) = 0x70a08231 + 32-byte padded address
        data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
        result = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        return Decimal(int(result, 16)) / Decimal(10) ** decimals

    def balance_all(self) -> Dict[str, Decimal]:
        out = {CHAINS[self.kind]["native"]: Decimal("0")}
        for symbol in CHAINS[self.kind]["tokens"]:
            out[symbol] = Decimal("0")
        for symbol in out:
            try:
                if symbol == CHAINS[self.kind]["native"]:
                    wei = int(self._rpc("eth_getBalance", [self.address(), "latest"]), 16)
                    out[symbol] = Decimal(wei) / Decimal(10) ** 18
                else:
                    token, decimals = self._token(symbol)
                    data = "0x70a08231" + self.address().lower().replace("0x", "").rjust(64, "0")
                    result = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
                    out[symbol] = Decimal(int(result, 16)) / Decimal(10) ** decimals
            except (ChainError, ValueError):
                continue
        return out

    # ---------- writing ----------

    def _fees(self) -> Dict[str, int]:
        """EIP-1559 fees: twice the node's gas price as the ceiling, the node's own tip suggestion.

        The tip used to be `max(1 gwei, gas_price / 10)`. On Base the whole gas price is a fraction
        of a gwei, so that tip was *above* the ceiling and every node rejected the transaction
        ("max priority fee per gas higher than max fee per gas"); on Polygon a tenth of the gas price
        is below the tip validators require. The node knows what its chain wants — ask it, and never
        let the tip exceed the ceiling.
        """
        gas_price = int(self._rpc("eth_gasPrice", []), 16)
        max_fee = max(gas_price * 2, 1)
        tip = 0
        try:
            tip = int(self._rpc("eth_maxPriorityFeePerGas", []), 16)
        except Exception:  # noqa: BLE001 - not every endpoint implements it; fall back below
            tip = 0
        if tip <= 0:
            tip = max(gas_price // 10, 1)
        return {"maxFeePerGas": max_fee, "maxPriorityPerGas": min(tip, max_fee)}

    def native_symbol(self) -> str:
        """The coin this chain charges gas in (ETH on Base, POL on Polygon)."""
        return CHAINS[self.kind]["native"]

    def native_balance(self) -> Decimal:
        """How much gas coin this address holds, in whole units."""
        wei = int(self._rpc("eth_getBalance", [self.address(), "latest"]), 16)
        return Decimal(wei) / Decimal(10) ** 18

    def transfer_cost(self) -> Decimal:
        """Worst-case cost of one transfer in the native coin, at the current fee ceiling.

        Uses the ERC-20 figure (65k gas) even for a plain send: the floor should hold for the
        expensive case, and 21k of a 65k budget is not a meaningful saving.
        """
        fees = self._fees()
        return Decimal(ERC20_TRANSFER_GAS * fees["maxFeePerGas"]) / Decimal(10) ** 18

    # ---------- swaps ----------

    @staticmethod
    def _word(value: int) -> str:
        return f"{int(value):064x}"

    @staticmethod
    def _addr_word(address: str) -> str:
        return str(address).lower().replace("0x", "").rjust(64, "0")

    def _words(self, data: str) -> list:
        body = str(data or "")[2:] if str(data or "").startswith("0x") else str(data or "")
        return [body[i:i + 64] for i in range(0, len(body), 64) if body[i:i + 64]]

    def swap_routers(self) -> list:
        """The trusted operators on this chain, in the order the wallet will try them."""
        return [{"operator": name, "label": entry["label"], "router": entry["router"],
                 "fee_bps": entry["fee_bps"]}
                for name, entry in sorted(SWAP_ROUTERS.get(self.kind, {}).items())]

    def _swap_operator(self, operator: str = "") -> Dict[str, Any]:
        registry = SWAP_ROUTERS.get(self.kind, {})
        if not registry:
            raise ChainError("no_swap_route", f"{self.kind} has no trusted swap operator")
        name = str(operator or "")
        if name:
            entry = registry.get(name)
            if entry is None:
                raise ChainError("unknown_operator",
                                 f"{name!r} is not a trusted swap operator on {self.kind} "
                                 f"({', '.join(sorted(registry))})")
            return {"operator": name, **entry}
        first = sorted(registry)[0]
        return {"operator": first, **registry[first]}

    def _swap_path(self, asset_in: str, asset_out: str) -> list:
        native = CHAINS[self.kind]["native"]
        weth = WETH.get(self.kind, "")
        if not weth:
            raise ChainError("no_swap_route", f"{self.kind} has no wrapped native coin on file")
        if asset_in == asset_out:
            raise ChainError("bad_swap", f"swapping {asset_in} for itself is not a swap")
        if asset_in == native:
            return [weth, self._token(asset_out)[0]]
        if asset_out == native:
            return [self._token(asset_in)[0], weth]
        return [self._token(asset_in)[0], weth, self._token(asset_out)[0]]

    def _swap_units(self, asset: str, amount: Decimal) -> int:
        native = CHAINS[self.kind]["native"]
        decimals = 18 if asset == native else self._token(asset)[1]
        units = int(Decimal(str(amount)) * Decimal(10) ** decimals)
        if units <= 0:
            raise ChainError("bad_amount", f"{amount} {asset} rounds to zero")
        return units

    def quote_swap(self, asset_in: str, asset_out: str, amount_in: Decimal,
                   operator: str = "") -> Dict[str, Any]:
        """What one swap would return right now. A view call: nothing is signed, nothing is sent."""
        asset_in, asset_out = str(asset_in).upper(), str(asset_out).upper()
        op = self._swap_operator(operator)
        path = self._swap_path(asset_in, asset_out)
        units = self._swap_units(asset_in, amount_in)
        data = ("0x" + SELECTORS["getAmountsOut"] + self._word(units) + self._word(64)
                + self._word(len(path)) + "".join(self._addr_word(a) for a in path))
        try:
            result = self._rpc("eth_call", [{"to": op["router"], "data": data}, "latest"])
        except ChainError as exc:
            raise ChainError("no_swap_route",
                             f"{op['label']} has no route {asset_in}→{asset_out}: {exc.message}") from exc
        words = self._words(result)
        if len(words) < 2 or int(words[-1] or "0", 16) <= 0:
            raise ChainError("no_swap_route",
                             f"{op['label']} quoted nothing for {asset_in}→{asset_out}")
        native = CHAINS[self.kind]["native"]
        decimals = 18 if asset_out == native else self._token(asset_out)[1]
        amount_out = Decimal(int(words[-1], 16)) / Decimal(10) ** decimals
        return {"operator": op["operator"], "label": op["label"], "router": op["router"],
                "asset_in": asset_in, "asset_out": asset_out,
                "amount_in": str(amount_in), "amount_out": str(amount_out),
                "rate": str(amount_out / Decimal(str(amount_in))) if Decimal(str(amount_in)) > 0 else "0",
                "path": path}

    def plan_swap(self, asset_in: str, amount_in: Decimal, asset_out: str, min_out: Decimal,
                  operator: str = "", deadline_seconds: int = 600) -> TxPlan:
        """Build the swap. The minimum out is decided by policy, never by the caller's slippage wish."""
        asset_in, asset_out = str(asset_in).upper(), str(asset_out).upper()
        op = self._swap_operator(operator)
        path = self._swap_path(asset_in, asset_out)
        native = CHAINS[self.kind]["native"]
        units_in = self._swap_units(asset_in, amount_in)
        units_min = self._swap_units(asset_out, min_out)
        me = self.address()
        deadline = int(time.time()) + max(60, int(deadline_seconds))
        path_words = "".join(self._addr_word(a) for a in path)
        approve_template = None

        if asset_in == native:
            data = ("0x" + SELECTORS["swapExactETHForTokens"] + self._word(units_min)
                    + self._word(128) + self._addr_word(me) + self._word(deadline)
                    + self._word(len(path)) + path_words)
            value = units_in
        elif asset_out == native:
            data = ("0x" + SELECTORS["swapExactTokensForETH"] + self._word(units_in)
                    + self._word(units_min) + self._word(160) + self._addr_word(me)
                    + self._word(deadline) + self._word(len(path)) + path_words)
            value = 0
        else:
            data = ("0x" + SELECTORS["swapExactTokensForTokens"] + self._word(units_in)
                    + self._word(units_min) + self._word(160) + self._addr_word(me)
                    + self._word(deadline) + self._word(len(path)) + path_words)
            value = 0

        if asset_in != native:
            # Exact amount, pinned router. Never an unlimited allowance: a router that is later
            # compromised can only take what this one swap was about to spend anyway.
            token = self._token(asset_in)[0]
            approve_template = {"to": token, "value": 0,
                                "gas": self._estimate_gas(token, 0,
                                                          "0x" + SELECTORS["approve"]
                                                          + self._addr_word(op["router"])
                                                          + self._word(units_in), APPROVE_GAS),
                                "chainId": CHAINS[self.kind]["chain_id"],
                                "data": (SELECTORS["approve"] + self._addr_word(op["router"])
                                         + self._word(units_in))}

        gas = self._estimate_gas(op["router"], value, data, SWAP_GAS)
        plan = TxPlan(kind=self.kind, asset=asset_in, amount=Decimal(str(amount_in)),
                      payee=op["router"], fee=Decimal("0"),
                      summary=f"swap {amount_in} {asset_in} → at least {min_out} {asset_out} "
                              f"via {op['label']}",
                      raw={"tx_template": {"to": op["router"], "value": value, "gas": gas,
                                           "chainId": CHAINS[self.kind]["chain_id"],
                                           "data": data[2:]},
                           **({"approve_template": approve_template} if approve_template else {}),
                           "swap": {"operator": op["operator"], "label": op["label"],
                                    "router": op["router"], "asset_in": asset_in,
                                    "asset_out": asset_out, "amount_in": str(amount_in),
                                    "min_out": str(min_out), "path": path,
                                    "deadline": deadline}})
        fees = self._fees()
        gas_total = gas + (int(approve_template["gas"]) if approve_template else 0)
        plan.fee = Decimal(gas_total * fees["maxFeePerGas"]) / Decimal(10) ** 18
        return plan

    def _estimate_gas(self, to: str, value: int, data: str, fallback: int) -> int:
        """Ask the node, and fall back to a ceiling if it will not answer.

        A swap that would revert (no route, slippage already blown) fails here, before anything is
        signed — which is exactly where a bad quote should die.
        """
        try:
            answer = self._rpc("eth_estimateGas", [{"from": self.address(), "to": to,
                                                    "value": hex(int(value)), "data": data}])
            estimate = int(answer, 16)
            if estimate > 0:
                return min(int(estimate * 125 // 100) + 5_000, 900_000)
        except Exception:      # noqa: BLE001 - a node that will not estimate is not a refusal
            pass
        return fallback

    def plan_transfer(self, amount: Decimal, payee: str, account_index: int = 0, asset: str = "") -> TxPlan:
        asset = (asset or self.asset).upper()
        if not payee.lower().startswith("0x") or len(payee) != 42:
            raise ChainError("invalid_payee", "not an EVM address (0x + 40 hex)")
        native = CHAINS[self.kind]["native"]
        fees = self._fees()
        nonce = int(self._rpc("eth_getTransactionCount", [self.address(), "pending"]), 16)
        chain_id = CHAINS[self.kind]["chain_id"]

        if asset == native:
            value = int(amount * Decimal(10) ** 18)
            if value <= 0:
                raise ChainError("bad_amount", f"{amount} rounds to zero wei — nothing to send")
            gas = NATIVE_TRANSFER_GAS
            # eth-account validates the destination: it must be a checksummed address
            try:
                from eth_utils import to_checksum_address
            except ImportError as exc:  # pragma: no cover
                raise ChainError("missing_dependency", "eth-utils is not installed") from exc
            to = to_checksum_address(payee)
            data = b""
            decimals = 18
        else:
            token, decimals = self._token(asset)
            units = int(amount * Decimal(10) ** decimals)
            if units <= 0:
                raise ChainError("bad_amount", f"{amount} {asset} rounds to zero")
            data = bytes.fromhex(
                "a9059cbb"
                + payee.lower().replace("0x", "").rjust(64, "0")
                + units.to_bytes(32, "big").hex())
            gas = ERC20_TRANSFER_GAS
            to = token
            value = 0
            # Refuse before signing: a signed transfer with no token balance reverts on-chain and
            # still costs gas, so the wallet would pay to fail.
            held_tokens = self.balance(asset)
            if held_tokens < amount:
                raise ChainError("insufficient_funds",
                                 f"need {amount} {asset}, have {held_tokens} {asset} at {self.address()}")
            gas_held = int(self._rpc("eth_getBalance", [self.address(), "latest"]), 16)
            if gas_held < gas * fees["maxFeePerGas"]:
                raise ChainError("insufficient_gas",
                                 f"{asset} transfers need {native} for gas; this address holds "
                                 f"{Decimal(gas_held) / Decimal(10) ** 18:.6f} {native}")

        fee_wei = gas * fees["maxFeePerGas"]
        if asset == native:
            total = value + fee_wei
            held = int(self._rpc("eth_getBalance", [self.address(), "latest"]), 16)
            if total > held:
                raise ChainError("insufficient_funds",
                                 f"need {Decimal(total) / Decimal(10) ** 18:.6f} {native} "
                                 f"(amount + gas), have {Decimal(held) / Decimal(10) ** 18:.6f}")

        tx = {
            "to": to, "value": value, "gas": gas, "nonce": nonce, "chainId": chain_id,
            "maxFeePerGas": fees["maxFeePerGas"],
            "maxPriorityFeePerGas": fees["maxPriorityPerGas"],
            "data": data,
            "type": 2,
        }
        signed = self._account().sign_transaction(tx)
        raw_hex = signed.raw_transaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        return TxPlan(
            kind=self.kind, asset=asset, amount=amount, payee=payee,
            fee=Decimal(fee_wei) / Decimal(10) ** 18,
            raw={"raw_tx": raw_hex, "nonce": nonce, "chain_id": chain_id, "to": to, "gas": gas,
                 "calldata": data.hex() if data else "",
                 # the unsigned transaction, so broadcast can re-sign against a fresh nonce
                 "tx_template": {"to": to, "value": value, "gas": gas, "chainId": chain_id,
                                 "data": data.hex() if data else ""}},
            summary=f"{amount} {asset} -> {payee[:10]}… on {self.kind}")

    # ---------- x402: sign an authorization instead of broadcasting ----------

    def plan_authorization(self, amount: Decimal, payee: str, asset: str = "",
                           resource: str = "", valid_seconds: int = 900,
                           network: str = "") -> TxPlan:
        """EIP-3009 `TransferWithAuthorization`, signed off-chain.

        This is the x402 `exact` scheme: the payer signs an authorisation, the facilitator submits it
        and pays the gas. Nothing is broadcast here, so an approval costs no fee — and the signature
        is bound to an amount, a payee, an expiry and a single-use nonce, so it cannot be replayed or
        inflated after the human approves it.
        """
        import secrets as _secrets
        import time as _time

        symbol = (asset or "USDC").upper()
        token, decimals = self._token(symbol)
        units = int(amount * Decimal(10) ** decimals)
        if units <= 0:
            raise ChainError("bad_amount", f"{amount} {symbol} rounds to zero")
        now = int(_time.time())
        authorization = {
            "from": self.address(),
            "to": payee,
            "value": str(units),
            "validAfter": str(now - 60),
            "validBefore": str(now + max(60, int(valid_seconds))),
            "nonce": "0x" + _secrets.token_hex(32),
        }
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": CHAINS[self.kind]["tokens_name"].get(symbol, "USD Coin"),
                "version": CHAINS[self.kind]["tokens_version"].get(symbol, "2"),
                "chainId": CHAINS[self.kind]["chain_id"],
                "verifyingContract": token,
            },
            "message": {
                "from": self.address(), "to": payee, "value": units,
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(authorization["nonce"][2:]),
            },
        }
        signed = self._account().sign_typed_data(full_message=typed)
        return TxPlan(
            kind=self.kind, asset=symbol, amount=amount, payee=payee, fee=Decimal("0"),
            raw={
                "x402": {
                    "x402Version": 1,
                    "scheme": "exact",
                    # the seller's own name for this chain ("base", "eip155:8453"): its facilitator
                    # compares this field with the offer it made, and does not know our chain ids
                    "network": network or self.kind,
                    "payload": {
                        "signature": signed.signature.hex() if isinstance(signed.signature, bytes)
                                     else str(signed.signature),
                        "authorization": authorization,
                    },
                },
                "resource": resource,
                "token": token,
                "signature": signed.signature.hex() if isinstance(signed.signature, bytes)
                             else str(signed.signature),
            },
            summary=f"authorise {amount} {symbol} to {payee[:10]}… (settled by the seller's facilitator)")

    def payment_proof(self, txid: str, address: str, asset: str = "") -> Dict[str, Any]:
        """Prove an EVM payment: the sender, recipient and amount are public.

        A token transfer is read out of the calldata, a native one out of `value`. The receipt's
        status decides whether it actually happened — a reverted transfer is not a payment.
        """
        tx = self._rpc("eth_getTransactionByHash", [txid])
        if not tx:
            return {"received": False, "amount": "0", "confirmations": 0, "reason": "transaction not found"}
        receipt = self._rpc("eth_getTransactionReceipt", [txid]) or {}
        confirmed = str(receipt.get("status", "0x0")).lower() in ("0x1", "1")
        symbol = (asset or self.asset).upper()
        native = CHAINS[self.kind]["native"]
        target = address.lower()
        amount = Decimal("0")
        recipient_ok = False

        if symbol == native:
            value = int(tx.get("value", "0x0"), 16)
            recipient_ok = str(tx.get("to", "")).lower() == target
            amount = Decimal(value) / Decimal(10) ** 18
        else:
            token, decimals = self._token(symbol)
            data = str(tx.get("input") or tx.get("data") or "")
            if data.startswith("0xa9059cbb") and len(data) >= 138:
                to_word = data[10:74].rjust(64, "0")
                recipient = "0x" + to_word[-40:]
                units = int(data[74:138], 16)
                recipient_ok = recipient.lower() == target and str(tx.get("to", "")).lower() == token.lower()
                amount = Decimal(units) / Decimal(10) ** decimals

        height = int(receipt.get("blockNumber", "0x0"), 16)
        head = int(self._rpc("eth_blockNumber", []), 16)
        return {"received": bool(confirmed and recipient_ok),
                "amount": f"{amount:f}", "confirmations": max(0, head - height + 1) if height else 0,
                "settled": confirmed, "recipient_matched": recipient_ok,
                "to": str(tx.get("to", "")), "from": str(tx.get("from", ""))}

    def broadcast(self, plan: TxPlan, fee_cap=None) -> Dict[str, Any]:
        if plan.raw.get("x402"):
            # Nothing to broadcast: the paid resource submits this authorisation itself.
            return {"signed": True, "settlement": "x402", "fee": "0",
                    "authorization": plan.raw["x402"]["payload"]["authorization"],
                    "signature": plan.raw["signature"]}
        raw_hex = plan.raw.get("raw_tx")
        if not raw_hex:
            raise ChainError("bad_plan", "no signed transaction in this plan")

        template = plan.raw.get("tx_template")
        approve = plan.raw.get("approve_template")
        approve_fee = Decimal("0")
        if approve:
            # An ERC-20 swap needs an allowance, and the allowance must exist *before* the swap is
            # mined or the router pulls nothing and the swap reverts (costing the gas anyway). So:
            # sign and send the approve, wait for it, and only then send the swap. Exact amount,
            # pinned router, and the second transaction is only built if the first actually landed.
            raw_approve, _, approve_fee = self._sign_template(approve, fee_cap)
            approve_hash = self._rpc("eth_sendRawTransaction", [raw_approve])
            plan.raw["approve_hash"] = approve_hash
            self._wait_mined(approve_hash, code="approve_failed",
                             what="the allowance transaction")

        if template:
            raw_hex, nonce, fee = self._sign_template(template, fee_cap)
            plan.raw["nonce"] = nonce
            plan.fee = fee + approve_fee

        tx_hash = self._rpc("eth_sendRawTransaction", [raw_hex])
        return {"tx_hash": tx_hash, "explorer": self.explorer_tx.format(tx=tx_hash),
                "fee": str(plan.fee), "signed": True,
                **({"approve_hash": plan.raw["approve_hash"]} if plan.raw.get("approve_hash") else {})}

    def _sign_template(self, template: Dict[str, Any], fee_cap=None):
        """Sign one transaction now, with a fresh nonce and the current fees.

        A nonce fixed fifteen minutes ago is already spent if anything else went out — two pendings
        signed on the same nonce means one transfer silently replaces the other, while both receipts
        say "sent".
        """
        nonce = int(self._rpc("eth_getTransactionCount", [self.address(), "pending"]), 16)
        fees = self._fees()
        if fee_cap is not None and fee_cap > 0:
            worst = (Decimal(int(template["gas"])) * Decimal(fees["maxFeePerGas"])) / Decimal(10) ** 18
            if worst > Decimal(str(fee_cap)):
                # the cap was checked at plan time; the chain has repriced since. Refusing here is
                # the whole point - otherwise a hostile or busy RPC sets the price and the wallet
                # pays it while the receipt says "sent".
                raise ChainError("fee_too_high",
                                 f"gas is now {worst} {self.asset}, above the {fee_cap} cap; "
                                 f"nothing was signed")
        tx = {
            "to": template["to"], "value": int(template["value"]), "gas": int(template["gas"]),
            "chainId": int(template["chainId"]), "nonce": nonce,
            "maxFeePerGas": fees["maxFeePerGas"], "maxPriorityFeePerGas": fees["maxPriorityPerGas"],
            "data": bytes.fromhex(template["data"]) if template.get("data") else b"",
            "type": 2,
        }
        signed = self._account().sign_transaction(tx)
        raw_hex = signed.raw_transaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        return raw_hex, nonce, Decimal(int(template["gas"]) * fees["maxFeePerGas"]) / Decimal(10) ** 18

    def _wait_mined(self, tx_hash: str, timeout: float = 90.0, code: str = "not_mined",
                    what: str = "the transaction") -> Dict[str, Any]:
        """Wait for a receipt. A reverted transaction is a failure, not a slow one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                if str(receipt.get("status", "0x0")).lower() not in ("0x1", "1"):
                    raise ChainError(code, f"{what} reverted on chain ({tx_hash})")
                return receipt
            time.sleep(1.5)
        raise ChainError(code + "_timeout",
                         f"{what} was sent but not mined within {int(timeout)}s ({tx_hash}); "
                         f"nothing further was sent")


def from_secret(kind: str, secret_hex: str, rpc_url: str = "") -> EvmAdapter:
    return EvmAdapter(kind, secret_hex, rpc_url=rpc_url)

"""Predefined public nodes. No chain data, no local daemon, no sync.

The wallet daemon holds keys; the chain is somebody else's job. Each chain has an ordered list of
public endpoints and the first healthy one wins; a failing provider is skipped, not fatal.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import ChainError

# Order matters: first healthy wins.
REGISTRY: Dict[str, List[Dict[str, str]]] = {
    "monero-mainnet": [
        # TLS only: a node is the wallet's view of the chain, so a cleartext one lets anyone on the
        # path see which outputs you queried — and a hostile node can lie about heights and balances.
        {"label": "stackwallet", "url": "https://monero.stackwallet.com:18081", "proto": "monero"},
        {"label": "cakewallet", "url": "https://xmr-node.cakewallet.com:18081", "proto": "monero"},
    ],
    "monero-stagenet": [
        {"label": "rino-stagenet", "url": "https://stagenet.community.rino.io:38081", "proto": "monero"},
    ],
    "monero-testnet": [
        {"label": "rino-testnet", "url": "https://testnet.community.rino.io:28081", "proto": "monero"},
    ],
    "bitcoin-mainnet": [
        {"label": "mempool.space", "url": "https://mempool.space/api", "proto": "esplora"},
        {"label": "blockstream", "url": "https://blockstream.info/api", "proto": "esplora"},
    ],
    "bitcoin-testnet": [
        {"label": "mempool.space-testnet", "url": "https://mempool.space/testnet/api", "proto": "esplora"},
        {"label": "blockstream-testnet", "url": "https://blockstream.info/testnet/api", "proto": "esplora"},
    ],
    "xrp-mainnet": [
        {"label": "xrplcluster", "url": "https://xrplcluster.com/", "proto": "xrpl"},
        {"label": "ripple-s1", "url": "https://s1.ripple.com:51234/", "proto": "xrpl"},
    ],
    "xrp-testnet": [
        {"label": "xrpl-altnet", "url": "https://s.altnet.rippletest.net:51234/", "proto": "xrpl"},
    ],
    "base-mainnet": [
        {"label": "base", "url": "https://mainnet.base.org", "proto": "evm"},
        {"label": "base-publicnode", "url": "https://base.publicnode.com", "proto": "evm"},
    ],
    "base-sepolia": [
        {"label": "base-sepolia", "url": "https://sepolia.base.org", "proto": "evm"},
        {"label": "base-sepolia-publicnode", "url": "https://base-sepolia.publicnode.com", "proto": "evm"},
    ],
    "polygon-mainnet": [
        {"label": "polygon", "url": "https://polygon-rpc.com", "proto": "evm"},
    ],
    "arbitrum-mainnet": [
        {"label": "arbitrum", "url": "https://arb1.arbitrum.io/rpc", "proto": "evm"},
    ],
}

_CACHE: Dict[str, tuple[float, Optional[Dict[str, str]]]] = {}
_TTL = 120.0


def candidates(kind: str) -> List[Dict[str, str]]:
    return list(REGISTRY.get(kind, []))


def _http(url: str, data: Optional[bytes], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "agent-wallet/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def probe(node: Dict[str, str], timeout: float = 6.0) -> Dict[str, Any]:
    """Ask one endpoint for its height. Returns {ok, height|error}."""
    proto, url = node.get("proto"), node.get("url", "")
    try:
        if proto == "monero":
            body = _http(url.rstrip("/") + "/get_info", None, timeout)
            data = json.loads(body.decode("utf-8"))
            return {"ok": True, "height": data.get("height"), "synced": data.get("synchronized", True)}
        if proto == "esplora":
            body = _http(url.rstrip("/") + "/blocks/tip/height", None, timeout)
            return {"ok": True, "height": int(body.decode("utf-8").strip())}
        if proto == "xrpl":
            # XRPL nodes answer JSON-RPC on POST; without this branch every XRP entry reported
            # "unknown protocol 'xrpl'" while `chains` happily showed a live height
            payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "method": "server_info", "params": [{}]}).encode()
            data = json.loads(_http(url, payload, timeout).decode("utf-8"))
            info = ((data.get("result") or {}).get("info") or {})
            ledger = info.get("validated_ledger") or info.get("closed_ledger") or {}
            return {"ok": True, "height": ledger.get("seq")}
        if proto == "evm":
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
            data = json.loads(_http(url, payload, timeout).decode("utf-8"))
            return {"ok": True, "height": int(str(data.get("result", "0x0")), 16)}
        return {"ok": False, "error": f"unknown protocol {proto!r}"}
    except Exception as exc:  # noqa: BLE001 - a dead endpoint is data, never an exception to the caller
        reason = getattr(exc, "reason", None) or exc
        return {"ok": False, "error": str(reason)[:120]}


def pick(kind: str, timeout: float = 6.0, force: bool = False) -> Optional[Dict[str, str]]:
    """First healthy endpoint for a chain, cached for two minutes."""
    now = time.time()
    cached = _CACHE.get(kind)
    if cached and not force and now - cached[0] < _TTL:
        return cached[1]
    chosen = None
    for node in candidates(kind):
        if probe(node, timeout).get("ok"):
            chosen = node
            break
    _CACHE[kind] = (now, chosen)
    return chosen


def health(kinds: List[str]) -> List[Dict[str, Any]]:
    """Every endpoint for every requested chain, with live height — for the UI and `wallet nodes`."""
    rows: List[Dict[str, Any]] = []
    for kind in kinds:
        for node in candidates(kind):
            result = probe(node)
            rows.append({"kind": kind, **node, **result})
    return rows

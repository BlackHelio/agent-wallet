"""Address checksums: a shape is not an address.

A regex says "95 base58 characters that start with 4". It cannot tell a real Monero address from the
same string with one character changed — and one changed character is either a burned payment or, if
an attacker can steer it, their wallet. Base58 and bech32 both carry a checksum for exactly this
reason, so both are verified here before a payment is filed:

- Monero: base58 → 69 bytes, `keccak256(prefix ‖ keys)[:4]` must equal the trailing four.
- Bitcoin: bech32/bech32m polymod (BIP-173/350), which also rejects a mixed-case string.

Everything is pure Python on top of `eth_utils.keccak` (already a dependency for the EVM signer).
"""

from __future__ import annotations

from typing import Optional

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}

#: Monero network bytes for standard (non-integrated) addresses.
XMR_PREFIXES = {
    "monero-mainnet": {18},
    "monero-stagenet": {24},
    "monero-testnet": {53},
}


#: Monero encodes base58 in **8-byte blocks**, each with a fixed character width — not as one big
#: integer (which is what Bitcoin does). Decoding a Monero address with the Bitcoin algorithm yields
#: 70 bytes and a nonsense prefix byte, so the block widths are spelled out here.
#: Index = bytes in the block, value = characters it occupies.
ENCODED_BLOCK_SIZE = [0, 2, 3, 5, 6, 7, 9, 10, 11]
_BLOCK_FOR_CHARS = {chars: size for size, chars in enumerate(ENCODED_BLOCK_SIZE) if size}


def xmr_decode(text: str) -> Optional[bytes]:
    """Block-wise base58 decode. None when a character or a block width is out of range."""
    out = bytearray()
    pos = 0
    while len(text) - pos > 11:
        chunk = text[pos:pos + 11]
        pos += 11
        value = 0
        for ch in chunk:
            index = _INDEX.get(ch)
            if index is None:
                return None
            value = value * 58 + index
        if value >= 1 << 64:
            return None
        out += value.to_bytes(8, "big")
    rest = text[pos:]
    if not rest:
        return bytes(out)
    size = _BLOCK_FOR_CHARS.get(len(rest))
    if size is None:
        return None
    value = 0
    for ch in rest:
        index = _INDEX.get(ch)
        if index is None:
            return None
        value = value * 58 + index
    if value >= 1 << (8 * size):
        return None
    out += value.to_bytes(size, "big")
    return bytes(out)


def xmr_encode(raw: bytes) -> str:
    """Inverse of `xmr_decode`, block-wise. Used to build test vectors and to re-encode a checksum."""
    out = []
    pos = 0
    while len(raw) - pos > 8:
        block = raw[pos:pos + 8]
        pos += 8
        value = int.from_bytes(block, "big")
        chars = []
        for _ in range(11):
            value, rem = divmod(value, 58)
            chars.append(ALPHABET[rem])
        out.append("".join(reversed(chars)))
    rest = raw[pos:]
    if rest:
        value = int.from_bytes(rest, "big")
        chars = []
        for _ in range(ENCODED_BLOCK_SIZE[len(rest)]):
            value, rem = divmod(value, 58)
            chars.append(ALPHABET[rem])
        out.append("".join(reversed(chars)))
    return "".join(out)


def xmr_valid(address: str, kind: str = "monero-mainnet") -> bool:
    """Block base58 + Keccak checksum, and the right network byte."""
    raw = xmr_decode((address or "").strip())
    expected = XMR_PREFIXES.get(kind)
    if raw is None or expected is None or len(raw) != 69:
        return False
    if raw[0] not in expected:
        return False
    from eth_utils.crypto import keccak   # Keccak-256, not SHA3-256: Monero uses the pre-standard one
    return keccak(raw[:-4])[:4] == raw[-4:]


# ---- bech32 / bech32m (BIP-173 / BIP-350) --------------------------------------------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_BECH32M_CONST = 0x2BC830A3


def _polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GEN[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def bech32_valid(address: str) -> bool:
    """True for a correct bech32 (segwit v0) or bech32m (v1+) address on bc/tb/bcrt."""
    text = (address or "").strip()
    if not text or text.lower() != text and text.upper() != text:
        return False                     # mixed case is invalid by spec
    text = text.lower()
    if not text.startswith(("bc1", "tb1", "bcrt1")):
        return False
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        return False
    hrp, data_part = text[:pos], text[pos + 1:]
    try:
        data = [_CHARSET.index(c) for c in data_part]
    except ValueError:
        return False
    check = _polymod(_hrp_expand(hrp) + data)
    if check == 1:
        version_const = 0                # bech32
    elif check == _BECH32M_CONST:
        version_const = 1                # bech32m
    else:
        return False
    payload = data[:-6]
    if not payload:
        return False
    version = payload[0]
    if version > 16:
        return False
    # v0 is bech32, v1+ is bech32m; anything else is a library-invented address
    if (version == 0 and version_const != 0) or (version > 0 and version_const != 1):
        return False
    # The witness program is carried as 5-bit groups; its length limits are in BYTES. Comparing the
    # group count with the byte limits accepted a 20-byte P2WPKH only by coincidence (32 groups) and
    # refused every P2WSH and every taproot address (52 groups for 32 bytes).
    program = _convert_bits(payload[1:], 5, 8)
    if program is None or not (2 <= len(program) <= 40):
        return False
    if version == 0 and len(program) not in (20, 32):
        return False
    return True


def _convert_bits(groups: list[int], from_bits: int, to_bits: int) -> Optional[bytes]:
    """Regroup bits without padding (BIP-173). None when the leftover bits are not zero padding."""
    acc = 0
    bits = 0
    out = bytearray()
    for value in groups:
        if value < 0 or value >> from_bits:
            return None
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & ((1 << to_bits) - 1))
    if bits >= from_bits or (acc & ((1 << bits) - 1)):
        return None
    return bytes(out)


#: Base58Check version bytes: P2PKH and P2SH per network.
_BASE58_VERSIONS = {"main": {0x00, 0x05}, "test": {0x6F, 0xC4}}


def base58check_valid(address: str, network: str = "main") -> bool:
    """A legacy (`1…`) or script (`3…`) Bitcoin address: 21 bytes + a double-SHA256 checksum."""
    import hashlib

    text = (address or "").strip()
    if not text:
        return False
    number = 0
    for char in text:
        index = _INDEX.get(char)
        if index is None:
            return False
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    raw = b"\x00" * (len(text) - len(text.lstrip(ALPHABET[0]))) + raw
    if len(raw) != 25:
        return False
    body, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != checksum:
        return False
    return body[0] in _BASE58_VERSIONS.get(network, set())


def btc_valid(address: str, network: str = "main") -> bool:
    """Any Bitcoin address this wallet can pay: segwit/taproot (bech32, bech32m) or Base58Check."""
    text = (address or "").strip()
    prefixes = ("bc1",) if network == "main" else ("tb1", "bcrt1")
    if text.lower().startswith(("bc1", "tb1", "bcrt1")):
        return text.lower().startswith(prefixes) and bech32_valid(text)
    return base58check_valid(text, network)

def checksum_ok(asset: str, address: str, kind: str = "") -> bool:
    """One entry point for callers that know the asset but not the details."""
    asset = (asset or "").upper()
    if asset == "XMR":
        return xmr_valid(address, kind or "monero-mainnet")
    if asset == "BTC":
        return btc_valid(address, "test" if kind.endswith(("testnet", "regtest")) else "main")
    return True                            # XRP checks its own checksum, EVM has none to check

"""Encrypted keystore: scrypt KDF + XChaCha20-Poly1305, atomic writes, 0600.

The envelope never contains plaintext, and there is no code path in this module that logs,
prints, or returns key material to a caller other than `open_sealed`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

from . import compat

VERSION = 3
AAD = b"agent-wallet/keystore/v3"
LEGACY_AAD = b"agent-wallet/keystore/v2"   # read-only: v2 files are upgraded on unlock
KDF_N = 1 << 15
KDF_R = 8
KDF_P = 1
KDF_MAXMEM = 64 * 1024 * 1024
# A six-digit PIN is a million possibilities, so it is charged far more per guess than a passphrase:
# 128 MB and ~1 s here, against 32 MB and ~70 ms. That is the whole defence for the PIN path — the
# attempt counter stops a live attacker, and this stops someone who walked off with the file.
PIN_KDF_N = 1 << 19        # measured on this host: 537 MB, 1.2 s — 14 days per million guesses, single core
NONCE_BYTES = 24
SALT_BYTES = 16
DATA_KEY_BYTES = 32


class KeystoreError(Exception):
    """Base class. Messages never include key material or the passphrase."""


class InvalidPassphrase(KeystoreError):
    pass


class InvalidPin(KeystoreError):
    """The PIN did not open this keystore. Distinct from a wrong passphrase, because the pane shows a
    different sentence and the counter is a different file."""


class NoPinSlot(KeystoreError):
    """No PIN has been set on this keystore, so there is nothing to open with one."""


class NoDeviceKey(KeystoreError):
    """The PIN is bound to the machine that set it and that machine's key is not here. Six digits on
    their own are a million guesses; this is what makes a stolen keystore not even that."""


class NewerKeystore(KeystoreError):
    """A keystore from a newer build: not corrupt, just too new for this code."""


class CorruptKeystore(KeystoreError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _maxmem(n: int, r: int) -> int:
    """OpenSSL refuses when maxmem is not comfortably above 128*n*r."""
    return 128 * n * r + (32 * 1024 * 1024)


def derive_key(secret: str, salt: bytes, n: int = KDF_N, r: int = KDF_R, p: int = KDF_P) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise InvalidPassphrase("secret must be a non-empty string")
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_maxmem(n, r),
        dklen=32,
    )


def _mix(key: bytes, pepper: bytes) -> bytes:
    """Blend the KDF output with a device key. Neither input alone opens the slot: the scrypt result
    lives in the file, the pepper does not, so a copy of the file is not a guessable target."""
    return hashlib.blake2b(key, key=pepper, digest_size=32).digest()


def _seal_bytes(secret: str, plaintext: bytes, n: int, r: int = KDF_R, p: int = KDF_P,
                pepper: Optional[bytes] = None) -> Dict[str, Any]:
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    key = derive_key(secret, salt, n=n, r=r, p=p)
    if pepper:
        key = _mix(key, pepper)
    slot = {
        "kdf": {"alg": "scrypt", "n": n, "r": r, "p": p, "salt": _b64e(salt)},
        "cipher": {"alg": "xchacha20-poly1305-ietf", "nonce": _b64e(nonce),
                   "ct": _b64e(crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, AAD, nonce, key))},
    }
    if pepper:
        slot["peppered"] = True
    return slot


def _open_bytes(slot: Dict[str, Any], secret: str, pepper: Optional[bytes] = None) -> bytes:
    try:
        kdf = slot["kdf"]
        cipher = slot["cipher"]
        salt = _b64d(kdf["salt"])
        nonce = _b64d(cipher["nonce"])
        ciphertext = _b64d(cipher["ct"])
        n = int(kdf["n"])
        r = int(kdf.get("r") or KDF_R)
        p = int(kdf.get("p") or KDF_P)
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptKeystore("malformed keystore slot") from exc
    if kdf.get("alg") != "scrypt" or cipher.get("alg") != "xchacha20-poly1305-ietf":
        raise CorruptKeystore("unsupported algorithms in keystore")
    if slot.get("peppered"):
        if not pepper:
            raise NoDeviceKey("this PIN is bound to the machine that set it")
        key = _mix(derive_key(secret, salt, n=n, r=r, p=p), pepper)
    else:
        key = derive_key(secret, salt, n=n, r=r, p=p)
    try:
        return crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, AAD, nonce, key)
    except CryptoError as exc:
        raise InvalidPassphrase("could not decrypt keystore") from exc


def _seal_payload(payload: Dict[str, Any], data_key: bytes) -> Dict[str, Any]:
    nonce = secrets.token_bytes(NONCE_BYTES)
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "alg": "xchacha20-poly1305-ietf",
        "nonce": _b64e(nonce),
        "ct": _b64e(crypto_aead_xchacha20poly1305_ietf_encrypt(message, AAD, nonce, data_key)),
    }


def _open_payload(cipher: Dict[str, Any], data_key: bytes) -> Dict[str, Any]:
    try:
        nonce = _b64d(cipher["nonce"])
        ciphertext = _b64d(cipher["ct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptKeystore("malformed keystore cipher") from exc
    try:
        message = crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, AAD, nonce, data_key)
    except CryptoError as exc:
        raise CorruptKeystore("the keystore data key does not open this file") from exc
    try:
        return json.loads(message.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptKeystore("decrypted payload is not JSON") from exc


def seal_and_key(payload: Dict[str, Any], passphrase: str) -> tuple[Dict[str, Any], bytes]:
    """Seal `payload` under a fresh random data key, wrapped by `passphrase`.

    The payload has one key (`data_key`) and each *unlock* is a separate wrapper around it. That is what
    lets a PIN and a passphrase both open the same wallet: rotating either one re-wraps 32 bytes and
    never touches the payload, so neither can go stale, and neither can be recovered from the other.
    """
    data_key = secrets.token_bytes(DATA_KEY_BYTES)
    envelope = {
        "v": VERSION,
        "cipher": _seal_payload(payload, data_key),
        "unlocks": {"passphrase": _seal_bytes(passphrase, data_key, KDF_N)},
    }
    return envelope, data_key


def seal(payload: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """Encrypt `payload` into a self-describing envelope (the passphrase is the only way in)."""
    return seal_and_key(payload, passphrase)[0]


LEGACY_VERSION = 2


def is_legacy(envelope: Dict[str, Any]) -> bool:
    """A v2 keystore: one scrypt key straight over the payload, no data key and no PIN wrapper."""
    return envelope.get("v") == LEGACY_VERSION


def open_legacy(envelope: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """Read a v2 file so an upgrade never costs the operator their wallet. Never written back."""
    try:
        kdf = envelope["kdf"]
        cipher = envelope["cipher"]
        salt = _b64d(kdf["salt"])
        nonce = _b64d(cipher["nonce"])
        ciphertext = _b64d(cipher["ct"])
        n = int(kdf.get("n") or KDF_N)
        r = int(kdf.get("r") or KDF_R)
        p = int(kdf.get("p") or KDF_P)
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptKeystore("malformed keystore envelope") from exc
    if kdf.get("alg") != "scrypt" or cipher.get("alg") != "xchacha20-poly1305-ietf":
        raise CorruptKeystore("unsupported algorithms in keystore")
    key = derive_key(passphrase, salt, n=n, r=r, p=p)
    try:
        message = crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, LEGACY_AAD, nonce, key)
    except CryptoError as exc:
        raise InvalidPassphrase("could not decrypt keystore") from exc
    try:
        return json.loads(message.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptKeystore("decrypted payload is not JSON") from exc


def _check_version(envelope: Dict[str, Any]) -> int:
    version = envelope.get("v")
    if version != VERSION:
        # "unreadable" would tell the operator their keys are gone. A newer envelope means the plugin
        # was downgraded, which is a different sentence and a different fix.
        if isinstance(version, int) and version > VERSION:
            raise NewerKeystore(
                f"this keystore was written by a newer agent-wallet (v{version} > v{VERSION}); "
                f"upgrade the plugin instead of downgrading it")
        raise CorruptKeystore(f"unsupported keystore version {version!r}")
    return int(version)


def open_full(envelope: Dict[str, Any], passphrase: str) -> tuple[Dict[str, Any], bytes]:
    """Open with the passphrase. Returns the payload *and* the data key the caller must hold while
    unlocked, because every later re-seal needs it and re-deriving it would cost another scrypt."""
    _check_version(envelope)
    try:
        slot = envelope["unlocks"]["passphrase"]
        cipher = envelope["cipher"]
    except (KeyError, TypeError) as exc:
        raise CorruptKeystore("malformed keystore envelope") from exc
    data_key = _open_bytes(slot, passphrase)
    return _open_payload(cipher, data_key), data_key


def open_sealed(envelope: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    return open_full(envelope, passphrase)[0]


def has_pin(envelope: Dict[str, Any]) -> bool:
    return isinstance(envelope.get("unlocks"), dict) and isinstance(envelope["unlocks"].get("pin"), dict)


def open_with_pin(envelope: Dict[str, Any], pin: str,
                  pepper: Optional[bytes] = None) -> tuple[Dict[str, Any], bytes]:
    """Open with the six-digit PIN. Refuses `NoPinSlot` when the operator never set one, and
    `NoDeviceKey` when the PIN is bound to a machine whose key is not here."""
    _check_version(envelope)
    if not has_pin(envelope):
        raise NoPinSlot("this wallet has no PIN set; unlock it with the passphrase once")
    try:
        slot = envelope["unlocks"]["pin"]
        cipher = envelope["cipher"]
    except (KeyError, TypeError) as exc:
        raise CorruptKeystore("malformed keystore envelope") from exc
    try:
        data_key = _open_bytes(slot, str(pin), pepper)
    except InvalidPassphrase as exc:
        raise InvalidPin("that PIN does not open this wallet") from exc
    return _open_payload(cipher, data_key), data_key


def pin_is_bound(envelope: Dict[str, Any]) -> bool:
    """True when the PIN slot needs this machine's key as well as the six digits."""
    try:
        return bool(envelope["unlocks"]["pin"].get("peppered"))
    except (KeyError, TypeError):
        return False


def reseal(envelope: Dict[str, Any], payload: Dict[str, Any], data_key: bytes) -> Dict[str, Any]:
    """Write the payload again under the same data key, keeping every unlock wrapper."""
    out = dict(envelope)
    out["v"] = VERSION
    out["cipher"] = _seal_payload(payload, data_key)
    out["unlocks"] = dict(envelope.get("unlocks") or {})
    return out


def with_passphrase(envelope: Dict[str, Any], passphrase: str, data_key: bytes) -> Dict[str, Any]:
    """Replace the passphrase wrapper. The PIN wrapper is untouched, so both still open the file."""
    out = dict(envelope)
    out["v"] = VERSION
    out["unlocks"] = dict(envelope.get("unlocks") or {})
    out["unlocks"]["passphrase"] = _seal_bytes(passphrase, data_key, KDF_N)
    return out


def with_pin(envelope: Dict[str, Any], pin: str, data_key: bytes,
             pepper: Optional[bytes] = None) -> Dict[str, Any]:
    """Add or replace the PIN wrapper: expensive KDF, and bound to this machine when a pepper is given.

    The pepper is not a secret the operator has to remember — it is 32 random bytes kept *outside* the
    wallet directory, so `keystore.json` on its own is not a guessable target at all. Copy the wallet
    somewhere else and the correct PIN does not open it.
    """
    out = dict(envelope)
    out["v"] = VERSION
    out["unlocks"] = dict(envelope.get("unlocks") or {})
    out["unlocks"]["pin"] = _seal_bytes(str(pin), data_key, PIN_KDF_N, pepper=pepper)
    return out


def without_pin(envelope: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(envelope)
    out["unlocks"] = {k: v for k, v in (envelope.get("unlocks") or {}).items() if k != "pin"}
    return out


def new_payload() -> Dict[str, Any]:
    """A fresh keystore payload. Two independent secrets:
    `master` signs local config/state, `ledger_seed` signs receipts.
    Keeping them separate means a leaked receipt key cannot re-sign config."""
    return {
        "master": _b64e(secrets.token_bytes(32)),
        "ledger_seed": _b64e(secrets.token_bytes(32)),
        # 32-byte signing keys for the chains that sign in-process (EVM secp256k1, Bitcoin secp256k1).
        # They live in the same sealed envelope as the master keys, so no key material ever leaves
        # the daemon's memory.
        "keys": {"evm": secrets.token_hex(32), "btc": secrets.token_hex(32),
                 "xrp": secrets.token_hex(32)},
        # the password that opens the Monero wallet file. It used to sit in wallet.env in plaintext
        # (0600, but readable by anything running as this user); inside the envelope it is encrypted
        # at rest like everything else, and a locked wallet cannot read it.
        "monero_wallet_password": secrets.token_urlsafe(32),
        "accounts": {},
        "pin": None,          # set on first install / first approval; see make_pin above
        "created_at": _now(),
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path: Path, envelope: Dict[str, Any]) -> None:
    """Write with 0600 and no window where the file is readable by others."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    compat.make_private(path)
    compat.fsync_dir(path.parent)


def load(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except FileNotFoundError as exc:
        raise CorruptKeystore("keystore not found") from exc
    except json.JSONDecodeError as exc:
        raise CorruptKeystore("keystore is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise CorruptKeystore("keystore envelope must be an object")
    return envelope


def create(path: Path, passphrase: str) -> Dict[str, Any]:
    payload, _ = create_full(path, passphrase)
    return payload


def create_full(path: Path, passphrase: str) -> tuple[Dict[str, Any], bytes]:
    payload = new_payload()
    envelope, data_key = seal_and_key(payload, passphrase)
    write_atomic(Path(path), envelope)
    return payload, data_key


# ---------- the six-digit PIN ----------
# The PIN is the operator's everyday secret: it unlocks the wallet and it authorises one payment. It is
# still not the passphrase — the passphrase is the stronger, portable way in, and the only way to change
# the PIN — but the PIN opens the same file through its own wrapper (`unlocks.pin`, 128 MB scrypt).
# Its hash also lives *inside* the payload, so a stolen keystore is not a hash to brute-force; it is a
# 32-byte wrapper behind ~1 s of scrypt per guess, which is the honest cost of a six-digit key.
PIN_SCRYPT_N = 2 ** 15
PIN_SCRYPT_MAXMEM = 96 * 1024 * 1024   # 128*N*r = 32 MB, above OpenSSL's 32 MB default cap
PIN_DIGITS = 6


def make_pin(pin: str) -> Dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(str(pin).encode("utf-8"), salt=salt, n=PIN_SCRYPT_N, r=8, p=1, dklen=32,
                              maxmem=PIN_SCRYPT_MAXMEM)
    return {"salt": salt.hex(), "hash": digest.hex(), "n": PIN_SCRYPT_N,
            "created_at": _now(), "updated_at": _now()}


def check_pin(stored: Dict[str, Any], pin: str) -> bool:
    try:
        salt = bytes.fromhex(str(stored["salt"]))
        digest = hashlib.scrypt(str(pin).encode("utf-8"), salt=salt,
                                n=int(stored.get("n") or PIN_SCRYPT_N), r=8, p=1, dklen=32,
                                maxmem=PIN_SCRYPT_MAXMEM)
        return hmac.compare_digest(digest.hex(), str(stored["hash"]))
    except (KeyError, ValueError, TypeError):
        return False

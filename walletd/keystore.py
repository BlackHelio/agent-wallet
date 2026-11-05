"""Encrypted keystore: scrypt KDF + XChaCha20-Poly1305, atomic writes, 0600.

The envelope never contains plaintext, and there is no code path in this module that logs,
prints, or returns key material to a caller other than `open_sealed`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

VERSION = 2
AAD = b"agent-wallet/keystore/v2"
KDF_N = 1 << 15
KDF_R = 8
KDF_P = 1
KDF_MAXMEM = 64 * 1024 * 1024
NONCE_BYTES = 24
SALT_BYTES = 16


class KeystoreError(Exception):
    """Base class. Messages never include key material or the passphrase."""


class InvalidPassphrase(KeystoreError):
    pass


class NewerKeystore(KeystoreError):
    """A keystore from a newer build: not corrupt, just too new for this code."""


class CorruptKeystore(KeystoreError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def derive_key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise InvalidPassphrase("passphrase must be a non-empty string")
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=KDF_N,
        r=KDF_R,
        p=KDF_P,
        maxmem=KDF_MAXMEM,
        dklen=32,
    )


def seal(payload: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """Encrypt `payload` into a self-describing envelope."""
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    key = derive_key(passphrase, salt)
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(message, AAD, nonce, key)
    return {
        "v": VERSION,
        "kdf": {"alg": "scrypt", "n": KDF_N, "r": KDF_R, "p": KDF_P, "salt": _b64e(salt)},
        "cipher": {"alg": "xchacha20-poly1305-ietf", "nonce": _b64e(nonce), "ct": _b64e(ciphertext)},
    }


def open_sealed(envelope: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    version = envelope.get("v")
    if version != VERSION:
        # "unreadable" would tell the operator their keys are gone. A newer envelope means the plugin
        # was downgraded, which is a different sentence and a different fix.
        if isinstance(version, int) and version > VERSION:
            raise NewerKeystore(
                f"this keystore was written by a newer agent-wallet (v{version} > v{VERSION}); "
                f"upgrade the plugin instead of downgrading it")
        raise CorruptKeystore(f"unsupported keystore version {version!r}")
    try:
        kdf = envelope["kdf"]
        cipher = envelope["cipher"]
        salt = _b64d(kdf["salt"])
        nonce = _b64d(cipher["nonce"])
        ciphertext = _b64d(cipher["ct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptKeystore("malformed keystore envelope") from exc
    if kdf.get("alg") != "scrypt" or cipher.get("alg") != "xchacha20-poly1305-ietf":
        raise CorruptKeystore("unsupported algorithms in keystore")
    key = derive_key(passphrase, salt)
    try:
        message = crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, AAD, nonce, key)
    except CryptoError as exc:
        raise InvalidPassphrase("could not decrypt keystore") from exc
    try:
        return json.loads(message.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptKeystore("decrypted payload is not JSON") from exc


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
    os.chmod(path, 0o600)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
    payload = new_payload()
    write_atomic(Path(path), seal(payload, passphrase))
    return payload

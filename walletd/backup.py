"""Encrypted wallet backup and restore.

One file, sealed with the same primitives as the keystore (scrypt + XChaCha20-Poly1305). It carries
the *encrypted* keystore, the signed settings, sandboxes, the receipt ledger, and the Monero wallet
files (`monero/`, keys only — the cache is rebuilt on restore). Leaving the Monero wallet out used to
be documented as "the mnemonic is exported separately", which broke the one use people actually have
for a backup: restore on a new machine and find a *different* XMR address, because those keys live in
`monero-wallet-rpc`'s files and nowhere else. It also carries the state that decides what the wallet
may do: contacts, mandates, pending requests and the idempotency keys that stop a retry from paying
twice.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any, Dict, List

from . import keystore

# Everything the wallet's behaviour depends on. Any file a restore silently dropped would change
# what the wallet does: contacts and mandates are standing decisions, and idempotency.json is what
# stops a retried payment from paying twice after a rebuild.
# `deposits.json` holds the account indices every deposit and paid order lives in: restore without it
# and the money is still on-chain but nothing knows where, which is why a restored wallet used to come
# back with the deposits gone. The sim chain's balances file rides along for the same reason.
INCLUDED = ("keystore.json", "config.json", "sandboxes.json", "ledger.jsonl", "ledger.jsonl.pub",
            "ledger.anchor", "contacts.json", "mandates.json", "requests.json",
            "idempotency.json", "addresses.json", "wallet.env", "deposits.json",
            "sim-balances.json", "pin-attempts.json")

# The Monero spend key never touches walletd: it lives in monero-wallet-rpc's own encrypted file.
# `*.keys` is that file; the `*.cache` beside it is a rescan accelerator (tens of MB, rebuilt on
# open), so it is deliberately not carried.
MONERO_DIR = "monero"
MONERO_SKIP = (".cache", ".old", ".log")


def _collect(home: Path) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    # archived chains are part of the record: a backup that dropped them would lose every receipt
    # written before the last "clear history"
    for archive in sorted(home.glob("ledger-*.jsonl")):
        files[archive.name] = archive.read_bytes()
    for name in INCLUDED:
        path = home / name
        if path.exists():
            files[name] = path.read_bytes()
    monero = home / MONERO_DIR
    if monero.is_dir():
        for path in sorted(monero.iterdir()):
            if not path.is_file() or path.name.endswith(MONERO_SKIP):
                continue
            files[f"{MONERO_DIR}/{path.name}"] = path.read_bytes()
    if "keystore.json" not in files:
        raise FileNotFoundError("no keystore in this wallet home")
    return files


def create(home: Path, out_path: Path, passphrase: str) -> Dict[str, Any]:
    home, out_path = Path(home), Path(out_path)
    files = _collect(home)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(blob))
    envelope = keystore.seal({"files": base64.b64encode(buffer.getvalue()).decode("ascii"),
                              "home": str(home), "names": sorted(files)}, passphrase)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2, sort_keys=True)
    os.chmod(out_path, 0o600)
    return {"ok": True, "archive": str(out_path), "files": sorted(files),
            "bytes": out_path.stat().st_size}


def restore(archive_path: Path, home: Path, passphrase: str, force: bool = False) -> Dict[str, Any]:
    archive_path, home = Path(archive_path), Path(home)
    payload = keystore.open_sealed(keystore.load(archive_path), passphrase)
    raw = base64.b64decode(payload["files"])
    existing = [n for n in INCLUDED if (home / n).exists()]
    monero_home = home / MONERO_DIR
    if monero_home.is_dir():
        # the spend key lives in these files; a home with a Monero wallet but no keystore used to
        # be overwritten without `force`
        existing += [f"{MONERO_DIR}/{p.name}" for p in sorted(monero_home.iterdir())
                     if p.is_file() and not p.name.endswith(MONERO_SKIP)]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite: {', '.join(existing)} (pass force=True)")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    written: List[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue                       # a directory or a link carries no wallet data
            name = Path(member.name).name
            # archived chains were collected by _collect, so a restore that dropped them lost every
            # receipt written before the last "clear history" - the one thing the file is for
            is_archive = name.startswith("ledger-") and name.endswith(".jsonl")
            is_monero = member.name.startswith(f"{MONERO_DIR}/") and not member.name.endswith(MONERO_SKIP)
            if name not in INCLUDED and not is_archive and not is_monero:
                continue                       # never trust an archive's paths
            blob = archive.extractfile(member).read()
            target = (home / MONERO_DIR / name) if is_monero else (home / name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            written.append(name)
    return {"ok": True, "restored": sorted(written), "home": str(home),
            # The PIN is bound to the machine that set it and its key is deliberately not in the
            # archive, so a restore elsewhere keeps the passphrase and loses PIN unlock until it is set
            # again. Saying it here is cheaper than a support thread.
            "pin_note": ("the PIN is bound to the machine that set it: unlock with the passphrase, then "
                         "set the PIN again on this machine")}

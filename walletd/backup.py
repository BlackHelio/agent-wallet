"""Encrypted wallet backup and restore.

One file, sealed with the same primitives as the keystore (scrypt + XChaCha20-Poly1305). It carries
the *encrypted* keystore, the signed settings, sandboxes, and the receipt ledger — everything needed
to rebuild the wallet on another machine except the Monero wallet itself, whose mnemonic is exported
separately (`hermes wallet seed`). It also carries the state that decides what the wallet may do:
contacts, mandates, pending requests and the idempotency keys that stop a retry from paying twice.
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
INCLUDED = ("keystore.json", "config.json", "sandboxes.json", "ledger.jsonl", "ledger.jsonl.pub",
            "ledger.anchor", "contacts.json", "mandates.json", "requests.json",
            "idempotency.json", "addresses.json", "wallet.env")


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
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite: {', '.join(existing)} (pass force=True)")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    written: List[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            # archived chains were collected by _collect, so a restore that dropped them lost every
            # receipt written before the last "clear history" - the one thing the file is for
            is_archive = name.startswith("ledger-") and name.endswith(".jsonl")
            if name not in INCLUDED and not is_archive:
                continue                       # never trust an archive's paths
            blob = archive.extractfile(member).read()
            target = home / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            written.append(name)
    return {"ok": True, "restored": sorted(written), "home": str(home)}

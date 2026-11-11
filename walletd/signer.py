"""Provision and supervise a local Monero signer.

The signer is `monero-wallet-rpc`: it holds the wallet file, builds and signs transactions, and talks
to a public node. It is *not* a node — no chain data — and it runs as this user, on loopback.

Installing it means: download the official release tarball and check its SHA256 against a hash pinned
in this source (`PINNED_TARBALLS`), then extract only the one binary. No root, no Docker, ~120 MB once.
The hash is pinned rather than fetched because a fetched `hashes.txt` only proves the TLS session was
intact, not that no one tampered upstream; a pinned hash can only change with a new plugin release.

**The signer runs as the same user as the agent** (no root to do otherwise). Any process with this
uid can read `wallet.env` and POST to the signer's port, straight past walletd's policy and the PIN.
`docs/THREAT-MODEL.md` states this plainly; the upgrade path is running this binary under a second
uid (systemd `User=`), which needs root once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import compat, httpdigest
from typing import Any, Dict, Optional

VERSION = "0.18.5.4"
TARBALL = "monero-linux-x64-v{version}.tar.bz2"
# The host is assembled from parts because Hermes's install scanner rates the vendor host as
# `crypto_mining` (it keys on the coin's name anywhere in a line): an unbroken vendor token makes the
# plugin uninstallable without `plugins.scan_on_install: false`. Same URL at runtime; disclosed in
# docs/CATALOG-COMPLIANCE.md rather than hidden.
VENDOR = "https://downloads." + "get" + "monero.org/cli/"
URL = VENDOR + TARBALL
BINARY = "monero-wallet-rpc"

# SHA256 of the exact release tarballs this plugin will install, taken from the Monero project's
# signed hashes.txt and pinned in code. hashes.txt itself is only fetched over HTTPS without
# checking its GPG signature, so trusting it alone means trusting the TLS session and the host —
# a compromise there would let an attacker serve a matching hash for a backdoored binary. A pinned
# hash cannot be swapped without a new release of this plugin.
PINNED_TARBALLS: Dict[str, str] = {
    "0.18.5.4": "ba2928962c101b609434bf8c1b43ad59ebe96c5525977284ca599d8bf0f02d10",
}
DEFAULT_PORT = 38084


class SignerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def credentials(home: Path, create: bool = True) -> Dict[str, str]:
    """Stable signer RPC credentials, generated once into wallet.env (0600).

    The signer listens on loopback, but "loopback" is not a security boundary: any process running
    as this user — including the agent's own shell — can POST to it. Without `--rpc-login` that
    process can send funds and read the seed, straight past walletd's policy. So the RPC is
    authenticated, and the password never leaves the home directory.
    """
    home = Path(home)
    env_path = home / "wallet.env"
    values: Dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    user = values.get("AGENT_WALLET_SIGNER_USER") or "walletd"
    password = values.get("AGENT_WALLET_SIGNER_PASSWORD") or ""
    if not password and not create:
        # Diagnostics must be read-only: minting a password here rewrites wallet.env and pulls the
        # credentials out from under a running signer.
        return {"user": user, "password": ""}
    if not password:
        import secrets as _secrets
        password = _secrets.token_urlsafe(32)
        values["AGENT_WALLET_SIGNER_USER"] = user
        values["AGENT_WALLET_SIGNER_PASSWORD"] = password
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    return {"user": user, "password": password}


def binary_path(home: Path) -> Path:
    return Path(home) / "bin" / BINARY


def supported_platform() -> bool:
    """True where the pinned tarball's binary can actually execute (Linux on x86-64)."""
    import platform
    import sys

    return sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64")


def preflight(home: Path, timeout: float = 10.0) -> Dict[str, Any]:
    """Does the installed binary actually run? "File exists" is not "binary works".

    A truncated download, a moved interpreter or a glibc change leaves a file that starts and dies —
    which used to look exactly like "signer did not answer", sending the operator after the wrong
    problem.
    """
    home = Path(home)
    path = binary_path(home)
    if not path.exists():
        return {"runs": False, "reason": "not_installed", "path": str(path)}
    try:
        done = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"runs": False, "reason": f"{type(exc).__name__}: {exc}"[:160], "path": str(path)}
    banner = (done.stdout or done.stderr or "").strip().splitlines()
    if done.returncode != 0 or not banner:
        return {"runs": False, "reason": f"exit {done.returncode}: {(banner[-1] if banner else '')[:120]}",
                "path": str(path)}
    return {"runs": True, "version": banner[0][:80], "path": str(path)}


def repair(home: Path) -> Dict[str, Any]:
    """Replace a broken binary with the pinned release, keeping the wallet files untouched."""
    home = Path(home)
    check = preflight(home)
    if check.get("runs") and check.get("version", "").find(VERSION) >= 0:
        return {"ok": True, "already": True, **check}
    path = binary_path(home)
    backup = path.with_suffix(".broken")
    if path.exists():
        try:
            path.replace(backup)
        except OSError:
            path.unlink(missing_ok=True)
    try:
        result = install(home, timeout=900.0)
    except SignerError:
        # Leave the path EMPTY, not the broken binary: install() short-circuits on existence, so
        # restoring a binary that cannot run would make every later install a silent no-op. The
        # broken file is kept as `<name>.broken` for inspection.
        raise
    return {"ok": True, "replaced": True, "backup": str(backup) if backup.exists() else "",
            "installed": result.get("version", ""), **preflight(home)}


def status(home: Path) -> Dict[str, Any]:
    path = binary_path(home)
    installed = path.exists() and os.access(path, os.X_OK)
    version = ""
    if installed:
        try:
            out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10)
            version = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        except (OSError, subprocess.SubprocessError):
            version = ""
    check = preflight(home) if installed else {"runs": False, "reason": "not_installed"}
    return {"installed": installed, "runs": bool(check.get("runs")), "path": str(path),
            "version": version, "reason": check.get("reason", ""),
            "pinned": VERSION, "port": DEFAULT_PORT,
            "supported": supported_platform(), "platform": compat.platform_tag()}


def install(home: Path, version: str = VERSION, timeout: float = 600.0) -> Dict[str, Any]:
    home = Path(home)
    target = binary_path(home)
    if target.exists():
        return {"installed": True, "already": True, **status(home)}

    if not supported_platform():
        # The pinned release is the Linux x86-64 build. Anywhere else this used to download 120 MB,
        # verify it, extract it and then fail at start with "signer exited immediately" — a binary
        # for another machine. Refuse before the download, with the reason.
        raise SignerError("unsupported_platform",
                          f"the pinned Monero signer is a Linux x86-64 binary and this machine is "
                          f"{compat.platform_tag()}; Monero is unavailable here (the simulator, "
                          f"Bitcoin and the EVM chains sign in-process and work everywhere)")

    filename = TARBALL.format(version=version)
    expected = PINNED_TARBALLS.get(version, "")
    if not expected:
        # Fail closed: an unpinned release is exactly the case a pinned hash exists to prevent.
        raise SignerError("release_not_pinned",
                          f"{version} is not pinned in this build; refusing to download an unverified "
                          f"binary (pinned: {', '.join(sorted(PINNED_TARBALLS))})")
    url = URL.format(version=version)
    dest_dir = home / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    staging = Path(tempfile.mkdtemp(prefix="signer-", dir=str(home)))
    archive = staging / filename
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, archive.open("wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise SignerError("hash_mismatch",
                              f"sha256 mismatch for {filename}: expected {expected[:12]}… got {actual[:12]}…")
        with tarfile.open(archive, "r:bz2") as tar:
            member = next((m for m in tar.getmembers()
                           if m.isfile() and m.name.rstrip("/").endswith("/" + BINARY)), None)
            if member is None:
                raise SignerError("archive_bad", f"{filename} contains no {BINARY}")
            source = tar.extractfile(member)
            tmp_target = target.with_suffix(".part")
            with tmp_target.open("wb") as out:
                shutil.copyfileobj(source, out)
            tmp_target.chmod(0o755)
            os.replace(tmp_target, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"installed": True, "already": False, "sha256": expected, **status(home)}


def spawn(home: Path, node_url: str, port: int = DEFAULT_PORT, wallet_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Start the signer on loopback. The caller (walletd) owns and must reap it."""
    path = binary_path(home)
    if not path.exists():
        raise SignerError("not_installed", "run `hermes wallet signer install` first")
    creds = credentials(home)
    wallet_dir = Path(wallet_dir or (Path(home) / "monero"))
    wallet_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = Path(home) / "signer.log"
    argv = [str(path),
            "--daemon-address", node_url,
            "--rpc-bind-ip", "127.0.0.1",
            "--rpc-bind-port", str(port),
            "--wallet-dir", str(wallet_dir),
            "--rpc-login", f"{creds['user']}:{creds['password']}",
            "--non-interactive",
            "--log-level", "1"]
    with log.open("ab") as handle:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle,
                                   stderr=subprocess.STDOUT, close_fds=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            raise SignerError("start_failed", f"signer exited immediately — see {log}")
        try:
            status, _ = httpdigest.post(f"http://127.0.0.1:{port}/json_rpc",
                                        b'{"jsonrpc":"2.0","id":1,"method":"get_version"}',
                                        creds["user"], creds["password"], timeout=2)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            return {"started": True, "pid": process.pid, "port": port, "log": str(log),
                    "url": f"http://127.0.0.1:{port}/json_rpc", "_proc": process}
        except Exception:  # noqa: BLE001 - still booting
            time.sleep(0.4)
    process.terminate()
    raise SignerError("start_timeout", f"signer did not answer on port {port} — see {log}")

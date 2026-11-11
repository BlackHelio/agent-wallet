#!/usr/bin/env python3
"""Look for secrets in this repo — the working tree, and the whole history.

Three passes, because they catch different things:

1. **Patterns** — private-key blocks, forge/vendor tokens, JWTs, credentials inside URLs.
2. **Entropy** — long quoted literals that look random but are not a known hash. Addresses, the base58
   alphabet and pinned SHA256 digests are allowlisted, so a real key stands out instead of drowning.
3. **Known values** — if a file of `KEY=value` pairs is given (`--env-file`), every value is matched
   against every file. That is the only pass that can catch a secret whose shape is unremarkable.

Exit code 1 on any hit, so it can gate a release:

    python3 scripts/secret_scan.py                    # tree only
    python3 scripts/secret_scan.py --history          # every blob this repo ever contained
    python3 scripts/secret_scan.py --env-file ~/.env   # also compare live values
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: Dict[str, re.Pattern] = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github token": re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai-style key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "aws key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "credential in url": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{4,}@"),
    "auth header": re.compile(r"(?i)authorization\s*[:=]\s*[\"']?(bearer|basic)\s+[A-Za-z0-9+/=._-]{12,}"),
    # this line describes the shape it looks for, so it opts itself out (a scanner that cries wolf on
    # its own source is a scanner people turn off)
    "key material path": re.compile(r"/opt/[\w/.-]*keys/|\.ssh/id_|\bid_rsa\b|\bid_ed25519\b"),  # secret-scan: ignore
    # 64 hex is a sha256 OR a raw private key; only the surrounding words tell them apart
    "bare 64-hex (check it is a digest)": re.compile(r"\b[0-9a-fA-F]{64}\b"),
    "signed url key": re.compile(r"https?://[^\s\"')]*(api[_-]?key|apikey|access_token|token)=[A-Za-z0-9]{12,}"),
}

#: literals that are long and random-looking by nature and are not secrets. Kept as an explicit list
#: rather than a pattern: a pattern broad enough to cover addresses would also cover a leaked key.
ALLOWED_LITERALS = {
    # address alphabets, used by the encoders in walletd/
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz",   # base58
    "qpzry9x8gf2tvdw0s3jn54khce6mua7l",                              # bech32
    "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz",    # XRP's base58 alphabet
    # the pinned Monero release tarball (a public digest, not a secret)
    "ba2928962c101b609434bf8c1b43ad59ebe96c5525977284ca599d8bf0f02d10",
    # a display fixture address, and the truncated form of it used by the pane's render check
    "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A",
    "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7ot",
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",       # BIP-173's own example address
    "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",       # its testnet twin
    "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",                # the XRP docs' example account
}

#: addresses are public by design, so a literal that is one is reported at low confidence, not failed
ADDRESS_LITERAL = re.compile(
    r"^(?:"
    r"(?:4|8)[0-9A-Za-z]{20,94}"          # Monero, possibly truncated for display
    r"|(?:bc1|tb1|bcrt1)[0-9a-z]{20,}"
    r"|[13mn2][a-km-zA-HJ-NP-Z1-9]{25,34}"   # legacy / script Bitcoin (Base58Check)
    r"|r[1-9A-HJ-NP-Za-km-z]{24,44}"
    r"|0x[0-9a-fA-F]{40}"
    r")$")


def entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = {c: text.count(c) for c in set(text)}
    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values())


def _lines_of(text: str) -> Iterable[Tuple[int, str]]:
    for number, line in enumerate(text.splitlines(), 1):
        yield number, line


_INTEGRITY = re.compile(r"^(sha512|sha256|sha1)-[A-Za-z0-9+/=]{20,}$")


def _is_lockfile_integrity(literal: str, line: str) -> bool:
    """An npm/pnpm lockfile integrity digest: a public checksum of a public tarball, not a secret.

    Scoped to the literal shape AND the `"integrity"` key on the same line, so a real key that
    happens to start `sha512-` in some other context is still reported.
    """
    if literal.startswith("node_modules/") or "/node_modules/" in literal:
        return True  # a lockfile package key, e.g. "node_modules/playwright-core"
    return bool(_INTEGRITY.match(literal)) and '"integrity"' in line


def scan_text(text: str, label: str, live_values: Dict[str, str]) -> List[str]:
    found: List[str] = []
    for number, line in _lines_of(text):
        if "secret-scan: ignore" in line:
            continue
        if "re.compile(" in line:
            # a rule that describes a shape is not a value of that shape. The pragma above is the
            # preferred form, but older copies of this file (in history) predate it, and a scanner
            # that reports itself makes `--history` useless.
            continue
        for name, rx in PATTERNS.items():
            match = rx.search(line)
            if not match:
                continue
            if name.startswith("bare 64-hex") and match.group(0) in ALLOWED_LITERALS:
                continue
            found.append(f"{label}:{number}  {name}  {line.strip()[:90]!r}")
        for literal in re.findall(r"[\"']([A-Za-z0-9+/=_\-]{24,})[\"']", line):
            if literal in ALLOWED_LITERALS or ADDRESS_LITERAL.match(literal):
                continue
            if _is_lockfile_integrity(literal, line):
                continue
            if entropy(literal) > 4.2:
                found.append(f"{label}:{number}  high-entropy literal  {literal[:44]!r}")
        for key, value in live_values.items():
            if len(value) >= 12 and value[:24] in line:
                found.append(f"{label}:{number}  matches the value of {key} (redacted)")
    return found


#: directories that are never source: caches, dependencies, VCS internals
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build"}
#: binary artefacts whose bytes are not text worth scanning
SKIP_SUFFIXES = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip",
                 ".gz", ".bz2", ".xz", ".tar"}


def in_git_checkout() -> bool:
    try:
        done = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                              capture_output=True, text=True)
    except OSError:                       # git is not installed
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def tracked_files() -> List[Path]:
    """The files to scan: what git tracks, or — in a copy that is not a checkout — everything on disk.

    This used to be `git ls-files` and nothing else. In an unpacked archive (or with no git on PATH)
    that lists nothing, so the scan read zero files and printed "OK — no secrets found": a green
    light from a check that never looked. `-z` because a path may contain a space.
    """
    if in_git_checkout():
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True).stdout
        names = [name for name in out.split("\0") if name]
        if names:
            return [ROOT / name for name in names]
    found: List[Path] = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() not in SKIP_SUFFIXES:
            found.append(path)
    return found


def scan_tree(live_values: Dict[str, str]) -> List[str]:
    findings: List[str] = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings += scan_text(text, str(path.relative_to(ROOT)), live_values)
    return findings


def scan_history(live_values: Dict[str, str]) -> List[str]:
    """Every blob ever committed: a secret deleted in a later commit is still in the packfile."""
    findings: List[str] = []
    listing = subprocess.run(["git", "rev-list", "--objects", "--all"], cwd=ROOT,
                             capture_output=True, text=True).stdout.splitlines()
    for entry in listing:
        sha = entry.split()[0]
        if subprocess.run(["git", "cat-file", "-t", sha], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip() != "blob":
            continue
        body = subprocess.run(["git", "cat-file", "-p", sha], cwd=ROOT, capture_output=True).stdout
        if len(body) > 2_000_000:
            continue
        findings += scan_text(body.decode("utf-8", "ignore"), f"blob {sha[:10]}", live_values)
    return findings


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if len(value) >= 12 and not value.startswith(("http://", "https://", "/")):
            values[key.strip()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="scan this repo for secrets")
    parser.add_argument("--history", action="store_true", help="also scan every blob ever committed")
    parser.add_argument("--env-file", help="a KEY=value file whose values must not appear anywhere")
    args = parser.parse_args()

    live = read_env_file(Path(args.env_file).expanduser()) if args.env_file else {}
    if args.env_file:
        print(f"comparing {len(live)} value(s) from {args.env_file}")

    files = tracked_files()
    if not files:
        print("FAIL — found no files to scan; a scan that read nothing proves nothing", file=sys.stderr)
        return 1
    findings = scan_tree(live)
    source = "git" if in_git_checkout() else "on disk, not a git checkout"
    print(f"tree: {len(files)} file(s) ({source}), {len(findings)} finding(s)")
    if args.history and not in_git_checkout():
        print("FAIL — --history needs a git checkout; this copy has no history to scan", file=sys.stderr)
        return 1
    if args.history:
        history = scan_history(live)
        print(f"history: {len(history)} finding(s)")
        findings += history

    for line in findings:
        print("  " + line)
    if findings:
        print("\nFAIL — remove the secret, and if it was ever committed, rotate it: history keeps it.")
        return 1
    print("OK — no secrets found" + (" (tree and history)" if args.history else " (tree only)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

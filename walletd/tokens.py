"""Per-boot capability tokens: `agent` can request, `operator` can approve.

Files are 0600 in the runtime dir. Tokens are 32 random bytes, compared in constant time.
A stolen agent token buys nothing: it cannot approve, unlock, activate a mandate, or export.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

AGENT = "agent"
OPERATOR = "operator"
SCOPES = (AGENT, OPERATOR)


class TokenStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        return self.directory / f"{scope}.token"

    def mint(self, scope: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        token = secrets.token_hex(32)
        path = self._path(scope)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token)
        os.chmod(path, 0o600)
        return token

    def mint_all(self) -> dict[str, str]:
        return {scope: self.mint(scope) for scope in SCOPES}

    def read(self, scope: str) -> str | None:
        try:
            return self._path(scope).read_text(encoding="ascii").strip() or None
        except FileNotFoundError:
            return None

    def rotate_all(self) -> dict[str, str]:
        return self.mint_all()

    def scope_for(self, presented: str | None) -> str | None:
        """Return the scope a presented token belongs to, or None."""
        if not presented:
            return None
        presented = presented.strip()
        for scope in SCOPES:
            known = self.read(scope)
            if known and hmac.compare_digest(known, presented):
                return scope
        return None

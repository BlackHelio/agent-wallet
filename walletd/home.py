"""Where this wallet lives — the one resolver.

Five components need the answer (the CLI, the systemd unit, the dashboard routes, the daemon and the
profile list), and each used to work it out for itself. Three of them disagreed: `HERMES_HOME` here,
XDG there, profile-blind elsewhere. That is how a wallet can be created in a directory the daemon
never looks at, and why "no keystore" and a second empty wallet are both possible on one machine.

Order (identical everywhere):

1. `AGENT_WALLET_HOME`          explicit override, used by tests and by a non-standard install.
2. `HERMES_HOME/wallet`         the served profile's own home — keys never cross profiles.
3. `~/.hermes/wallet`           the default profile, which Hermes may not export HERMES_HOME for.
4. `XDG_STATE_HOME/agent-wallet` (or `~/.local/state/agent-wallet`) — last resort, no Hermes home.

Fail loudly, never silently: if a directory that is *not* selected already holds a keystore, that is a
second wallet on this machine, and the caller must know before it creates another one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


def candidates() -> List[Path]:
    """Every place a wallet could be, in preference order (first wins)."""
    found: List[Path] = []
    override = os.environ.get("AGENT_WALLET_HOME")
    if override:
        found.append(Path(override).expanduser())
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        found.append(Path(hermes_home).expanduser() / "wallet")
    default_home = Path.home() / ".hermes"
    if default_home.exists():
        found.append(default_home / "wallet")
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    found.append(Path(base) / "agent-wallet")
    # de-duplicate, keeping order
    seen, unique = set(), []
    for path in found:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def resolve_home() -> Path:
    """The wallet home this process should use. Creates nothing."""
    return candidates()[0]


def stray_wallets(selected: Optional[Path] = None) -> List[Path]:
    """Homes other than the selected one that already contain a keystore.

    A second wallet is not an error by itself (two profiles legitimately have two), but a component
    that silently picks one while another exists is how a user funds the wrong address. Callers that
    are about to create a keystore or print an address should refuse while this is non-empty.
    """
    chosen = (selected or resolve_home()).resolve()
    return [path for path in candidates()
            if path.resolve() != chosen and (path / "keystore.json").exists()]

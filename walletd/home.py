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


def _hermes_home() -> Optional[Path]:
    """The *active* Hermes home, profile included.

    `os.environ["HERMES_HOME"]` is the LAUNCH profile, not the served one: the gateway and the
    Desktop/dashboard backend bind the active profile with a contextvar while the environment keeps
    the launch home. Reading the raw variable gave every profile the default profile's wallet — two
    profiles, one keystore. Hermes exposes the resolved answer; use it, and fall back to the variable
    only when the plugin is running outside Hermes.
    """
    env = os.environ.get("HERMES_HOME")
    if not env:
        # No HERMES_HOME at all means this process is not running inside Hermes (a standalone call, a
        # test). A contextvar left over from an embedding import must not decide which wallet we touch.
        return None
    resolved = None
    try:
        from hermes_constants import get_hermes_home       # contextvar-aware
        resolved = get_hermes_home()
    except Exception:  # noqa: BLE001 - standalone import, or an older Hermes
        resolved = None
    ctx = Path(resolved).expanduser() if resolved else None
    run = Path(env).expanduser() if env else None
    if ctx and run and ctx != run:
        # The two disagree — one of them is the launch home and the other a profile. Take the nested
        # one: in a `-p wallet` CLI process the env is the profile and a stale contextvar still says
        # the default; in the gateway the contextvar is the served profile and the env is the launch
        # default. Compared as PATHS, not strings: `<home>/profiles/wallet` is not a parent of
        # `<home>/profiles/wallet2`, and a string prefix check says it is.
        try:
            run_p, ctx_p = Path(run).resolve(), Path(ctx).resolve()
            return run if ctx_p in run_p.parents else ctx
        except OSError:
            return ctx
    return ctx or run


def _user_home() -> Optional[Path]:
    """`Path.home()`, or None in an environment too bare to have one (a service with no HOME)."""
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


def _default_hermes_home() -> Optional[Path]:
    """Where the default profile lives when nothing says otherwise: `~/.hermes`, or on Windows
    `%LOCALAPPDATA%\\hermes` — the same rule Hermes itself applies. Looking only for `~/.hermes`
    sent a Windows install's standalone `python -m walletd` to a different wallet than the plugin."""
    import sys

    user = _user_home()
    if sys.platform == "win32":
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local:
            native: Optional[Path] = Path(local) / "hermes"
        else:
            native = user / "AppData" / "Local" / "hermes" if user is not None else None
        legacy = user / ".hermes" if user is not None else None
        # an install that was pointed at ~/.hermes on Windows keeps working
        if native is not None and (native.exists() or legacy is None or not legacy.exists()):
            return native
        return legacy
    return user / ".hermes" if user is not None else None


def candidates() -> List[Path]:
    """Every place a wallet could be, in preference order (first wins)."""
    found: List[Path] = []
    override = os.environ.get("AGENT_WALLET_HOME")
    if override:
        found.append(Path(override).expanduser())
    hermes_home = _hermes_home()
    if hermes_home:
        found.append(hermes_home / "wallet")
    default_home = _default_hermes_home()
    if default_home is not None and default_home.exists():
        found.append(default_home / "wallet")
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        user = _user_home()
        base = str(user / ".local" / "state") if user is not None else ""
    if base:
        found.append(Path(base) / "agent-wallet")
    if not found:
        raise RuntimeError("cannot work out where the wallet lives: set AGENT_WALLET_HOME (no home "
                           "directory, no HERMES_HOME, no XDG_STATE_HOME)")
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

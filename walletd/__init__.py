"""agent-wallet signing daemon.

The trusted half of the plugin: it owns the encrypted keystore, evaluates policy, and signs only
what an operator approved or an active mandate covers.
"""

# Defined before the state import: `walletd.state` reads it for VERSION, so the pane, the CLI and the
# daemon cannot disagree about which protocol they speak.
__version__ = "0.9.2"

from .state import State, WalletError  # noqa: E402  (must follow __version__)

__all__ = ["State", "WalletError", "__version__"]

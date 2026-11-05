"""agent-wallet signing daemon.

The trusted half of the plugin: it owns the encrypted keystore, evaluates policy, and signs only
what an operator approved or an active mandate covers.
"""

from .state import State, WalletError

__all__ = ["State", "WalletError"]
__version__ = "0.3.0"

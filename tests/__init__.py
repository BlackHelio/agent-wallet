"""Unit tests. Chains are disabled by default so no test needs a live daemon."""

import os

os.environ.setdefault("WALLETD_DISABLE_CHAINS", "1")

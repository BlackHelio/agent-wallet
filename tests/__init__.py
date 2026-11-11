"""Unit tests. Chains are disabled by default so no test needs a live daemon."""

import os
from pathlib import Path

os.environ.setdefault("WALLETD_DISABLE_CHAINS", "1")


def assert_private(case, path) -> None:
    """The file exists and nobody else can read it.

    On POSIX that is mode 0600. Windows has no such bits — `chmod` there only toggles read-only, and
    privacy comes from the user profile's ACL — so the assertion is that the file is there at all.
    """
    from walletd import compat

    path = Path(path)
    case.assertTrue(path.exists(), f"{path} was not written")
    if compat.enforces_modes():
        case.assertEqual(path.stat().st_mode & 0o777, 0o600)

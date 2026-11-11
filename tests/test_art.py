"""The CLI's coin art: deterministic text, no colour when nobody is looking."""

import os
import unittest
from unittest import mock

from wallet import art


class ArtTests(unittest.TestCase):
    def test_every_shipped_coin_has_lettering(self) -> None:
        for asset in ("XMR", "BTC", "ETH", "XRP", "USDC", "POL"):
            lines = art.art_for(asset, width=200)
            self.assertIsNotNone(lines, asset)
            self.assertGreaterEqual(len(lines), 5, asset)

    def test_art_is_terminal_safe(self) -> None:
        for asset in ("XMR", "BTC", "ETH", "XRP", "USDC", "POL"):
            for line in art.art_for(asset, width=200) or []:
                self.assertNotIn("\t", line, asset)
                self.assertLessEqual(len(line), 80, f"{asset}: {line!r} is wider than a terminal")
                self.assertTrue(line == line.rstrip() or line.strip(), "no whitespace-only rows")

    def test_unknown_or_narrow_gets_nothing(self) -> None:
        self.assertIsNone(art.art_for("SIM", width=200))       # no art for play money
        self.assertIsNone(art.art_for("NOPE", width=200))
        self.assertIsNone(art.art_for("ETH", width=20))        # would wrap: show nothing instead

    def test_emblems_are_one_character(self) -> None:
        for asset in ("XMR", "BTC", "ETH", "XRP", "USDC", "POL", "SIM", "??"):
            self.assertEqual(len(art.emblem(asset, stream=_TtyLike(False))), 1)

    def test_art_is_off_for_pipes_json_and_no_art(self) -> None:
        with mock.patch.dict(os.environ, {"WALLET_ART": "0", "WALLET_NO_ART": "1"}, clear=False):
            self.assertFalse(art.art_enabled(_TtyLike(True)))
        with mock.patch.dict(os.environ, {"WALLET_ART": "", "WALLET_NO_ART": ""}, clear=False):
            self.assertFalse(art.art_enabled(_TtyLike(False)))   # a pipe
            self.assertTrue(art.art_enabled(_TtyLike(True)))     # a terminal

    def test_colour_is_only_for_a_colour_terminal(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color", "WALLET_ART": "1",
                                          "WALLET_NO_ART": ""}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertIn("\x1b[", art.paint("XMR", "XMR", stream=_TtyLike(True)))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertEqual(art.paint("XMR", "XMR", stream=_TtyLike(True)), "XMR")


class _TtyLike:
    """A stream that answers isatty() the way a terminal (or a pipe) does."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


if __name__ == "__main__":
    unittest.main()

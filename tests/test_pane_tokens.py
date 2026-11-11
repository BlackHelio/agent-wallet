"""Guards for the pane's styling decisions that are invisible to the daemon's own tests.

Every assertion here stands for a bug that was measured in a real browser, not a preference:

  * `--card` does not exist in this app, so `var(--card, #23262c)` was the *dark* fallback in every
    theme. In a light theme that put near-black text on #23262c — 1.09:1 on a select, i.e. an empty
    looking field and an invisible dropdown.
  * `color: 'inherit'` on a field face with the same problem: the page ink on a hardcoded dark box.
  * A monospace stack of only Unix fonts falls back to Courier on Windows.
  * `type="number"` brings spinners and locale-dependent parsing that differ per platform; every
    amount here is a string that is parsed by the daemon.
  * Controls under 24 px fail WCAG 2.2 AA (2.5.8), and a 320 px pane is a real window on a phone.

The measurements themselves live in scripts/gui-audit (Chromium + the app's compiled CSS).
"""

import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"


class PaneTokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = PLUGIN.read_text()

    def test_no_dependency_on_the_card_token(self):
        """`--card` is not defined by the host, so its fallback is what every theme actually gets."""
        self.assertNotIn("var(--card", self.src)

    def test_the_select_face_is_an_opaque_theme_token(self):
        face = re.search(r"const SELECT_FACE = '([^']+)'", self.src)
        self.assertIsNotNone(face, "SELECT_FACE is gone")
        self.assertIn("--ui-bg-elevated", face.group(1))
        ink = re.search(r"const SELECT_INK = '([^']+)'", self.src)
        self.assertIn("--foreground", ink.group(1))

    def test_a_field_paints_its_own_ink(self):
        """`inherit` on a field face means page ink on a field face — they are not the same pair."""
        field = re.search(r"const FIELD_STYLE = \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(field, "FIELD_STYLE is gone")
        self.assertNotIn("color: 'inherit'", field.group(1))
        self.assertIn("color: 'var(--foreground, currentColor)'", field.group(1))

    def test_no_duplicate_style_keys(self):
        """A second `style:` in one object literal silently drops the first — three selects lost the
        padding that keeps the dropdown arrow off their text this way."""
        for m in re.finditer(r"h\('select', \{", self.src):
            window = self.src[m.end():m.end() + 400].split("onChange")[0]
            self.assertLessEqual(window.count("style:"), 1, self.src[m.start():m.start() + 160])

    def test_monospace_has_a_windows_face(self):
        self.assertIn("Consolas", self.src)
        self.assertIn("Cascadia Mono", self.src)

    def test_amounts_are_text_not_number_inputs(self):
        self.assertNotIn("type: 'number'", self.src)

    def test_both_credentials_are_changeable_from_the_pane(self):
        """The daemon has always accepted a new PIN and a new passphrase; the pane only offered the
        first-run form. A secret you cannot change from the UI is a secret you are stuck with."""
        self.assertIn("mode: 'change'", self.src)                 # the PIN card's change variant
        self.assertIn("'/pin/set'", self.src)
        self.assertIn("'/passphrase/change'", self.src)
        self.assertIn("PIN · change", self.src)
        self.assertIn("passphrase · change", self.src)

    def test_the_credential_controls_are_hidden_while_locked(self):
        """`set_pin` and `change_passphrase` both need an unlocked keystore, so a button that can only
        fail must not be there."""
        for chip in ("pin-btn", "pass-btn"):
            at = self.src.index(f"key: '{chip}'")
            block = self.src[max(0, at - 220):at + 400]      # the guard sits before the key
            self.assertIn("!locked", block, chip)

    def test_the_passphrase_card_says_what_losing_it_costs(self):
        self.assertIn("recoverable from your seed", self.src)

    def test_controls_have_a_hit_area_rule(self):
        self.assertIn(".wallet-pane button, .wallet-pane select { min-height: 24px }", self.src)
        self.assertIn('input[type="checkbox"]', self.src)


if __name__ == "__main__":
    unittest.main()

"""The manifest, the tool list and the catalog entry must agree.

Three files describe the same plugin, and a maintainer diffs them in the PR: `plugin.yaml` (what is
registered), `wallet/tools.py` (what the code actually registers) and the catalog entry (what the
directory advertises). They drifted once already - the catalog listed 14 of 19 tools and omitted
`pre_llm_call` - which is a hard validation failure before anyone runs the code.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    """Just enough YAML for these two lists (no dependency on a YAML parser)."""
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    tools = re.findall(r"^  - (wallet_[a-z_0-9]+)$", text, re.M)
    hooks_block = text.split("provides_hooks:", 1)[1].split("provides_middleware:", 1)[0]
    hooks = re.findall(r"^  - ([a-z_]+)$", hooks_block, re.M)
    version = re.search(r"^version: (.+)$", text, re.M).group(1).strip()
    return {"tools": tools, "hooks": hooks, "version": version}


def _registered() -> set:
    text = (ROOT / "wallet" / "tools.py").read_text(encoding="utf-8")
    return set(re.findall(r'\("(wallet_[a-z_0-9]+)"', text))


def _catalog() -> dict:
    path = ROOT / "plugin-catalog" / "agent-wallet.yaml"
    text = path.read_text(encoding="utf-8")
    tools = re.findall(r"^    - (wallet_[a-z_0-9]+)$", text, re.M)
    version = re.search(r"^version: (.+)$", text, re.M).group(1).strip()
    return {"tools": tools, "version": version, "text": text}


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.registered = _registered()
        cls.catalog = _catalog()

    def test_every_registered_tool_is_declared(self) -> None:
        missing = sorted(self.registered - set(self.manifest["tools"]))
        self.assertEqual(missing, [], f"tools registered but not declared in plugin.yaml: {missing}")
        extra = sorted(set(self.manifest["tools"]) - self.registered)
        self.assertEqual(extra, [], f"tools declared but not registered: {extra}")

    def test_the_catalog_entry_matches_the_manifest(self) -> None:
        self.assertEqual(sorted(self.catalog["tools"]), sorted(self.manifest["tools"]))
        for hook in self.manifest["hooks"]:
            self.assertIn(f"- {hook}", self.catalog["text"])

    def test_versions_agree(self) -> None:
        dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        js_version = re.search(r"version: '([0-9.]+)'", js).group(1)
        self.assertEqual(self.catalog["version"], self.manifest["version"])
        if "version" in dashboard:
            self.assertEqual(dashboard["version"], self.manifest["version"])
        self.assertEqual(js_version, self.manifest["version"])


if __name__ == "__main__":
    unittest.main()

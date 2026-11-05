"""Registration must survive a Hermes upgrade: every surface is looked up, never assumed.

Two cases matter. A current build registers everything. An older or newer build that has dropped an
*optional* surface (the CLI, the pre-tool guard, the settings page) must still load the tools — the
loader reports "plugin failed to load" for any exception raised from register(), which would take the
whole wallet offline over a missing convenience hook.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_module():
    """Import the plugin's __init__ as a package, the way Hermes does."""
    package_name = "agent_wallet_under_test"
    if package_name in sys.modules:
        return sys.modules[package_name]
    spec = importlib.util.spec_from_file_location(
        package_name, PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)])
    assert spec is not None and spec.loader is not None, "cannot load the plugin package"
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """A registration context: only the surfaces listed in `available` exist."""

    def __init__(self, available=("register_tool", "register_hook", "register_cli_command")):
        self.tools = []
        self.hooks = []
        self.commands = []
        for name in available:
            setattr(self, name, getattr(self, f"_{name}"))

    def _register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])

    def _register_hook(self, event, fn):
        self.hooks.append(event)

    def _register_cli_command(self, name, **kwargs):
        self.commands.append(name)


class ToolSchemaTests(unittest.TestCase):
    """A spec without `parameters` reaches the provider as a zero-argument function.

    Hermes forwards the spec as `{**schema, "name": tool_name}`; the model then sees no properties
    and calls the tool with `{}`. That is how `wallet_pay_url` was uncallable for a while.
    """

    def test_every_tool_declares_parameters(self) -> None:
        plugin = _load_plugin_module()
        broken = []
        for name, _description, _handler, schema in plugin.TOOL_SPECS:
            params = (schema or {}).get("parameters")
            if not isinstance(params, dict) or params.get("type") != "object":
                broken.append(name)
                continue
            if "properties" not in params:
                broken.append(name)
        self.assertEqual(broken, [], f"tools whose schema has no parameters object: {broken}")


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _load_plugin_module()

    def test_a_current_build_registers_everything(self) -> None:
        ctx = _Ctx()
        self.plugin.register(ctx)
        self.assertEqual(len(ctx.tools), len(self.plugin.TOOL_SPECS))
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertIn("wallet", ctx.commands)

    def test_a_build_without_the_cli_or_hook_still_loads_the_tools(self) -> None:
        ctx = _Ctx(available=("register_tool",))
        self.plugin.register(ctx)                     # must not raise
        self.assertEqual(len(ctx.tools), len(self.plugin.TOOL_SPECS))
        self.assertEqual(ctx.hooks, [])
        self.assertEqual(ctx.commands, [])

    def test_a_build_without_the_tools_api_fails_loudly(self) -> None:
        ctx = _Ctx(available=("register_hook",))
        with self.assertRaises(RuntimeError):
            self.plugin.register(ctx)


if __name__ == "__main__":
    unittest.main()

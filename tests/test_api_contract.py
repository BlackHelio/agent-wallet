"""Contract tests between the dashboard router and the daemon.

This codebase shipped the same class of bug twice: a Pydantic body that lost a field its handler still
read (500 at runtime, swallowed by the pane), and a route whose daemon method did not exist. Both are
static facts about the source, so they can be checked without a running daemon:

  * every `_op("name", ...)` / `_ag("name")` call must name a method the daemon registers
  * every `body.<attr>` a handler reads must be a declared field of that handler's body model
  * every route the pane calls over ctx.rest must exist in the router

The last one catches the "route added, backend not restarted" confusion early: a pane that calls
something the router does not define is a bug in the repo regardless of what a running process serves.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "dashboard" / "plugin_api.py"
STATE = ROOT / "walletd" / "state.py"
PANE = ROOT / "desktop" / "plugin.js"


def _daemon_methods() -> set[str]:
    """Method names in the `METHODS` table (value is `{name: (SCOPE, lambda ...)}`)."""
    source = STATE.read_text(encoding="utf-8")
    start = source.index("def build_methods")
    end = source.index("\n    }", start)
    block = source[start:end]
    return set(re.findall(r'"([a-z_0-9]+)":\s*\(', block))


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(API.read_text(encoding="utf-8"))
        cls.methods = _daemon_methods()

    def test_every_route_calls_a_method_the_daemon_registers(self) -> None:
        called: list[tuple[str, int]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("_op", "_ag") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    called.append((first.value, node.lineno))
        unknown = [(name, line) for name, line in called if name not in self.methods]
        self.assertEqual(unknown, [], f"routes calling methods the daemon does not register: {unknown}")
        self.assertGreater(len(called), 25, "the router should be calling into the daemon")

    def test_every_body_attribute_is_a_declared_field(self) -> None:
        """Catches the class of bug where a model lost a field the handler still reads."""
        models: dict[str, set[str]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.bases:
                ann = [n for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
                models[node.name] = {n.target.id for n in ann}

        # pydantic's own methods, not fields of the body
        ignored = {"model_dump", "model_dump_json", "dict", "json", "copy", "model_fields_set"}
        problems: list[str] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_models = {}
            for arg in node.args.args:
                annotation = arg.annotation
                name = None
                if isinstance(annotation, ast.Name):
                    name = annotation.id
                elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                    name = annotation.value
                if name in models:
                    body_models[arg.arg] = name
            if not body_models:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                        and inner.value.id in body_models:
                    model = body_models[inner.value.id]
                    if inner.attr not in models[model] and inner.attr not in ignored:
                        problems.append(f"{node.name}(): {inner.value.id}.{inner.attr} is not a field "
                                        f"of {model} (line {inner.lineno})")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_declared_body_field_is_actually_forwarded(self) -> None:
        """The inverse of the shipped bug: a field the pane sends, the model declares, and the handler
        forgets to pass on. That is how `passphrase` and `idempotency_key` were silently dropped."""
        models: dict[str, set[str]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.bases:
                ann = [n for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
                models[node.name] = {n.target.id for n in ann}
        problems: list[str] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            model = None
            for arg in node.args.args:
                annotation = arg.annotation
                name = annotation.id if isinstance(annotation, ast.Name) else None
                if name in models:
                    model = name
            if model is None:
                continue
            body_source = ast.get_source_segment(API.read_text(encoding="utf-8"), node) or ""
            if "model_dump" in body_source:
                continue        # the handler forwards the whole body; nothing can be dropped
            for field in models[model]:
                # a field is forwarded if it is named in the params dict, read as body.<field>, or
                # accessed through getattr(body, "<field>")
                forwarded = (f'"{field}"' in body_source or f"'{field}'" in body_source
                             or f"body.{field}" in body_source or f'"{field}"' in body_source)
                if not forwarded:
                    problems.append(f"{node.name}(): declares {model}.{field} but never forwards it")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_pane_only_calls_routes_the_router_serves(self) -> None:
        router = API.read_text(encoding="utf-8")
        served = set()
        for method, path in re.findall(r'@router\.(get|post|put|delete)\("([^"]+)"\)', router):
            served.add(path.split("?")[0])
        pane = PANE.read_text(encoding="utf-8")
        # send('/x') and act('/x', ...) are the two helpers the pane uses for API calls
        called = set(re.findall(r"(?:send|act)\('(/[^'?]*)[?']", pane))
        called |= set(re.findall(r"ctx\.rest\('(/[^'?]*)[?']", pane))
        missing = sorted(called - served)
        self.assertEqual(missing, [], f"the pane calls routes the router does not serve: {missing}")


if __name__ == "__main__":
    unittest.main()

"""Contract tests between the dashboard router and the daemon.

This codebase shipped the same class of bug twice: a Pydantic body that lost a field its handler still
read (500 at runtime, swallowed by the pane), and a route whose daemon method did not exist. Both are
static facts about the source, so they can be checked without a running daemon:

  * every `_op("name", ...)` / `_ag("name")` call must name a method the daemon registers
  * every `body.<attr>` a handler reads must be a declared field of that handler's body model
  * every route the pane calls over ctx.rest/send/act must exist in the router
  * every key the pane reads off a route's answer must be a key that route actually returns

The route check catches the "route added, backend not restarted" confusion early: a pane that calls
something the router does not define is a bug in the repo regardless of what a running process serves.
The shape check catches the other half of the same bug class — a route that exists and answers, but
with the wrong key, which the pane's `.catch(() => ({ x: [] }))` turns into a silently empty section.

Both are structural, so they are read off the source: Python through `ast`, the pane through
balanced-paren extraction (a route call is often spread over four lines, and a regex over single
quotes misses exactly the ones somebody just added).
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

# The pane helpers that reach the bridge. `send` and `act` are the wallet page's wrappers over
# `ctx.rest`; the settings page calls `ctx.rest` directly.
CALLERS = ("ctx.rest", "send", "act")
PATH_RE = re.compile(r"^/(?:[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*)$")


def _daemon_methods() -> set[str]:
    """Method names in the `METHODS` table (value is `{name: (SCOPE, lambda ...)}`)."""
    source = STATE.read_text(encoding="utf-8")
    start = source.index("def build_methods")
    end = source.index("\n    }", start)
    block = source[start:end]
    return set(re.findall(r'"([a-z_0-9]+)":\s*\(', block))


def _balanced_calls(source: str, callee: str) -> list[tuple[str, int]]:
    """Every `callee(...)` in the pane, with its argument text and 1-based line.

    Balanced parens, not a regex over the first quoted string: `act('/approve', {\n  expect_payee:\n
    request.payee`, …)` is a call the regex form cannot see, and it is the shape every new action
    takes. The first non-whitespace token inside the parens is the path when it is a string literal.
    """
    out: list[tuple[str, int]] = []
    pattern = re.compile(r"(?<![\w.])" + re.escape(callee) + r"\(")
    for match in pattern.finditer(source):
        # `function send(` and `const send = (` are declarations, not calls
        before = source[max(0, match.start() - 12):match.start()]
        if before.rstrip().endswith("function"):
            continue
        depth, i = 0, match.end() - 1
        while i < len(source):
            char = source[i]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = source[match.end():i]
        head = re.match(r"\s*'([^']*)'|\s*\"([^\"]*)\"", args)
        if head:
            out.append((head.group(1) if head.group(1) is not None else head.group(2),
                        source[:match.start()].count("\n") + 1))
    return out


def _pane_routes(source: str) -> dict[str, int]:
    """path -> first line it is called on. Only literal paths: a call site that builds its path from
    a variable is not checkable here, and silently treating it as a route would be worse."""
    routes: dict[str, int] = {}
    for callee in CALLERS:
        for path, line in _balanced_calls(source, callee):
            bare = path.split("?")[0]
            if not PATH_RE.match(bare):
                continue                      # `/api/plugins/agent-wallet/ledger.csv` hrefs etc.
            routes.setdefault(bare, line)
    return routes


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = API.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.methods = _daemon_methods()
        cls.pane = PANE.read_text(encoding="utf-8")

    # ── router internals ──────────────────────────────────────────────────────────────────────

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
            body_source = ast.get_source_segment(self.source, node) or ""
            if "model_dump" in body_source:
                continue        # the handler forwards the whole body; nothing can be dropped
            for field in models[model]:
                # a field is forwarded if it is named in the params dict, read as body.<field>, or
                # accessed through getattr(body, "<field>")
                forwarded = (f'"{field}"' in body_source or f"'{field}'" in body_source
                             or f"body.{field}" in body_source)
                if not forwarded:
                    problems.append(f"{node.name}(): declares {model}.{field} but never forwards it")
        self.assertEqual(problems, [], "\n".join(problems))

    # ── pane ↔ router ─────────────────────────────────────────────────────────────────────────

    def test_the_pane_only_calls_routes_the_router_serves(self) -> None:
        """A route the pane calls and the router does not serve is a 404 the pane swallows."""
        served = {path.split("?")[0]
                  for _, path in re.findall(r'@router\.(get|post|put|delete)\("([^"]+)"\)', self.source)}
        called = _pane_routes(self.pane)
        missing = sorted(f"{path} (plugin.js:{line})" for path, line in called.items()
                         if path not in served)
        self.assertEqual(missing, [],
                         "the pane calls routes the router does not serve — add the route to "
                         f"dashboard/plugin_api.py: {missing}")
        self.assertGreater(len(called), 35, "the pane should be calling the bridge")

    def test_a_missing_route_fails_this_test(self) -> None:
        """The check above is only worth its keep if it fails on a path nobody serves."""
        fake = self.pane + "\n  const broken = () => act('/not/a/real/route', {})\n"
        called = _pane_routes(fake)
        self.assertIn("/not/a/real/route", called)
        served = {path for _, path in re.findall(r'@router\.(get|post|put|delete)\("([^"]+)"\)', self.source)}
        self.assertNotIn("/not/a/real/route", served)

    def test_every_shape_the_pane_reads_is_a_key_the_route_returns(self) -> None:
        """`X.requests || []` is a shape promise. The route must answer that key.

        The pane's pattern is `<route>(...).catch(() => ({ key: [] }))`: the fallback names the key
        the render reads. A route that answers a different key (or a bare list) leaves the section
        empty with no error anywhere, which is the bug this exists to stop.

        The router's answer is derived from the source: the keys of a returned dict literal, plus
        the keys `_op`/`_ag` results carry through a `**spread` (taken from the daemon method's own
        `return {...}`).
        """
        # route handler name -> (route path, method names it calls through _op/_ag)
        handlers: dict[str, tuple[str, list[str]]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            path = None
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) \
                        and isinstance(deco.args[0], ast.Constant):
                    path = deco.args[0].value
            if path is None:
                continue
            calls: list[str] = [str(n.args[0].value) for n in ast.walk(node)
                                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                and n.func.id in ("_op", "_ag") and n.args
                                and isinstance(n.args[0], ast.Constant)]
            handlers[path] = (node.name, calls)

        def returned_keys(path: str) -> set[str]:
            module_keys: set[str] = set()
            for node in self.tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == handlers[path][0]:
                    for ret in ast.walk(node):
                        if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
                            for key in ret.value.keys:
                                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                    module_keys.add(key.value)
                        # `{"ok": True, **_ag("x")}`: the spread carries the daemon's keys
                        if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
                            for call in ret.value.values:
                                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                                        and call.func.id in ("_op", "_ag") and call.args \
                                        and isinstance(call.args[0], ast.Constant):
                                    module_keys |= _daemon_return_keys(call.args[0].value)
            # a handler that returns `_ag(...)` directly serves whatever the daemon returns
            for call in handlers[path][1]:
                module_keys |= _daemon_return_keys(call)
            return module_keys

        problems: list[str] = []
        checked = 0
        for path, line in sorted(_pane_routes(self.pane).items()):
            if path not in handlers:
                continue                                    # covered by the missing-route test
            # `.catch(() => ({ requests: [] }))` straight after the call
            tail = self.pane[self.pane.index(path, 0):][:400] if path in self.pane else ""
            for key in re.findall(r"\(\s*=>\s*\(\{\s*(\w+)\s*:", tail):
                checked += 1
                if key not in returned_keys(path):
                    problems.append(f"{path} (plugin.js:{line}) falls back to {{{key}: []}} but the "
                                    f"route returns {sorted(returned_keys(path))}")
            # `res.requests || []` where `res` is bound to this route's answer. Bounded to the 400
            # chars after the binding: `res` and `result` are reused by half a dozen handlers in this
            # file, and reading the whole file attributed one route's answer to another's reads.
            for match in re.finditer(r"const\s+(\w+)\s*=\s*await\s+(?:send|act)\('" + re.escape(path) + r"'", self.pane):
                var = match.group(1)
                local = self.pane[match.end():match.end() + 400]
                for key in re.findall(r"\b" + re.escape(var) + r"\.(\w+)", local):
                    checked += 1
                    if key not in returned_keys(path):
                        problems.append(f"{path}: the pane reads {var}.{key}, the route returns "
                                        f"{sorted(returned_keys(path))}")
        self.assertGreaterEqual(checked, 8, "the shape check should cover every polled route")
        self.assertEqual(problems, [], "\n".join(problems))


def _daemon_return_keys(method: str) -> set[str]:
    """Keys of the dict literal `<method>` returns in walletd/state.py (`return {...}`)."""
    source = STATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            keys: set[str] = set()
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
                    for key in ret.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
            return keys
    return set()


if __name__ == "__main__":
    unittest.main()

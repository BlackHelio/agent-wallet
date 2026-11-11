#!/usr/bin/env bash
# Deploy the agent-wallet plugin, and prove the pane the app loads is the pane in this tree.
#
#   scripts/deploy.sh                 # deploy into ${HERMES_HOME:-$HOME/.hermes}
#   scripts/deploy.sh --root <dir>    # deploy into another Hermes home (a test root)
#   scripts/deploy.sh --dry-run       # print every step, touch nothing
#
# What it writes, in order:
#   1. <root>/plugins/agent-wallet/          plugin.yaml + dashboard/ walletd/ wallet/ desktop/
#      (a tree the package owns wholesale, so each of those is REPLACED, never merged: an adapter
#      added to walletd/chains/ that a merge forgot is a phantom "chain not available" in the daemon)
#   2. <root>/desktop-plugins/agent-wallet/  the app-level door the Desktop loader actually imports
#   3. the reconcile pass                    hermes_cli.desktop_plugin_halves, which refreshes the
#      door-2 copy from door 1 and prunes halves whose package is gone
#   4. verification                          the two copies against this tree, a removed top-level
#      file that is still served, and marker strings that only this bundle carries
#
# Why the reconcile is step 3, on every run, not a one-off: desktop-plugins/ is a derived copy, and
# a stale one there pins the app to an hours-old build with no error anywhere — the pane renders,
# it is just the previous UI.
#
# The two copies are NOT byte-identical, on purpose. Since the reconcile lands the package's own
# desktop/plugin.js next to a .hermes-package.json, the app treats the folder as a package half and
# loads it with defaultEnabled=false. A standalone copy carries a first-line header instead and no
# marker, and that is what the app renders. So: door 2 == source + header, and door 1 == source.
#
# After a deploy the renderer keeps the module it imported: the pane re-imports on the app's own
# Plugins toggle or an app restart, in the user's window. And if walletd's code changed, the daemon
# still runs the old modules until a restart — this script does not restart anything.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="agent-wallet"
ROOT="${HERMES_HOME:-$HOME/.hermes}"
DRY_RUN=0

# Everything in the package that is deployed into the plugin root. CONFIG_FILES are copied; TREE_DIRS
# are replaced whole. Anything else at the top level (tests/, docs/, scripts/, plugin-catalog/, the
# READMEs) stays in the checkout: it is not needed to run the plugin.
CONFIG_FILES=(plugin.yaml)
TREE_DIRS=(dashboard walletd wallet desktop)
# Files inside a package-owned tree that must NOT be deployed (build residue, not code).
TREE_EXCLUDES=(__pycache__ .pytest_cache .mypy_cache)
# The top-level, in-repo files whose absence in the installed copy is expected and fine.
NOT_DEPLOYED=(.git tests docs scripts plugin-catalog skills .github CHANGELOG.md README.md
              PLAN.md LICENSE requirements.txt .gitignore .gitattributes)
# First line of the app-level copy. The app reads it as "standalone plugin, not a package half".
STANDALONE_HEADER="// standalone desktop copy: loaded from desktop-plugins, not materialised from plugins/${NAME}/desktop"

die()  { printf '\033[1mdeploy: FAILED\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '  ·  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
say()  { printf '  +  %s\n' "$*"; }

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  printf '\nusage: scripts/deploy.sh [--dry-run] [--root DIR]\n'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --root)    [ $# -ge 2 ] || die "--root needs a directory"; ROOT="$2"; shift ;;
    --root=*)  ROOT="${1#--root=}" ;;
    -h|--help) usage; exit 0 ;;
    *)         die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[ -n "$ROOT" ] || die "--root is empty"
mkdir -p -- "$ROOT" 2>/dev/null || die "cannot create $ROOT"
ROOT="$(cd "$ROOT" && pwd)" || die "cannot resolve --root"
PKG="$ROOT/plugins/$NAME"
APP="$ROOT/desktop-plugins/$NAME"

TMP_ALL=()
cleanup() { for p in "${TMP_ALL[@]}"; do rm -rf -- "$p"; done; }
trap cleanup EXIT

# ---------------------------------------------------------------------------------------------
# 1. what we are deploying, and does it even load
# ---------------------------------------------------------------------------------------------
step "source ($HERE)"
cd "$HERE"
for f in "${CONFIG_FILES[@]}" desktop/plugin.js; do
  [ -f "$f" ] || die "missing $HERE/$f — run this from a full checkout"
done
for d in "${TREE_DIRS[@]}"; do
  [ -d "$d" ] || die "missing $HERE/$d/"
done
SRC_JS="desktop/plugin.js"
SRC_MD5="$(md5sum "$SRC_JS" | cut -d' ' -f1)"
[ -n "$SRC_MD5" ] || die "could not checksum $SRC_JS"
PKG_VERSION="$(sed -n 's/^version:[[:space:]]*//p' plugin.yaml | head -1)"
note "$SRC_JS  md5 $SRC_MD5  (plugin.yaml version ${PKG_VERSION:-unknown})"

# A bundle that does not parse is caught here, not on the user's screen as "failed to load".
if command -v node >/dev/null 2>&1; then
  tmp="$(mktemp --suffix=.mjs)"; TMP_ALL+=("$tmp"); cp "$SRC_JS" "$tmp"
  if ! out="$(node --check "$tmp" 2>&1)"; then die "node --check fails on $SRC_JS: $out"; fi
  note "node --check: ok"
else
  note "node not found: skipping the parse check"
fi

# ---------------------------------------------------------------------------------------------
# 2. markers: strings only the current bundle carries, and one removed string it must NOT carry
# ---------------------------------------------------------------------------------------------
# The checksums prove the two copies agree with each other and with this tree. This proves the
# source is the bundle we think it is: a checkout left on an older commit has consistent checksums
# everywhere, and deploys happily. A marker is a literal the current desktop/plugin.js contains; the
# package version is one by construction (bumped with every release, asserted by tests/test_packaging).
MARKERS=()
MARKERS+=("version: '$PKG_VERSION'")
[ -n "${WALLET_DEPLOY_MARKER:-}" ] && MARKERS+=("$WALLET_DEPLOY_MARKER")

# Strings a current release removed. Each is a greeting from a bundle that is hours old, and the
# grep is the cheap way to catch a copy this script did not write. Add one per removed feature.
#   E.g. the orders section: gone from the bundle, so the string must not come back.
REMOVED_MARKERS=()

for m in "${MARKERS[@]}"; do
  grep -qF -- "$m" "$SRC_JS" || die "marker not in $SRC_JS: $m (this tree is not the build it claims to be)"
done
for m in "${REMOVED_MARKERS[@]}"; do
  if grep -qF -- "$m" "$SRC_JS"; then die "removed string still in $SRC_JS: $m"; fi
done
note "markers present in source: ${#MARKERS[@]} (${MARKERS[*]})"
[ ${#REMOVED_MARKERS[@]} -gt 0 ] && note "removed strings absent: ${#REMOVED_MARKERS[@]}"

# The demo/library header, if any, is inherited from the source: the app-level copy must add exactly
# one line in front of the source, so a source that already starts with it is refused rather than
# silently producing a two-line file on the second deploy.
if head -1 "$SRC_JS" | grep -qF 'standalone desktop copy'; then
  die "$SRC_JS starts with a standalone header — this is a deployed copy, not a source bundle"
fi

# ---------------------------------------------------------------------------------------------
# 3. plan
# ---------------------------------------------------------------------------------------------
step "plan (root: $ROOT)"
say "plugin root   $PKG"
for f in "${CONFIG_FILES[@]}"; do say "  copy      $f"; done
for d in "${TREE_DIRS[@]}"; do say "  replace   $d/"; done
say "app-level    $APP"
say "  copy      desktop/plugin.js + header line, no .hermes-package.json"
say "reconcile    ${PKG}/desktop/plugin.js -> ${APP}/plugin.js (desktop_plugin_halves)"
say "verify       md5 both copies vs source; markers in both; no marker file; no stale top-level file"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\n\033[1m--dry-run\033[0m nothing was written\n'
  exit 0
fi

# Everything below is a hard failure: a deploy that stops halfway leaves one door on the new bundle
# and the other on the old one, which is the bug this script exists to prevent.

# ---------------------------------------------------------------------------------------------
# 4. stage everything first, then swap: a failure never leaves a half-copied tree
# ---------------------------------------------------------------------------------------------
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/agent-wallet-deploy.XXXXXX")"
TMP_ALL+=("$STAGE")

step "stage"
mkdir -p "$STAGE/pkg" "$STAGE/app"
for f in "${CONFIG_FILES[@]}"; do
  mkdir -p "$STAGE/pkg/$(dirname "$f")"
  cp -p -- "$f" "$STAGE/pkg/$f"
done
for d in "${TREE_DIRS[@]}"; do
  cp -a -- "$d" "$STAGE/pkg/$d"
  for x in "${TREE_EXCLUDES[@]}"; do find "$STAGE/pkg/$d" -name "$x" -prune -exec rm -rf {} + ; done
done
{ printf '%s\n' "$STANDALONE_HEADER"; cat -- "$SRC_JS"; } > "$STAGE/app/plugin.js"
say "staged $(du -sh "$STAGE/pkg" | cut -f1) package, $(wc -c < "$STAGE/app/plugin.js") B app-level copy"

# Leave a marker file behind at all and the app loads the pane default-off (see the header block).
rm -f -- "$STAGE/app/.hermes-package.json"

# ---------------------------------------------------------------------------------------------
# 5. publish the plugin root: config files copied, owned trees replaced
# ---------------------------------------------------------------------------------------------
step "plugin root -> $PKG"
mkdir -p -- "$PKG"
for f in "${CONFIG_FILES[@]}"; do
  install -m 0644 -- "$STAGE/pkg/$f" "$PKG/$f.new"
  mv -f -- "$PKG/$f.new" "$PKG/$f"
  say "copied    $NAME/$f"
done
for d in "${TREE_DIRS[@]}"; do
  rm -rf -- "$PKG/.$d.new"
  mv -- "$STAGE/pkg/$d" "$PKG/.$d.new"
  rm -rf -- "$PKG/$d"
  mv -- "$PKG/.$d.new" "$PKG/$d"
  say "replaced  $NAME/$d/ ($(find "$PKG/$d" -type f | wc -l) files)"
done

# A file that used to be deployed and no longer is would keep being served: say what is left, and
# remove it. Only names this script knows are deployed are removed; anything else is reported.
if [ -n "$(ls -A "$PKG")" ]; then
  for entry in $(ls -A "$PKG"); do
    keep=0
    for f in "${CONFIG_FILES[@]}" "${TREE_DIRS[@]}"; do [ "$entry" = "$f" ] && keep=1; done
    case "$entry" in
      .hermes-package.json|.hermes-catalog.json|.hermes-origin.json|agent-wallet.yaml|\
      *.pyc|*.sock|*.sock.token|keystore*.json|*.jsonl)
        keep=1 ;;                                  # app-owned sidecars and runtime state
    esac
    for f in "${NOT_DEPLOYED[@]}"; do [ "$entry" = "$f" ] && keep=1; done
    if [ "$keep" -eq 0 ]; then
      say "removed   $NAME/$entry (not part of a current deploy)"
      rm -rf -- "${PKG:?}/$entry"
    fi
  done
fi

# Editable installs and dev checkouts leave these next to the code. A stale __pycache__ is imported
# in preference to a newer .py whose mtime a copy did not carry; the daemon then runs old bytecode.
find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
for x in "${TREE_EXCLUDES[@]}"; do find "$PKG" -name "$x" -prune -exec rm -rf {} + 2>/dev/null || true; done

# ---------------------------------------------------------------------------------------------
# 6. publish the app-level door, then let the reconcile pass have the last word on it
# ---------------------------------------------------------------------------------------------
step "app-level -> $APP"
mkdir -p -- "$APP"
install -m 0644 -- "$STAGE/app/plugin.js" "$APP/plugin.js.new"
mv -f -- "$APP/plugin.js.new" "$APP/plugin.js"
say "copied    desktop-plugins/$NAME/plugin.js (with standalone header)"
if [ -f "$APP/.hermes-package.json" ]; then
  say "removed   desktop-plugins/$NAME/.hermes-package.json (a package marker loads the pane default-off)"
  rm -f -- "$APP/.hermes-package.json"
fi

step "reconcile"
# The reconcile pass lives in hermes_cli, outside the plugin. Find an interpreter that imports it;
# try HERMES_PY, then /opt/hermes, then the checkout's parent, then bare python3.
HERMES_PY=""
for candidate in "${HERMES_PY:-}" "${HERMES_HOME_PY:-}" \
                 "${HERMES_ROOT:-/opt/hermes}/.venv/bin/python" "$(dirname "$HERE")/../.venv/bin/python" \
                 "$(command -v python3 || true)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  for pp in "${HERMES_ROOT:-/opt/hermes}" "$(cd "$HERE/.." && pwd)" ""; do
    if PYTHONPATH="$pp:${PYTHONPATH:-}" "$candidate" -c 'import hermes_cli.desktop_plugin_halves' >/dev/null 2>&1; then
      HERMES_PY="$candidate"; HERMES_PP="$pp"; break 2
    fi
  done
done
if [ -z "$HERMES_PY" ]; then
  # Not fatal: steps 5 and 6 wrote both doors by hand, and verification below proves they agree.
  note "hermes_cli.desktop_plugin_halves not importable: reconcile skipped (both copies written directly)"
else
  note "reconcile via $HERMES_PY"
  HERMES_HOME="$ROOT" PYTHONPATH="${HERMES_PP}:${PYTHONPATH:-}" "$HERMES_PY" - "$ROOT" <<'PY' || die "reconcile failed"
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
from hermes_cli.desktop_plugin_halves import reconcile_desktop_halves_report  # noqa: E402

report = reconcile_desktop_halves_report(root, root / "desktop-plugins")
print(f"  ·  touched={len(report['touched'])} refused={len(report['refused'])}")
for item in report["refused"]:
    print(f"  ·  refused {item['name']}: {item['reason']}")
if not report["refused"] and not report["touched"]:
    print("  ·  already current")
PY
fi

# The reconcile materialises the package half, which carries a marker and no header. Make the pane
# standalone again — the app renders this folder, and a package half is loaded default-off.
if [ -f "$APP/.hermes-package.json" ] || ! head -1 "$APP/plugin.js" | grep -qF 'standalone desktop copy'; then
  install -m 0644 -- "$STAGE/app/plugin.js" "$APP/plugin.js.new"
  mv -f -- "$APP/plugin.js.new" "$APP/plugin.js"
  rm -f -- "$APP/.hermes-package.json"
  say "restored  desktop-plugins/$NAME/plugin.js as a standalone copy (the reconcile wrote a package half)"
fi

# ---------------------------------------------------------------------------------------------
# 7. verify: exit non-zero with a reason if either door is not this bundle
# ---------------------------------------------------------------------------------------------
step "verify"
fail=0
bad() { printf '  FAIL  %s\n' "$*" >&2; fail=$((fail + 1)); }
ok()  { printf '  ok    %s\n' "$*"; }

PKG_JS="$PKG/desktop/plugin.js"
APP_JS="$APP/plugin.js"
PKG_MD5=""; APP_MD5=""; SRC_MD5_AFTER=""
[ -f "$PKG_JS" ] && PKG_MD5="$(md5sum "$PKG_JS" | cut -d' ' -f1)" || bad "missing $PKG_JS"
[ -f "$APP_JS" ] && APP_MD5="$(md5sum "$APP_JS" | cut -d' ' -f1)" || bad "missing $APP_JS"
SRC_MD5_AFTER="$(md5sum "$SRC_JS" | cut -d' ' -f1)"

# (a) door 1 is this bundle, byte for byte.
if [ "$PKG_MD5" = "$SRC_MD5" ] && [ "$SRC_MD5_AFTER" = "$SRC_MD5" ]; then
  ok "plugins/$NAME/desktop/plugin.js = source ($PKG_MD5)"
else
  bad "plugins/$NAME/desktop/plugin.js is not the source: $PKG_MD5 vs $SRC_MD5 (source moved: $SRC_MD5_AFTER)"
fi

# (b) door 2 is this bundle plus the one header line, and nothing else.
if [ -n "$APP_MD5" ] && [ -n "$PKG_MD5" ] && {
  expected="$( { printf '%s\n' "$STANDALONE_HEADER"; cat -- "$PKG_JS"; } | md5sum | cut -d' ' -f1)"
  [ "$APP_MD5" = "$expected" ]
}; then
  ok "desktop-plugins/$NAME/plugin.js = source + standalone header ($APP_MD5)"
else
  bad "desktop-plugins/$NAME/plugin.js does not match source + header: $APP_MD5"
  note "the app renders this file; a mismatch here is exactly the stale-bundle bug"
fi

# (c) the marker strings are in the file the app actually loads.
for f in "$PKG_JS" "$APP_JS"; do
  [ -f "$f" ] || continue
  for m in "${MARKERS[@]}"; do
    if grep -qF -- "$m" "$f"; then ok "$(basename "$(dirname "$f")")/$(basename "$f"): has '$m'"
    else bad "$f is missing marker '$m' — this is not the current bundle"; fi
  done
  for m in "${REMOVED_MARKERS[@]}"; do
    if grep -qF -- "$m" "$f"; then bad "$f still carries removed string '$m'"; fi
  done
  still="$(head -n 2 "$f" | grep -cF 'standalone desktop copy' || true)"
  if [ "$still" -gt 1 ]; then bad "$f has $still standalone headers"; fi
done

# (d) the app-level folder is a standalone plugin: no package marker, exactly one header line.
if [ -f "$APP/.hermes-package.json" ]; then
  bad "desktop-plugins/$NAME/.hermes-package.json exists: the app loads this pane with defaultEnabled=false"
else
  ok "no .hermes-package.json (standalone desktop plugin)"
fi
if [ -f "$APP_JS" ]; then
  if head -1 "$APP_JS" | grep -qF 'standalone desktop copy'; then ok "standalone header on line 1"
  else bad "no standalone header on $APP_JS line 1"; fi
fi

# (e) one version across the pieces the pane and the daemon actually load.
pkg_ver_file="$(sed -n 's/^version:[[:space:]]*//p' "$PKG/plugin.yaml" 2>/dev/null | head -1)"
daemon_ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PKG/walletd/__init__.py" 2>/dev/null | head -1)"
pane_ver="$(sed -n "s/^[[:space:]]*version: '\([^']*\)'.*/\1/p" "$APP_JS" 2>/dev/null | head -1)"
for pair in "plugin.yaml:$pkg_ver_file" "walletd/__init__.py:$daemon_ver" "pane export:$pane_ver"; do
  what="${pair%%:*}"; ver="${pair#*:}"
  if [ -n "$ver" ] && [ "$ver" = "$PKG_VERSION" ]; then ok "$what reports $ver"
  else bad "$what reports '${ver:-nothing}', the source says '$PKG_VERSION'"; fi
done

# (f) a file that is gone from the package must not still be served. The tree check catches the
#     whole class: anything top-level in the installed copy that is neither deployed nor a known
#     sidecar is stale.
for entry in $(ls -A "$PKG" 2>/dev/null || true); do
  known=0
  for f in "${CONFIG_FILES[@]}" "${TREE_DIRS[@]}" "${NOT_DEPLOYED[@]}"; do [ "$entry" = "$f" ] && known=1; done
  case "$entry" in
    .hermes-*.json|*.sock|*.sock.token|keystore*.json|*.jsonl|*.pyc) known=1 ;;
  esac
  if [ "$known" -eq 0 ]; then bad "stale file still served: plugins/$NAME/$entry"; fi
done
stale_py="$(find "$PKG" -name __pycache__ -type d 2>/dev/null | head -3 || true)"
if [ -n "$stale_py" ]; then bad "__pycache__ under the installed copy (old bytecode wins over new .py): $stale_py"
else ok "no __pycache__ in the installed copy"; fi

if [ "$fail" -ne 0 ]; then
  printf '\n\033[1mdeploy: FAILED\033[0m %d check(s); the app may still render the previous bundle.\n' "$fail" >&2
  printf 'Nothing was rolled back: fix the tree, then run this again.\n' >&2
  exit 1
fi

step "done"
printf '  %s (%s) deployed to %s\n' "$NAME" "${PKG_VERSION:-?}" "$ROOT"
printf '  beat %s at both doors; restart walletd if walletd/ changed, and re-open the\n' "${SRC_MD5:0:12}"
printf '  pane (Plugins toggle or app restart) — the renderer keeps the module it imported.\n'

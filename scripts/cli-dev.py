#!/usr/bin/env python3
"""Run the CLI from this checkout against the live daemon, skipping the installed plugin.

`hermes wallet ...` loads whatever copy is installed under the plugins directory, so a change in
this repo is invisible until a reinstall. During development that is a slow, error-prone loop:
edit, copy, restart. This harness builds the same argparse tree `setup()` hands to the plugin
loader and calls `run()` directly, so `python3 scripts/cli-dev.py sandbox list` exercises the code
in this checkout against the real daemon and the real wallet home.

Usage: scripts/cli-dev.py [--json] <command> [args...]
Exit code is the CLI's own, so it can be used in a script.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wallet import cli  # noqa: E402  (path has to be set first)


def main(argv):
    parser = argparse.ArgumentParser(prog="wallet", add_help=True)
    cli.setup(parser)
    args = parser.parse_args(argv)
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

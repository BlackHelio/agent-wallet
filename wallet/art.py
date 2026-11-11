"""Coin logos for the terminal.

The logos are the CC0 `spothq/cryptocurrency-icons` set, rasterised at 40x40 and halved into 20
columns x 10 rows of braille pixels — the coin as a coin, not its name in letters. They are stored
precomputed on purpose: the CLI must not need an image library, and a logo that renders differently
on another machine is worse than no logo.

Presentation only: `--json`, a non-TTY stdout, `NO_COLOR` or `WALLET_NO_ART=1` all bypass this, and
nothing in here is imported by the daemon.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

RESET = "\x1b[0m"

#: Brand-ish colours, only used when the terminal can take it.
COLOURS: Dict[str, str] = {
    "XMR": "\x1b[38;2;255;102;0m",
    "BTC": "\x1b[38;2;247;147;26m",
    "ETH": "\x1b[38;2;138;146;178m",
    "XRP": "\x1b[38;2;200;208;218m",
    "USDC": "\x1b[38;2;39;117;202m",
    "POL": "\x1b[38;2;130;71;229m",
    "SIM": "\x1b[38;2;150;150;160m",
}

FULL_NAMES: Dict[str, str] = {
    "XMR": "MONERO", "BTC": "BITCOIN", "ETH": "ETHEREUM", "XRP": "XRP",
    "USDC": "USDC", "POL": "POLYGON", "SIM": "SIMULATOR",
}

#: One-character emblems for the compact table.
EMBLEMS: Dict[str, str] = {
    "XMR": "\u25c9", "BTC": "\u20bf", "ETH": "\u25c6", "XRP": "\u2715",
    "USDC": "$", "POL": "\u2b21", "SIM": "\u25a7",
}

_ART: Dict[str, List[str]] = {
    "XMR": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⡿⠟⠛⠉⠉⠛⠻⢿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⡟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠈⢻⣿⣿⡄⠀",
        "⢀⣿⣿⡟⠀⢸⣦⡀⠀⠀⠀⠀⢀⣴⡇⠀⢻⣿⣿⡀",
        "⢸⣿⣿⡇⠀⢸⣿⢿⣦⡀⢀⣴⡿⣿⡇⠀⢸⣿⣿⡇",
        "⠸⣿⣿⣇⣀⣸⣿⠀⠙⢿⡿⠋⠀⣿⣇⣀⣸⣿⣿⠇",
        "⠀⢻⣿⣿⣟⠛⠛⠀⠀⠀⠀⠀⠀⠛⠛⣻⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣷⣤⣀⠀⠀⠀⠀⣀⣤⣾⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
    "BTC": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⣿⣿⣿⡿⠻⡿⢿⣿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⣿⣿⣿⡉⠙⠁⠘⠃⢸⣿⣿⣿⣿⣿⡄⠀",
        "⢀⣿⣿⣿⣿⣿⣿⣿⠀⠀⣶⣦⡀⠈⢻⣿⣿⣿⣿⡀",
        "⢸⣿⣿⣿⣿⣿⣿⡇⠀⢈⠛⠛⠁⢀⣼⣿⣿⣿⣿⡇",
        "⠸⣿⣿⣿⣿⡿⠿⠀⢀⣿⣿⣷⠀⠀⣿⣿⣿⣿⣿⠇",
        "⠀⢻⣿⣿⣿⣶⣦⠀⣀⠈⠉⠁⢀⣠⣿⣿⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣿⣿⣤⣿⣀⣿⣿⣿⣿⣿⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
    "XRP": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⣿⡛⢿⣿⣿⣿⣿⣿⣿⡿⢛⣿⣿⣿⡄⠀",
        "⢀⣿⣿⣿⣿⣿⣦⡙⢿⣿⣿⡿⢋⣴⣿⣿⣿⣿⣿⡀",
        "⢸⣿⣿⣿⣿⣿⣿⣿⣦⣬⣥⣴⣿⣿⣿⣿⣿⣿⣿⡇",
        "⠸⣿⣿⣿⣿⣿⣿⠟⢋⣥⣬⡙⠻⣿⣿⣿⣿⣿⣿⠇",
        "⠀⢻⣿⣿⣿⠟⢁⣴⣿⣿⣿⣿⣦⡈⠻⣿⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
    "ETH": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⣿⣿⣿⠟⠘⣿⣿⣿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⣿⣿⣿⣿⠏⠀⢸⢌⢿⣿⣿⣿⣿⣿⡄⠀",
        "⢀⣿⣿⣿⣿⣿⣿⠃⠀⠀⣸⣻⢮⣿⣿⣿⣿⣿⣿⡀",
        "⢸⣿⣿⣿⣿⣿⣡⣤⣶⣯⣿⣿⣿⣷⣿⣿⣿⣿⣿⡇",
        "⠸⣿⣿⣿⣿⣿⡟⠷⣿⣟⣿⣿⣿⣿⣿⣿⣿⣿⣿⠇",
        "⠀⢻⣿⣿⣿⣿⣿⣦⠈⠙⢻⣿⣿⣿⣿⣿⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣿⣿⣿⣷⡄⢸⣿⣿⣿⣿⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣾⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
    "USDC": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⠿⠛⣿⣿⣿⣿⠛⠿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⡟⢁⣴⣾⣿⡟⢻⣿⣷⣦⡈⢻⣿⣿⡄⠀",
        "⢀⣿⣿⡏⢠⣿⣿⡟⢁⣤⣤⡉⣿⣿⣿⡄⢻⣿⣿⡀",
        "⢸⣿⣿⡃⣾⣿⣿⣷⣄⣙⡛⠻⣿⣿⣿⣷⠘⣿⣿⡇",
        "⠸⣿⣿⡇⢸⣿⣿⣏⠹⣿⡿⠆⣸⣿⣿⡇⢸⣿⣿⠇",
        "⠀⢻⣿⣿⣄⠹⢿⣿⣷⡆⢰⣾⣿⣿⠏⣠⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣷⣤⣉⣿⣿⣿⣿⣉⣠⣾⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
    "POL": [
        "⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣤⡀⠀⠀⠀⠀",
        "⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀⠀⠀",
        "⠀⢠⣿⣿⠿⠛⣉⣈⠙⠻⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀",
        "⢀⣿⣿⣿⠀⣾⣿⣿⣿⣆⣸⣿⠿⢿⣿⣿⣿⣿⣿⡀",
        "⢸⣿⣿⣿⠀⠿⣿⣿⡿⠛⢉⣤⣶⣦⣌⠙⣿⣿⣿⡇",
        "⠸⣿⣿⣿⣿⣶⣄⣡⣴⡾⢻⣿⣿⣿⣿⠀⣿⣿⣿⠇",
        "⠀⢻⣿⣿⣿⣿⣿⣿⣿⣧⣌⠙⠻⠟⢉⣠⣿⣿⡟⠀",
        "⠀⠀⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣾⣿⣿⣿⠟⠀⠀",
        "⠀⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⠁⠀⠀⠀",
        "⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠛⠋⠉⠁⠀⠀⠀⠀⠀⠀",
    ],
}

def art_enabled(stream=None) -> bool:
    """Art is for a human looking at a terminal. Pipes, files and --json get the plain form."""
    stream = stream or sys.stdout
    forced = os.environ.get("WALLET_ART", "").lower()
    if forced in ("1", "true", "yes", "on"):
        return True          # e.g. `WALLET_ART=1 hermes wallet coin XMR | less -R`
    if os.environ.get("WALLET_NO_ART", "").lower() in ("1", "true", "yes", "on"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a wrapped stream may not answer
        return False


def colour_enabled(stream=None) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not art_enabled(stream):
        return False
    return os.environ.get("TERM", "") not in ("dumb", "")


def paint(text: str, asset: str, stream=None) -> str:
    if not colour_enabled(stream):
        return text
    return f"{COLOURS.get((asset or '').upper(), '')}{text}{RESET}"


def art_for(asset: str, width: Optional[int] = None) -> Optional[List[str]]:
    """The coin's logo, or None when there is none or the terminal is too narrow to show it."""
    lines = _ART.get((asset or "").upper())
    if not lines:
        return None
    if width is None:
        width = _terminal_width()
    widest = max(len(line) for line in lines)
    return list(lines) if widest + 4 <= width else None


def _terminal_width(default: int = 80) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        try:
            return int(os.environ.get("COLUMNS") or default)
        except ValueError:
            return default


def emblem(asset: str, stream=None) -> str:
    sym = EMBLEMS.get((asset or "").upper(), "\u25cb")
    return paint(sym, asset, stream)


def render_art(asset: str, indent: str = "  ", stream=None) -> Optional[str]:
    """The logo, indented and coloured, or None if it does not fit."""
    lines = art_for(asset)
    if not lines:
        return None
    return "\n".join(indent + paint(line, asset, stream) for line in lines)

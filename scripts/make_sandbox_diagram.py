#!/usr/bin/env python3
"""Emit `docs/sandbox.svg` — the sandboxing feature as one figure.

Generated rather than hand-drawn so the labels stay in sync with the codes in `walletd/state.py`
(`_sandbox_limits` / `_sandbox_decision`) and the layout cannot drift when a row is added.

Render to PNG by opening docs/sandbox.svg in a browser (or any SVG rasteriser). A standalone SVG
as a page root makes Playwright's screenshot hang, so inline it into an HTML page first if you use
scripts/gui-audit/.
"""
from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1240, 812
INK, MUTED, LINE, PANEL = "#14181d", "#4d5560", "#d5d9de", "#fafbfc"
BG = "#ffffff"
ALLOW, ALLOW_BG = "#1c6b45", "#e9f4ee"
DENY, DENY_BG = "#9c2b2b", "#fbeceb"
ESC, ESC_BG = "#7a5100", "#fdf3df"
HUMAN, AGENT = "#1f4e79", "#5b3d8a"

out = []


def rect(x, y, w, h, fill=BG, stroke=LINE, r=10, sw=1.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" '
               f'stroke="{stroke}" stroke-width="{sw}"{d}/>')


def text(x, y, s, size: float = 13, fill=INK, weight="400",
         anchor="start", mono=False):
    fam = "ui-monospace, SFMono-Regular, Menlo, monospace" if mono else \
        "ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    out.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{fill}" '
               f'font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>')


def arrow(x1, y1, x2, y2, color=MUTED, dash=None, label="", lx: float = 0, ly: float = 0):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.8" '
               f'marker-end="url(#a)"{d}/>')
    if label:
        text(lx, ly, label, 11.5, color, "600", anchor="middle", mono=True)


# ── header ────────────────────────────────────────────────────────────────────
out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>')
out.append('<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
           f'fill="{MUTED}"/></marker></defs>')
text(28, 42, "A sandbox is a budget, not a permission", 23, INK, "600")
text(28, 66, "The human moves a capped sum into a budget once. Inside it the agent spends without "
             "asking; outside it nothing changes.", 13.5, MUTED)
text(28, 86, "walletd/state.py  ·  class Sandbox  ·  _sandbox_decision()  ·  _sandbox_limits()",
     13, MUTED, mono=True)

# ── geometry ─────────────────────────────────────────────────────────────────
AX, AW = 28, 240
BX, BW = 340, 484
CX, CW = 880, 332
TOP = 108

# ── column A: the human ──────────────────────────────────────────────────────
rect(AX, TOP, AW, 596, PANEL)
text(AX + 16, TOP + 26, "HUMAN", 12.5, HUMAN, "700")
text(AX + 16, TOP + 44, "you, in the Wallet pane", 11.5, MUTED)

human_rows = [
    ("grant a budget", "amount · chain · expiry · label"),
    ("fill it", "budget mode: one on-chain transfer\npooled mode: a ceiling, no transfer"),
    ("widen it", "unattended = any payee\ncosts the PIN and the passphrase"),
    ("end it", "revoke → leftover swept to\naccount 0, derived from the seed"),
    ("watch it", "remaining, spent and expiry,\nand every spend as a receipt"),
]
y = TOP + 60
for title, body in human_rows:
    rect(AX + 12, y, AW - 24, 66, BG)
    text(AX + 24, y + 22, title, 13, INK, "600")
    for i, line in enumerate(body.split("\n")):
        text(AX + 24, y + 40 + i * 14, line, 10.8, MUTED)
    y += 72

# the budget's life, as chips
text(AX + 16, y + 24, "THE BUDGET'S LIFE", 11, MUTED, "700")
life = ["created", "funded", "active", "expired", "swept"]
for i, stage in enumerate(life):
    cy = y + 42 + i * 25
    out.append(f'<circle cx="{AX + 24}" cy="{cy}" r="3.2" fill="{HUMAN}"/>')
    if i < len(life) - 1:
        out.append(f'<line x1="{AX+24}" y1="{cy+5}" x2="{AX+24}" y2="{cy+20}" stroke="{LINE}" '
                   f'stroke-width="1.4"/>')
    text(AX + 38, cy + 4, stage, 11, MUTED, mono=True)

# ── column B: the gate ───────────────────────────────────────────────────────
rect(BX, TOP, BW, 596, PANEL)
text(BX + 16, TOP + 26, "WALLETD — ONE CHECK ON EVERY SPEND", 12.5, INK, "700")
text(BX + 16, TOP + 44, "in this order; the first refusal wins", 11.5, MUTED)

rows = [
    ("record edited by hand", "sandbox_tampered", DENY, "refused — but a sweep home still works"),
    ("profile not on the list", "sandbox_profile", DENY, "the agent's profile is not this budget's"),
    ("revoked · destroyed · unfunded", "sandbox_closed", DENY, "over, or never filled"),
    ("amount + fee reserve > remaining", "sandbox_budget", DENY, "the fee comes out of the same budget"),
    ("one-time budget already used", "sandbox_one_time_used", DENY, "self-destructs after it settles"),
    ("past its expiry", "sandbox_expired", DENY, "the clock starts at creation, not at funding"),
    ("another chain or asset", "sandbox_scope", DENY, "one budget covers one asset"),
    ("payee is not blessed", "escalate", ESC, "a human must approve THIS destination"),
    ("otherwise", "sandbox_ok", ALLOW, "no PIN, no approval — the money is the bound"),
]
y = TOP + 60
for cond, code, colour, gloss in rows:
    bg = ALLOW_BG if colour == ALLOW else DENY_BG if colour == DENY else ESC_BG
    rect(BX + 12, y, BW - 24, 56, bg, bg)
    out.append(f'<rect x="{BX+12}" y="{y}" width="4" height="56" rx="2" fill="{colour}"/>')
    text(BX + 28, y + 22, cond, 13, INK, "600")
    text(BX + 28, y + 40, gloss, 11.2, MUTED)
    text(BX + BW - 26, y + 32, code, 12, colour, "600", mono=True, anchor="end")
    y += 60

# ── column C: the agent ─────────────────────────────────────────────────────
rect(CX, TOP, CW, 596, PANEL)
text(CX + 16, TOP + 26, "AGENT", 12.5, AGENT, "700")
text(CX + 16, TOP + 44, "prompt-injectable by assumption", 11.5, MUTED)
agent_rows = [
    ("may spend inside", "any payee only when the operator\narmed it unattended, or the payee\nis a saved contact"),
    ("cannot raise the ceiling", "budget, expiry, chain and allowlist\nare sealed; editing them fails"),
    ("cannot fake a confirmation", "the approval card is built from the\nsealed row, never from the request"),
    ("cannot escape by id", "an unknown sandbox id is refused,\nnot read as \"no sandbox\""),
]
y = TOP + 60
for title, body in agent_rows:
    rect(CX + 12, y, CW - 24, 66, BG)
    text(CX + 24, y + 22, title, 13, INK, "600")
    for i, line in enumerate(body.split("\n")):
        text(CX + 24, y + 39 + i * 13, line, 10.8, MUTED)
    y += 72

rect(CX + 12, y, CW - 24, 158, BG)
text(CX + 24, y + 22, "what it can read", 13, INK, "600")
for i, line in enumerate([
        "its own status: budget, spent,",
        "remaining, expiry — nothing else.",
        "",
        "never the keys, never the seal,",
        "never the passphrase."]):
    text(CX + 24, y + 42 + i * 15, line, 10.8, MUTED)
text(CX + 24, y + 132, "none of this is the agent being good:", 10.8, AGENT, "600")
text(CX + 24, y + 147, "walletd refuses, and the agent gets a code back.", 10.8, MUTED)

# ── the four flows, in the gutters only ─────────────────────────────────────
mid_l = (AX + AW + BX) / 2
mid_r = (BX + BW + CX) / 2
# Each arrow sits level with the row it belongs to: grant and spend at the top of the ladder, then the
# ladder's last two rows — `escalate` leaves to the human (left), `sandbox_ok` goes to the agent (right).
ESC_ROW_Y = TOP + 60 + 7 * 60 + 28     # centre of the `payee is not blessed` row
ALLOW_ROW_Y = TOP + 60 + 8 * 60 + 28   # centre of the `otherwise` row
arrow(AX + AW + 2, 168, BX - 4, 168, HUMAN, label="grant", lx=mid_l, ly=158)
arrow(BX - 4, ESC_ROW_Y, AX + AW + 2, ESC_ROW_Y, ESC, dash="5 4", label="escalate", lx=mid_l, ly=ESC_ROW_Y - 10)
arrow(CX - 2, 168, BX + BW + 4, 168, MUTED, label="spend", lx=mid_r, ly=158)
arrow(BX + BW + 2, ALLOW_ROW_Y, CX - 4, ALLOW_ROW_Y, ALLOW, label="allow", lx=mid_r, ly=ALLOW_ROW_Y - 10)

# ── footer: what the seal buys ───────────────────────────────────────────────
rect(28, 720, W - 56, 78, "#f4f6f8")
text(44, 744, "The seal is what makes a confirmation honest", 13, INK, "600")
for i, line in enumerate([
        "Every row in sandboxes.json carries a seal. If the address, the arm or the allowlist is edited "
        "outside the wallet, the row reads tampered",
        "and every spend is refused — the leftover can still be swept home, because that destination "
        "comes from the seed, not from the row."]):
    text(44, 764 + i * 15, line, 11.2, MUTED)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
       f'viewBox="0 0 {W} {H}">' + "".join(out) + "</svg>")
Path(__file__).resolve().parent.parent.joinpath("docs", "sandbox.svg").write_text(svg, encoding="utf-8")
print(f"docs/sandbox.svg  {len(svg)} bytes")

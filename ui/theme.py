"""The dashboard's look, shared by every view: a paper ledger.

Raftioneer is a log that three servers agree on, so the page looks like one:
warm paper, ink, monospaced labels, and each auction a numbered lot ticket.
Colour is kept for meaning - who leads, which term wrote an entry, pass or
fail - and the logo is the cluster itself (see logo_svg).
"""
from __future__ import annotations

import time

from nicegui import ui

PAPER = "#f6f1e7"
CARD = "#fffdf8"
LINE = "#e4dccb"
INK = "#26211c"
MUTED = "#7a6f60"
CHAI = "#b4571f"      # Tapri's colour: a clay cup of chai

PRIMARY = INK
LEADER = "#e08a00"    # amber
FOLLOWER = "#2f6f6a"  # teal
CANDIDATE = "#f97316"
DOWN = "#a8a29e"
FROZEN = "#38bdf8"

# Log cells are coloured by term, so a new leader's entries look different.
TERM_COLOURS = ["#6366f1", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#8b5cf6", "#84cc16", "#f97316"]

OP_SHORT = {
    "PLACE_BID": "BID", "REGISTER": "REG", "LOGIN": "IN", "LOGOUT": "OUT", "CREATE_AUCTION": "NEW",
    "CLOSE_EXPIRED": "CLOSE", "SAVE_SUMMARY": "RECAP", "DEPOSIT": "DEP", "CONFIRM_RECEIPT": "PAID",
    "NOOP": "NO-OP",
}

CSS = f"""
body {{ background: {PAPER}; color: {INK}; }}
.q-btn, .q-tab__label {{ text-transform: none; }}  /* sentence case everywhere, not the library's capitals */
.rt-card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; box-shadow: none; }}
.rt-muted {{ color: {MUTED}; }}
.rt-price {{ font-size: 1.55rem; font-weight: 750; letter-spacing: -0.02em; color: {INK}; }}
.rt-mono {{ font-family: ui-monospace, "Ubuntu Mono", SFMono-Regular, Menlo, monospace; }}
.rt-clamp {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}

/* small monospaced capitals: section headings, lot numbers, column labels */
.rt-label {{ font-family: ui-monospace, "Ubuntu Mono", SFMono-Regular, Menlo, monospace; font-size: 11px;
            letter-spacing: .09em; text-transform: uppercase; color: {MUTED}; }}
.rt-title {{ font-size: 1.25rem; font-weight: 700; letter-spacing: -0.01em; }}

/* header: paper, not a dark bar */
.rt-header {{ background: {PAPER} !important; color: {INK} !important; border-bottom: 1px solid {LINE}; }}
.rt-wordmark {{ font-family: ui-monospace, "Ubuntu Mono", SFMono-Regular, Menlo, monospace; font-weight: 700;
               font-size: 1.2rem; letter-spacing: -0.02em; line-height: 1; }}

/* a lot ticket: the actions sit below a perforation */
.rt-tear {{ border-top: 1.5px dashed {LINE}; width: 100%; }}

@keyframes rt-flash {{ 0% {{ background: #fdecc8; }} 100% {{ background: {CARD}; }} }}
.rt-flash {{ animation: rt-flash 1.4s ease-out; }}

@keyframes rt-pop {{ 0% {{ transform: scale(.3); opacity: 0 }} 70% {{ transform: scale(1.15); opacity: 1 }} 100% {{ transform: scale(1) }} }}
.rt-pop {{ animation: rt-pop .5s ease-out; }}

.rt-leader {{ border: 2px solid {LEADER}; }}

@keyframes rt-slide {{ from {{ transform: translateX(16px) scale(.8); opacity: 0 }} to {{ transform: none; opacity: 1 }} }}
.rt-cell {{ width: 40px; height: 40px; border-radius: 6px; font-size: 9.5px; font-weight: 700; color: white;
           display: flex; align-items: center; justify-content: center; flex: none; letter-spacing: .02em; }}
.rt-cell.rt-new {{ animation: rt-slide .45s ease-out; }}
.rt-cell.rt-pending {{ background: transparent !important; border: 2px dashed currentColor; }}

@keyframes rt-blink {{ 50% {{ opacity: .3 }} }}
.rt-live {{ width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: rt-blink 1.6s infinite; }}
.rt-live.rt-bad {{ background: #ef4444; }}

@keyframes rt-shake {{ 0%,100% {{ transform: none }} 25% {{ transform: translateX(-4px) }} 75% {{ transform: translateX(4px) }} }}
.rt-shake {{ animation: rt-shake .35s 2; }}

.rt-down {{ filter: grayscale(1); opacity: .65; }}
.rt-chat-q {{ background: {INK}; color: {PAPER}; border-radius: 12px 12px 3px 12px; }}
.rt-chat-a {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 12px 12px 12px 3px; }}

/* evidence ledger rows */
.rt-row {{ border-top: 1px solid {LINE}; }}
.rt-row:hover {{ background: #fbf7ee; }}
"""


def install() -> None:
    ui.colors(primary=PRIMARY, secondary=FOLLOWER, accent=LEADER, positive="#15803d",
              negative="#b91c1c", warning="#b45309", info="#0369a1")
    ui.add_css(CSS)


# The logo is the cluster: three dots laid out like the diagram on the Cluster
# tab. The amber one is whichever server leads right now; a hollow one is down.
LOGO_DOTS = {"app-server-1": (7, 6), "app-server-2": (7, 22), "app-server-3": (23, 14)}


def logo_svg(roles: dict[str, str] | None = None, size: int = 30) -> str:
    """roles: node id -> LEADER / FOLLOWER / CANDIDATE / DOWN / FROZEN (None = all unknown)."""
    roles = roles or {}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 28" width="{size}" height="{size}">']
    points = list(LOGO_DOTS.values())
    for i, (x1, y1) in enumerate(points):
        for x2, y2 in points[i + 1:]:
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{INK}" stroke-width="1.4"/>')
    for node, (x, y) in LOGO_DOTS.items():
        role = roles.get(node, "FOLLOWER")
        if role == "LEADER":
            parts.append(f'<circle cx="{x}" cy="{y}" r="5.6" fill="{LEADER}" stroke="{INK}" stroke-width="1.4"/>')
        elif role in ("DOWN", "FROZEN"):
            parts.append(f'<circle cx="{x}" cy="{y}" r="4.2" fill="{PAPER}" stroke="{INK}" stroke-width="1.4"/>')
        else:
            parts.append(f'<circle cx="{x}" cy="{y}" r="4.2" fill="{INK}"/>')
    parts.append("</svg>")
    return "".join(parts)


FAVICON = logo_svg({"app-server-3": "LEADER"})


def money(amount: float) -> str:
    return f"Rs {amount:,.2f}"


def short_money(amount: float) -> str:
    return f"Rs {amount:,.0f}" if amount == int(amount) else money(amount)


def name(user_id: str) -> str:
    return (user_id or "").removeprefix("user_")


def countdown(end_ms: int) -> tuple[str, int]:
    """("2:13", seconds left)."""
    left = max(0, int(end_ms / 1000 - time.time()))
    if left >= 3600:
        return f"{left // 3600}h {left % 3600 // 60:02d}m", left
    return f"{left // 60}:{left % 60:02d}", left


def clock(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


def role_colour(role: str) -> str:
    return {"LEADER": LEADER, "FOLLOWER": FOLLOWER, "CANDIDATE": CANDIDATE, "FROZEN": FROZEN}.get(role, DOWN)


# Auctions the scripts and tests create; hidden from the floor unless asked for.
TEST_PREFIXES = ("pytest ", "Stress-test item", "Chaos-run item", "Failover-check item", "Benchmark item")


def is_test_auction(title: str) -> bool:
    return title.startswith(TEST_PREFIXES)

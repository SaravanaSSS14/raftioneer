"""Colours, CSS animations and small formatting helpers shared by the views."""
from __future__ import annotations

import time

from nicegui import ui

PRIMARY = "#4f46e5"   # indigo
LEADER = "#f59e0b"    # amber
FOLLOWER = "#4f46e5"
CANDIDATE = "#f97316"
DOWN = "#9ca3af"
FROZEN = "#38bdf8"

# Log cells are coloured by term, so a new leader's entries look different.
TERM_COLOURS = ["#6366f1", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#8b5cf6", "#84cc16", "#f97316"]

OP_SHORT = {
    "PLACE_BID": "BID", "REGISTER": "REG", "LOGIN": "IN", "LOGOUT": "OUT", "CREATE_AUCTION": "NEW",
    "CLOSE_EXPIRED": "CLOSE", "SAVE_SUMMARY": "RECAP", "DEPOSIT": "DEP", "CONFIRM_RECEIPT": "PAID",
    "NOOP": "NO-OP",
}

CSS = """
body { background: #f4f6fb; }
.rt-card { border-radius: 14px; box-shadow: 0 1px 2px rgba(16,24,40,.05), 0 2px 8px rgba(16,24,40,.06); }
.rt-muted { color: #64748b; }
.rt-price { font-size: 1.55rem; font-weight: 750; letter-spacing: -0.02em; color: #0f172a; }
.rt-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.rt-clamp { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

@keyframes rt-flash { 0% { background: #fef3c7; } 100% { background: #ffffff; } }
.rt-flash { animation: rt-flash 1.4s ease-out; }

@keyframes rt-pop { 0% { transform: scale(.3); opacity: 0 } 70% { transform: scale(1.15); opacity: 1 } 100% { transform: scale(1) } }
.rt-pop { animation: rt-pop .5s ease-out; }

@keyframes rt-ring { 0% { box-shadow: 0 0 0 0 rgba(245,158,11,.55) } 100% { box-shadow: 0 0 0 16px rgba(245,158,11,0) } }
.rt-leader { animation: rt-ring 1.5s infinite; border: 2px solid #f59e0b; }

@keyframes rt-slide { from { transform: translateX(16px) scale(.8); opacity: 0 } to { transform: none; opacity: 1 } }
.rt-cell { width: 40px; height: 40px; border-radius: 8px; font-size: 9.5px; font-weight: 700; color: white;
           display: flex; align-items: center; justify-content: center; flex: none; letter-spacing: .02em; }
.rt-cell.rt-new { animation: rt-slide .45s ease-out; }
.rt-cell.rt-pending { background: transparent !important; border: 2px dashed currentColor; }

@keyframes rt-blink { 50% { opacity: .3 } }
.rt-live { width: 9px; height: 9px; border-radius: 50%; background: #22c55e; animation: rt-blink 1.6s infinite; }
.rt-live.rt-bad { background: #ef4444; }

@keyframes rt-shake { 0%,100% { transform: none } 25% { transform: translateX(-4px) } 75% { transform: translateX(4px) } }
.rt-shake { animation: rt-shake .35s 2; }

.rt-down { filter: grayscale(1); opacity: .65; }
.rt-chat-q { background: #eef2ff; border-radius: 12px 12px 4px 12px; }
.rt-chat-a { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px 12px 12px 4px; }
"""


def install() -> None:
    ui.colors(primary=PRIMARY, secondary="#0f766e", accent=LEADER, positive="#16a34a",
              negative="#dc2626", warning="#d97706", info="#0284c7")
    ui.add_css(CSS)


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

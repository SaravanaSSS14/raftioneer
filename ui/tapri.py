"""Tapri, on every tab: a chai-cup button in the header opens a side panel.

Tapri is the auction assistant (the LLM node, reached through an app server's
AskAssistant RPC). It can be asked in general - house rules, your wallet,
what happens when a server crashes - or about one auction, picked in the
panel or by pressing "Ask Tapri" on a lot. The conversation stays in the
panel while the page is open.
"""
from __future__ import annotations

from nicegui import ui

import theme
from backend import Session, snapshots

ACTIVE = 2  # auction_pb2.ACTIVE
GENERAL = ""

GENERAL_QUESTIONS = [
    "What sold recently?",
    "How much can I bid with my wallet?",
    "How does escrow work?",
    "What if the server crashes during my bid?",
]
CLOSED_QUESTIONS = [
    "Who won, and for how much?",
    "How did the bidding go?",
    "Has the seller been paid?",
]
# While the model works (a small one, on CPU - it takes a few seconds).
BREWING = [
    "Putting the kettle on…",
    "Brewing - a strong answer takes a minute…",
    "Asking the regulars at the stall…",
    "Stirring in the handbook…",
]
AUCTION_QUESTIONS = [
    "Is this worth bidding on?",
    "What is the least I can bid now?",
    "Why did the end time move?",
    "What do I pay if I win?",
]


class Tapri:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.auction_id = GENERAL
        self.busy = False
        self.asked = 0
        self.closed_ids: set[str] = set()
        self._build()

    def button(self) -> None:
        """The way in, placed in the page header so it is on every tab and never covers content."""
        ui.button("Ask Tapri", icon="local_cafe", on_click=lambda: self.open()) \
            .props("unelevated rounded no-caps dense").classes("px-3") \
            .style(f"background:{theme.CHAI} !important; color:white")

    def _build(self) -> None:
        with ui.dialog().props("position=right full-height") as self.dialog, \
                ui.card().classes("p-0 gap-0 no-wrap").style(
                    f"width:440px; max-width:100vw; height:100vh; max-height:100vh; border-radius:0; "
                    f"background:{theme.PAPER}"):
            with ui.row().classes("w-full items-center gap-3 p-4 no-wrap").style(
                    f"border-bottom:1px solid {theme.LINE}"):
                ui.icon("local_cafe").classes("text-3xl").style(f"color:{theme.CHAI}")
                with ui.column().classes("gap-0 grow"):
                    ui.label("Tapri").classes("rt-title leading-tight")
                    ui.label("the chai-stall assistant · runs on the LLM node, grounded in the handbook") \
                        .classes("text-xs rt-muted")
                ui.button(icon="close", on_click=self.dialog.close).props("flat round dense")
            with ui.column().classes("w-full px-4 pt-3 pb-1 gap-1"):
                ui.label("Talking about").classes("rt-label")
                self.topic = ui.select({GENERAL: "Everything - rules, wallet, the system"}, value=GENERAL,
                                       on_change=self._topic_changed).props("outlined dense options-dense") \
                    .classes("w-full")
            with ui.scroll_area().classes("w-full grow") as self.scroller:
                self.chat = ui.column().classes("w-full p-2 gap-3")
                with self.chat:
                    self.hello = ui.label("Rules, lots, your wallet, what sold and to whom - ask away.") \
                        .classes("text-sm rt-muted px-2")
            self.chips = ui.row().classes("w-full gap-1 px-4")
            with ui.row().classes("w-full items-center gap-2 p-4 no-wrap"):
                self.box = ui.input(placeholder="Ask Tapri…").props("outlined dense").classes("grow")
                ui.button(icon="send", on_click=lambda: self.send(self.box.value)).props("round unelevated")
            self.box.on("keydown.enter", lambda: self.send(self.box.value))
        self._draw_chips()

    # ------------------------------------------------------------ topic

    def _options(self) -> dict[str, str]:
        """General, every live lot, and the most recently closed ones."""
        options = {GENERAL: "Everything - rules, wallet, the system"}
        lots = [a for a in snapshots.auctions if not theme.is_test_auction(a.item.title)]
        for auction in sorted((a for a in lots if a.status == ACTIVE), key=lambda a: a.end_time_ms):
            options[auction.auction_id] = f"Lot {auction.auction_id[:4]} · {auction.item.title}"
        closed = sorted((a for a in lots if a.status != ACTIVE), key=lambda a: a.end_time_ms, reverse=True)[:6]
        self.closed_ids = {a.auction_id for a in closed}
        for auction in closed:
            options[auction.auction_id] = f"Lot {auction.auction_id[:4]} · {auction.item.title} (closed)"
        return options

    def open(self, auction_id: str = GENERAL) -> None:
        """Open the panel; with an auction id, about that auction."""
        options = self._options()
        if auction_id and auction_id in options:
            self.auction_id = auction_id
        elif self.auction_id not in options:
            self.auction_id = GENERAL  # the auction talked about earlier has closed
        self.topic.set_options(options, value=self.auction_id)
        self._draw_chips()
        self.dialog.open()

    def _topic_changed(self, event) -> None:
        self.auction_id = event.value or GENERAL
        self._draw_chips()

    def _draw_chips(self) -> None:
        self.chips.clear()
        with self.chips:
            questions = (GENERAL_QUESTIONS if not self.auction_id
                         else CLOSED_QUESTIONS if self.auction_id in self.closed_ids else AUCTION_QUESTIONS)
            for question in questions:
                ui.chip(question, on_click=lambda q=question: self.send(q)) \
                    .props("outline dense clickable").classes("text-[12px]")

    # ------------------------------------------------------------ asking

    async def send(self, question: str) -> None:
        question = (question or "").strip()
        if not question or self.busy:
            return
        if not self.session.signed_in:
            ui.notify("Sign in to ask Tapri", type="warning")
            return
        self.busy = True
        self.box.set_value("")
        self.hello.set_visibility(False)
        about = self.topic.options.get(self.auction_id, "") if self.auction_id else ""
        with self.chat:
            with ui.column().classes("w-full items-end gap-0"):
                if about:
                    ui.label(f"about {about}").classes("text-[10px] rt-muted rt-mono")
                ui.label(question).classes("rt-chat-q px-3 py-2 text-sm max-w-[85%]")
            with ui.row().classes("w-full") as thinking:
                with ui.row().classes("rt-chat-a px-3 py-2 items-center gap-2"):
                    ui.spinner("dots", size="1.4em")
                    ui.label(BREWING[self.asked % len(BREWING)]).classes("text-sm rt-muted")
        self.asked += 1
        self.scroller.scroll_to(percent=1.0)
        try:
            reply = await self.session.ask(question, self.auction_id)
        except Exception:
            reply = None
        finally:
            self.busy = False
        thinking.delete()
        with self.chat, ui.row().classes("w-full"):
            with ui.column().classes("rt-chat-a px-3 py-2 gap-2 max-w-[92%]"):
                if reply is None or not reply.success:
                    ui.label(reply.error_message if reply else "Couldn't reach the cluster.") \
                        .classes("text-sm text-negative")
                else:
                    # The fine print is read from the replicated state, not written by
                    # the model: if the two ever disagree, the fine print is right.
                    answer, _, fine = reply.answer.partition("\n\nFine print")
                    ui.label(answer).classes("text-sm whitespace-pre-line")
                    if fine:
                        ui.label("Fine print" + fine).classes("text-xs rt-muted pl-2") \
                            .style(f"border-left:2px solid {theme.LINE}")
                    with ui.row().classes("gap-1 items-center"):
                        ui.badge("template fallback - LLM node unreachable" if reply.from_fallback
                                 else "LLM · raftioneer-tapri",
                                 color="grey-7" if reply.from_fallback else "brown-6").props("outline")
                        for source in reply.sources:
                            ui.chip(source, icon="menu_book").props("dense outline color=teal-8") \
                                .classes("text-[11px]").tooltip("Handbook section the answer was grounded in")
        self.scroller.scroll_to(percent=1.0)

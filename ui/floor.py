"""The auction floor: live auctions, bidding, Tapri, selling, the wallet and
escrow - what a student using Raftioneer sees."""
from __future__ import annotations

import math
import uuid

from nicegui import app, ui

import theme
from backend import Session, bid_history, get_auction, snapshots
from theme import money, name

PREMIUM_PERCENT = 5.0  # the cluster's default BUYERS_PREMIUM_PERCENT
PERSONA_TOP_BUDGET = 4000.0  # nisargh's budget, the largest in client/main.py
ACTIVE, CLOSED = 2, 3  # auction_pb2.AuctionStatus values

SUGGESTED_QUESTIONS = [
    "How much can I bid with my wallet?",
    "Why did the end time move?",
    "How does escrow work?",
    "What if the server crashes during my bid?",
]


def next_valid(auction) -> float:
    """A sensible suggested bid: about 5% over the highest, or the reserve."""
    if auction.current_highest_bid > 0:
        return float(math.ceil(auction.current_highest_bid * 1.05 / 10) * 10)
    if auction.reserve_price > 0:
        return float(auction.reserve_price)
    return 100.0


def retrigger(element, css_class: str) -> None:
    """Restart a one-shot CSS animation (flash, pop, shake) on an element:
    take the class off now and put it back a moment later, so the browser
    sees it arrive again. (The one-shot timer removes itself afterwards.)"""
    element.classes(remove=css_class)
    ui.timer(0.06, lambda: element.classes(add=css_class), once=True)


class AuctionCard:
    def __init__(self, floor: "Floor", auction) -> None:
        self.floor, self.id = floor, auction.auction_id
        self.end_ms, self.bid = auction.end_time_ms, -1.0
        with floor.grid:
            with ui.card().classes("rt-card p-4 gap-2") as self.card:
                with ui.row().classes("w-full items-start no-wrap gap-2"):
                    self.title = ui.label().classes("font-semibold text-[15px] leading-snug grow")
                    self.clock = ui.chip(icon="schedule").props("dense square outline")
                self.desc = ui.label().classes("text-sm rt-muted rt-clamp min-h-[2.5rem]")
                with ui.row().classes("w-full items-end no-wrap"):
                    with ui.column().classes("gap-0 grow"):
                        self.price = ui.label().classes("rt-price")
                        self.who = ui.label().classes("text-xs rt-muted")
                    self.extended = ui.badge(color="orange-7").props("rounded")
                    self.extended.tooltip("Anti-sniping: a late bid moved the end time")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    ui.button("Bid", icon="gavel", on_click=lambda: floor.open_bid(self.id)) \
                        .props("unelevated").classes("grow")
                    ui.button(icon="support_agent", on_click=lambda: floor.open_ask(self.id)) \
                        .props("flat round color=primary").tooltip("Ask Tapri about this item")
        self.update(auction)

    def update(self, auction) -> None:
        self.end_ms = auction.end_time_ms
        self.title.set_text(auction.item.title)
        self.desc.set_text(auction.item.description or "No description")
        if auction.current_highest_bid > 0:
            self.price.set_text(money(auction.current_highest_bid))
            self.who.set_text(f"highest bid by {name(auction.current_highest_bidder_id)}")
        else:
            self.price.set_text("No bids yet")
            reserve = f"reserve {money(auction.reserve_price)}" if auction.reserve_price else "no reserve"
            self.who.set_text(f"{reserve} · seller {name(auction.item.seller_id)}")
        if self.bid >= 0 and auction.current_highest_bid != self.bid:
            retrigger(self.card, "rt-flash")
            retrigger(self.price, "rt-pop")
        self.bid = auction.current_highest_bid
        extra = (auction.end_time_ms - auction.original_end_time_ms) / 1000 if auction.original_end_time_ms else 0
        self.extended.set_text(f"+{extra:.0f}s anti-snipe")
        self.extended.set_visibility(extra >= 1)
        self.tick()

    def tick(self) -> None:
        text, left = theme.countdown(self.end_ms)
        self.clock.set_text(text if left else "closing…")
        colour = "red-7" if left < 15 else "orange-8" if left < 60 else "grey-8"
        self.clock.props(f"color={colour}")


class Floor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.cards: dict[str, AuctionCard] = {}
        self.version_seen = -1
        self.show_tests = False
        self.wallet_labels: dict[str, ui.label] = {}
        self._build()
        ui.timer(1.0, self.tick)
        ui.timer(2.0, self.refresh_wallet)
        ui.timer(0.1, self._restore_session, once=True)

    # A sign-in survives a page reload: it is kept in this browser's storage
    # and restored if the cluster still accepts the session.

    def _remember(self) -> None:
        app.storage.user["session"] = {"token": self.session.token, "username": self.session.username,
                                       "user_id": self.session.user_id}

    def _forget(self) -> None:
        app.storage.user.pop("session", None)

    async def _restore_session(self) -> None:
        saved = app.storage.user.get("session")
        if not saved or self.session.signed_in:
            return
        self.session.token, self.session.username, self.session.user_id = (
            saved["token"], saved["username"], saved["user_id"])
        try:
            wallet = await self.session.wallet()
            valid = wallet.success
        except Exception:
            valid = False
        if not valid:
            self.session.token = ""
            self._forget()
        self._build_side()
        self.version_seen = -1

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        with ui.row().classes("w-full items-start gap-6 no-wrap max-lg:flex-wrap"):
            with ui.column().classes("grow gap-4 min-w-0"):
                with ui.row().classes("w-full items-center gap-3"):
                    ui.label("Live auctions").classes("text-xl font-bold")
                    self.count = ui.badge("0", color="grey-4", text_color="grey-9").props("rounded")
                    ui.space()
                    ui.switch("test auctions", on_change=self._toggle_tests).props("dense color=grey-7") \
                        .classes("text-sm rt-muted").tooltip("Show the auctions the test scripts create")
                    ui.button("Sell an item", icon="add_circle", on_click=self.open_sell).props("unelevated")
                self.grid = ui.element("div").classes("grid gap-4 w-full") \
                    .style("grid-template-columns: repeat(auto-fill, minmax(280px, 1fr))")
                self.empty = ui.label("No live auctions right now - list one with “Sell an item”.") \
                    .classes("rt-muted")
                ui.label("Recently closed").classes("text-lg font-bold mt-4")
                self.closed = ui.column().classes("w-full gap-3")
            self.side = ui.column().classes("w-[340px] shrink-0 gap-4 max-lg:w-full")
        self._build_side()

    def _toggle_tests(self, event) -> None:
        self.show_tests = event.value
        self.version_seen = -1

    def _visible(self, auctions):
        return [a for a in auctions if self.show_tests or not theme.is_test_auction(a.item.title)]

    # ------------------------------------------------------------ live updates

    async def tick(self) -> None:
        if snapshots.auctions_version != self.version_seen:
            self.version_seen = snapshots.auctions_version
            self._sync_cards()
            self._render_closed()
            self._render_wins()
        for card in self.cards.values():
            card.tick()

    def _sync_cards(self) -> None:
        live = [a for a in self._visible(snapshots.auctions) if a.status == ACTIVE]
        live.sort(key=lambda a: a.end_time_ms)
        ids = {a.auction_id for a in live}
        for gone in [i for i in self.cards if i not in ids]:
            self.cards.pop(gone).card.delete()
        for auction in live:
            if auction.auction_id in self.cards:
                self.cards[auction.auction_id].update(auction)
            else:
                self.cards[auction.auction_id] = AuctionCard(self, auction)
                retrigger(self.cards[auction.auction_id].card, "rt-pop")
        self.count.set_text(str(len(live)))
        self.empty.set_visibility(not live)

    def _render_closed(self) -> None:
        closed = [a for a in self._visible(snapshots.auctions) if a.status == CLOSED]
        closed.sort(key=lambda a: a.end_time_ms, reverse=True)
        self.closed.clear()
        with self.closed:
            if not closed:
                ui.label("Nothing has closed yet.").classes("rt-muted")
            for auction in closed[:6]:
                self._result_card(auction)

    def _result_card(self, auction) -> None:
        result = snapshots.summaries.get(auction.auction_id)
        with ui.card().classes("rt-card w-full p-4 gap-1"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.icon("sell" if auction.current_highest_bidder_id else "block") \
                    .classes("text-xl " + ("text-positive" if auction.current_highest_bidder_id else "text-grey-6"))
                ui.label(auction.item.title).classes("font-semibold grow")
                if result is not None and result.escrow_status:
                    self._escrow_chip(result.escrow_status)
            if result is None or not result.escrow_status:
                ui.label("Settling…").classes("text-sm rt-muted")
                return
            if result.winner_id:
                ui.label(f"Sold to {name(result.winner_id)} for {money(result.final_price)} "
                         f"(+ {money(result.buyers_premium)} buyer's premium)").classes("text-sm")
            else:
                ui.label("No sale - no bid met the rules").classes("text-sm rt-muted")
            if result.summary_ready:
                with ui.row().classes("items-start gap-2 no-wrap mt-1"):
                    ui.icon("support_agent").classes("text-primary mt-[2px]")
                    ui.label(result.summary).classes("text-sm italic text-slate-600")
                    ui.badge("template" if result.summary_from_fallback else "LLM",
                             color="grey-5" if result.summary_from_fallback else "indigo-4").props("outline")
            else:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner("dots", size="1.2em")
                    ui.label("Tapri is writing the recap…").classes("text-sm rt-muted")
            if result.winner_id == self.session.user_id and result.escrow_status.startswith("HELD"):
                ui.button("I received it - release escrow", icon="handshake",
                          on_click=lambda aid=auction.auction_id: self.confirm(aid)) \
                    .props("unelevated color=positive").classes("mt-2")

    @staticmethod
    def _escrow_chip(status: str) -> None:
        label = status.replace(" (mock)", "")
        colour = {"HELD": "orange-7", "RELEASED": "positive", "NO_SALE": "grey-6"}.get(label, "negative")
        ui.chip(f"escrow {label.lower()}" if label != "NO_SALE" else "no sale",
                icon="lock" if label == "HELD" else "lock_open" if label == "RELEASED" else None) \
            .props(f"dense outline color={colour}")

    # ------------------------------------------------------------ side panel

    def _build_side(self) -> None:
        self.side.clear()
        self.wallet_labels = {}
        with self.side:
            if self.session.signed_in:
                self._account_card()
                self.wins = ui.column().classes("w-full")
                self.activity = ui.column().classes("w-full")
                self._render_wins()
            else:
                self._sign_in_card()
            self._about_card()

    def _sign_in_card(self) -> None:
        with ui.card().classes("rt-card w-full p-5 gap-3"):
            ui.label("Join the auction").classes("text-lg font-bold")
            ui.label("Register once, then sign in. New wallets get Rs 5,000 of mock money.") \
                .classes("text-sm rt-muted")
            username = ui.input("Username").props("outlined dense").classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True) \
                .props("outlined dense").classes("w-full")

            async def sign_in() -> None:
                reply = await self.session.login(username.value.strip(), password.value)
                if reply is None:
                    ui.notify("The cluster has no leader right now - try again in a moment", type="warning")
                elif reply.success:
                    self._remember()
                    ui.notify(f"Signed in as {self.session.username}", type="positive")
                    self._build_side()
                    self.version_seen = -1
                else:
                    ui.notify(reply.error_message, type="negative")

            async def register() -> None:
                reply = await self.session.register(username.value.strip(), password.value)
                if reply is None or not reply.success:
                    ui.notify(reply.error_message if reply else "No leader right now - try again", type="negative")
                    return
                ui.notify("Account created - Rs 5,000 welcome credit added", type="positive")
                await sign_in()

            password.on("keydown.enter", sign_in)
            with ui.row().classes("w-full gap-2 no-wrap"):
                ui.button("Sign in", on_click=sign_in).props("unelevated").classes("grow")
                ui.button("Register", on_click=register).props("outline").classes("grow")

    def _account_card(self) -> None:
        with ui.card().classes("rt-card w-full p-5 gap-3"):
            with ui.row().classes("w-full items-center no-wrap gap-3"):
                with ui.avatar(color="primary", text_color="white", size="md"):
                    ui.label(self.session.username[:1].upper())
                with ui.column().classes("gap-0 grow"):
                    ui.label(self.session.username).classes("font-semibold")
                    ui.label("signed in").classes("text-xs rt-muted")

                async def sign_out() -> None:
                    await self.session.logout()
                    self._forget()
                    self._build_side()
                    self.version_seen = -1

                ui.button(icon="logout", on_click=sign_out).props("flat round").tooltip("Sign out")
            ui.separator()
            ui.label("Wallet balance").classes("text-xs rt-muted uppercase tracking-wide")
            self.wallet_labels["balance"] = ui.label("…").classes("rt-price")
            with ui.grid(columns=3).classes("w-full gap-2"):
                for key, label, tip in [
                    ("available", "Available", "What a new bid can use (bid + 5% premium)"),
                    ("committed", "In bids", "Promised to auctions you are winning"),
                    ("in_escrow", "In escrow", "Paid for won items, waiting for hand-over"),
                ]:
                    with ui.column().classes("gap-0").tooltip(tip):
                        ui.label(label).classes("text-[11px] rt-muted")
                        self.wallet_labels[key] = ui.label("…").classes("text-sm font-semibold")
            ui.button("Add money (mock)", icon="account_balance_wallet", on_click=self.open_deposit) \
                .props("outline").classes("w-full")

    def _about_card(self) -> None:
        with ui.card().classes("rt-card w-full p-4 gap-1"):
            ui.label("How it works").classes("font-semibold")
            for icon, text in [
                ("hub", "Every bid is a Raft log entry, committed on a majority of three servers."),
                ("payments", "A bid needs its price + 5% premium in available funds."),
                ("timer", "A bid in the last 10 s extends the auction (anti-sniping)."),
                ("lock", "The winner pays into escrow; the seller is paid on hand-over."),
            ]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon(icon).classes("text-primary mt-[2px]")
                    ui.label(text).classes("text-sm text-slate-600")

    def _render_wins(self) -> None:
        if not self.session.signed_in or not hasattr(self, "wins"):
            return
        mine = [a for a in snapshots.auctions
                if a.status == CLOSED and a.current_highest_bidder_id == self.session.user_id]
        self.wins.clear()
        if not mine:
            return
        with self.wins, ui.card().classes("rt-card w-full p-4 gap-2"):
            ui.label("Your wins").classes("font-semibold")
            for auction in sorted(mine, key=lambda a: a.end_time_ms, reverse=True)[:5]:
                result = snapshots.summaries.get(auction.auction_id)
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(auction.item.title).classes("text-sm grow rt-clamp")
                    if result is not None and result.escrow_status:
                        self._escrow_chip(result.escrow_status)
                if result is not None and result.escrow_status.startswith("HELD"):
                    ui.button("Confirm receipt", icon="handshake",
                              on_click=lambda aid=auction.auction_id: self.confirm(aid)) \
                        .props("dense unelevated color=positive").classes("w-full")

    async def refresh_wallet(self) -> None:
        if not self.session.signed_in or not self.wallet_labels:
            return
        try:
            wallet = await self.session.wallet()
            history = await self.session.transactions()
        except Exception:
            return
        if not wallet.success:  # session expired or replaced
            if "session" in wallet.error_message.lower():
                self.session.token = ""
                self._forget()
                self._build_side()
            return
        for key in ("balance", "available", "committed", "in_escrow"):
            self.wallet_labels[key].set_text(money(getattr(wallet, key)))
        self.activity.clear()
        with self.activity, ui.card().classes("rt-card w-full p-4 gap-1"):
            ui.label("Wallet activity").classes("font-semibold")
            for txn in list(history.transactions)[:6]:
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.icon({"WELCOME_CREDIT": "redeem", "DEPOSIT": "add_card", "ESCROW_HOLD": "lock",
                             "SALE_PROCEEDS": "savings"}.get(txn.kind, "receipt")).classes("text-slate-500")
                    ui.label(txn.kind.replace("_", " ").lower()).classes("text-sm grow")
                    ui.label(("+" if txn.amount > 0 else "−") + money(abs(txn.amount))) \
                        .classes("text-sm font-semibold " + ("text-positive" if txn.amount > 0 else "text-negative"))

    # ------------------------------------------------------------ actions

    def _need_session(self) -> bool:
        if not self.session.signed_in:
            ui.notify("Sign in first (panel on the right)", type="warning")
            return False
        return True

    def _auction(self, auction_id: str):
        return next((a for a in snapshots.auctions if a.auction_id == auction_id), None)

    async def open_bid(self, auction_id: str) -> None:
        if not self._need_session():
            return
        if self._auction(auction_id) is None:
            return
        state = {"available": 0.0}

        with ui.dialog() as dialog, ui.card().classes("w-[460px] p-6 gap-3") as card:
            title = ui.label().classes("text-lg font-bold")
            rule = ui.label().classes("text-sm rt-muted")
            amount = ui.number("Your bid (Rs)", format="%.2f", min=0.01).props("outlined").classes("w-full")
            need = ui.label().classes("text-sm")

            def show_need() -> None:
                value = float(amount.value or 0)
                total = value * (1 + PREMIUM_PERCENT / 100)
                ok = total <= state["available"] + 0.005
                need.set_text(f"Needs {money(total)} with the 5% premium · "
                              f"you have {money(state['available'])} available")
                need.classes(replace="text-sm " + ("text-slate-600" if ok else "text-negative"))

            async def refresh(suggest: bool) -> None:
                """Re-read the auction, its bids and the wallet (after an outbid, say)."""
                auction = await get_auction(auction_id) or self._auction(auction_id)
                history = await bid_history(auction_id)
                wallet = await self.session.wallet()
                available = wallet.available if wallet.success else 0.0
                if auction.current_highest_bidder_id == self.session.user_id:
                    # Raising your own winning bid replaces that promise.
                    available += auction.current_highest_bid * (1 + PREMIUM_PERCENT / 100)
                state["available"] = available
                title.set_text(auction.item.title)
                if auction.current_highest_bid > 0:
                    text = f"more than {money(auction.current_highest_bid)}"
                elif auction.reserve_price > 0:
                    text = f"at least {money(auction.reserve_price)} (the reserve)"
                else:
                    text = "any amount above Rs 0"
                rule.set_text(f"Your bid must be {text}.")
                if suggest:
                    amount.set_value(next_valid(auction))
                show_need()
                history_title.set_text(f"Bid history ({len(history)})")
                history_box.clear()
                with history_box:
                    if not history:
                        ui.label("No bids yet - be the first.").classes("text-sm rt-muted")
                    for bid in reversed(history[-10:]):
                        mine = bid.bidder_id == self.session.user_id
                        with ui.row().classes("w-full no-wrap text-sm"):
                            ui.label(name(bid.bidder_id) + (" (you)" if mine else "")) \
                                .classes("grow" + (" font-semibold text-primary" if mine else ""))
                            ui.label(money(bid.amount)).classes("font-semibold")
                            ui.label(theme.clock(bid.timestamp_ms / 1000)).classes("rt-muted rt-mono w-20 text-right")

            amount.on_value_change(show_need)

            async def place() -> None:
                button.props("loading")
                request_id = uuid.uuid4().hex  # the same id on every retry: exactly-once
                value = float(amount.value or 0)
                reply = await self.session.bid(auction_id, value, request_id)
                button.props(remove="loading")
                if reply is None:
                    ui.notify("No leader right now - your bid was not placed. Try again.", type="warning")
                elif reply.accepted:
                    note = " (a retry - answered from the request table)" if reply.replayed else ""
                    ui.notify(f"Your bid of {money(value)} is committed on a majority{note}",
                              type="positive", icon="gavel")
                    dialog.close()
                else:
                    ui.notify(reply.error_message, type="negative", multi_line=True)
                    retrigger(card, "rt-shake")
                    # Outbid in the meantime: show the new state and a fresh suggestion.
                    await refresh(suggest="greater than" in reply.error_message)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                button = ui.button("Place bid", icon="gavel", on_click=place).props("unelevated")
            amount.on("keydown.enter", place)

            ui.separator()
            history_title = ui.label().classes("text-sm font-semibold")
            history_box = ui.column().classes("w-full gap-1 max-h-48 overflow-auto")
        await refresh(suggest=True)
        dialog.open()

    async def open_ask(self, auction_id: str = "") -> None:
        if not self._need_session():
            return
        auction = self._auction(auction_id) if auction_id else None
        with ui.dialog() as dialog, ui.card().classes("w-[600px] max-w-full p-0 gap-0"):
            with ui.row().classes("w-full items-center gap-3 p-4 bg-slate-50 no-wrap"):
                with ui.avatar(color="amber-6", text_color="white"):
                    ui.label("T")
                with ui.column().classes("gap-0 grow"):
                    ui.label("Tapri · the auction assistant").classes("font-semibold")
                    ui.label(f"about: {auction.item.title}" if auction else "house rules, your wallet, anything")\
                        .classes("text-xs rt-muted")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")
            with ui.scroll_area().classes("w-full h-[380px]") as scroller:
                chat = ui.column().classes("w-full p-2 gap-3")
            with ui.row().classes("w-full gap-2 px-4"):
                for question in SUGGESTED_QUESTIONS:
                    ui.chip(question, on_click=lambda q=question: send(q)).props("outline dense clickable")
            with ui.row().classes("w-full items-center gap-2 p-4 no-wrap"):
                box = ui.input(placeholder="Ask Tapri…").props("outlined dense").classes("grow")
                ui.button(icon="send", on_click=lambda: send(box.value)).props("round unelevated")
            box.on("keydown.enter", lambda: send(box.value))

            async def send(question: str) -> None:
                question = (question or "").strip()
                if not question:
                    return
                box.set_value("")
                with chat:
                    with ui.row().classes("w-full justify-end"):
                        ui.label(question).classes("rt-chat-q px-3 py-2 text-sm max-w-[80%]")
                    with ui.row().classes("w-full") as thinking:
                        with ui.row().classes("rt-chat-a px-3 py-2 items-center gap-2"):
                            ui.spinner("dots", size="1.4em")
                            ui.label("Tapri is thinking (a 1.5B model on CPU)…").classes("text-sm rt-muted")
                scroller.scroll_to(percent=1.0)
                try:
                    reply = await self.session.ask(question, auction_id)
                except Exception:
                    reply = None
                thinking.delete()
                with chat, ui.row().classes("w-full"):
                    with ui.column().classes("rt-chat-a px-3 py-2 gap-2 max-w-[90%] rt-pop"):
                        if reply is None or not reply.success:
                            ui.label(reply.error_message if reply else "Couldn't reach the cluster.") \
                                .classes("text-sm text-negative")
                            return
                        answer, _, fine = reply.answer.partition("\n\nFine print")
                        ui.label(answer).classes("text-sm whitespace-pre-line")
                        if fine:
                            ui.label("Fine print" + fine).classes("text-xs text-slate-500 border-l-2 pl-2")
                        with ui.row().classes("gap-1 items-center"):
                            ui.badge("offline fallback" if reply.from_fallback else "LLM · raftioneer-tapri",
                                     color="grey-6" if reply.from_fallback else "indigo-5").props("outline")
                            for source in reply.sources:
                                ui.chip(source, icon="menu_book").props("dense outline color=teal-7") \
                                    .classes("text-[11px]").tooltip("Handbook section the answer was grounded in")
                scroller.scroll_to(percent=1.0)
        dialog.open()

    def open_sell(self) -> None:
        if not self._need_session():
            return
        with ui.dialog() as dialog, ui.card().classes("w-[480px] p-6 gap-3"):
            ui.label("Sell an item").classes("text-lg font-bold")
            title = ui.input("Title", placeholder="e.g. Firefox cycle, 21 gears").props("outlined").classes("w-full")
            description = ui.textarea("Description", placeholder="Leave blank - Tapri will write one") \
                .props("outlined autogrow").classes("w-full")
            with ui.row().classes("w-full gap-3 no-wrap"):
                reserve = ui.number("Reserve (Rs)", value=0, min=0, format="%.0f").props("outlined").classes("grow")
                duration = ui.select({60: "1 minute", 120: "2 minutes", 300: "5 minutes", 600: "10 minutes",
                                      3600: "1 hour"}, value=120, label="Runs for").props("outlined").classes("grow")
            # The client node's personas never bid past their budgets (client/main.py).
            ui.label(f"The simulated bidders spend at most {money(PERSONA_TOP_BUDGET)} - above that, only "
                     "people bidding by hand will bid.") \
                .classes("text-xs text-warning") \
                .bind_visibility_from(reserve, "value", backward=lambda v: (v or 0) >= PERSONA_TOP_BUDGET)

            async def create() -> None:
                if not title.value.strip():
                    ui.notify("Give the item a title", type="warning")
                    return
                button.props("loading")
                if not description.value.strip():
                    ui.notify("Tapri is writing the description…", type="info", icon="edit_note")
                reply = await self.session.create(title.value.strip(), description.value.strip(),
                                                  int(duration.value), float(reserve.value or 0))
                button.props(remove="loading")
                if reply is not None and reply.success:
                    ui.notify(f"Listed - auction {reply.auction_id} is live on all three servers", type="positive")
                    dialog.close()
                else:
                    ui.notify(reply.error_message if reply else "No leader right now - try again", type="negative")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                button = ui.button("List it", icon="sell", on_click=create).props("unelevated")
        dialog.open()

    def open_deposit(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[380px] p-6 gap-3"):
            ui.label("Add money (mock)").classes("text-lg font-bold")
            ui.label("No real payment - the amount is added to your wallet through a Raft command.") \
                .classes("text-sm rt-muted")
            amount = ui.number("Amount (Rs)", value=5000, min=1, format="%.0f").props("outlined").classes("w-full")
            with ui.row().classes("gap-2"):
                for preset in (1000, 5000, 20000):
                    ui.chip(theme.short_money(preset), on_click=lambda p=preset: amount.set_value(p)) \
                        .props("outline clickable")

            async def deposit() -> None:
                reply = await self.session.deposit(float(amount.value or 0), uuid.uuid4().hex)
                if reply is not None and reply.success:
                    ui.notify(f"Wallet balance now {money(reply.balance)}", type="positive")
                    dialog.close()
                    await self.refresh_wallet()
                else:
                    ui.notify(reply.error_message if reply else "No leader right now", type="negative")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Add", icon="add", on_click=deposit).props("unelevated")
        dialog.open()

    async def confirm(self, auction_id: str) -> None:
        reply = await self.session.confirm_receipt(auction_id)
        if reply is not None and reply.success:
            ui.notify("Escrow released - the seller has been paid", type="positive", icon="handshake")
            await snapshots.refresh_summary(auction_id)
            await self.refresh_wallet()
        else:
            ui.notify(reply.error_message if reply else "No leader right now", type="negative")

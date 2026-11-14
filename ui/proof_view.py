"""Proof: run the project's checks live, from the browser.

Each card runs one of the existing test suites or check scripts as a
subprocess (exactly the command shown on the card) and streams its output.
The runs are shared by every open tab, so one person can start a suite on
the laptop and the projector shows it too.
"""
from __future__ import annotations

import asyncio

from nicegui import background_tasks, ui

import ops

_lock = asyncio.Lock()  # one suite at a time: they share one cluster


async def _run(suite: ops.Suite) -> None:
    async with _lock:
        await ops.run_suite(suite)


def start(suite: ops.Suite) -> None:
    if suite.state == "running":
        return
    suite.state, suite.summary = "running", "waiting for the suite before it…" if _lock.locked() else ""
    suite.version += 1
    background_tasks.create(_run(suite), name=f"suite-{suite.key}")


class SuiteCard:
    def __init__(self, suite: ops.Suite) -> None:
        self.suite = suite
        self.seen = -1
        self.lines_shown = 0
        self.last_state = suite.state
        with ui.card().classes("rt-card w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center no-wrap gap-4"):
                ui.icon(suite.icon).classes("text-3xl text-primary")
                with ui.column().classes("gap-0 grow min-w-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(suite.title).classes("font-semibold text-[15px]")
                        ui.badge("live cluster" if suite.live else "no Docker needed",
                                 color="indigo-1" if suite.live else "teal-1",
                                 text_color="indigo-9" if suite.live else "teal-9").props("rounded")
                    ui.label(suite.what).classes("text-sm rt-muted")
                    ui.label("$ " + suite.shown_command).classes("text-[11px] rt-mono text-slate-400 truncate w-full")
                self.status = ui.row().classes("items-center gap-2 no-wrap shrink-0")
                self.button = ui.button("Run", icon="play_arrow", on_click=lambda: start(suite)) \
                    .props("unelevated").classes("shrink-0")
            self.bar = ui.linear_progress(value=0, show_value=False).props("rounded size=6px")
            with ui.expansion("Output", icon="terminal").props("dense").classes("w-full text-sm rt-muted") \
                    as self.output:
                self.log = ui.log(max_lines=600).classes("w-full h-64 text-[12px] rt-mono bg-slate-900 text-slate-100")
        self.update()

    def update(self) -> None:
        suite = self.suite
        if suite.version == self.seen:
            return
        self.seen = suite.version
        if suite.state == "running" and self.last_state != "running":
            self.log.clear()
            self.lines_shown = 0
            self.output.open()
        for line in suite.lines[self.lines_shown:]:
            self.log.push(line)
        self.lines_shown = len(suite.lines)
        self.bar.set_value(suite.progress)
        self.bar.props("indeterminate" if suite.state == "running" and suite.progress == 0 else "remove indeterminate")
        if suite.state != "running":
            self.bar.props(remove="indeterminate")
        self.bar.props(f"color={'negative' if suite.state == 'failed' else 'positive' if suite.state == 'passed' else 'primary'}")
        self.button.set_enabled(suite.state != "running")
        changed = suite.state != self.last_state
        self.last_state = suite.state
        self.status.clear()
        with self.status:
            if suite.state == "running":
                ui.spinner("dots", size="1.6em")
                ui.label(suite.summary or "running…").classes("text-sm rt-muted max-w-[260px] truncate")
            elif suite.state in ("passed", "failed"):
                ok = suite.state == "passed"
                ui.icon("check_circle" if ok else "cancel") \
                    .classes(("text-positive" if ok else "text-negative") + " text-4xl" + (" rt-pop" if changed else ""))
                with ui.column().classes("gap-0"):
                    ui.label("PASS" if ok else "FAIL").classes(
                        "font-bold " + ("text-positive" if ok else "text-negative"))
                    ui.label(f"{suite.summary[:70]} · {suite.seconds:.1f}s").classes("text-xs rt-muted max-w-[260px]")


class ProofView:
    def __init__(self) -> None:
        with ui.card().classes("rt-card w-full p-5 gap-1"):
            with ui.row().classes("w-full items-center gap-3"):
                with ui.column().classes("gap-0 grow"):
                    ui.label("Proof - run the checks live").classes("text-xl font-bold")
                    ui.label("Every claim on the slides has a check behind it. Each card runs the exact command "
                             "shown, against this running cluster, and streams its output.").classes("rt-muted")
                self.overall = ui.label().classes("font-semibold")
                ui.button("Run all", icon="playlist_play", on_click=self.run_all).props("unelevated")
        with ui.element("div").classes("grid gap-4 w-full") \
                .style("grid-template-columns: repeat(auto-fill, minmax(560px, 1fr)); align-items: start"):
            self.cards = [SuiteCard(suite) for suite in ops.SUITES]
        ui.timer(0.3, self.tick)

    def tick(self) -> None:
        for card in self.cards:
            card.update()
        done = [s for s in ops.SUITES if s.state in ("passed", "failed")]
        passed = [s for s in done if s.state == "passed"]
        self.overall.set_text(f"{len(passed)}/{len(ops.SUITES)} passed" if done else "")
        self.overall.classes(replace="font-semibold " + (
            "text-negative" if len(passed) < len(done) else "text-positive"))

    def run_all(self) -> None:
        for suite in ops.SUITES:
            start(suite)

"""Evidence: every claim we make, with the check behind it.

One row per claim. Each check is one of the project's own test suites or
scripts, run as a subprocess (exactly the command shown in the row) - the
dashboard and the terminal are two ways to press the same button. Two groups:
the quick checks against the running cluster, and the full suites, which take
longer; a verdict stays on the page with the time it finished.

The runs are shared by every open tab, so a suite started on the laptop
shows on the projector too.
"""
from __future__ import annotations

import asyncio

from nicegui import background_tasks, ui

import ops
import theme

_lock = asyncio.Lock()  # one suite at a time: they share one cluster


async def _run(suite: ops.Suite) -> None:
    async with _lock:
        await ops.run_suite(suite)


def start(suite: ops.Suite) -> None:
    if suite.state == "running":
        return
    suite.state, suite.summary, suite.last_line = "running", "", "queued…" if _lock.locked() else ""
    suite.version += 1
    background_tasks.create(_run(suite), name=f"suite-{suite.key}")


class Row:
    """One claim: verdict, claim and check, the Run button, and the output on demand."""

    def __init__(self, suite: ops.Suite) -> None:
        self.suite = suite
        self.seen = -1
        self.lines_shown = 0
        self.last_state = suite.state
        with ui.column().classes("rt-row w-full px-4 py-3 gap-1"):
            with ui.row().classes("w-full items-center no-wrap gap-4"):
                self.mark = ui.row().classes("w-8 justify-center shrink-0")
                with ui.column().classes("gap-0 grow min-w-0"):
                    ui.label(suite.claim).classes("font-semibold text-[15px] leading-snug")
                    ui.label(suite.title).classes("text-sm rt-muted")
                    with ui.row().classes("items-center gap-3"):
                        ui.label("$ " + suite.shown_command).classes("text-[11px] rt-mono rt-muted truncate")
                        ui.label(("live cluster" if suite.live else "no Docker needed") + f" · about {suite.about}") \
                            .classes("rt-label").style("font-size:10px")
                self.verdict = ui.column().classes("gap-0 items-end shrink-0 w-[250px]")
                self.toggle = ui.button(icon="terminal", on_click=self._toggle_output) \
                    .props("flat round dense").tooltip("Show / hide the output")
                self.button = ui.button("Run", on_click=lambda: start(suite)).props("unelevated no-caps dense") \
                    .classes("px-4 shrink-0")
            self.bar = ui.linear_progress(value=0, show_value=False).props("rounded size=3px")
            self.log = ui.log(max_lines=600).classes("w-full h-56 text-[12px] rt-mono") \
                .style(f"background:{theme.INK}; color:{theme.PAPER}")
            self.log.set_visibility(False)
        self.update()

    def _toggle_output(self) -> None:
        self.log.set_visibility(not self.log.visible)

    def update(self) -> None:
        suite = self.suite
        if suite.version == self.seen:
            return
        self.seen = suite.version
        if suite.state == "running" and self.last_state != "running":
            self.log.clear()
            self.lines_shown = 0
        for line in suite.lines[self.lines_shown:]:
            self.log.push(line)
        self.lines_shown = len(suite.lines)
        running = suite.state == "running"
        self.bar.set_visibility(running)
        self.bar.set_value(suite.progress)
        self.bar.props("indeterminate" if running and suite.progress == 0 else "remove indeterminate")
        self.button.set_enabled(not running)
        changed = suite.state != self.last_state
        self.last_state = suite.state
        if changed and suite.state == "failed":
            self.log.set_visibility(True)  # a failure should explain itself
        self.mark.clear()
        self.verdict.clear()
        with self.mark:
            if running:
                ui.spinner("dots", size="1.5em")
            elif suite.state in ("passed", "failed"):
                ok = suite.state == "passed"
                ui.icon("check_circle" if ok else "cancel") \
                    .classes(("text-positive" if ok else "text-negative") + " text-3xl" + (" rt-pop" if changed else ""))
            else:
                ui.icon("radio_button_unchecked").classes("text-2xl").style(f"color:{theme.LINE}")
        with self.verdict:
            if running:
                # the script's latest line, so the row shows life without a wall of text
                ui.label(suite.last_line or "starting…").classes("text-xs rt-mono rt-muted truncate w-full text-right")
            elif suite.state in ("passed", "failed"):
                ok = suite.state == "passed"
                ui.label(("PASS" if ok else "FAIL") + f" · {suite.seconds:.1f} s · at {theme.clock(suite.finished_at)}") \
                    .classes("rt-mono text-sm font-bold " + ("text-positive" if ok else "text-negative"))
                ui.label(suite.summary[:80]).classes("text-xs rt-muted text-right truncate w-full")
            else:
                ui.label("not run yet").classes("text-xs rt-muted")


class ProofView:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        with ui.row().classes("w-full items-end gap-3"):
            with ui.column().classes("gap-0 grow"):
                ui.label("Evidence").classes("rt-title")
                ui.label("Each claim, and the check behind it.").classes("rt-muted text-sm")
            self.overall = ui.label().classes("rt-mono font-bold")
        self._group("live", "Live checks", "against the running cluster, seconds each")
        self._group("before", "Full suites", "test suites and the end-to-end walkthrough")
        ui.timer(0.3, self.tick)

    def _group(self, group: str, heading: str, hint: str) -> None:
        suites = [s for s in ops.SUITES if s.group == group]
        with ui.card().classes("rt-card w-full p-0 gap-0"):
            with ui.row().classes("w-full items-center gap-3 px-4 py-3"):
                ui.label(heading).classes("rt-label").style(f"color:{theme.INK}; font-weight:700")
                ui.label(hint).classes("text-xs rt-muted grow")
                ui.button(f"Run all {len(suites)}", on_click=lambda: [start(s) for s in suites]) \
                    .props("outline no-caps dense").classes("px-3")
            self.rows += [Row(suite) for suite in suites]

    def tick(self) -> None:
        for row in self.rows:
            row.update()
        done = [s for s in ops.SUITES if s.state in ("passed", "failed")]
        passed = [s for s in done if s.state == "passed"]
        self.overall.set_text(f"{len(passed)}/{len(ops.SUITES)} claims verified" if done else "")
        self.overall.classes(replace="rt-mono font-bold " + (
            "text-negative" if len(passed) < len(done) else "text-positive"))

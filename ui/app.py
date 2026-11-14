#!/usr/bin/env python3
"""Raftioneer's demo UI - a web page for the live presentation.

    python ui/app.py            # then open http://localhost:8080

It is one more client node: pure Python (NiceGUI), talking to the app
servers over gRPC exactly like client/main.py, plus a remote control for
Docker (to kill / freeze / restart nodes) and for the test suites. Run it on
the machine running `docker compose`, with the stack up and the stubs
generated (./scripts/generate_protos.sh).

Three tabs:
  Auction floor   sign in, bid, ask Tapri, sell, wallet and escrow
  Raft cluster    roles, terms, each node's log, kill/freeze/restart, elections
  Proof           the test suites and check scripts, run live
"""
import argparse
import asyncio

from nicegui import app, ui

import theme
from backend import Session, snapshots
from cluster_view import ClusterView
from floor import Floor
from proof_view import ProofView

_pollers: list[asyncio.Task] = []


async def start_pollers() -> None:
    _pollers.append(asyncio.create_task(snapshots.poll_cluster()))
    _pollers.append(asyncio.create_task(snapshots.poll_auctions()))


app.on_startup(start_pollers)


@ui.page("/")
def index(tab: str = "floor") -> None:
    """tab = floor, cluster or proof (e.g. /?tab=cluster for a second window)."""
    theme.install()
    session = Session()

    with ui.header().classes("bg-slate-900 items-center px-6 py-2 gap-4 no-wrap"):
        ui.icon("gavel").classes("text-amber-400 text-3xl")
        with ui.column().classes("gap-0"):
            ui.label("Raftioneer").classes("text-xl font-bold text-white leading-tight")
            ui.label("Campus clearance auction · gRPC · Raft · LLM").classes("text-xs text-slate-400")
        ui.space()
        with ui.tabs().props("dense no-caps active-color=amber-4 indicator-color=amber-4") \
                .classes("text-slate-200") as tabs:
            floor_tab = ui.tab("floor", "Auction floor", icon="storefront")
            cluster_tab = ui.tab("cluster", "Raft cluster", icon="hub")
            proof_tab = ui.tab("proof", "Proof", icon="verified")
        ui.space()
        with ui.row().classes("items-center gap-2 no-wrap"):
            live = ui.element("div").classes("rt-live")
            status = ui.label("connecting…").classes("text-sm text-slate-300")

    def header_status() -> None:
        snap = snapshots.cluster
        up = sum(1 for s in snap.nodes.values() if s is not None)
        if snap.leader:
            status.set_text(f"{up}/3 up · leader {snap.leader} · term {snap.term}")
            live.classes(remove="rt-bad")
        else:
            status.set_text(f"{up}/3 up · no leader")
            live.classes(add="rt-bad")

    ui.timer(0.5, header_status)

    first = {"cluster": cluster_tab, "proof": proof_tab}.get(tab, floor_tab)
    with ui.tab_panels(tabs, value=first, animated=False) \
            .classes("w-full max-w-[1440px] mx-auto bg-transparent"):
        with ui.tab_panel(floor_tab).classes("px-2"):
            Floor(session)
        with ui.tab_panel(cluster_tab).classes("px-2"):
            ClusterView()
        with ui.tab_panel(proof_tab).classes("px-2 gap-4"):
            ProofView()


if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from another machine")
    args = parser.parse_args()
    ui.run(title="Raftioneer", host=args.host, port=args.port, reload=False, show=False,
           favicon="🔨", reconnect_timeout=10)

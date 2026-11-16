#!/usr/bin/env python3
"""Raftioneer's demo UI - a web page for the live presentation.

    python ui/app.py            # then open http://localhost:8080

It is one more client node: pure Python (NiceGUI), talking to the app
servers over gRPC exactly like client/main.py, plus a remote control for
Docker (to kill / freeze / restart nodes) and for the test suites. Run it on
the machine running `docker compose`, with the stack up and the stubs
generated (./scripts/generate_protos.sh).

Three tabs, and Tapri (the assistant) on all of them:
  Floor      sign in, bid, sell, wallet and escrow
  Cluster    all five nodes, roles, terms, each node's log, kill/freeze/restart
  Evidence   every claim with the check behind it, run live
"""
import argparse
import asyncio

from nicegui import app, ui

import theme
from backend import Session, snapshots
from cluster_view import ClusterView
from floor import Floor
from proof_view import ProofView
from tapri import Tapri

_pollers: list[asyncio.Task] = []


async def start_pollers() -> None:
    _pollers.append(asyncio.create_task(snapshots.poll_cluster()))
    _pollers.append(asyncio.create_task(snapshots.poll_auctions()))


app.on_startup(start_pollers)


@ui.page("/")
def index(tab: str = "floor") -> None:
    """tab = floor, cluster or evidence (e.g. /?tab=cluster for a second window)."""
    theme.install()
    session = Session()

    tapri = Tapri(session)  # the assistant's side panel; its button sits in the header

    with ui.header().classes("rt-header items-center px-6 py-2 gap-3 no-wrap").props("elevated=false"):
        # The logo is the cluster: the amber dot is whoever leads right now.
        logo = ui.html(theme.logo_svg(), sanitize=False).tooltip(
            "The logo is the cluster - the amber dot is the current leader")
        with ui.column().classes("gap-[3px]"):
            ui.label("raftioneer").classes("rt-wordmark")
            ui.label("campus clearance auction").classes("rt-label").style("font-size:9.5px")
        ui.space()
        with ui.tabs().props("dense no-caps active-color=primary indicator-color=accent") as tabs:
            floor_tab = ui.tab("floor", "Floor")
            cluster_tab = ui.tab("cluster", "Cluster")
            proof_tab = ui.tab("proof", "Evidence")
        ui.space()
        with ui.row().classes("items-center gap-3 no-wrap"):
            live = ui.element("div").classes("rt-live")
            status = ui.label("connecting…").classes("text-xs rt-mono rt-muted")
            tapri.button()

    logo_roles: dict = {}

    def header_status() -> None:
        snap = snapshots.cluster
        up = sum(1 for s in snap.nodes.values() if s is not None)
        if snap.leader:
            status.set_text(f"term {snap.term} · leader {snap.leader} · {up}/3 up")
            live.classes(remove="rt-bad")
        else:
            status.set_text(f"no leader · {up}/3 up")
            live.classes(add="rt-bad")
        roles = {node: ("DOWN" if s is None else s.role) for node, s in snap.nodes.items()}
        if snap.leader:  # an old leader that has not stepped down yet is not the leader
            roles = {node: ("FOLLOWER" if role == "LEADER" and node != snap.leader else role)
                     for node, role in roles.items()}
        if roles != logo_roles:
            logo_roles.clear()
            logo_roles.update(roles)
            logo.set_content(theme.logo_svg(roles))

    ui.timer(0.5, header_status)

    first = {"cluster": cluster_tab, "proof": proof_tab, "evidence": proof_tab}.get(tab, floor_tab)
    with ui.tab_panels(tabs, value=first, animated=False) \
            .classes("w-full max-w-[1440px] mx-auto bg-transparent"):
        with ui.tab_panel(floor_tab).classes("px-2"):
            Floor(session, tapri)
        with ui.tab_panel(cluster_tab).classes("px-2"):
            ClusterView()
        with ui.tab_panel(proof_tab).classes("px-2 gap-4"):
            ProofView()


if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from another machine")
    args = parser.parse_args()
    # storage_secret signs the per-browser storage that keeps a sign-in across
    # page reloads (a local demo, so a fixed value is fine).
    ui.run(title="Raftioneer", host=args.host, port=args.port, reload=False, show=False,
           favicon=theme.FAVICON, reconnect_timeout=10, storage_secret="raftioneer-demo-dashboard")

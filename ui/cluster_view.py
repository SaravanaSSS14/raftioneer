"""The Raft cluster, live: who leads, each node's log entry by entry, and the
buttons that break it (kill, freeze, restart) so the audience can watch it
recover. Everything shown comes from each node's GetClusterStatus."""
from __future__ import annotations

import asyncio
import time

from nicegui import ui

import ops
import theme
from backend import NODES, cluster, snapshots

# Where each node sits in the diagram (a triangle).
POSITIONS = {"app-server-1": (110, 175), "app-server-2": (300, 62), "app-server-3": (490, 175)}


def diagram_svg(roles: dict[str, str], terms: dict[str, int], leader: str | None) -> str:
    """Three nodes; heartbeat dots flow from the leader to every live follower."""
    parts = ['<svg viewBox="0 0 600 250" width="100%" style="max-height:260px">']
    nodes = list(POSITIONS)
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            (x1, y1), (x2, y2) = POSITIONS[a], POSITIONS[b]
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#e2e8f0" stroke-width="3"/>')
    if leader:
        lx, ly = POSITIONS[leader]
        for node in nodes:
            if node == leader or roles.get(node) not in ("FOLLOWER", "CANDIDATE"):
                continue
            fx, fy = POSITIONS[node]
            for delay in (0, 0.45):
                parts.append(
                    f'<circle r="6" fill="{theme.LEADER}" opacity="0.9">'
                    f'<animateMotion dur="0.9s" begin="{delay}s" repeatCount="indefinite" '
                    f'path="M{lx},{ly} L{fx},{fy}"/></circle>'
                )
    for node in nodes:
        x, y = POSITIONS[node]
        role = roles.get(node, "DOWN")
        colour = theme.role_colour(role)
        if role == "LEADER":
            parts.append(f'<circle cx="{x}" cy="{y}" r="44" fill="{colour}" opacity="0.25">'
                         '<animate attributeName="r" values="40;56;40" dur="1.5s" repeatCount="indefinite"/>'
                         '<animate attributeName="opacity" values="0.35;0;0.35" dur="1.5s" repeatCount="indefinite"/>'
                         '</circle>')
        dash = ' stroke-dasharray="6 4"' if role == "CANDIDATE" else ""
        parts.append(f'<circle cx="{x}" cy="{y}" r="38" fill="{colour}" stroke="white" stroke-width="4"{dash}/>')
        icon = {"LEADER": "♛", "DOWN": "✕", "FROZEN": "❄", "CANDIDATE": "?"}.get(role, "")
        parts.append(f'<text x="{x}" y="{y - 4}" text-anchor="middle" font-size="22" fill="white" '
                     f'font-weight="700">{icon or node[-1]}</text>')
        parts.append(f'<text x="{x}" y="{y + 18}" text-anchor="middle" font-size="11" fill="white" '
                     f'font-weight="600">{role.lower()}</text>')
        label = node + (f" · term {terms[node]}" if node in terms else "")
        parts.append(f'<text x="{x}" y="{y + 58}" text-anchor="middle" font-size="13" fill="#334155" '
                     f'font-weight="600">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


class NodeCard:
    def __init__(self, view: "ClusterView", node: str) -> None:
        self.view, self.node = view, node
        self.tail_seen: tuple = ()
        self.max_index_seen = 0
        with ui.card().classes("rt-card p-4 gap-2 grow basis-0 min-w-[280px]") as self.card:
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label(node).classes("font-bold grow")
                self.role = ui.badge("…").props("rounded")
            with ui.grid(columns=4).classes("w-full gap-1"):
                self.stats = {}
                for key, label in [("term", "term"), ("last_log_index", "log"),
                                   ("commit_index", "commit"), ("last_applied", "applied")]:
                    with ui.column().classes("gap-0"):
                        ui.label(label).classes("text-[11px] rt-muted")
                        self.stats[key] = ui.label("–").classes("font-semibold rt-mono")
            self.digest = ui.label("").classes("text-[11px] rt-mono rt-muted")
            ui.label("newest log entries →").classes("text-[11px] rt-muted mt-1")
            self.log = ui.row().classes("w-full gap-1 no-wrap overflow-hidden justify-end min-h-[44px]")
            with ui.row().classes("w-full gap-2 mt-1"):
                self.kill = ui.button("Kill", icon="power_settings_new",
                                      on_click=lambda: view.act(ops.kill, node, "killed (SIGKILL)")) \
                    .props("outline dense color=negative")
                self.freeze = ui.button("Freeze", icon="ac_unit",
                                        on_click=lambda: view.act(ops.freeze, node, "frozen (docker pause)")) \
                    .props("outline dense color=info")
                self.unfreeze = ui.button("Unfreeze", icon="wb_sunny",
                                          on_click=lambda: view.act(ops.unfreeze, node, "unfrozen")) \
                    .props("outline dense color=info")
                self.start = ui.button("Restart", icon="restart_alt",
                                       on_click=lambda: view.act(ops.start, node, "restarted")) \
                    .props("outline dense color=positive")

    def update(self, status, docker_state: str) -> None:
        paused = docker_state == "paused"
        role = "FROZEN" if paused else ("DOWN" if status is None else status.role)
        self.role.set_text(role.lower())
        self.role.style(f"background:{theme.role_colour(role)} !important")
        self.card.classes(add="rt-leader" if role == "LEADER" else "", remove="" if role == "LEADER" else "rt-leader")
        self.card.classes(add="rt-down" if role in ("DOWN", "FROZEN") else "",
                          remove="" if role in ("DOWN", "FROZEN") else "rt-down")
        stopped = docker_state in ("exited", "dead", "created")
        self.kill.set_visibility(not stopped and not paused)
        self.freeze.set_visibility(not stopped and not paused)
        self.unfreeze.set_visibility(paused)
        self.start.set_visibility(stopped)
        if status is None:
            return
        for key, label in self.stats.items():
            label.set_text(str(getattr(status, key)))
        self.digest.set_text(f"state digest {status.applied_digest[:12]}… at #{status.last_applied}")
        self._draw_log(status)

    def _draw_log(self, status) -> None:
        tail = tuple((e.index, e.term, e.op, e.index <= status.commit_index) for e in status.recent_entries)
        if tail == self.tail_seen:
            return
        self.tail_seen = tail
        self.log.clear()
        with self.log:
            for entry in status.recent_entries:
                colour = theme.TERM_COLOURS[entry.term % len(theme.TERM_COLOURS)]
                committed = entry.index <= status.commit_index
                classes = "rt-cell" + ("" if committed else " rt-pending") + \
                    (" rt-new" if entry.index > self.max_index_seen else "")
                style = f"background:{colour}" if committed else f"color:{colour}"
                with ui.element("div").classes(classes).style(style):
                    with ui.column().classes("gap-0 items-center leading-none"):
                        ui.label(theme.OP_SHORT.get(entry.op, entry.op[:5]))
                        ui.label(f"#{entry.index}").classes("text-[8px] opacity-80 font-normal")
                    ui.tooltip(f"#{entry.index} · term {entry.term} · {entry.label}"
                               + ("" if committed else " · not committed yet"))
        self.max_index_seen = max(self.max_index_seen, max((e.index for e in status.recent_entries), default=0))


class ClusterView:
    def __init__(self) -> None:
        self.version_seen = -1
        self.events_seen = -1
        self.docker: dict[str, str] = {}
        self.diagram_key = None
        self._build()
        ui.timer(0.5, self.tick)
        ui.timer(2.0, self.poll_docker)

    def _build(self) -> None:
        with ui.row().classes("w-full items-start gap-6 no-wrap max-lg:flex-wrap"):
            with ui.column().classes("grow gap-4 min-w-0"):
                with ui.card().classes("rt-card w-full p-5 gap-2"):
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.label("Raft cluster").classes("text-xl font-bold")
                        self.live = ui.element("div").classes("rt-live")
                        self.headline = ui.label().classes("rt-muted")
                        ui.space()
                        ui.button("Kill the leader", icon="bolt", on_click=self.kill_leader) \
                            .props("unelevated color=negative")
                        ui.button("Restart all", icon="restart_alt", on_click=self.restart_all).props("flat")
                    with ui.row().classes("w-full items-center gap-2"):
                        self.bidders = ui.switch("Simulated bidders (client node)", value=True,
                                                 on_change=self._toggle_bidders).props("dense color=primary")
                        self.bidders.tooltip("Node 5's personas (nisargh, nahush, karan, mrudul) bid on their own. "
                                             "Switch them off to demo bidding by hand.")
                    self.diagram = ui.html("", sanitize=False).classes("w-full")
                    self.banner = ui.label().classes("text-center w-full text-base font-semibold")
                with ui.row().classes("w-full gap-4 no-wrap max-xl:flex-wrap"):
                    self.cards = {node: NodeCard(self, node) for node in NODES}
                with ui.card().classes("rt-card w-full p-4 gap-1"):
                    with ui.row().classes("items-center gap-2"):
                        self.agree_icon = ui.icon("check_circle").classes("text-2xl")
                        self.agree = ui.label().classes("font-semibold")
                    ui.label("Each node keeps a running SHA-256 over every entry it has applied. Equal digests at "
                             "the same index mean the nodes fed their state machines exactly the same commands.") \
                        .classes("text-sm rt-muted")
            with ui.column().classes("w-[340px] shrink-0 gap-4 max-lg:w-full"):
                with ui.card().classes("rt-card w-full p-4 gap-2"):
                    ui.label("What just happened").classes("font-semibold")
                    self.events = ui.column().classes("w-full gap-1")
                with ui.card().classes("rt-card w-full p-4 gap-2"):
                    ui.label("Reading the log").classes("font-semibold")
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.element("div").classes("rt-cell").style(f"background:{theme.TERM_COLOURS[1]}") \
                            .props("aria-hidden=true")
                        ui.label("committed: stored on a majority, applied, permanent").classes("text-sm")
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.element("div").classes("rt-cell rt-pending").style(f"color:{theme.TERM_COLOURS[1]}")
                        ui.label("in the log, not yet committed").classes("text-sm")
                    ui.label("Colour = the term the entry was written in, so a new leader's entries look "
                             "different. BID, NEW (listing), REG, IN/OUT, DEP (deposit), CLOSE, RECAP, "
                             "PAID (escrow released), NO-OP (a new leader's first entry).") \
                        .classes("text-sm rt-muted")

    # ------------------------------------------------------------ updates

    async def poll_docker(self) -> None:
        states = await ops.container_states()
        if states:
            self.docker = states
            running = states.get("client") == "running"
            if running != self.bidders.value:
                self.bidders.set_value(running)  # mirror what another tab (or the terminal) did

    async def _toggle_bidders(self, event) -> None:
        # Only act when the switch disagrees with the container: a switch that
        # is merely being synced to the real state (above) must not act again.
        if event.value == (self.docker.get("client") == "running"):
            return
        code, out = await ops.set_simulated_bidders(event.value)
        await self.poll_docker()
        if code == 0:
            snapshots.event("info", f"simulated bidders {'started' if event.value else 'stopped'} from the UI")
            ui.notify(f"Simulated bidders {'on' if event.value else 'off'}", type="info")
        else:
            ui.notify(f"docker: {out.strip()[:200]}", type="negative")

    def _docker_state(self, node: str) -> str:
        return self.docker.get(ops.service_of(node), "running")

    def tick(self) -> None:
        snap = snapshots.cluster
        roles, terms = {}, {}
        for node, status in snap.nodes.items():
            if self._docker_state(node) == "paused":
                roles[node] = "FROZEN"
            else:
                roles[node] = "DOWN" if status is None else status.role
            if status is not None:
                terms[node] = status.term
            self.cards[node].update(status, self._docker_state(node))
        key = (tuple(sorted(roles.items())), snap.leader, tuple(sorted(terms.items())))
        if key != self.diagram_key:
            self.diagram_key = key
            self.diagram.set_content(diagram_svg(roles, terms, snap.leader))
        if snap.leader:
            self.headline.set_text(f"leader {snap.leader} · term {snap.term}")
            self.live.classes(remove="rt-bad")
        else:
            self.headline.set_text("no leader - election in progress")
            self.live.classes(add="rt-bad")
        self._agreement(snap)
        if snapshots.event_count != self.events_seen:
            self.events_seen = snapshots.event_count
            self._render_events()

    def _agreement(self, snap) -> None:
        live = [s for s in snap.nodes.values() if s is not None]
        if not live:
            self.agree.set_text("No node is answering")
            self._agree_icon("error", "negative")
            return
        top = max(s.last_applied for s in live)
        level = [s for s in live if s.last_applied == top]
        if len(level) == len(live) and len({s.applied_digest for s in level}) == 1:
            self.agree.set_text(f"Identical state on all {len(live)} running nodes - applied #{top}, "
                                f"digest {level[0].applied_digest[:12]}…")
            self._agree_icon("check_circle", "positive")
        elif len({s.applied_digest for s in level}) > 1:
            self.agree.set_text(f"Digests differ at #{top} - replicas have diverged!")
            self._agree_icon("error", "negative")
        else:
            behind = ", ".join(f"{s.node_id} at #{s.last_applied}" for s in live if s.last_applied < top)
            self.agree.set_text(f"Catching up: {behind} (others at #{top})")
            self._agree_icon("sync", "warning")

    def _agree_icon(self, icon: str, colour: str) -> None:
        self.agree_icon.set_name(icon)
        self.agree_icon.props(f"color={colour}")

    def _render_events(self) -> None:
        self.events.clear()
        colours = {"leader": "text-amber-600", "down": "text-red-600", "up": "text-green-600"}
        icons = {"leader": "military_tech", "down": "power_off", "up": "power", "info": "info"}
        with self.events:
            for event in list(snapshots.events)[:14]:
                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                    ui.icon(icons.get(event.kind, "info")).classes(f"{colours.get(event.kind, 'text-slate-500')} mt-[2px]")
                    ui.label(theme.clock(event.at)).classes("text-xs rt-mono rt-muted mt-[3px] w-14 shrink-0")
                    ui.label(event.text).classes("text-sm")

    # ------------------------------------------------------------ actions

    async def act(self, action, node: str, what: str) -> None:
        code, out = await action(node)
        if code == 0:
            snapshots.event("info", f"{node} {what} from the UI")
            ui.notify(f"{node} {what}", type="info")
        else:
            ui.notify(f"docker: {out.strip()[:200]}", type="negative")
        await self.poll_docker()

    async def kill_leader(self) -> None:
        snap = snapshots.cluster
        if not snap.leader:
            ui.notify("There is no leader to kill right now", type="warning")
            return
        victim, term = snap.leader, snap.term
        commit = snap.nodes[victim].commit_index if snap.nodes.get(victim) else 0
        self.banner.set_text(f"Killing {victim} with SIGKILL…")
        self.banner.classes(replace="text-center w-full text-base font-semibold text-negative")
        started = time.time()
        code, out = await ops.kill(victim)
        if code != 0:
            ui.notify(f"docker: {out.strip()[:200]}", type="negative")
            return
        snapshots.event("down", f"{victim} killed from the UI (it led term {term})")
        survivors = [a for n, a in NODES.items() if n != victim]
        while time.time() - started < 30:
            found = await asyncio.gather(*(cluster.status(a, timeout=0.3) for a in survivors))
            leaders = [s for s in found if s is not None and s.role == "LEADER" and s.term > term]
            if leaders:
                new = leaders[0]
                elapsed = time.time() - started
                self.banner.set_text(
                    f"✔ {new.node_id} took over as leader of term {new.term}, {elapsed:.2f} s after the kill - "
                    f"commit index {new.commit_index} (was {commit}): nothing committed was lost.")
                self.banner.classes(replace="text-center w-full text-base font-semibold text-positive rt-pop")
                return
            await asyncio.sleep(0.05)
        self.banner.set_text("No new leader after 30 s - is a majority still running?")

    async def restart_all(self) -> None:
        for node in NODES:
            state = self._docker_state(node)
            if state == "paused":
                await ops.unfreeze(node)
            elif state != "running":
                await ops.start(node)
        snapshots.event("info", "every app server started / unfrozen from the UI")
        self.banner.set_text("")
        await self.poll_docker()

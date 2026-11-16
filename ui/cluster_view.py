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

# Where each node sits in the diagram: the three app servers form a triangle
# (the same one as the logo), the client node is on the left, the LLM node on
# the right.
POSITIONS = {"app-server-1": (300, 80), "app-server-2": (300, 226), "app-server-3": (452, 153)}
CLIENT_AT, LLM_AT = (84, 153), (676, 153)
BOX_W, BOX_H = 112, 64


def _box(x: int, y: int, colour: str, title: str, subtitle: str) -> str:
    return (f'<rect x="{x - BOX_W // 2}" y="{y - BOX_H // 2}" width="{BOX_W}" height="{BOX_H}" rx="10" '
            f'fill="{colour}"/>'
            f'<text x="{x}" y="{y - 3}" text-anchor="middle" font-size="14" fill="white" font-weight="700">{title}</text>'
            f'<text x="{x}" y="{y + 15}" text-anchor="middle" font-size="10.5" fill="white" opacity=".9">{subtitle}</text>')


def _caption(x: int, y: int, text: str) -> str:
    return f'<text x="{x}" y="{y}" text-anchor="middle" font-size="11" fill="{theme.MUTED}">{text}</text>'


def diagram_svg(roles: dict[str, str], terms: dict[str, int], leader: str | None,
                client_state: str = "running", llm_state: str = "running") -> str:
    """All five nodes. Amber dots are the leader's heartbeats to each live
    follower; dark dots are the client node's bids, which go to whoever leads
    (so they change course when the leader does). The LLM node sits outside
    the cluster: asked over gRPC, never part of deciding a bid."""
    parts = ['<svg viewBox="0 0 760 300" width="100%" style="max-height:310px">']
    # the Raft cluster's outline
    parts.append(f'<rect x="232" y="30" width="290" height="262" rx="18" fill="none" stroke="{theme.LINE}" '
                 'stroke-width="2" stroke-dasharray="7 5"/>')
    parts.append(_caption(377, 20, "Raft cluster - three app servers, one replicated log"))
    nodes = list(POSITIONS)
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            (x1, y1), (x2, y2) = POSITIONS[a], POSITIONS[b]
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{theme.LINE}" stroke-width="3"/>')

    # client node -> leader: bids
    cx, cy = CLIENT_AT
    bidding = client_state == "running" and leader is not None
    if bidding:
        lx, ly = POSITIONS[leader]
        parts.append(f'<line x1="{cx + BOX_W // 2}" y1="{cy}" x2="{lx}" y2="{ly}" stroke="{theme.INK}" '
                     'stroke-width="1.5" opacity=".35"/>')
        for delay in (0, 0.8):
            parts.append(f'<circle r="4.5" fill="{theme.INK}"><animateMotion dur="1.6s" begin="{delay}s" '
                         f'repeatCount="indefinite" path="M{cx + BOX_W // 2},{cy} L{lx},{ly}"/></circle>')
    # cluster -> LLM node
    gx, gy = LLM_AT
    llm_up = llm_state == "running"
    parts.append(f'<line x1="522" y1="{gy}" x2="{gx - BOX_W // 2}" y2="{gy}" stroke="{theme.CHAI if llm_up else theme.DOWN}" '
                 'stroke-width="2" stroke-dasharray="5 4"/>')

    # leader -> followers: heartbeats
    if leader:
        lx, ly = POSITIONS[leader]
        for node in nodes:
            if node == leader or roles.get(node) not in ("FOLLOWER", "CANDIDATE"):
                continue
            fx, fy = POSITIONS[node]
            for delay in (0, 0.45):
                parts.append(
                    f'<circle r="5" fill="{theme.LEADER}" opacity="0.95">'
                    f'<animateMotion dur="0.9s" begin="{delay}s" repeatCount="indefinite" '
                    f'path="M{lx},{ly} L{fx},{fy}"/></circle>'
                )
    for node in nodes:
        x, y = POSITIONS[node]
        role = roles.get(node, "DOWN")
        colour = theme.role_colour(role)
        dash = ' stroke-dasharray="6 4"' if role == "CANDIDATE" else ""
        parts.append(f'<circle cx="{x}" cy="{y}" r="33" fill="{colour}" stroke="{theme.CARD}" stroke-width="4"{dash}/>')
        icon = {"LEADER": "♛", "DOWN": "✕", "FROZEN": "❄", "CANDIDATE": "?"}.get(role, "")
        parts.append(f'<text x="{x}" y="{y - 3}" text-anchor="middle" font-size="20" fill="white" '
                     f'font-weight="700">{icon or node[-1]}</text>')
        parts.append(f'<text x="{x}" y="{y + 15}" text-anchor="middle" font-size="10.5" fill="white" '
                     f'font-weight="600">{role.lower()}</text>')
        label = node + (f" · term {terms[node]}" if node in terms else "")
        parts.append(f'<text x="{x}" y="{y + 51}" text-anchor="middle" font-size="12" fill="{theme.INK}" '
                     f'font-weight="600">{label}</text>')

    client_colour = {"running": theme.INK, "paused": theme.FROZEN}.get(client_state, theme.DOWN)
    parts.append(_box(cx, cy, client_colour, "bidders", "client node"))
    parts.append(_caption(cx, cy + 50, "nisargh · nahush · karan · mrudul"))
    parts.append(_caption(cx, cy + 65, "bids go to the leader" if bidding
                          else "switched off" if client_state != "running" else "waiting for a leader"))
    parts.append(_box(gx, gy, theme.CHAI if llm_up else theme.DOWN, "Tapri", "LLM node"))
    parts.append(_caption(gx, gy + 50, "asked over gRPC for answers,"))
    parts.append(_caption(gx, gy + 65, "descriptions and recaps" if llm_up else "DOWN - templates are used instead"))
    parts.append(_caption((522 + gx - BOX_W // 2) // 2, gy - 8, "gRPC"))
    parts.append("</svg>")
    return "".join(parts)


class ServiceSwitch:
    """An on/off switch for a container that is not a Raft member (the client
    node, the LLM node). It mirrors the container's real state, and ignores
    the state poll while its own change is still being carried out."""

    def __init__(self, view: "ClusterView", service: str, label: str, tip: str, what: str) -> None:
        self.view, self.service, self.what = view, service, what
        self.switching = False
        self.switch = ui.switch(label, value=True, on_change=self._toggled).props("dense color=primary")
        self.switch.tooltip(tip)

    def sync(self, states: dict[str, str]) -> None:
        if self.switching:
            return  # mid-change: syncing now would flip the switch back for a moment
        running = states.get(self.service) == "running"
        if running != self.switch.value:
            self.switch.set_value(running)  # mirror what another tab (or the terminal) did

    async def _toggled(self, event) -> None:
        # Only act when the switch disagrees with the container: a switch that
        # is merely being synced to the real state must not act again.
        state = self.view.docker.get(self.service, "")
        if self.switching or event.value == (state == "running"):
            return
        self.switching = True
        self.switch.disable()
        try:
            code, out = await ops.set_service(self.service, event.value, state)
        finally:
            self.switching = False
            self.switch.enable()
        await self.view.poll_docker()
        if code == 0:
            snapshots.event("info", f"{self.what} {'started' if event.value else 'stopped'} from the UI")
            ui.notify(f"{self.what} {'on' if event.value else 'off'}", type="info")
        else:
            ui.notify(f"docker: {out.strip()[:200]}", type="negative")


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
                        ui.label("The cluster").classes("rt-title")
                        self.live = ui.element("div").classes("rt-live")
                        ui.space()
                        ui.button("Kill the leader", icon="bolt", on_click=self.kill_leader) \
                            .props("unelevated color=negative")
                        ui.button("Restart all", icon="restart_alt", on_click=self.restart_all).props("flat")
                    # The cluster's state at a glance, large enough to read from the back.
                    with ui.row().classes("w-full gap-0 no-wrap my-1").style(
                            f"border:1px solid {theme.LINE}; border-radius:8px"):
                        self.stats = {}
                        for key, label in [("leader", "leader"), ("term", "term"), ("up", "nodes up"),
                                           ("commit", "commit index"), ("election", "last election")]:
                            with ui.column().classes("gap-0 px-4 py-2 grow basis-0").style(
                                    "" if key == "leader" else f"border-left:1px solid {theme.LINE}"):
                                ui.label(label).classes("rt-label")
                                self.stats[key] = ui.label("–").classes("rt-mono font-bold text-[22px] leading-tight")
                    with ui.row().classes("w-full items-center gap-6"):
                        self.switches = [
                            ServiceSwitch(self, "client", "Simulated bidders",
                                          "Node 5, the client node: four personas bidding on their own.",
                                          "simulated bidders"),
                            ServiceSwitch(self, "llm_server", "LLM node (Tapri)",
                                          "Node 1, the LLM service behind Tapri. While it is off, answers, "
                                          "descriptions and recaps come from templates.", "LLM node"),
                        ]
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
                    ui.label("What just happened").classes("rt-label")
                    self.events = ui.column().classes("w-full gap-1")
                self._legend()

    @staticmethod
    def _legend() -> None:
        """The legend: what every colour and label on this tab means."""
        def swatch(colour: str, text: str, dashed: bool = False) -> None:
            with ui.row().classes("items-center gap-2 no-wrap"):
                border = f"border:2px dashed {colour}; background:transparent" if dashed else f"background:{colour}"
                ui.element("div").style(f"width:14px;height:14px;border-radius:50%;flex:none;{border}")
                ui.label(text).classes("text-sm")

        with ui.card().classes("rt-card w-full p-4 gap-2"):
            ui.label("Legend").classes("rt-label")

            ui.label("Nodes (circles and badges)").classes("text-xs font-semibold mt-1")
            swatch(theme.LEADER, "leader ♛ - the only node that accepts writes; its card has an amber border")
            swatch(theme.FOLLOWER, "follower - copies the leader's log, serves reads")
            swatch(theme.CANDIDATE, "candidate ? - its election timer ran out; asking for votes", dashed=True)
            swatch(theme.FROZEN, "frozen ❄ - docker pause: alive but gets no CPU (a zombie)")
            swatch(theme.DOWN, "down ✕ - container killed or not answering")
            ui.label("Amber dots moving from the leader to a follower are its heartbeats "
                     "(AppendEntries, every 0.2 s). A follower that stops hearing them starts an election. "
                     "Dark dots are the bidders' writes: they go to whoever leads, so they change course after "
                     "an election. The LLM node is outside the cluster - it is asked, it never decides.") \
                .classes("text-sm rt-muted")

            ui.label("Node card numbers").classes("text-xs font-semibold mt-2")
            ui.label("term - the election period the node believes it is in (goes up with each election). "
                     "log - index of its newest entry. commit - newest entry known to be on a majority. "
                     "applied - newest entry run on its auction database. log > commit means entries are "
                     "waiting for a majority; commit > applied means it is catching up.") \
                .classes("text-sm")

            ui.label("Log entries (squares, newest on the right)").classes("text-xs font-semibold mt-2")
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.element("div").classes("rt-cell").style(f"background:{theme.TERM_COLOURS[1]}") \
                    .props("aria-hidden=true")
                ui.label("filled - committed: on a majority of nodes, applied, can never be undone").classes("text-sm")
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.element("div").classes("rt-cell rt-pending").style(f"color:{theme.TERM_COLOURS[1]}")
                ui.label("dashed - written to this node's log, not committed yet (a deposed leader's "
                         "uncommitted entries can be overwritten)").classes("text-sm")
            ui.label("The colour is the term the entry was created in, so a new leader's entries switch colour. "
                     f"There are {len(theme.TERM_COLOURS)} colours, repeating every "
                     f"{len(theme.TERM_COLOURS)} terms:").classes("text-sm")
            with ui.row().classes("gap-1 items-center"):
                for term, colour in enumerate(theme.TERM_COLOURS):
                    ui.label(str(term)).classes("text-[10px] text-white font-bold text-center rounded") \
                        .style(f"background:{colour};width:22px;line-height:18px")
                ui.label("= term mod 8").classes("text-xs rt-muted")
            ui.label("Small #number = the entry's log index; the tooltip has the term and the command.") \
                .classes("text-sm rt-muted")

            ui.label("Entry labels (the command inside)").classes("text-xs font-semibold mt-2")
            with ui.grid().classes("w-full gap-x-3 gap-y-0 text-sm").style("grid-template-columns: auto 1fr"):
                for short, meaning in [
                    ("BID", "a bid (accepted or refused - both are recorded)"),
                    ("NEW", "an auction listed"), ("REG", "an account registered"),
                    ("IN / OUT", "log in / log out"), ("DEP", "a mock wallet deposit"),
                    ("CLOSE", "the leader's clock closing expired auctions; escrow held"),
                    ("RECAP", "the LLM's post-auction summary"), ("PAID", "buyer confirmed receipt; escrow released"),
                    ("NO-OP", "a new leader's first entry, which commits everything before it"),
                ]:
                    ui.label(short).classes("rt-mono font-semibold")
                    ui.label(meaning).classes("rt-muted")

    # ------------------------------------------------------------ updates

    async def poll_docker(self) -> None:
        states = await ops.container_states()
        if states:
            self.docker = states
            for switch in self.switches:
                switch.sync(states)

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
        client, llm = self.docker.get("client", "running"), self.docker.get("llm_server", "running")
        key = (tuple(sorted(roles.items())), snap.leader, tuple(sorted(terms.items())), client, llm)
        if key != self.diagram_key:
            self.diagram_key = key
            self.diagram.set_content(diagram_svg(roles, terms, snap.leader, client, llm))
        if snap.leader:
            self.live.classes(remove="rt-bad")
        else:
            self.live.classes(add="rt-bad")
        live_nodes = [s for s in snap.nodes.values() if s is not None]
        self.stats["leader"].set_text(snap.leader or "none - electing")
        self.stats["leader"].style(f"color:{theme.LEADER if snap.leader else '#b91c1c'}")
        self.stats["term"].set_text(str(snap.term))
        self.stats["up"].set_text(f"{len(live_nodes)} / {len(snap.nodes) or 3}")
        self.stats["commit"].set_text(str(max((s.commit_index for s in live_nodes), default=0)))
        took = snapshots.last_election_seconds
        self.stats["election"].set_text(f"{took:.2f} s" if took is not None else "–")
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
                # One number on the screen: the strip's "last election" shows this
                # kill-to-new-leader time too, not the poller's own (shorter) one,
                # which starts only when it notices the leader is gone. Set it once
                # the poller has seen the new leader, so it cannot overwrite it.
                for _ in range(40):
                    if snapshots.cluster.leader == new.node_id:
                        break
                    await asyncio.sleep(0.05)
                snapshots.last_election_seconds = elapsed
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

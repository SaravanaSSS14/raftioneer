# Raftioneer — Distributed Online Auction System

## Project Context

Course: Distributed Systems with gRPC & Raft (CS G623, BITS Pilani). Assignment Option F.
Team: 2 members (instructor-approved exception; default team size is 3).
- **Member A (this codebase owner)**: Raft consensus, Docker/infra, failure-injection demo, presentation materials.
- **Member B**: Business logic layer (bid handling, timer/winner logic, escrow stub), LLM server integration.

Milestone 1 (gRPC, LLM integration, core logic): internal target Sept 28, 2026 — hard deadline Sept 30, 2026.
Milestone 2 (Raft consensus, fault tolerance): internal target Nov 18, 2026 — hard deadline Nov 20, 2026.

## Hard Constraints

- **Language: Python, exclusively, for every component.** This is a mandatory assignment requirement, not a preference. Never suggest or introduce Go, Java, Node.js, or any other language for backend/Raft/business logic. Reference implementations in other languages (e.g. `hashicorp/raft`, MIT 6.5840 labs) may be consulted conceptually only — never ported or copied.
- **All inter-service communication must use gRPC.** No REST, no raw sockets, no message queues as a substitute.
- Escrow/payment is explicitly permitted to be a mock/stub — do not over-engineer this.
- The LLM server must run as an independent, separate node — the application servers call it via gRPC (`getLLMAnswer`), it does not hold authoritative auction state.

## Architecture

Five nodes, orchestrated via a single `docker-compose.yml`:
1. LLM server — domain-specific, CPU-optimized pre-trained model, independent service
2. App server — Raft leader (initial)
3. App server — Raft follower
4. App server — Raft follower
5. Client node(s) — includes a bidder-simulation script for concurrency testing

Writes (bids) are accepted only by the current Raft leader. Followers redirect or reject direct write attempts rather than accepting them. Raft-replicated state includes: bid history, current highest bid, and auction timer state (the anti-sniping extension, if implemented, must also be Raft-replicated, not held in local process memory).

## Tech Stack

- gRPC + Protocol Buffers (proto3) — `grpcio==1.83.1`, `grpcio-tools==1.83.1`, `protobuf==7.36.1`
- Concurrency: `asyncio` / `grpc.aio` — Raft handles concurrent RPCs, timers, and state transitions simultaneously; do not introduce threading unless there's a specific reason `asyncio` can't handle it
- State persistence: disk-persisted write-ahead log (SQLite via `sqlite3`, or flat append-only log files) — the Raft log must survive process restarts, not just in-memory crashes
- Testing: `pytest==9.1.1`, run against the full Compose stack for integration tests
- Containerization: Docker Compose v5.5.0
- Python 3.12.3

## Repository Layout

```
proto/          # .proto interface definitions — the contract between Member A and Member B's work
app_server/     # Raft + business logic
llm_server/     # LLM integration, isolated service
client/         # CLI/web client + bidder-simulation script
deploy/         # docker-compose.yml and related config
```

## Conventions

- Branch naming by feature, not by person: `raft/leader-election`, `app/bid-timer`, `llm/faq-prompt`
- `main` must remain runnable via `docker compose up` at all times — no committing broken states to `main`
- `.proto` files are a frozen interface once agreed with Member B — do not change method signatures without flagging it explicitly; this is a cross-team coordination point, not a unilateral edit
- Raft implementation: follow the reference paper's Figure 2 (Ongaro & Ousterhout, "In Search of an Understandable Consensus Algorithm") as the specification — implement leader election, log replication, and failure detection as described there, not an ad-hoc simplification
- Every Raft log line should include node ID, current term, and current state (leader/follower/candidate) — this is essential for debugging election/replication issues and should be present from the first implementation, not added later
- Never assume a single successful test run proves correctness for election or replication logic — restart the cluster repeatedly, in varied startup orders, before considering it working

## What This File Does NOT Cover

Deep conceptual explanations (why Raft requires majority quorum, why terms prevent split-brain, why followers reject stale-term requests) are intentionally kept out of this file — that reasoning lives in a separate planning conversation. If you (Claude Code) are asked to implement something non-trivial here, prefer asking the user for their understanding of the "why" over silently generating a correct-looking implementation — this project is being used for learning, not just for a working deliverable.

## Progress Log

Update this section whenever a milestone checkpoint, weekly integration checkpoint, or other significant state change is reached (per the Week 1 day-by-day / weekly schedule in the top-level `Raftioneer_Project_Reference.md`). Keep entries short — one line per checkpoint, most recent last. This is a running status, not a changelog of every commit.

- 2026-09-05 (Day 5, Week 1): Repo had only an initial commit (`.gitignore` + empty `README.md`) — no `.proto`, no directory structure, no compose file. Behind schedule against Day 1–4 plan. Resuming work: repo scaffold + draft `.proto` contracts.
- 2026-09-05 (Day 5, Week 1): Drafted `proto/auction.proto`, `proto/raft.proto`, `proto/llm.proto` — compiles cleanly, stubs verified importable. Scaffolded `app_server`, `llm_server`, `client` as real `grpc.aio` services with stub logic (not empty placeholders), and `deploy/docker-compose.yml` wiring all 5 nodes. `docker compose config` validates. **Not yet verified:** actual `docker compose up --build` (blocked locally — this machine's user isn't in the `docker` group; see README troubleshooting note). `.proto` is a first draft, not yet reviewed/signed off by Member B.
- 2026-09-05 (Day 5, Week 1): `docker compose up --build` confirmed working live — all 5 containers start, `client` logs `reachable` against all 3 app servers over real `AuctionService.ListAuctions` calls.
- 2026-09-05 (Day 6/7, Week 1): Pushed repo to GitHub (`SaravanaSSS14/raftioneer`, private). Published app-server/LLM ports to the host (`50051-53`, `50060`) and added `scripts/smoke_test.py` as an automated pass/fail check, replacing manual log-watching. **Day 7 clean-checkout criterion verified**: fresh `git clone` into a scratch directory, `docker compose up --build` from that clone alone (no local venv/uncommitted state involved) — all 5 services started and `client` reached all 3 app servers repeatedly. Member A's Day 1–7 track is complete. **Still pending (needs Member B, who is offline as of this entry):** `.proto` review/sign-off, his data model + LLM model selection (his Day 3–4 tasks, not started), and the joint Day 7 retrospective / Week 2 assignment confirmation.
- 2026-09-18 (Week 3 window, running ~2 weeks behind the internal calendar): No commits landed between Sep 5 and Sep 18 from either member. Member B has been working locally (business logic + LLM server, per his ownership) and says he'll push Sep 19. Member A's M1 checkpoint is self-imposed for Sep 20. Reconfirmed scope: Member B owns **both** business logic (`app_server`'s bid/auction/session logic) and the LLM server — Member A does not touch either.
- 2026-09-18: Upgraded `client/main.py` from a pure connectivity ping into a real bidder flow (`Login` -> `ListAuctions` -> `PlaceBid`), written against the frozen `.proto` contract rather than today's stub responses, so it exercises more of the flow automatically once Member B's real logic lands — no changes needed here when it does. Expanded `scripts/smoke_test.py` to check contract-level invariants (failed login never returns a token; a made-up auction id is never "found"; an unauthenticated bid is never accepted) instead of just reachability — deliberately avoids assuming any specific seed data, since that's Member B's implementation detail. Both verified against locally-run stub instances (no Docker group access in the assistant's shell this session); not yet re-verified against `docker compose up` on this machine.
- 2026-09-18: Added a real `pytest` suite (`tests/`) formalizing the same contract invariants, parametrized across all 3 app-server replicas — 16/16 pass against locally-run stubs. Added `requirements-dev.txt` (pytest was previously only installed ad-hoc, undeclared anywhere). This is the harness that should give fast, objective pass/fail the moment Member B's push lands, without needing to know his internals first.
- 2026-09-18: Member B pushed his business-logic work to a `member-b` branch (not `main`). Code review found 8 bugs (full detail in `Raftioneer_Bug_Tracker.pdf` / `Raftioneer_Bug_Solutions_Guide.pdf`); all fixed and verified on `member-b`, including adding a `CreateAuction` RPC to `auction.proto` post-freeze (no RPC previously existed to create an item/auction at all — project-lead decision, flagged plainly rather than done silently). Also added real password verification (auto-register on first login, verified against a stored hash after) and an automatic auction-timer/winner-determination background task — neither existed before. **`member-b` merged into `main`** (`0ade4d1`) after dry-run confirmed zero conflicts and a fresh end-to-end run confirmed the merged code works: login, password rejection, create auction, bid, automatic timer close with correct winner, all verified with no errors. **Still on `main` after this merge:** LLM integration is still stub-only (separate, hasn't landed from Member B). Documented but not fixed: unsalted password hashing, foreign-key constraints declared but not actually enforced (SQLite's `PRAGMA foreign_keys` is per-connection, not persistent — `get_connection()` never sets it), several broad `except Exception` blocks, one dead-code function (`auth.create_auth_credentials`) — see the Bug Solutions Guide's quality notes.

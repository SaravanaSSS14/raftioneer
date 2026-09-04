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

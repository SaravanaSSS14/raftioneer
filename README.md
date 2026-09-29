# Raftioneer — Distributed Online Auction System

A distributed online auction built with **gRPC in Python**: three auction-server
replicas, an independent **LLM node**, and a simulated bidder client, all started
from a single `docker compose up`.

The demo theme is a *campus clearance auction* — students listing what they leave
behind at the end of the semester (a cycle, a mini-fridge, a guitar) and bidding on
each other's — with an LLM assistant that answers bidder questions, writes listings,
and recaps every sale.

> **Status — Milestone 1 complete:** gRPC services, session authentication,
> concurrency-safe bidding, automatic auction close and winner determination,
> mock escrow settlement, and LLM integration with graceful degradation.
> **Milestone 2** adds Raft consensus and fault tolerance across the replicas.

*Advanced Operating Systems (CS G623) · BITS Pilani, Pilani Campus · First Semester 2026–27*

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Demo walkthrough](#demo-walkthrough)
- [API overview](#api-overview)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Roadmap: Milestone 2](#roadmap-milestone-2)
- [Team](#team)

---

## Features

**Auctions and bidding**
- Session-based login with salted PBKDF2 password hashing; accounts register on first login.
- Create auctions with a duration and an optional reserve price; browse, filter by status, or fetch one.
- Bid validation: a bid must beat the current highest bid and meet any reserve, and sellers cannot
  bid on their own auctions.
- Concurrency-safe: the highest bid only moves through a compare-and-set update, so simultaneous
  bids can never lose or overwrite a higher one (verified with 400 simultaneous bids per replica).
- A background timer closes expired auctions, determines the winner, and records a **mock escrow
  settlement** with a 5% buyer's premium (`HELD (mock)`, or `NO_SALE` when nobody bid).

**LLM integration** (an independent node — the app servers call it over gRPC)
- **Tapri**, the bidder assistant (named after the campus chai stall): answers questions with the
  auction's live details injected as context.
- Writes a listing when a seller leaves the description blank.
- Writes a recap of every closed auction, including how the price compares to the average of
  earlier sales.
- **Never on the bidding path:** if the LLM node is slow, loading, or down, every feature falls back
  to plain text and bidding is unaffected.

**Tooling**
- Themed seed data, a guided tour, an auction board, a concurrency stress test, smoke tests, and a
  59-test pytest suite.

## Architecture

```
                 ┌──────────────┐        ┌──────────────┐       ┌──────────────┐
   client  ────▶ │ app_server_1 │ ─────▶ │  llm_server  │ ────▶ │    ollama    │
 (4 bidder       │ app_server_2 │  gRPC  │ (gRPC face,  │ HTTP  │ qwen2.5:1.5b │
  personas)      │ app_server_3 │        │  stateless)  │       │    (CPU)     │
     gRPC        └──────────────┘        └──────────────┘       └──────────────┘
                  auth · auctions ·        LLM node (Node 1)
                  bids · timer · escrow
                  each with its own SQLite database
```

- **Five nodes:** the LLM node, three app-server replicas, and the client. The LLM node is
  `llm_server` (the gRPC service) plus its `ollama` model backend.
- **All inter-service traffic is gRPC** ([`proto/`](proto/)). The only HTTP hop is
  `llm_server → ollama`, inside the LLM node.
- **The LLM node holds no auction state.** App servers inject the facts it needs on every call.
- **Each replica keeps its own SQLite database** in Milestone 1; replicating state between them
  with Raft is Milestone 2.

**Lifecycle of a bid:** `Login` → `ListAuctions` → `PlaceBid` (validated, compare-and-set) → the
timer closes the auction at its end time → settlement (winner, premium, mock escrow) → the recap is
written in the background → `GetAuctionSummary` returns all of it.

## Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 — every service |
| RPC | gRPC + Protocol Buffers (`grpcio 1.83.1`, `protobuf 7.36.1`) |
| Concurrency | `asyncio` / `grpc.aio` |
| Storage | SQLite, one database file per app-server replica |
| LLM | Ollama serving `qwen2.5:1.5b` on CPU — no GPU required |
| Deployment | Docker Compose |
| Testing | `pytest 9.1.1` |

## Repository layout

```
proto/          gRPC contracts: auction.proto (client-facing), llm.proto (LLM node),
                raft.proto (replica-to-replica, for Milestone 2)
app_server/     auction node: RPC handlers, auction timer, settlement, assistant context
  store/        SQLite data access and password hashing
llm_server/     LLM node: gRPC service, Ollama client, prompts
client/         bidder simulation (four named personas)
deploy/         docker-compose.yml - the whole system
scripts/        seeding, guided tour, auction board, stress test, smoke tests, stub generation
tests/          pytest: contract tests (live stack) + unit tests
```

## Getting started

### Prerequisites

- Docker with **Docker Compose v2** (`docker compose version`)
- About 10 GB of free disk and an internet connection for the first run (the Ollama image and the
  model); later runs are fully local
- Python 3.12 for the helper scripts and tests (not needed just to run the system)
- Ports `50051`–`50053` and `50060` free on the host

### 1. Start the system

```bash
docker compose -f deploy/docker-compose.yml up --build -d
```

The first run pulls the Ollama image and downloads the ~1 GB `qwen2.5:1.5b` model into a named
Docker volume; later runs start in seconds. The app servers and client are usable immediately, and
the LLM node warms the model up as soon as it starts (`docker compose -f deploy/docker-compose.yml
logs llm_server` shows `loaded and warm`).

| Service | Address on the host |
| --- | --- |
| App servers | `localhost:50051`, `localhost:50052`, `localhost:50053` |
| LLM server | `localhost:50060` |

### 2. Set up the helper scripts

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
./scripts/generate_protos.sh          # gRPC stubs into generated/ (not committed)
```

### 3. Check that everything is up

```bash
python scripts/smoke_test.py          # every RPC on every node: OK / FAIL
python scripts/llm_smoke_test.py      # the three LLM RPCs against the real model
```

## Demo walkthrough

**Seed the demo data** — six themed auctions on every replica (listed by Tanmay and Parm):

```bash
python scripts/seed_demo_data.py
```

**Watch the bidders** — four personas (Nisargh, Nahush, Karan and Mrudul), each with their own
bidding style, bid on random open auctions on every replica every 5 seconds:

```bash
docker compose -f deploy/docker-compose.yml logs -f client
```

**Run the guided tour** — the whole system end to end in about 20 seconds:

```bash
python scripts/demo_queries.py        # add --server localhost:50052 to use another replica
```

It browses the open auctions, asks the assistant about one, lists an item with no description (the
LLM writes it), runs a short bidding war with one bid correctly rejected, and waits for the auction
to close:

```
=== 5. Two students bid (8-second auction) ===
  nisargh bids 120.00: accepted
  nahush bids 150.00: accepted
  nisargh bids 155.00: accepted
  nahush bids 100.00: rejected (Bid amount must be greater than current highest bid (155.0))

=== 6. Auction closes: winner, mock escrow, recap ===
  winner:        user_nisargh
  final price:   155.00  (+ buyer's premium 7.75)
  escrow:        HELD (mock)
  recap (LLM): This Raftioneer auction concluded with a final price of 155.00, which was 184% above
  the average sale price of 54.61. ...
```

**See the whole board** — every open auction on every replica, soonest-closing first:

```bash
python scripts/board.py
```

**Take the LLM node down** — every feature keeps working on plain-text fallbacks:

```bash
docker compose -f deploy/docker-compose.yml stop llm_server
python scripts/demo_queries.py        # answers, the listing and the recap now come from templates
docker compose -f deploy/docker-compose.yml start llm_server
```

**Prove concurrency safety** — hundreds of simultaneous bids on one auction; the final highest bid
must equal the highest bid the server accepted:

```bash
python scripts/stress_bids.py                                    # 20 bidders, 400 bids
python scripts/stress_bids.py --server localhost:50052 --bidders 50 --bids 1000
```

**Shut down** — `docker compose -f deploy/docker-compose.yml down` (keeps the downloaded model;
add `-v` to delete it too).

## API overview

`AuctionService` ([`proto/auction.proto`](proto/auction.proto)) — served by every app server:

| RPC | Purpose |
| --- | --- |
| `Login` / `Logout` | Session management; first login registers the account |
| `ListAuctions` / `GetAuction` | Browse (optionally by status) or fetch one auction |
| `CreateAuction` | List an item; a blank description is written by the LLM |
| `PlaceBid` | Must beat the highest bid, meet any reserve, and not be the seller's own auction |
| `AskAssistant` | Bidder FAQ, with the auction's details as context; always answers |
| `GetAuctionSummary` | Outcome of a closed auction: winner, premium, escrow status, recap |

`LLMService` ([`proto/llm.proto`](proto/llm.proto)) — served by the LLM node, called only by app
servers: `GetLLMAnswer`, `GenerateItemDescription`, `SummarizeAuction`.

`RaftService` ([`proto/raft.proto`](proto/raft.proto)) — `RequestVote` and `AppendEntries`,
following Figure 2 of the Raft paper; implemented in Milestone 2.

## Design decisions

- **The LLM is optional infrastructure.** No bid ever waits on it; each LLM-backed feature has a
  deterministic fallback and says so (`from_fallback`, `summary_from_fallback`).
- **Facts are computed, never inferred by the model.** The app server hands the model the auction's
  state, the next valid bid, and the pricing trend as finished statements; a 1.5B model is fluent
  but unreliable at arithmetic.
- **Compare-and-set bidding.** `UPDATE … WHERE current_highest_bid < :amount` makes "no lost or
  overwritten bids" an explicit guarantee rather than a property of the scheduler.
- **Idempotent settlement.** The timer settles every *closed and unsettled* auction, which covers
  both ways an auction can close (timer or late bid) and survives restarts.
- **Additive contract changes only.** `CreateAuction`, `AskAssistant` and `GetAuctionSummary` were
  added without changing any existing message, so older clients keep working.

## Testing

With the system running:

```bash
pytest tests/                         # 59 tests, about 25 s
```

- [`tests/test_contract.py`](tests/test_contract.py) — contract invariants against the live stack,
  on all three replicas (e.g. a bid without a valid session is never accepted, a seller cannot bid
  on their own auction, a closed auction reports its winner, settlement and recap, the assistant
  always answers — LLM up or down).
- [`tests/test_assistant.py`](tests/test_assistant.py), [`tests/test_llm_service.py`](tests/test_llm_service.py),
  [`tests/test_pricing_trend_db.py`](tests/test_pricing_trend_db.py) — unit tests that need no running stack.

`scripts/stress_bids.py` and `scripts/smoke_test.py` complement the suite from the outside.

## Configuration

| Variable | Service | Default | Meaning |
| --- | --- | --- | --- |
| `LLM_SERVER` | app servers | `llm_server:50060` | LLM node address; unset = plain-text fallbacks only |
| `LLM_TIMEOUT_SECONDS` | app servers | `60` | Per-call LLM deadline (CPU inference) |
| `BUYERS_PREMIUM_PERCENT` | app servers | `5` | Mock commission added to the winning bid |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | llm_server | `http://ollama:11434` / `qwen2.5:1.5b` | Model backend |
| `OLLAMA_KEEP_ALIVE` | llm_server | `30m` | How long the model stays in memory between requests |
| `BIDDER_USERNAME` / `BIDDER_PASSWORD` | client | unset | Bid as one account instead of the personas |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'auction_pb2'` | Run `./scripts/generate_protos.sh` |
| `port is already allocated` | Stop the previous stack: `docker compose -f deploy/docker-compose.yml down` |
| `permission denied` on the Docker socket | Add your user to the `docker` group, or use `sudo` |
| `llm_server` restarts or never loads the model | Give Docker at least 4 GB of memory |
| First assistant call after stopping `llm_server` takes 15–20 s | Expected once: gRPC gives up on the missing container, then falls back instantly |

## Known limitations

- **Replicas do not share state yet** — an auction created on one replica is not visible on the
  others. This is exactly what Milestone 2 addresses.
- The 1.5B model occasionally over-generalizes a fact; the templated fallback never does.
- Escrow is a mock by design: nothing is charged.
- Foreign keys are declared but not enforced, and the gRPC channels are unencrypted (fine for a
  local deployment).

## Roadmap: Milestone 2

- **Leader election** — `RequestVote`, randomized election timeouts, term tracking.
- **Log replication** — `AppendEntries`; a bid is committed once a majority of replicas has it.
- **Failure detection** — heartbeats and automatic re-election when the leader is lost.
- **Fault-tolerance demo** — kill the leader mid-auction; no committed bid is lost.

Followers will answer write requests with `accepted=false` and a `leader_hint`, a field already
present in the contract.

## Team

| Name | ID |
| --- | --- |
| Saravanasudharsan S | 2026H1030068P |
| Ashutosh Golani | 2026H1030082P |

Advanced Operating Systems (CS G623) · BITS Pilani, Pilani Campus

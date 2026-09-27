# Raftioneer — Distributed Online Auction System

A campus clearance auction: students list the things they're leaving behind
at the end of the semester (a cycle, a mini-fridge, a guitar), bid on each
other's, and an LLM assistant answers questions, writes listings and recaps
each sale. Built on gRPC in Python, with Raft-replicated state to follow in
Milestone 2.

**Milestone 1 (this release):** gRPC services, session auth, concurrency-safe
bidding, automatic auction timer and winner determination, a mock escrow
settlement, and an independent LLM node — all runnable from one
`docker compose up`.

## Architecture

```
                 ┌──────────────┐        ┌────────────┐      ┌────────────┐
   client  ────▶ │ app_server_1 │ ─────▶ │ llm_server │ ───▶ │   ollama   │
 (named bidder   │ app_server_2 │  gRPC  │ (gRPC face,│ HTTP │ qwen2.5:1.5b│
  personas)      │ app_server_3 │        │ no auction │      │ (CPU)      │
                 └──────────────┘        │  state)    │      └────────────┘
                   auth · auctions ·     └────────────┘
                   bids · timer · escrow
```

- **Five nodes**: the LLM node, three app-server replicas, and the client. The
  LLM node is `llm_server` (the gRPC service) plus its `ollama` model backend.
- **All inter-service traffic is gRPC** (`proto/`). Only `llm_server → ollama`
  is HTTP, and that is inside the LLM node.
- **The LLM is never on the bidding path.** App servers call it for FAQ
  answers, item descriptions and post-auction recaps; if it is slow, still
  loading, or down, those features fall back to plain text and bids are
  unaffected.
- Each replica keeps its own SQLite database. Replicating state between
  replicas is Milestone 2.

## Repository Layout

```
proto/          # .proto interface definitions — the contract
app_server/     # auction node: auth, bidding, timer, escrow stub, assistant glue
llm_server/     # LLM node: gRPC service, Ollama client, prompts
client/         # bidder simulation (named personas)
deploy/         # docker-compose.yml
scripts/        # seed data, sample queries, board, stress test, smoke tests
tests/          # pytest: contract tests (live stack) + unit tests
generated/      # gRPC stubs generated from proto/ — gitignored, not committed
```

## Running the Full Stack

```bash
docker compose -f deploy/docker-compose.yml up --build
```

**First run only:** Compose pulls the Ollama image (about 9 GB on disk) and
downloads the ~1 GB `qwen2.5:1.5b` model into a named volume. Later runs start
in seconds. The app servers, client and everything except LLM features work
immediately; LLM features switch from plain-text fallbacks to real answers as
soon as the `llm_server` container reports it is up.

Ports on the host: app servers `localhost:50051 / 50052 / 50053`, LLM server
`localhost:50060`.

## Demo

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
./scripts/generate_protos.sh

python scripts/seed_demo_data.py   # 6 themed auctions on every replica
docker compose -f deploy/docker-compose.yml logs -f client
```

The client bids as four named students (`deepika_hostel3`, `karan_hostel5`, …)
against the seeded items, so the logs read like people competing for a cycle.
When an auction closes, the app server log shows the winner, the mock escrow
settlement and the recap:

```
auction 44e1db05 closed - winner=user_deepika_hostel3 amount=155.0 (item=Study lamp ...)
settlement auction=44e1db05: Rs 162.75 escrowed for user_deepika_hostel3 (Rs 155.00 + Rs 7.75 buyer's premium) ... [mock]
recap auction=44e1db05 (LLM): ...
```

### Sample queries

```bash
python scripts/demo_queries.py      # guided tour, ~20 s with the LLM up
```

It browses auctions, asks the assistant about one, lists an item with **no
description** (the LLM writes it), places bids (including one that must be
rejected), waits for the auction to close, and prints the winner, settlement
and recap. Add `--server localhost:50052` to target another replica.

Once an auction has actually sold, later recaps mention how the sale compares
to the running average price (`"in line with"` / `"12% above"` / `"8% below"`
the average of other closed auctions) — computed once in Python and handed to
the LLM as a fact to relay, not raw numbers to do its own arithmetic on.

### The auction board

```bash
python scripts/board.py                                          # every active auction, all 3 replicas
python scripts/board.py --servers localhost:50051,localhost:50052
```

One `ListAuctions` call per replica (no new RPC), printed as a single table —
item, highest bid, reserve, time left — soonest-closing first. Meant to be run
once mid-demo instead of narrating over scrolling `client` logs.

### Concurrency stress test

```bash
python scripts/stress_bids.py                        # 20 bidders, 400 simultaneous bids
python scripts/stress_bids.py --bidders 50 --bids 1000 --server localhost:50052
```

Fires simultaneous bids at one auction and checks the final highest bid equals
the highest bid the server accepted (no lost or overwritten bids).

## Features

| Feature | Where |
| --- | --- |
| Login / logout, salted-PBKDF2 passwords, auto-register on first login | `app_server/store/auth.py`, `Login` |
| Create auction (title, description, duration, reserve price) | `CreateAuction` |
| Browse, filter by status, fetch one | `ListAuctions`, `GetAuction` |
| Bidding: must beat the highest bid and meet any reserve | `PlaceBid` |
| Automatic close + winner determination | `run_auction_timer` in `app_server/main.py` |
| Mock escrow settlement + 5% buyer's premium | `settle_auction`, `app_server/assistant.py` |
| Bidder FAQ assistant ("Tapri"), with the auction's details as context | `AskAssistant` |
| Blank description → LLM writes the listing | `CreateAuction` |
| Post-auction recap, incl. a pricing trend vs. other closed auctions | `GetAuctionSummary` |

`AskAssistant` and `GetAuctionSummary` are additions to the original contract,
alongside `CreateAuction`; all are purely additive (existing clients are
unaffected).

## Configuration

| Variable | Service | Default | Meaning |
| --- | --- | --- | --- |
| `LLM_SERVER` | app servers | `llm_server:50060` (compose) | LLM node address; unset → plain-text fallbacks only |
| `LLM_TIMEOUT_SECONDS` | app servers | `60` | per-call LLM deadline (CPU inference) |
| `BUYERS_PREMIUM_PERCENT` | app servers | `5` | mock commission added to the winning bid |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | llm_server | `http://ollama:11434`, `qwen2.5:1.5b` | model backend |
| `BIDDER_USERNAME` / `BIDDER_PASSWORD` | client | unset | pin the client to one account instead of the personas |

## Testing

With the stack up:

```bash
source venv/bin/activate
./scripts/generate_protos.sh

python scripts/smoke_test.py        # quick OK/FAIL check of every RPC on every node
python scripts/llm_smoke_test.py    # the three LLM RPCs against the real model
pytest tests/                       # full suite (about 25 s)
```

`tests/test_contract.py` checks contract-level invariants against the live
stack (a bid without a valid session is never accepted; a closed auction
reports its winner, settlement and recap; an auction with no bids is a
no-sale; the assistant always answers, LLM up or down). `tests/test_assistant.py`
and `tests/test_llm_service.py` are unit tests that need no running stack.

## Known behaviour

- If the LLM node is stopped, the *first* assistant call afterwards can take
  around 15 s before falling back (gRPC and DNS giving up on the missing
  container); later calls fall back immediately.
- Replicas do not share state: an auction created on `app_server_1` is not
  visible on `app_server_2` until Milestone 2.

## Status

Milestone 1 is functionally complete: every RPC works, the LLM node runs in
its own container and is wired into the app servers with graceful fallback,
and the whole system starts from one `docker compose up`. See `CLAUDE.md` →
Progress Log for the detailed history.

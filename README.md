# Raftioneer — Distributed Online Auction System

A fault-tolerant online auction built with **gRPC in Python** and a from-scratch **Raft**
implementation: three auction servers that replicate every write through a Raft log, an
independent **LLM node**, and a simulated bidder client, all started from a single
`docker compose up`.

The demo theme is a *campus clearance auction* — students listing what they leave
behind at the end of the semester (a cycle, a mini-fridge, a guitar) and bidding on
each other's — with an LLM assistant that answers bidder questions, writes listings,
and recaps every sale.

> **Status — Milestone 2 core and extensions complete:** leader election, log replication
> with a disk-persisted log, failure detection and automatic recovery, leader-only writes
> with client redirects, and a replicated auction state machine — plus three extensions:
> **anti-sniping** as replicated timer state, **exactly-once bids**, and a **chaos run with a
> consistency checker** that kills, freezes and partitions nodes while bids keep coming.
> Kill the leader mid-auction and a new one takes over in about a second with no committed
> bid lost.
> Milestone 1 (gRPC services, auth, concurrency-safe bidding, timer and winner, mock
> escrow, LLM integration) is unchanged underneath and still fully working.

*Advanced Operating Systems (CS G623) · BITS Pilani, Pilani Campus · First Semester 2026–27*

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [How Raft is used here](#how-raft-is-used-here)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Demo walkthrough](#demo-walkthrough)
- [Fault-tolerance demo](#fault-tolerance-demo)
- [API overview](#api-overview)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Performance](#performance)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Team](#team)

---

## Features

**Raft consensus** (`app_server/raft/`, written from Figure 2 of the Raft paper)
- **Leader election** — `RequestVote`, randomized election timeouts, terms, one vote per term,
  and the election restriction (only a candidate with an up-to-date log can win).
- **Log replication** — `AppendEntries` with the consistency check, conflict repair, and
  commit on a majority; a new leader commits a no-op to settle entries from earlier terms.
- **Persistence** — the term, the vote and the log are on disk (SQLite, fsync) before a node
  replies to any RPC, so a restarted node picks up exactly where it was.
- **Failure detection** — heartbeats every 200 ms; a follower that hears nothing for a random
  0.8–1.6 s starts an election.
- **Replicated state machine** — every write (login, new auction, bid, auction close and
  settlement, recap) is a log entry that every node applies in the same order.

**Auctions and bidding**
- Session-based login with salted PBKDF2 password hashing; accounts register on first login.
- Create auctions with a duration and an optional reserve price; browse, filter by status, or fetch one.
- Bid validation: a bid must be a positive amount, beat the current highest bid and meet any
  reserve, and sellers cannot bid on their own auctions.
- Concurrency-safe: bids are decided in log order, and the highest bid only moves through a
  compare-and-set update (verified with 400 simultaneous bids; every replica agrees afterwards).
- The leader's timer closes expired auctions, determines the winner, and records a **mock escrow
  settlement** with a 5% buyer's premium (`HELD (mock)`, or `NO_SALE` when nobody bid) — through
  the log, so a leader crash never loses the auction clock.

**Extensions** (Milestone 2, beyond the required core)
- **Anti-sniping** — a bid in an auction's last 10 s pushes its end to 10 s after that bid, up to
  60 s past the original end. The rule is fixed when the auction is listed and the moved end time is
  replicated state, so every node closes the auction at the same moment, whoever is leader.
- **Exactly-once bids** — a client may tag a bid with a `request_id`. The state machine records the
  outcome of every tagged bid, so a retry after a timeout or a leader change gets the *original*
  answer back (`replayed=true`) instead of being applied again — or, worse, being refused as "not
  higher than the current bid" when the first attempt had actually won.
- **Chaos run and consistency checker** — `scripts/chaos.py` kills leaders and followers, freezes a
  leader (a "zombie"), strands a leader without its majority, and cuts a leader off the network,
  while a bidder keeps bidding; `scripts/consistency_check.py` then proves every replica holds
  identical state (same applied-log digest, auctions, bid histories and outcomes), no term ever had
  two leaders, and every bid was recorded exactly once.

**LLM integration** (an independent node — the app servers call it over gRPC)
- **Tapri**, the bidder assistant (named after the campus chai stall): answers questions with the
  auction's live details injected as context.
- Writes a listing when a seller leaves the description blank.
- Writes a recap of every closed auction, including how the price compares to the average of
  earlier sales.
- **Kept honest:** every answer about an auction ends with *fine print* read from the replicated
  state (the next valid bid, the bid count, the time left), and every LLM recap is fact-checked —
  the wrong bid count or price and it is replaced by the template before it reaches the log.
- **Never on the bidding or consensus path:** if the LLM node is slow, loading, or down, every
  feature falls back to plain text and bidding is unaffected.

**Presentation**
- **One-command demo** — `scripts/demo.py` runs the whole Milestone 2 story in six narrated acts,
  pausing between them; `scripts/cluster.py --watch` shows elections live in a second terminal.
- **A narrator with a dry streak** — after each key moment the demo scripts add a one-line aside
  ("app-server-3 has left the chat. Nobody held a funeral; they held an election."). Facts are
  always printed first and plainly; the asides never replace one, and `RAFTIONEER_SERIOUS=1` (or
  `--serious`) switches them off. The client node's personas and Tapri's fallback text share the
  same voice.

**Tooling**
- Cluster status, an automated leader-failover check, a chaos run, a consistency checker, themed
  seed data, a guided tour, an auction board with replica agreement and anti-sniping extensions, a
  concurrency stress test, smoke tests, a randomized Raft churn test, and a 109-test pytest suite
  (including Raft tests with simulated crashes and network partitions). CI runs the lint, every
  test that needs no Docker, and a five-seed churn on every push.

## Architecture

```
                 ┌───────────────────────────┐        ┌──────────────┐       ┌──────────────┐
   client  ────▶ │  app_server_1  (leader)   │ ─────▶ │  llm_server  │ ────▶ │    ollama    │
 (4 bidder       │      │  AppendEntries     │  gRPC  │ (gRPC face,  │ HTTP  │ qwen2.5:1.5b │
  personas)      │      ▼  RequestVote       │        │  stateless)  │       │    (CPU)     │
     gRPC        │  app_server_2  app_server_3        └──────────────┘       └──────────────┘
                 │  (followers)              │          LLM node (Node 1)
                 └───────────────────────────┘
                   one Raft cluster: each node keeps the Raft log on disk
                   and a SQLite copy of the auction state built from it
```

- **Five nodes:** the LLM node, three app servers forming one Raft cluster, and the client. The
  LLM node is `llm_server` (the gRPC service) plus its `ollama` model backend.
- **All inter-service traffic is gRPC** ([`proto/`](proto/)): clients ↔ app servers
  (`AuctionService`), app server ↔ app server (`RaftService`), app servers → LLM node
  (`LLMService`). The only HTTP hop is `llm_server → ollama`, inside the LLM node.
- **Any app server can become leader.** Whichever wins the election accepts writes; the others
  replicate, serve reads, and redirect writes to it.
- **The LLM node holds no auction state** and takes no part in consensus. App servers inject the
  facts it needs on every call.

**Lifecycle of a bid:** the client sends `PlaceBid` to the leader → the leader checks the session
and appends a `PLACE_BID` command to its log → the entry reaches a majority and is committed →
every node applies it (bid rules checked against the replicated state) → the leader answers the
client. At the end time the leader's timer commits a `CLOSE_EXPIRED` command, every node closes
and settles the auction, and the recap is committed as `SAVE_SUMMARY`.

## How Raft is used here

| Piece | Where | What it does |
| --- | --- | --- |
| Persistent state | [`raft/storage.py`](app_server/raft/storage.py) | `currentTerm`, `votedFor` and the log in SQLite, on disk before any reply |
| The algorithm | [`raft/node.py`](app_server/raft/node.py) | roles, election timer, `RequestVote` / `AppendEntries` handlers, replication, commit, apply |
| Node-to-node RPC | [`raft/transport.py`](app_server/raft/transport.py) | gRPC client and servicer for `RaftService` |
| State machine | [`state_machine.py`](app_server/state_machine.py) | what each committed command does to the auction database — deterministic on every node |
| Client service | [`service.py`](app_server/service.py) | leader-only writes, `leader_hint` redirects, reads from any node, `GetClusterStatus` |
| Auction clock | [`auction_timer.py`](app_server/auction_timer.py) | the leader-run timer: closes, settlements and recaps through the log |
| Wiring | [`main.py`](app_server/main.py) and [`config.py`](app_server/config.py) | starts the node; every setting from the environment |

- **The log is the source of truth.** Each node's SQLite auction database is rebuilt from the log
  every time the node starts.
- **Commands are deterministic.** Anything that could differ between nodes — the time, ids, a
  salted password hash, LLM-written text, the premium percentage — is decided once by the leader
  and carried inside the command.
- **Writes go to the leader; reads go anywhere.** A follower answers a write with
  `leader_hint`; a read on a follower can trail the leader by one round trip.
- **The assignment's Raft functions** map onto [`proto/raft.proto`](proto/raft.proto) as
  follows: `requestVote(from, to, term, lastLogIndex, lastLogTerm)` is `RequestVote`
  (`candidate_id` = from; the RPC's target is "to"), `requestVoteReply(from, to, term,
  voteGranted)` is `RequestVoteResponse`; `appendEntries(from, to, term, prevIndex, prevTerm,
  commitIndex, entries)` is `AppendEntries` (`leader_id`, `prev_log_index`, `prev_log_term`,
  `leader_commit`, `entries`), and `appendEntriesReply(from, to, term, entryAppended,
  matchIndex)` is `AppendEntriesResponse` (`success`, `match_index`).

## Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 — every service, including the Raft implementation |
| RPC | gRPC + Protocol Buffers (`grpcio 1.83.1`, `protobuf 7.36.1`) |
| Concurrency | `asyncio` / `grpc.aio` — one event loop per node, no threads in the Raft core |
| Consensus | Raft, implemented from the paper (no consensus library) |
| Storage | SQLite: the Raft log (fsync on every write) and a rebuilt auction database, per node |
| LLM | Ollama serving `qwen2.5:1.5b` on CPU — no GPU required |
| Deployment | Docker Compose |
| Testing | `pytest 9.1.1` |

## Repository layout

```
proto/          gRPC contracts: auction.proto (client-facing), raft.proto (node to node),
                llm.proto (LLM node)
app_server/     auction node
  raft/         Raft: storage.py (persistent state), node.py (the algorithm), transport.py (gRPC)
  state_machine.py   the replicated commands and what applying each one does
  service.py    the gRPC AuctionService: leader-only writes, reads from any node
  auction_timer.py   closing, settling and recapping auctions, from the leader
  assistant.py  the assistant's context, fallbacks, fine print and recap fact check
  config.py     every setting, from environment variables
  main.py       wires it all together and starts the node
  store/        SQLite data access and password hashing
llm_server/     LLM node: gRPC service, Ollama client, prompts
client/         bidder simulation (four named personas), leader-aware
deploy/         docker-compose.yml - the whole system
scripts/        demo.py (the whole demo), cluster status, failover check, chaos run,
                consistency check, raft_churn.py (randomized Raft safety test, no Docker),
                seeding, guided tour, auction board, stress test, smoke tests,
                narrate.py (shared presentation helpers), stub generation
tests/          pytest: Raft and state-machine tests (no Docker), contract tests (live stack)
.github/        CI: lint, offline tests and a short churn on every push
```

## Getting started

### Prerequisites

- Docker with **Docker Compose v2** (`docker compose version`)
- About 10 GB of free disk and an internet connection for the first run (the Ollama image and the
  model); later runs are fully local, LLM node included
- Python 3.12 for the helper scripts and tests (not needed just to run the system)
- Ports `50051`–`50053` and `50060` free on the host

### 1. Start the system

```bash
docker compose -f deploy/docker-compose.yml up --build -d
```

The three app servers elect a leader within about a second of starting. The first run also pulls
the Ollama image and downloads the ~1 GB `qwen2.5:1.5b` model into a named Docker volume; later
runs start in seconds. The LLM node warms the model up as soon as it starts
(`docker compose -f deploy/docker-compose.yml logs llm_server` shows `loaded and warm`).

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
python scripts/cluster.py             # who is leader, each node's term, log, commit and applied index
python scripts/smoke_test.py          # every RPC on every node, plus "exactly one leader": OK / FAIL
python scripts/llm_smoke_test.py      # the three LLM RPCs against the real model
```

```
ADDRESS           NODE           ROLE        TERM LEADER            LOG  COMMIT  APPLIED
---------------------------------------------------------------------------------------
localhost:50051   app-server-1   FOLLOWER       1 app-server-2        5       5        5
localhost:50052   app-server-2   LEADER         1 app-server-2        5       5        5
localhost:50053   app-server-3   FOLLOWER       1 app-server-2        5       5        5
```

## Demo walkthrough

**The whole thing in one command** — six acts, each introduced, run, and paused on until you press
Enter (`--auto` runs straight through, `--serious` drops the narrator's asides, `--from 4` starts
at act 4):

```bash
python scripts/demo.py
python scripts/cluster.py --watch     # in a second terminal: roles and terms, live
```

| Act | What it shows | Script |
| --- | --- | --- |
| 1. The cluster | who leads, each node's log | `cluster.py` |
| 2. One write, three copies | seeded once, listed by every replica | `seed_demo_data.py`, `board.py` |
| 3. The auction | assistant with fine print, anti-sniping, a retried bid, the close | `demo_queries.py` |
| 4. Kill the leader | a crash mid-auction, a new leader, nothing lost | `failover_check.py` |
| 5. Chaos | two rounds of faults, then every safety check | `chaos.py --rounds 2 --seed 3` |
| 6. Under load | 400 simultaneous bids, every replica agrees | `stress_bids.py` |

Each act is an ordinary script, described below, and can be run on its own.

**Seed the demo data** — six themed auctions (listed by Tanmay and Parm), created once through the
leader and replicated to every app server:

```bash
python scripts/seed_demo_data.py
```

**Watch the bidders** — four personas (Nisargh, Nahush, Karan and Mrudul), each with their own
bidding style, browse on any app server and bid through the leader every 5 seconds:

```bash
docker compose -f deploy/docker-compose.yml logs -f client
```

**Run the guided tour** — the whole system end to end in about 20 seconds, through the leader:

```bash
python scripts/demo_queries.py
```

It browses the open auctions, asks the assistant about one, lists an item with no description (the
LLM writes it), runs a short bidding war with one bid correctly rejected and the clock pushed back by
anti-sniping, retries a bid to show it counts once, and waits for the auction to close:

```
=== 5. Two students bid (8-second auction, with anti-sniping) ===
  nisargh bids 120.00: accepted
  nahush bids 150.00: accepted
  nisargh bids 155.00: accepted
  nahush bids 100.00: rejected (Bid amount must be greater than current highest bid (155.0))
  anti-sniping: bids in the closing seconds pushed the end back 2.1s - nobody wins by bidding in the last instant

=== 5b. Nahush's phone loses signal and retries the same bid ===
  sent: accepted (replayed=False)
  retried: accepted (replayed=True) - the original outcome, not a second bid
  bid history: 120, 150, 155, 170 - the 170 is in it once

=== 6. Auction closes: winner, mock escrow, recap ===
  winner:        user_karan
  final price:   220.06  (+ buyer's premium 11.00)
  escrow:        HELD (mock)
  recap (LLM): ...
```

The extension buys the other bidders time to answer — in this run Karan, one of the client node's
personas, used it to outbid both students.

**See the whole board** — every open auction, soonest-closing first, with how many replicas agree
on its highest bid:

```bash
python scripts/board.py
```

**Take the LLM node down** — every feature keeps working on plain-text fallbacks:

```bash
docker compose -f deploy/docker-compose.yml stop llm_server
python scripts/demo_queries.py        # answers, the listing and the recap now come from templates
docker compose -f deploy/docker-compose.yml start llm_server
```

**Prove concurrency safety** — hundreds of simultaneous bids on one auction through the leader; the
final highest bid must equal the highest bid accepted, on every replica:

```bash
python scripts/stress_bids.py                       # 20 bidders, 400 bids
python scripts/stress_bids.py --bidders 50 --bids 1000
```

**Shut down** — `docker compose -f deploy/docker-compose.yml down` (keeps the downloaded model;
add `-v` to delete it too). Each node's Raft log lives inside its container, so `stop`/`start`
and `kill`/`start` keep the data and `down` starts the cluster afresh.

## Fault-tolerance demo

**Kill the leader mid-auction** — one command runs the whole scenario and checks every step:

```bash
python scripts/failover_check.py
```

```
1. Bidding through the current leader
   leader: app-server-1 at localhost:50051 (term 1)
   auction 8846cfa0: 10 bids placed, highest 200.00, replicated to every node
2. Killing the leader (app_server_1) with SIGKILL
3. Waiting for the surviving nodes to elect a new leader
   new leader: app-server-2 at localhost:50052 (term 2), 0.83s after the kill
  PASS  the new leader was elected in a later term
4. Checking nothing committed was lost
  PASS  new leader reports the same highest bid (200.00)
  PASS  a bidder's existing session still works on the new leader (sessions are replicated)
5. Bidding continues on the new leader
  PASS  5 more bids committed, highest now 260.00
6. Restarting the old leader (app_server_1)
  PASS  app-server-1 rejoined and caught up in 0.5s
  PASS  app-server-1 is now a follower of app-server-2
  PASS  app-server-1 reports the same final highest bid (260.00)
PASS: leader failover lost nothing
```

Or by hand, watching the logs (every Raft line carries node, term and role):

```bash
python scripts/cluster.py                                       # find the leader
docker compose -f deploy/docker-compose.yml kill app_server_1   # whichever is leader
python scripts/cluster.py                                       # a new leader, in a later term
docker compose -f deploy/docker-compose.yml start app_server_1  # rejoins as a follower and catches up
docker compose -f deploy/docker-compose.yml logs app_server_2 | grep term=
```

**Lose the majority** — with two of three app servers down, writes are refused (nothing can be
committed without a majority) while reads are still served; start one back and writes resume.

**Run the chaos** — random faults while a bidder keeps bidding, then every check:

```bash
python scripts/chaos.py                          # 6 rounds of random faults
python scripts/chaos.py --rounds 12 --seed 11    # replayable
python scripts/consistency_check.py              # the end-of-run checks on their own, any time
```

```
  round 1: killed both followers - app-server-2 was alone for 4s and could not commit, bids timed
           out and were retried; followers restarted; stable again in 1.4s
  round 2: cut leader app-server-2 (term 24) off the network -> app-server-1 leads term 25 after
           1.04s; reconnected; stable again in 0.2s
  ...
Checking
  PASS  one leader per term - 7 terms seen in 1511 samples
  PASS  every replica identical: same applied digest, auctions, bids and outcomes
  PASS  exactly once: 187 bids reported accepted, 187 in the history, none doubled, none missing
        6 bids needed retries through the faults; 6 of those had already been applied and were
        answered from the request table
PASS: 12 rounds of chaos - no split brain, no divergence, every bid exactly once
```

The faults: `kill-leader` (SIGKILL), `kill-follower`, `freeze-leader` (`docker pause` — the leader
is alive but silent, and wakes up in a stale term), `lose-majority` (both followers killed: the
leader is alone and cannot commit), and `partition` (the leader is cut off the network while still
running). The client node is paused only while the final checks compare replicas.

> The stress test, the failover check, the chaos run and the contract tests create auctions with
> bids far above the personas' budgets (so the simulated bidders leave them alone), and those sales
> count towards the pricing trend in later recaps. Restart the stack (`down`, then `up`) before a
> demo where the recaps matter.

## API overview

`AuctionService` ([`proto/auction.proto`](proto/auction.proto)) — served by every app server:

| RPC | Purpose | Who serves it |
| --- | --- | --- |
| `Login` / `Logout` | Session management; first login registers the account | Leader (followers redirect) |
| `CreateAuction` | List an item; a blank description is written by the LLM | Leader (followers redirect) |
| `PlaceBid` | Must beat the highest bid, meet any reserve, and not be the seller's own auction; an optional `request_id` makes retries safe (`replayed` marks a repeated answer) | Leader (followers redirect) |
| `ListAuctions` / `GetAuction` | Browse (optionally by status) or fetch one auction | Any node |
| `AskAssistant` | Bidder FAQ, with the auction's details as context; always answers | Any node |
| `GetAuctionSummary` | Outcome of a closed auction: winner, premium, escrow status, recap | Any node |
| `ListBids` | Every accepted bid on an auction, in the order they were accepted | Any node |
| `GetClusterStatus` | This node's Raft role, term, leader, log progress and applied-log digest | Any node |

A follower answers a write with `leader_hint` (the leader's address) instead of performing it.
`Auction` carries both `end_time_ms` (where anti-sniping has moved it) and `original_end_time_ms`.

`RaftService` ([`proto/raft.proto`](proto/raft.proto)) — between app servers only:
`RequestVote` and `AppendEntries`, field for field as in Figure 2 of the Raft paper.

`LLMService` ([`proto/llm.proto`](proto/llm.proto)) — served by the LLM node, called only by app
servers: `GetLLMAnswer`, `GenerateItemDescription`, `SummarizeAuction`.

## Design decisions

- **The Raft log is the source of truth; the database is a view of it.** Rebuilding the auction
  database from the log at every start means there is exactly one durable record to keep correct.
- **Deterministic commands.** Followers never consult their own clock, random generator,
  configuration or the LLM when applying an entry; the leader puts every such value in the command.
- **Bids are judged when applied, not when received,** so simultaneous bids are decided in log
  order — identically on every node — on top of the compare-and-set update.
- **A new leader waits until it has applied its own no-op** before it validates any request, so it
  never judges a request against state that is missing a committed write from the previous leader.
- **Group commit.** A proposal is queued, and the leader writes everything queued in one SQLite
  transaction — one disk sync for the batch — before adding it to the log and replicating it.
  Everything in the log is therefore always on disk, exactly as with one write per entry.
- **A write that times out is reported as "may still go through".** Raft cannot tell an entry that
  will never commit from one that will commit late; the client is told exactly that — and for bids,
  a `request_id` makes retrying safe.
- **Exactly-once lives in the state machine,** not the network layer: the outcome of each tagged bid
  is replicated state, so any node that becomes leader recognises a retry, even one of a bid that a
  previous leader accepted. Outcomes are recorded for refused bids too, so a retry always gets the
  same answer.
- **Anti-sniping rules are fixed at listing time** and stored with the auction, so a change of
  leader (or a node configured differently) can never change an auction's rules mid-auction.
- **The simulated bidders have budgets.** They stop where real bidders would, and they leave the
  test tools' high-value auctions alone, so those tests can't be disturbed by the demo traffic.
- **The LLM is optional infrastructure.** No bid ever waits on it; each LLM-backed feature has a
  deterministic fallback and says so (`from_fallback`, `summary_from_fallback`).
- **Facts are computed, never inferred by the model.** The model gets finished statements (the next
  valid bid, the pricing trend, the number of bids); a 1.5B model is fluent but unreliable at
  arithmetic.
- **The model writes, the code checks.** Answers carry fine print from the replicated state; recaps
  that misstate the bid count or the price are replaced by the template before they are committed.
- **Personality is a layer, not a substitute.** The narrator's asides and Tapri's dry fallbacks are
  printed after the facts, rotate deterministically (a recording reads the same every time), and
  switch off with `RAFTIONEER_SERIOUS=1`.
- **Additive contract changes only.** Every RPC and field added since the contract was frozen
  (`CreateAuction`, `AskAssistant`, `GetAuctionSummary`, `GetClusterStatus`, `ListBids`, the
  `leader_hint`s, `request_id` / `replayed`, `original_end_time_ms`, `applied_digest`)
  leaves existing messages unchanged, so older clients keep working.

## Testing

No running stack needed:

```bash
pytest tests/test_raft.py tests/test_state_machine.py tests/test_assistant.py \
       tests/test_llm_service.py tests/test_pricing_trend_db.py     # 71 tests, about 15 s
python scripts/raft_churn.py                                        # 30 random seeds, a few minutes
```

- [`tests/test_raft.py`](tests/test_raft.py) — the Raft implementation on an in-process network
  that can crash nodes and cut links: one leader per term, replication in order, a follower
  redirects, leader crash and recovery, a partitioned leader's uncommitted entry is discarded, a
  full-cluster restart recovers from disk, safety through repeated random crashes, a new leader
  backing up to repair a follower that missed entries, a leader deposed mid-request, group commit,
  and the `RequestVote` / `AppendEntries` rules one at a time.
- [`scripts/raft_churn.py`](scripts/raft_churn.py) — five servers, random crashes, restarts and
  partitions over many seeds; checks one leader per term, no lost commits and identical state.
- [`tests/test_state_machine.py`](tests/test_state_machine.py) — replaying the same log gives a
  byte-identical database; bid rules, settlement and recaps are applied deterministically;
  anti-sniping extends within the window and stops at the cap; a retried bid gets its original
  answer and is recorded once.

With the system running, the whole suite (109 tests, about 40 s):

```bash
pytest tests/
```

- [`tests/test_contract.py`](tests/test_contract.py) — contract invariants against the live stack:
  exactly one leader, followers redirect writes, a write on the leader is visible on every node,
  anti-sniping moves the end time identically everywhere, a retried bid is recorded once on every
  node, nodes at the same index report the same digest, plus the Milestone 1 invariants (a bid without a valid session is never accepted, a seller cannot
  bid on their own auction, a closed auction reports its winner, settlement and recap, the
  assistant always answers — LLM up or down).

`scripts/chaos.py`, `scripts/consistency_check.py`, `scripts/failover_check.py`,
`scripts/stress_bids.py` and `scripts/smoke_test.py` complement the suite from the outside.

## Performance

Measured on the Docker deployment (three app servers on one host), before and after group commit:

| Measure | One disk write per entry | Group commit |
| --- | --- | --- |
| Commit latency, one bid at a time (p50 / p99) | 20.1 / 24.6 ms | 20.0 / 25.1 ms |
| Throughput, 10 concurrent clients | 126 entries/s | 161 entries/s |
| Throughput, 50 concurrent clients | 122 entries/s | 198 entries/s |
| Throughput, 200 concurrent clients | 121 entries/s | 206 entries/s |
| Latency at 200 concurrent clients (p50) | 1.63 s | 0.95 s |

- **Under sustained load the leader keeps its leadership:** 6,000 bids from 300 concurrent clients
  in 30 s, and every server stayed in the same term with the same leader.
- **Restart cost:** a restarted server replayed an 11,715-entry log and caught up in 3.6 s.
- **Where the limit is:** each SQLite commit with full sync costs about 4.5 ms, and it still runs on
  the leader's event loop. In isolation Raft with group commit reaches about 4,500 commits/s; through
  gRPC, requests arrive one at a time and batches stay small. Moving the disk write to a thread
  would pipeline batches; that is listed as future work.

## Configuration

| Variable | Service | Default | Meaning |
| --- | --- | --- | --- |
| `NODE_ID` | app servers | — | This node's id in the Raft cluster (`app-server-1`, …) |
| `PEERS` | app servers | empty (one-node cluster) | The other members, `node-id=host:port,...` |
| `SELF_ADDRESS` | app servers | `localhost:<PORT>` | How peers and clients reach this node; given out as the `leader_hint` |
| `DATA_DIR` | app servers | `app_server/data/<NODE_ID>` | Where the Raft log and the rebuilt auction database live |
| `RAFT_ELECTION_TIMEOUT_MIN` / `_MAX` | app servers | `0.8` / `1.6` | Election timeout range, seconds |
| `RAFT_HEARTBEAT_SECONDS` | app servers | `0.2` | Leader heartbeat interval |
| `RAFT_COMMIT_TIMEOUT_SECONDS` | app servers | `2.0` | How long a write waits to be committed |
| `LLM_SERVER` | app servers | unset (compose: `llm_server:50060`) | LLM node address; unset = plain-text fallbacks only |
| `LLM_TIMEOUT_SECONDS` | app servers | `60` | Per-call LLM deadline (CPU inference) |
| `BUYERS_PREMIUM_PERCENT` | app servers | `5` | Mock commission; the leader's value is written into each close command |
| `ANTI_SNIPE_WINDOW_SECONDS` | app servers | `10` | A bid this close to the end pushes it to this long after the bid; `0` turns anti-sniping off |
| `ANTI_SNIPE_MAX_EXTENSION_SECONDS` | app servers | `60` | How far past its original end an auction can be pushed |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | llm_server | `http://ollama:11434` / `qwen2.5:1.5b` | Model backend |
| `OLLAMA_KEEP_ALIVE` | llm_server | `30m` | How long the model stays in memory between requests |
| `APP_SERVERS` | client | unset (compose: the three app servers) | Where to find the cluster |
| `BIDDER_USERNAME` / `BIDDER_PASSWORD` | client | unset | Bid as one account instead of the personas |
| `RAFTIONEER_SERIOUS` | client, scripts | unset | `1` = no narrator asides or persona reactions — facts only |
| `NO_COLOR` | scripts | unset | Set it to keep the scripts' output free of colour |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'auction_pb2'` | Run `./scripts/generate_protos.sh` |
| `port is already allocated` | Stop the previous stack: `docker compose -f deploy/docker-compose.yml down` |
| `permission denied` on the Docker socket | Add your user to the `docker` group, or use `sudo` |
| A write fails with `Not the leader - send this to app_server_2:50051` | Expected on a follower: send writes to the leader (`scripts/cluster.py`); the bundled scripts and client do this automatically |
| A write fails with `The cluster could not confirm this in time` | A majority of app servers is down; start them again. The write may still commit once they are back |
| `no leader` in `scripts/cluster.py` right after a kill | An election takes about a second; run it again |
| `llm_server` restarts or never loads the model | Give Docker at least 4 GB of memory |
| First assistant call after stopping `llm_server` takes 15–20 s | Expected once: gRPC gives up on the missing container, then falls back instantly |

## Known limitations

- **The Raft log is never compacted** (no snapshots), so a node's restart replays the whole log;
  fine at demo scale.
- **Reads on a follower can trail the leader** by about one round trip; reads are not linearizable.
- **Write throughput is about 200 entries/s** on one host (see Performance): the leader's disk sync
  runs on its event loop. Ample for an auction; a thread for the write is the next step.
- **Exactly-once covers bids only.** `Login`, `Logout` and `CreateAuction` have no `request_id`;
  a retried `CreateAuction` after a timeout can create a second listing. The request table also
  grows forever (it would be trimmed along with the log by snapshots).
- **Fixed membership:** the three app servers are configured up front; adding or removing a node
  needs a restart with new `PEERS`.
- No pre-vote or check-quorum: a node that was cut off can force one extra election when it
  rejoins, and a leader that has lost its majority keeps believing it leads until it hears a higher
  term (its writes time out rather than redirect).
- The 1.5B model still phrases facts loosely now and then (it once told a bidder to go "a little
  above 0" past a Rs 800 reserve). The fine print under every answer states the correct figure, and
  recaps are fact-checked, but the model's own sentence is shown as written.
- Escrow is a mock by design: nothing is charged.
- Foreign keys are declared but not enforced, and the gRPC channels are unencrypted (fine for a
  local deployment).

## Team

| Name | ID |
| --- | --- |
| Saravanasudharsan S | 2026H1030068P |
| Ashutosh Golani | 2026H1030082P |

Advanced Operating Systems (CS G623) · BITS Pilani, Pilani Campus

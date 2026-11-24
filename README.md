# Raftioneer — Distributed Online Auction System

A fault-tolerant online auction written in Python. Three auction servers keep identical state by
replicating every change through **Raft**, implemented from the paper; every service talks
over **gRPC**; a separate **LLM node** runs a small domain-tuned model on CPU. A web dashboard
shows the auction, the Raft cluster and the test suites live.

The demo theme is a **campus clearance auction**: at the end of the semester, students list what
they are leaving behind (a cycle, a mini-fridge, a guitar) and bid on each other's.

*Advanced Operating Systems (CS G623) · BITS Pilani, Pilani Campus · First Semester 2026–27 ·
Project option F: Distributed Online Auction System*

| | |
| --- | --- |
| **Consensus** | Raft with leader election, log replication, persistence, failure detection and pre-vote |
| **Auctions** | Accounts, listings, concurrency-safe bidding, an automatic auction clock with anti-sniping, winner determination |
| **Payments (mock)** | Wallets, a funds check on every bid, escrow held at close and released on hand-over |
| **Assistant** | Tapri: bidder FAQ, item descriptions and auction recaps, grounded in an auction handbook and the live auction data, including closed lots and their results; answers about results are checked against the record |
| **Fault tolerance** | Leader crash, follower crash, frozen leader, network partition and loss of majority, all tested |
| **Verification** | 165 automated tests, a chaos run with a consistency checker, a stress test, CI on every push |

---

## Contents

1. [Quick start](#1-quick-start)
2. [Architecture](#2-architecture)
3. [How the requirements are met](#3-how-the-requirements-are-met)
4. [The demo dashboard](#4-the-demo-dashboard)
5. [Fault tolerance](#5-fault-tolerance)
6. [API](#6-api)
7. [Design decisions](#7-design-decisions)
8. [Testing and verification](#8-testing-and-verification)
9. [Performance](#9-performance)
10. [Configuration](#10-configuration)
11. [Troubleshooting](#11-troubleshooting)
12. [Known limitations and next steps](#12-known-limitations-and-next-steps)
13. [Repository layout](#13-repository-layout)
14. [Team](#14-team)

---

## 1. Quick start

**Prerequisites:** Docker with Compose v2, Python 3.12, about 10 GB of free disk, and internet
access for the first run only (it downloads the Ollama image and a 1 GB model). Ports
`50051–50053`, `50060` and `8080` must be free.

```bash
# 1. Start the system: LLM node, three app servers, simulated bidders
docker compose -f deploy/docker-compose.yml up --build -d

# 2. Set up the Python tools (once)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
./scripts/generate_protos.sh

# 3. Add the demo auctions and open the dashboard
python scripts/seed_demo_data.py
python ui/app.py                      # then open http://localhost:8080
```

The app servers elect a leader within about a second. The LLM node is ready when
`docker compose -f deploy/docker-compose.yml logs llm_server` shows `loaded and warm`; until then,
assistant features answer from plain-text fallbacks and bidding is unaffected.

To stop: `docker compose -f deploy/docker-compose.yml down`. This keeps the downloaded model.
Each app server keeps its Raft log inside its container, so `down` followed by `up` starts a fresh
cluster.

---

## 2. Architecture

```
                         ┌──────────────── Raft cluster ────────────────┐
  Node 5: clients        │                                              │      Node 1: LLM node
 ┌────────────────┐ gRPC │  app_server_1      app_server_2    app_server_3 │ gRPC ┌────────────┐ HTTP ┌──────────┐
 │ simulated      │─────▶│   (leader)  ◀──AppendEntries──▶  (followers)    │─────▶│ llm_server │─────▶│  ollama  │
 │ bidders        │      │  AuctionService · PaymentService · RaftService  │      │ + handbook │      │ (CPU)    │
 │ demo dashboard │      │  each: Raft log on disk + auction state (SQLite) │      │ retrieval  │      └──────────┘
 └────────────────┘      └──────────────────────────────────────────────┘      └────────────┘
                                  Nodes 2–4: application servers
```

| Node | Container(s) | Role |
| --- | --- | --- |
| 1. LLM node | `llm_server`, `ollama` | Answers questions, writes descriptions and recaps. Holds no auction state |
| 2–4. App servers | `app_server_1..3` | One Raft cluster. Whichever node wins the election is the leader and accepts writes; the others replicate, serve reads and redirect writes |
| 5. Clients | `client`, plus `ui/app.py` on the host | Four simulated bidders, and the demo dashboard |

- **gRPC between all services** ([`proto/`](proto/)): clients ↔ app servers, app server ↔ app
  server (Raft), app servers → LLM node. The only non-gRPC hop is `llm_server → ollama`
  (Ollama's HTTP API), inside the LLM node.
- **Python throughout**, including the Raft implementation and the dashboard (NiceGUI).

**Life of a bid.**
1. The client sends `PlaceBid` to the leader (a follower answers with `leader_hint`).
2. The leader appends a `PLACE_BID` command to its log and replicates it.
3. Once a majority has stored the entry, it is committed.
4. Every node applies it in the same order, checking the bid rules and the bidder's funds against
   its replicated state.
5. The leader replies.

At the end time, the leader's clock commits `CLOSE_EXPIRED`: every node closes the auction and
moves the winner's payment into escrow.

---

## 3. How the requirements are met

### Milestone 2: Raft consensus and fault tolerance

| Requirement | How | Where |
| --- | --- | --- |
| Leader election | Randomised timeouts (0.8–1.6 s), terms, one vote per term, the election restriction, and pre-vote | [`app_server/raft/node.py`](app_server/raft/node.py) |
| Log replication | `AppendEntries` with the consistency check and conflict repair; commit on a majority; a new leader commits a no-op | `raft/node.py` |
| Persistence | Term, vote and log are written to SQLite with a full disk sync before any reply | [`raft/storage.py`](app_server/raft/storage.py) |
| Failure detection and recovery | Heartbeats every 200 ms; a missing leader is replaced in about a second; a restarted node catches up from the leader | `raft/node.py`, [`scripts/failover_check.py`](scripts/failover_check.py) |
| Strong consistency | Every write is a log entry; reads and writes are judged against replicated state; replicas compared with a running SHA-256 digest | [`state_machine.py`](app_server/state_machine.py), [`scripts/consistency_check.py`](scripts/consistency_check.py) |
| Auction state and bid history replicated | Accounts, sessions, auctions, bids, the auction clock, wallets, escrow and recaps are all replicated state | `state_machine.py` |
| Fault tolerance during active auctions | The auction clock is driven through the log, so a new leader continues it | [`auction_timer.py`](app_server/auction_timer.py) |
| Stress testing | 400 simultaneous bids; a write benchmark; a chaos run | [`scripts/stress_bids.py`](scripts/stress_bids.py), [`scripts/benchmark.py`](scripts/benchmark.py), [`scripts/chaos.py`](scripts/chaos.py) |

### Milestone 1: services, business logic and LLM integration

| Requirement | How |
| --- | --- |
| Real-time bidding without race conditions | Bids are decided in log order at apply time, plus a compare-and-set update of the highest bid |
| Auction timer and winner determination | The leader's clock closes expired auctions through the log; the highest bid wins. **Anti-sniping:** a bid in the last 10 s extends the auction |
| Payment and escrow (mock) | `PaymentService`: wallets with a Rs 5,000 welcome credit, deposits, a funds check at bid time, escrow held at close and released to the seller when the winner confirms receipt |
| Authentication and sessions | `Register` and `Login`; salted PBKDF2 password hashes; one-hour sessions |
| LLM: bidding assistant (FAQ) | `AskAssistant`: the LLM node retrieves the relevant sections of an [auction handbook](llm_server/knowledge/auction_handbook.md) and is given the auction (its bids, result and escrow state) or a summary of the floor, and the asker's wallet |
| LLM: item descriptions | A blank description on a new listing is written by the LLM node |
| LLM: auction results and pricing trends | A recap of every closed auction, compared with the average sale price |
| Domain-specific, context-aware LLM on a separate node | A custom model, `raftioneer-tapri`, is built at startup from `qwen2.5:1.5b` with the house persona and rules baked in. Answers are grounded in the handbook and the live auction data. The model is not fine-tuned |

### The assignment's function signatures

| Assignment | Here |
| --- | --- |
| `login(username, password)` → `loginResponse(status, token)` | `AuctionService.Login` (and `Register`) |
| `logout(token)` | `AuctionService.Logout` |
| `post(token, type, data)` | `AuctionService.Post`: types `bid`, `auction`, `deposit`, `confirm_receipt`, `ask` |
| `get(token, type, params)` → `getResponse(status, [(id, data)])` | `AuctionService.Get`: types `auctions`, `auction`, `bids`, `summary`, `wallet`, `transactions`, `cluster` |
| `processBusinessRequest(requestId, payload, context)` | [`business_api.py`](app_server/business_api.py) routes each `post`/`get` to the typed RPC that implements it |
| `getLLMAnswer(requestId, query, context)` | `LLMService.GetLLMAnswer` (`request_id`, `question`, `context`); the reply echoes `request_id` |
| `requestVote(from, to, term, lastLogIndex, lastLogTerm)` | `RaftService.RequestVote` (`candidate_id`, `to_id`, ...); the reply carries `from_id`, `to_id`, `term`, `vote_granted` |
| `appendEntries(from, to, term, prevIndex, prevTerm, commitIndex, entries)` | `RaftService.AppendEntries`; the reply carries `from_id`, `to_id`, `term`, `success` (entryAppended), `match_index` |

---

## 4. The demo dashboard

`python ui/app.py`, then open **http://localhost:8080**. It is one more client node: it talks to
the cluster over gRPC like any other client. It can also stop, freeze and restart containers, and
run the checks. The logo is the cluster itself: three dots, the amber one is the current leader.

| Tab | What it shows |
| --- | --- |
| **Floor** | Each auction as a numbered lot with its countdown. Register, sign in, bid (the dialog shows the minimum bid, the premium and the bid history, and refreshes if you are outbid), sell an item (leave the description blank and the LLM writes it). Also your wallet, your wins, closed lots with their recaps, and *Confirm receipt* to release escrow |
| **Cluster** | A status strip (leader, term, nodes up, commit index, last election) and all five nodes: the three app servers with heartbeats from the leader, the client node whose bids flow to whoever leads, and the LLM node outside the cluster. Each app server's term, log, commit and applied index and its newest log entries (coloured by term, dashed = not yet committed); a check that the replicas are identical; *Kill*, *Freeze* and *Restart*; a timed *Kill the leader*; switches for the simulated bidders and the LLM node; a timeline; and a legend |
| **Evidence** | One row per claim with the script that checks it, run exactly as shown: live checks (health, identical replicas, 400 simultaneous bids, leader failover, chaos) and full suites (the offline tests, randomized churn, the contract tests, the walkthrough) |
| **Tapri** (every tab) | The assistant in a side panel, asked in general or about any open or closed lot, with the handbook sections it used and the fine print from the record under each answer |

The same story is available without a browser: `python scripts/demo.py` runs six narrated
acts in the terminal, and `python scripts/cluster.py --watch` shows roles and terms live.

---

## 5. Fault tolerance

**Kill the leader.** Use the dashboard's *Kill the leader*, or run:

```bash
python scripts/failover_check.py
```

```
2. Killing the leader (app_server_1) with SIGKILL
3. Waiting for the surviving nodes to elect a new leader
   new leader: app-server-3 at localhost:50053 (term 3), 0.83s after the kill
4. Checking nothing committed was lost
  PASS  new leader reports the same highest bid
  PASS  a bidder's existing session still works on the new leader (sessions are replicated)
6. Restarting the old leader
  PASS  app-server-2 rejoined and caught up in 0.5s
PASS: leader failover lost nothing
```

**Chaos.** `python scripts/chaos.py --rounds 6 --seed 3` injects one fault per round while a bidder
keeps bidding:

- killing the leader or a follower (SIGKILL)
- freezing the leader (`docker pause`: alive but silent, it wakes up in a stale term)
- killing both followers (the leader cannot commit, so writes time out and are retried)
- cutting the leader off the network

Every run then checks for at most one leader per term, identical replicas, and every bid recorded
exactly once.

**Pre-vote.** Before starting an election, a node asks whether it could win, without raising its
term. Nodes that are still hearing from a leader say no. A node that was frozen, cut off or just
restarted therefore cannot force a healthy leader to step down, and each real failure causes
exactly one new term. Set `RAFT_PRE_VOTE=0` to compare.

---

## 6. API

All contracts are in [`proto/`](proto/).

| Service | RPCs | Served by |
| --- | --- | --- |
| `AuctionService` | `Register`, `Login`, `Logout`, `CreateAuction`, `PlaceBid`, `Post` | the leader (followers reply with `leader_hint`) |
| | `ListAuctions`, `GetAuction`, `ListBids`, `GetAuctionSummary`, `AskAssistant`, `GetClusterStatus`, `Get` | any node |
| `PaymentService` | `Deposit`, `ConfirmReceipt` | the leader |
| | `GetWallet`, `ListTransactions` | any node |
| `RaftService` | `RequestVote` (incl. pre-vote), `AppendEntries` | app servers, between themselves |
| `LLMService` | `GetLLMAnswer`, `GenerateItemDescription`, `SummarizeAuction` | the LLM node, for app servers only |

A bid or deposit may carry a `request_id`. A retry with the same id gets the original answer back
(`replayed`) instead of being applied twice.

---

## 7. Design decisions

- **The Raft log is the source of truth.** Each node's auction database is rebuilt from the log at
  startup; there is exactly one durable record to keep correct.
- **Commands are deterministic.** The leader decides anything that could differ between nodes (the
  time, ids, password hashes, LLM text, the premium percentage) and puts it in the command;
  applying an entry never reads a clock, a random generator, local configuration or the LLM.
- **Rules are checked when an entry is applied, not when the request arrives.** Simultaneous bids
  are therefore decided in log order on every node, and two bids can never spend the same money.
- **A new leader serves requests only after applying its own no-op,** so it never judges a request
  against state that is missing an entry committed by the previous leader.
- **Exactly-once lives in the state machine.** The outcome of every tagged request is replicated,
  so any future leader recognises a retry.
- **Group commit.** The leader writes all queued proposals in one transaction with one disk sync,
  before replicating them.
- **The LLM is never on the bidding or consensus path.** Every LLM feature has a plain-text
  fallback. Facts (the next valid bid, the largest affordable bid, the pricing trend, the bid count)
  are computed in Python and handed to the model. Answers about an auction end with *fine print*
  from the replicated state, and recaps that misstate the bid count or price are replaced before
  they are committed.
- **Retrieval, not fine-tuning, makes the model domain-specific:** BM25 search over a 15-section
  handbook (pure Python, no vector database) on top of a custom model definition. This fits a
  CPU-only node.
- **Contract changes are additive.** New RPCs and fields leave existing messages unchanged.

---

## 8. Testing and verification

```bash
# No Docker needed: Raft, state machine, payments, assistant, retrieval (117 tests, ~20 s)
pytest tests/test_raft.py tests/test_state_machine.py tests/test_assistant.py \
       tests/test_llm_service.py tests/test_pricing_trend_db.py tests/test_retrieval.py
python scripts/raft_churn.py            # randomized crashes and partitions, 30 seeds

# With the system running
pytest tests/test_contract.py           # 48 contract tests against every node
python scripts/smoke_test.py            # every RPC on every node
python scripts/consistency_check.py     # replicas identical
python scripts/stress_bids.py           # 400 simultaneous bids
python scripts/failover_check.py        # kill the leader, check nothing is lost
python scripts/chaos.py                 # random faults, then every safety check
```

| Suite | What it covers |
| --- | --- |
| [`test_raft.py`](tests/test_raft.py) (25) | An in-process cluster that can crash nodes and cut links: one leader per term, ordered replication, leader crash, partitioned leader, full restart from disk, log repair, pre-vote, and each RPC rule on its own |
| [`test_state_machine.py`](tests/test_state_machine.py) (27) | Replaying the same log gives a byte-identical database; bid rules; funds, escrow and deposits; anti-sniping; exactly-once |
| [`test_assistant.py`](tests/test_assistant.py), [`test_llm_service.py`](tests/test_llm_service.py), [`test_retrieval.py`](tests/test_retrieval.py), [`test_pricing_trend_db.py`](tests/test_pricing_trend_db.py) (65) | Context and fallbacks, wallet facts, closed lots and the floor, the recap and result fact checks, handbook retrieval, request id round trip |
| [`test_contract.py`](tests/test_contract.py) (48) | The live cluster: one leader, redirects, replication to every node, accounts, the funds check, escrow release, post/get, anti-sniping, exactly-once, digests |

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the lint, every offline test and
a five-seed churn on every push.

**Before a demo,** restart the stack (`down`, then `up`). The test tools create high-value
auctions, which would skew the pricing trends in later recaps.

---

## 9. Performance

Measured with `python scripts/benchmark.py` on the Docker deployment (three app servers on one
laptop):

| Measure | Result |
| --- | --- |
| Commit latency, one bid at a time (p50 / p95 / p99) | 18.7 / 24.0 / 33.5 ms |
| Throughput with 10 / 50 / 200 concurrent clients | 193 / 274 / 268 log entries per second |
| Latency with 200 concurrent clients (p50 / p99) | 0.74 / 0.90 s |
| Leader stability under load | No election during any run |

- Each committed write costs one full disk sync on the leader (about 4.5 ms). Group commit shares
  one sync across every proposal waiting at that moment.
- A restarted node replays its whole log (no snapshots); an 11,715-entry log took 3.6 s.

---

## 10. Configuration

All settings are environment variables, set per node in
[`deploy/docker-compose.yml`](deploy/docker-compose.yml).

| Variable | Default | Meaning |
| --- | --- | --- |
| `NODE_ID`, `PEERS`, `SELF_ADDRESS` | (compose) | This node's id, the other members (`id=host:port,...`), and how others reach it |
| `RAFT_ELECTION_TIMEOUT_MIN` / `_MAX` | `0.8` / `1.6` | Election timeout range, seconds |
| `RAFT_HEARTBEAT_SECONDS` | `0.2` | Leader heartbeat interval |
| `RAFT_COMMIT_TIMEOUT_SECONDS` | `2.0` | How long a write waits to be committed |
| `RAFT_PRE_VOTE` | `1` | `0` turns pre-vote off |
| `WELCOME_CREDIT` | `5000` | Mock money in every new wallet |
| `BUYERS_PREMIUM_PERCENT` | `5` | Added to the winning bid at settlement |
| `ANTI_SNIPE_WINDOW_SECONDS` / `ANTI_SNIPE_MAX_EXTENSION_SECONDS` | `10` / `60` | Anti-sniping window and cap (`0` = off) |
| `LLM_SERVER`, `LLM_TIMEOUT_SECONDS` | `llm_server:50060`, `60` | LLM node address and per-call deadline |
| `OLLAMA_MODEL`, `OLLAMA_CUSTOM_MODEL` | `qwen2.5:1.5b`, `raftioneer-tapri` | Base model, and the domain model built from it |
| `RAFTIONEER_SERIOUS` | unset | `1` removes the narrator's light-hearted asides from scripts and logs |

---

## 11. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No module named 'auction_pb2'` | Run `./scripts/generate_protos.sh` |
| `port is already allocated` | `docker compose -f deploy/docker-compose.yml down`, or stop what uses port 8080 |
| A write says `Not the leader - send this to ...` | Expected on a follower; the dashboard, client and scripts follow the hint automatically |
| A write says `The cluster could not confirm this in time` | A majority of app servers is down. Start them again; the write may still commit |
| The assistant is slow or answers from facts only | The model is still loading, or the laptop is busy; check `docker compose ... logs llm_server` |
| Elections happen while the laptop is under heavy load | Close other heavy applications; the app servers need CPU time every 200 ms |

---

## 12. Known limitations and next steps

- The log is never compacted (no snapshots), so a restarted node replays it in full.
- Reads on a follower can trail the leader by about one round trip; reads are not linearizable. A
  follower that has just restarted answers from an empty state until the leader's first heartbeat
  tells it what is committed (under a second).
- Membership is fixed at three app servers.
- No check-quorum: a leader cut off from its majority keeps leading until it hears a later term
  (its writes time out rather than commit).
- `CreateAuction` has no `request_id`, so a retried listing after a timeout can appear twice.
- The 1.5B model sometimes phrases things loosely; the fine print and the recap check keep the
  facts correct.
- Payments are a mock by design, and the gRPC channels are unencrypted (local deployment).

**Next steps,** roughly in order of value:

1. Snapshots and log compaction (Raft Section 7), so a restart no longer replays the whole log.
2. Check-quorum, so an isolated leader steps down on its own.
3. Linearizable reads on followers (ReadIndex), and no answers from a follower until it has
   heard from the leader.
4. A `request_id` on every write, not only bids and deposits.
5. Cluster membership changes, and TLS on the gRPC channels.

---

## 13. Repository layout

```
proto/             gRPC contracts: auction.proto (clients, payments), raft.proto, llm.proto
app_server/        an auction node
  raft/            node.py (the algorithm), storage.py (persistence), transport.py (gRPC)
  state_machine.py what each committed command does; deterministic
  service.py       AuctionService: leader-only writes, reads on any node
  payment_service.py  PaymentService: wallets, deposits, escrow release
  business_api.py  post/get, routed by processBusinessRequest
  auction_timer.py the auction clock, run by the leader through the log
  assistant.py     the assistant's context, fallbacks, fine print and recap check
  store/           SQLite access and password hashing
llm_server/        the LLM node: gRPC service, retrieval, prompts, Ollama client
  knowledge/       the auction handbook used for retrieval
ui/                the demo dashboard (NiceGUI): floor, cluster, evidence, Tapri
client/            simulated bidders (Node 5)
deploy/            docker-compose.yml for the whole system
scripts/           demo, cluster status, seeding, failover, chaos, consistency, stress, benchmark, churn
tests/             pytest suites (offline and live)
```

---

## 14. Team

| Name | ID |
| --- | --- |
| Saravanasudharsan S | 2026H1030068P |
| Ashutosh Golani | 2026H1030082P |

Reference: D. Ongaro and J. Ousterhout, *In Search of an Understandable Consensus Algorithm
(Extended Version)*; D. Ongaro, *Consensus: Bridging Theory and Practice* (PhD thesis, Section 9.6 on
pre-vote).

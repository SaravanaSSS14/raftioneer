# Raftioneer — Distributed Online Auction System

Fault-tolerant online auction platform built on gRPC and a custom Raft
implementation. See the top-level `Raftioneer_Project_Reference.md`
(one directory up) for the full project abstract, architecture, work
split, and schedule. See `CLAUDE.md` for engineering conventions.

## Repository Layout

```
proto/          # .proto interface definitions — the frozen contract
app_server/     # Raft + business logic (the auction node)
llm_server/     # LLM integration, isolated service
client/         # CLI client + bidder-simulation script
deploy/         # docker-compose.yml
scripts/        # dev tooling (proto generation, etc.)
generated/      # gRPC stubs generated from proto/ — gitignored, not committed
```

## Local Development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r app_server/requirements.txt

./scripts/generate_protos.sh   # regenerate generated/*_pb2*.py from proto/
```

## Running the Full Stack

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Brings up 5 nodes: the LLM server, three app-server replicas
(`app_server_1/2/3`), and a client that polls each app server's
`AuctionService` over gRPC to confirm reachability.

## Status

Week 1 skeleton: `.proto` contracts drafted, all five services build
and respond to gRPC calls with stub logic. No Raft, auth, or business
logic yet — see `CLAUDE.md` → Progress Log for current state.

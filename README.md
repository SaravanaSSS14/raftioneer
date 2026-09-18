# Raftioneer — Distributed Online Auction System

Fault-tolerant online auction platform built on gRPC and a custom Raft
implementation.

## Repository Layout

```
proto/          # .proto interface definitions — the frozen contract
app_server/     # Raft + business logic (the auction node)
llm_server/     # LLM integration, isolated service
client/         # CLI client + bidder-simulation script
deploy/         # docker-compose.yml
scripts/        # dev tooling (proto generation, manual smoke test)
tests/          # pytest contract tests, run against a live stack
generated/      # gRPC stubs generated from proto/ — gitignored, not committed
```

## Local Development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt

./scripts/generate_protos.sh   # regenerate generated/*_pb2*.py from proto/
```

## Running the Full Stack

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Brings up 5 nodes: the LLM server, three app-server replicas
(`app_server_1/2/3`), and a client that polls each app server's
`AuctionService` over gRPC to confirm reachability.

App servers are published on the host at `localhost:50051/50052/50053`,
the LLM server at `localhost:50060`.

## Testing

With the stack up (Compose, or services run locally on matching ports):

```bash
source venv/bin/activate
./scripts/generate_protos.sh

python scripts/smoke_test.py   # quick manual check, readable OK/FAIL output
pytest tests/                  # full contract test suite, CI-friendly
```

Both check contract-level invariants (e.g. a bid without a valid
session is never accepted) rather than specific seed data — they pass
regardless of how business logic is implemented internally.

## Status

`.proto` contracts drafted (pending Member B's sign-off), all five
services build and respond to gRPC calls. Infra, client, and testing
are in place; auth, bid/auction business logic, and real LLM
integration are still stub code — see `CLAUDE.md` → Progress Log for
current state.

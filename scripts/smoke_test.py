#!/usr/bin/env python3
"""Manual smoke test against a running `docker compose up` stack.

Not a pytest suite (there's no business logic yet to unit-test) — this
is a quick, repeatable replacement for "eyeball the logs" when checking
that the .proto contract is actually wired up end-to-end across all
three app-server replicas and the LLM server.

Usage (with the stack already up):
    source venv/bin/activate
    ./scripts/generate_protos.sh
    python scripts/smoke_test.py
"""
import sys

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
import llm_pb2
import llm_pb2_grpc

APP_SERVERS = ["localhost:50051", "localhost:50052", "localhost:50053"]
LLM_SERVER = "localhost:50060"
TIMEOUT_SECONDS = 3


def check_app_server(address: str) -> bool:
    try:
        with grpc.insecure_channel(address) as channel:
            stub = auction_pb2_grpc.AuctionServiceStub(channel)
            stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=TIMEOUT_SECONDS)
        print(f"OK   {address}  (AuctionService.ListAuctions responded)")
        return True
    except grpc.RpcError as exc:
        print(f"FAIL {address}  ({exc.code()})")
        return False


def check_llm_server(address: str) -> bool:
    try:
        with grpc.insecure_channel(address) as channel:
            stub = llm_pb2_grpc.LLMServiceStub(channel)
            stub.GetLLMAnswer(llm_pb2.GetLLMAnswerRequest(question="ping"), timeout=TIMEOUT_SECONDS)
        print(f"OK   {address}  (LLMService.GetLLMAnswer responded)")
        return True
    except grpc.RpcError as exc:
        print(f"FAIL {address}  ({exc.code()})")
        return False


def main() -> int:
    results = [check_app_server(addr) for addr in APP_SERVERS]
    results.append(check_llm_server(LLM_SERVER))
    if all(results):
        print("\nAll services reachable and responding to the frozen .proto contract.")
        return 0
    print("\nSome services did not respond — check `docker compose ps` / logs.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

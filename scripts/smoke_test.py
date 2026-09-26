#!/usr/bin/env python3
"""Manual smoke test against a running `docker compose up` stack.

Deliberately generic: it checks contract-level invariants that must
hold no matter how business logic is implemented internally — it
does NOT assume specific seed users, item names, or auction ids, since
those are implementation details, not part of the .proto contract.

Usage (with the stack already up):
    source venv/bin/activate
    ./scripts/generate_protos.sh
    python scripts/smoke_test.py
"""
import sys
import uuid

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

            # Login must be well-formed: a "success" response always
            # carries a session token, a failed one never does.
            login = stub.Login(
                auction_pb2.LoginRequest(username="smoke-test", password="wrong"),
                timeout=TIMEOUT_SECONDS,
            )
            assert not (login.success and not login.session_token), (
                "Login reported success but returned no session_token"
            )

            # ListAuctions must respond without error, filtered or not.
            stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=TIMEOUT_SECONDS)

            # A random, essentially-guaranteed-nonexistent auction id
            # must never be reported as found.
            bogus_id = f"smoke-test-{uuid.uuid4()}"
            got = stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=bogus_id), timeout=TIMEOUT_SECONDS)
            assert not got.found, f"GetAuction reported found=True for a made-up id ({bogus_id})"

            # Security invariant: a bid with no valid session must
            # never be accepted, regardless of internal bid logic.
            bid = stub.PlaceBid(
                auction_pb2.PlaceBidRequest(session_token="", auction_id=bogus_id, amount=1.0),
                timeout=TIMEOUT_SECONDS,
            )
            assert not bid.accepted, "PlaceBid accepted a bid with an empty session_token"

            # The assistant is session-gated like bidding, and an unknown
            # auction never has a summary.
            asked = stub.AskAssistant(
                auction_pb2.AskAssistantRequest(session_token="", question="hello?"),
                timeout=TIMEOUT_SECONDS,
            )
            assert not asked.success, "AskAssistant answered a request with an empty session_token"
            summary = stub.GetAuctionSummary(
                auction_pb2.GetAuctionSummaryRequest(auction_id=bogus_id), timeout=TIMEOUT_SECONDS
            )
            assert not summary.found, f"GetAuctionSummary reported found=True for a made-up id ({bogus_id})"

            # Logout must not error, valid session or not.
            stub.Logout(auction_pb2.LogoutRequest(session_token=""), timeout=TIMEOUT_SECONDS)

        print(
            f"OK   {address}  (Login, ListAuctions, GetAuction, PlaceBid, "
            "AskAssistant, GetAuctionSummary, Logout all well-formed)"
        )
        return True
    except (grpc.RpcError, AssertionError) as exc:
        print(f"FAIL {address}  ({exc})")
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
        print("\nAll services reachable and honoring the frozen .proto contract.")
        return 0
    print("\nSome checks failed — see above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Contract-level integration tests, run against a live stack
(`docker compose up`, or services started locally on matching ports).

Deliberately generic, same spirit as scripts/smoke_test.py: these check
invariants that must hold no matter how Member B implements business
logic internally (bid validation, session handling, etc.) — never
specific seed users, item names, or auction ids, since those are his
implementation details, not part of the frozen .proto contract.

Run with the stack already up:
    source venv/bin/activate
    ./scripts/generate_protos.sh
    pytest tests/
"""
import os
import uuid

import grpc
import pytest

import auction_pb2
import auction_pb2_grpc
import llm_pb2
import llm_pb2_grpc

APP_SERVERS = os.environ.get(
    "TEST_APP_SERVERS", "localhost:50051,localhost:50052,localhost:50053"
).split(",")
LLM_SERVER = os.environ.get("TEST_LLM_SERVER", "localhost:50060")
TIMEOUT_SECONDS = 3


@pytest.fixture(params=APP_SERVERS, ids=APP_SERVERS)
def app_stub(request):
    with grpc.insecure_channel(request.param) as channel:
        yield auction_pb2_grpc.AuctionServiceStub(channel)


@pytest.fixture
def llm_stub():
    with grpc.insecure_channel(LLM_SERVER) as channel:
        yield llm_pb2_grpc.LLMServiceStub(channel)


def test_failed_login_never_returns_a_session_token(app_stub):
    resp = app_stub.Login(
        auction_pb2.LoginRequest(username="pytest-user", password="wrong-password"),
        timeout=TIMEOUT_SECONDS,
    )
    assert not (resp.success and not resp.session_token)


def test_list_auctions_responds_without_error(app_stub):
    resp = app_stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=TIMEOUT_SECONDS)
    assert isinstance(resp.auctions, type(resp.auctions))  # a repeated field, never None


def test_get_auction_for_unknown_id_is_not_found(app_stub):
    bogus_id = f"pytest-{uuid.uuid4()}"
    resp = app_stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=bogus_id), timeout=TIMEOUT_SECONDS)
    assert not resp.found


def test_bid_without_valid_session_is_never_accepted(app_stub):
    resp = app_stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token="", auction_id=f"pytest-{uuid.uuid4()}", amount=1.0),
        timeout=TIMEOUT_SECONDS,
    )
    assert not resp.accepted


def test_logout_does_not_error(app_stub):
    app_stub.Logout(auction_pb2.LogoutRequest(session_token=""), timeout=TIMEOUT_SECONDS)


def test_llm_get_answer_responds(llm_stub):
    resp = llm_stub.GetLLMAnswer(llm_pb2.GetLLMAnswerRequest(question="ping"), timeout=TIMEOUT_SECONDS)
    assert isinstance(resp.answer, str)

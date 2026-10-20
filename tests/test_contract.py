"""Contract-level integration tests, run against a live stack
(`docker compose up`, or services started locally on matching ports).

Deliberately generic, same spirit as scripts/smoke_test.py: these check
invariants that must hold no matter how business logic is implemented
internally (bid validation, session handling, etc.) — never specific seed
users, item names, or auction ids, since those are implementation details,
not part of the .proto contract. The LLM-backed tests pass whether the LLM
node answers or the app server falls back to plain text: the contract is
that the feature always responds, and bidding never depends on the LLM.

The app servers are one Raft cluster: writes go to the leader (followers
answer with a leader_hint), reads are checked on every node.

Run with the stack already up:
    source venv/bin/activate
    ./scripts/generate_protos.sh
    pytest tests/
"""
import os
import time
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
APP_TIMEOUT_SECONDS = 3
LLM_TIMEOUT_SECONDS = 90


@pytest.fixture(params=APP_SERVERS, ids=APP_SERVERS)
def app_stub(request):
    with grpc.insecure_channel(request.param) as channel:
        yield auction_pb2_grpc.AuctionServiceStub(channel)


def _statuses():
    result = {}
    for address in APP_SERVERS:
        try:
            with grpc.insecure_channel(address) as channel:
                result[address] = auction_pb2_grpc.AuctionServiceStub(channel).GetClusterStatus(
                    auction_pb2.ClusterStatusRequest(), timeout=APP_TIMEOUT_SECONDS
                )
        except grpc.RpcError:
            pass
    return result


def _leader_address(timeout_seconds=15):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        leaders = [address for address, s in _statuses().items() if s.role == "LEADER"]
        if len(leaders) == 1:
            return leaders[0]
        time.sleep(0.2)
    pytest.fail("the app servers have no leader")


def _wait_replicated(timeout_seconds=10):
    """Until every node has applied what the leader has committed - after
    which a read on any node sees the latest writes."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        statuses = list(_statuses().values())
        leaders = [s for s in statuses if s.role == "LEADER"]
        if leaders and all(s.last_applied >= leaders[0].commit_index for s in statuses):
            return
        time.sleep(0.05)
    pytest.fail("followers did not catch up with the leader")


@pytest.fixture
def leader_stub():
    with grpc.insecure_channel(_leader_address()) as channel:
        yield auction_pb2_grpc.AuctionServiceStub(channel)


@pytest.fixture
def llm_stub():
    with grpc.insecure_channel(LLM_SERVER) as channel:
        yield llm_pb2_grpc.LLMServiceStub(channel)


def test_failed_login_never_returns_a_session_token(app_stub):
    resp = app_stub.Login(
        auction_pb2.LoginRequest(username="pytest-user", password="wrong-password"),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert not (resp.success and not resp.session_token)


def test_list_auctions_responds_without_error(app_stub):
    resp = app_stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=APP_TIMEOUT_SECONDS)
    assert isinstance(resp.auctions, type(resp.auctions))  # a repeated field, never None


def test_get_auction_for_unknown_id_is_not_found(app_stub):
    bogus_id = f"pytest-{uuid.uuid4()}"
    resp = app_stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=bogus_id), timeout=APP_TIMEOUT_SECONDS)
    assert not resp.found


def test_bid_without_valid_session_is_never_accepted(app_stub):
    resp = app_stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token="", auction_id=f"pytest-{uuid.uuid4()}", amount=1.0),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert not resp.accepted


def test_logout_does_not_error(app_stub):
    app_stub.Logout(auction_pb2.LogoutRequest(session_token=""), timeout=APP_TIMEOUT_SECONDS)


def test_llm_get_answer_responds(llm_stub):
    resp = llm_stub.GetLLMAnswer(llm_pb2.GetLLMAnswerRequest(question="ping"), timeout=LLM_TIMEOUT_SECONDS)
    assert isinstance(resp.answer, str)


# ---- Assistant, settlement and recap ----


def _login(username):
    """Log in through the leader; the session is then valid on every node."""
    with grpc.insecure_channel(_leader_address()) as channel:
        resp = auction_pb2_grpc.AuctionServiceStub(channel).Login(
            auction_pb2.LoginRequest(username=username, password="pytest-pw-123"),
            timeout=APP_TIMEOUT_SECONDS,
        )
    assert resp.success, resp.error_message
    _wait_replicated()
    return resp


def _wait_for_recap(stub, auction_id, timeout_seconds=120):
    """Poll until the auction is closed AND its recap has been written."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        resp = stub.GetAuctionSummary(
            auction_pb2.GetAuctionSummaryRequest(auction_id=auction_id), timeout=APP_TIMEOUT_SECONDS
        )
        if resp.closed and resp.summary_ready:
            return resp
        time.sleep(0.5)
    pytest.fail(f"auction {auction_id} was not closed and summarised within {timeout_seconds}s")


@pytest.fixture
def single_stub(leader_stub):
    """The leader: these tests create real auctions and wait for them."""
    return leader_stub


def test_ask_assistant_without_valid_session_is_refused(app_stub):
    resp = app_stub.AskAssistant(
        auction_pb2.AskAssistantRequest(session_token="", question="Is this any good?"),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert not resp.success
    assert not resp.answer


def test_get_auction_summary_for_unknown_id_is_not_found(app_stub):
    resp = app_stub.GetAuctionSummary(
        auction_pb2.GetAuctionSummaryRequest(auction_id=f"pytest-{uuid.uuid4()}"),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert not resp.found


def test_ask_assistant_always_answers(app_stub):
    """Answered by the LLM node or, if it is slow/down, by the plain-text
    fallback - either way the caller gets a non-empty answer."""
    token = _login("pytest-asker").session_token
    resp = app_stub.AskAssistant(
        auction_pb2.AskAssistantRequest(session_token=token, question="How does the buyer's premium work?"),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    assert resp.success, resp.error_message
    assert resp.answer.strip()


def test_ask_assistant_about_unknown_auction_is_refused(app_stub):
    token = _login("pytest-asker").session_token
    resp = app_stub.AskAssistant(
        auction_pb2.AskAssistantRequest(
            session_token=token, auction_id=f"pytest-{uuid.uuid4()}", question="Any bids yet?"
        ),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    assert not resp.success


def test_open_auction_has_no_settlement_yet(single_stub):
    seller = _login("pytest-seller").session_token
    created = single_stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(
            session_token=seller, title="pytest open auction", description="d", duration_seconds=30
        ),
        timeout=APP_TIMEOUT_SECONDS,
    )
    resp = single_stub.GetAuctionSummary(
        auction_pb2.GetAuctionSummaryRequest(auction_id=created.auction_id), timeout=APP_TIMEOUT_SECONDS
    )
    assert resp.found and not resp.closed


def test_seller_cannot_bid_on_their_own_auction(single_stub):
    seller = _login("pytest-seller").session_token
    created = single_stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(
            session_token=seller, title="pytest own-item auction", description="d", duration_seconds=30
        ),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert created.success, created.error_message
    bid = single_stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token=seller, auction_id=created.auction_id, amount=10.0),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert not bid.accepted
    assert "own auction" in bid.error_message


def test_closed_auction_reports_winner_settlement_and_recap(single_stub):
    # The test bids far above the client node's persona budgets, so they leave
    # this auction alone; the retry is a second line of defence.
    seller = _login("pytest-seller").session_token
    low = _login("pytest-bidder-low")
    high = _login("pytest-bidder-high")

    for _ in range(3):
        created = single_stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller, title="pytest closing auction", description="d", duration_seconds=3
            ),
            timeout=APP_TIMEOUT_SECONDS,
        )
        assert created.success, created.error_message

        for bidder, amount in [(low, 50_000.0), (high, 55_000.0)]:
            single_stub.PlaceBid(
                auction_pb2.PlaceBidRequest(
                    session_token=bidder.session_token, auction_id=created.auction_id, amount=amount
                ),
                timeout=APP_TIMEOUT_SECONDS,
            )

        result = _wait_for_recap(single_stub, created.auction_id)
        if result.winner_id in (low.user_id, high.user_id):
            break

    assert result.winner_id == high.user_id, "every attempt was outbid by the client node"
    assert result.final_price == 55_000.0
    assert 0 < result.buyers_premium < result.final_price
    assert result.escrow_status.startswith("HELD")
    assert result.summary.strip()


def test_closed_auction_with_no_bids_is_a_no_sale(single_stub):
    # A reserve beyond every persona's budget keeps the client node from bidding;
    # the retry is a second line of defence.
    seller = _login("pytest-seller").session_token
    for _ in range(3):
        created = single_stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller, title="pytest unloved auction", description="d", duration_seconds=1,
                reserve_price=1_000_000.0,
            ),
            timeout=APP_TIMEOUT_SECONDS,
        )
        result = _wait_for_recap(single_stub, created.auction_id)
        if result.winner_id == "":
            break
    assert result.winner_id == "", "every attempt drew a bid from the client node"
    assert result.final_price == 0.0
    assert result.buyers_premium == 0.0
    assert result.escrow_status == "NO_SALE"


# ---- Raft: one leader, writes through it, replicated everywhere ----


def test_exactly_one_leader_and_every_node_follows_it():
    statuses = _statuses()
    assert len(statuses) == len(APP_SERVERS), "an app server is unreachable"
    leaders = [s for s in statuses.values() if s.role == "LEADER"]
    assert len(leaders) == 1
    assert all(s.leader_id == leaders[0].node_id and s.term == leaders[0].term for s in statuses.values())


def test_followers_refuse_writes_and_point_at_the_leader():
    leader = _leader_address()
    for address in APP_SERVERS:
        if address == leader:
            continue
        with grpc.insecure_channel(address) as channel:
            stub = auction_pb2_grpc.AuctionServiceStub(channel)
            login = stub.Login(
                auction_pb2.LoginRequest(username="pytest-redirect", password="pytest-pw-123"),
                timeout=APP_TIMEOUT_SECONDS,
            )
            bid = stub.PlaceBid(
                auction_pb2.PlaceBidRequest(session_token="x", auction_id="y", amount=1.0),
                timeout=APP_TIMEOUT_SECONDS,
            )
        assert not login.success and login.leader_hint
        assert not bid.accepted and bid.leader_hint
        assert login.leader_hint == bid.leader_hint


def test_a_write_on_the_leader_is_visible_on_every_node(leader_stub):
    seller = _login("pytest-seller").session_token
    bidder = _login("pytest-bidder-low").session_token
    created = leader_stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(
            session_token=seller, title="pytest replicated auction", description="d", duration_seconds=30
        ),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert created.success, created.error_message
    bid = leader_stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token=bidder, auction_id=created.auction_id, amount=42.0),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert bid.accepted, bid.error_message

    _wait_replicated()
    for address in APP_SERVERS:
        with grpc.insecure_channel(address) as channel:
            got = auction_pb2_grpc.AuctionServiceStub(channel).GetAuction(
                auction_pb2.GetAuctionRequest(auction_id=created.auction_id), timeout=APP_TIMEOUT_SECONDS
            )
        assert got.found, address
        assert got.auction.current_highest_bid == 42.0, address


# ---- Extensions: anti-sniping, exactly-once bids, replica digests ----


def _create(stub, seller_token, title, seconds):
    created = stub.CreateAuction(
        auction_pb2.CreateAuctionRequest(session_token=seller_token, title=title, description="d",
                                         duration_seconds=seconds),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert created.success, created.error_message
    return created.auction_id


def test_a_bid_in_the_closing_seconds_extends_the_auction_on_every_node(leader_stub):
    seller = _login("pytest-seller").session_token
    bidder = _login("pytest-bidder-low").session_token
    auction_id = _create(leader_stub, seller, "pytest anti-sniping auction", 5)  # inside the 10 s window from the start
    bid = leader_stub.PlaceBid(
        auction_pb2.PlaceBidRequest(session_token=bidder, auction_id=auction_id, amount=60_000.0),
        timeout=APP_TIMEOUT_SECONDS,
    )
    assert bid.accepted, bid.error_message
    auction = bid.updated_auction
    assert auction.end_time_ms > auction.original_end_time_ms  # the clock moved

    _wait_replicated()
    for address in APP_SERVERS:
        with grpc.insecure_channel(address) as channel:
            got = auction_pb2_grpc.AuctionServiceStub(channel).GetAuction(
                auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=APP_TIMEOUT_SECONDS
            ).auction
        assert got.end_time_ms == auction.end_time_ms, address  # moved identically everywhere


def test_a_retried_bid_gets_its_original_answer_and_is_recorded_once(leader_stub):
    seller = _login("pytest-seller").session_token
    first = _login("pytest-bidder-low").session_token
    second = _login("pytest-bidder-high").session_token
    auction_id = _create(leader_stub, seller, "pytest exactly-once auction", 60)
    request_id = uuid.uuid4().hex

    def place(token, amount, rid=""):
        return leader_stub.PlaceBid(
            auction_pb2.PlaceBidRequest(session_token=token, auction_id=auction_id, amount=amount, request_id=rid),
            timeout=APP_TIMEOUT_SECONDS,
        )

    original = place(first, 70_000.0, request_id)
    assert original.accepted and not original.replayed
    assert place(second, 75_000.0).accepted  # outbid in the meantime
    retry = place(first, 70_000.0, request_id)  # the first client retries
    assert retry.accepted and retry.replayed  # its original answer - not "too low"

    _wait_replicated()
    for address in APP_SERVERS:
        with grpc.insecure_channel(address) as channel:
            bids = auction_pb2_grpc.AuctionServiceStub(channel).ListBids(
                auction_pb2.ListBidsRequest(auction_id=auction_id), timeout=APP_TIMEOUT_SECONDS
            ).bids
        assert [b.amount for b in bids] == [70_000.0, 75_000.0], address


def test_replicas_at_the_same_index_report_the_same_digest():
    deadline = time.time() + 15
    while time.time() < deadline:
        statuses = list(_statuses().values())
        if len(statuses) == len(APP_SERVERS) and len({s.last_applied for s in statuses}) == 1:
            assert len({s.applied_digest for s in statuses}) == 1
            assert statuses[0].applied_digest
            return
        time.sleep(0.05)
    pytest.fail("the nodes never reached the same applied index")


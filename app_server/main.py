"""An app server: one member of the Raft cluster plus the auction service.

    config.py         every setting, from environment variables
    raft/             the consensus algorithm (storage, node, transport)
    state_machine.py  what a committed log entry does to the auction data
    service.py        the gRPC AuctionService - leader-only writes, reads anywhere
    payment_service.py  the mock PaymentService - wallets, deposits, escrow
    business_api.py   the assignment's post/get, routed by processBusinessRequest
    auction_timer.py  closing, settling and recapping auctions, from the leader
    main.py           this file: wires them together and starts the node
"""
import asyncio
import logging
import os

import grpc

from config import (
    DATA_DIR, ELECTION_TIMEOUT, HEARTBEAT_SECONDS, LLM_SERVER, LLM_TIMEOUT_SECONDS, NODE_ID, PEERS, PORT, PRE_VOTE,
)

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s")

import auction_pb2_grpc  # noqa: E402  (imported after logging is configured)
import raft_pb2_grpc  # noqa: E402
import state_machine  # noqa: E402
from auction_timer import run_auction_timer  # noqa: E402
from business_api import BusinessApi  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from payment_service import PaymentServiceServicer  # noqa: E402
from raft import RaftNode, RaftStorage  # noqa: E402
from raft.transport import GrpcTransport, RaftServicer  # noqa: E402
from service import AuctionServiceServicer  # noqa: E402
from store import database  # noqa: E402

log = logging.getLogger(__name__)


async def serve():
    """Start the Raft node, the gRPC server (auction, payment and Raft
    services on one port) and the auction timer."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # The auction database is a cache of the replicated log: start it empty and
    # let Raft re-apply every committed entry into it. The log itself (raft.db)
    # is what survives a restart.
    database.DB_PATH = os.path.join(DATA_DIR, "auctions.db")
    database.DURABLE = False
    database.reset_database()

    storage = RaftStorage(os.path.join(DATA_DIR, "raft.db"))
    transport = GrpcTransport(PEERS)
    node = RaftNode(
        NODE_ID, list(PEERS), storage, transport, state_machine.apply,
        election_timeout=ELECTION_TIMEOUT, heartbeat_interval=HEARTBEAT_SECONDS, pre_vote=PRE_VOTE,
    )

    llm = LLMClient(LLM_SERVER, timeout_seconds=LLM_TIMEOUT_SECONDS) if LLM_SERVER else None
    log.info("LLM node: %s", LLM_SERVER or "not configured (assistant features use plain-text fallbacks)")

    server = grpc.aio.server()
    auctions = AuctionServiceServicer(node, llm)
    payments = PaymentServiceServicer(node)
    auctions.business = BusinessApi(auctions, payments)
    auction_pb2_grpc.add_AuctionServiceServicer_to_server(auctions, server)
    auction_pb2_grpc.add_PaymentServiceServicer_to_server(payments, server)
    raft_pb2_grpc.add_RaftServiceServicer_to_server(RaftServicer(node), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("app server starting on port %s (peers: %s)", PORT, PEERS or "none - single-node cluster")
    await server.start()

    node.start()
    timer_task = asyncio.create_task(run_auction_timer(node, llm))
    try:
        await server.wait_for_termination()
    finally:
        timer_task.cancel()
        await node.stop()
        await transport.close()
        storage.close()
        if llm is not None:
            await llm.close()


if __name__ == "__main__":
    asyncio.run(serve())

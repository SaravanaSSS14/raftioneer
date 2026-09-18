import asyncio
import logging
import os

import grpc

from generated import auction_pb2
from generated import auction_pb2_grpc

NODE_ID = os.environ.get("NODE_ID", "client")
APP_SERVERS = [s for s in os.environ.get("APP_SERVERS", "").split(",") if s]
POLL_INTERVAL_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)


async def ping(address: str) -> None:
    """Week 1 connectivity check: calls the real AuctionService stub on
    each app server so 'services can reach each other' is proven with
    an actual gRPC call, not a raw socket ping."""
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = auction_pb2_grpc.AuctionServiceStub(channel)
            await stub.ListAuctions(auction_pb2.ListAuctionsRequest(), timeout=3)
            log.info("reachable: %s", address)
    except grpc.aio.AioRpcError as exc:
        log.warning("unreachable: %s (%s)", address, exc.code())


async def main():
    if not APP_SERVERS:
        log.warning("APP_SERVERS is empty; nothing to ping")
    while True:
        await asyncio.gather(*(ping(addr) for addr in APP_SERVERS))
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())

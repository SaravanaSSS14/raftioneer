import asyncio
import logging
import os

import grpc

import auction_pb2
import auction_pb2_grpc

NODE_ID = os.environ.get("NODE_ID", "app-server-unknown")
PORT = os.environ.get("PORT", "50051")

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)


class AuctionServiceServicer(auction_pb2_grpc.AuctionServiceServicer):
    """Week 1 skeleton: proves the frozen .proto contract is wired up
    end-to-end. No auth, no Raft, no business logic yet — all of that
    lands in Weeks 2-6 per the project reference doc."""

    async def Login(self, request, context):
        log.info("Login(username=%s) [stub]", request.username)
        return auction_pb2.LoginResponse(success=False, error_message="not implemented yet")

    async def Logout(self, request, context):
        log.info("Logout [stub]")
        return auction_pb2.LogoutResponse(success=True)

    async def ListAuctions(self, request, context):
        log.info("ListAuctions [stub]")
        return auction_pb2.ListAuctionsResponse(auctions=[])

    async def GetAuction(self, request, context):
        log.info("GetAuction(auction_id=%s) [stub]", request.auction_id)
        return auction_pb2.GetAuctionResponse(found=False)

    async def PlaceBid(self, request, context):
        log.info("PlaceBid(auction_id=%s, amount=%s) [stub]", request.auction_id, request.amount)
        return auction_pb2.PlaceBidResponse(accepted=False, error_message="not implemented yet")


async def serve():
    server = grpc.aio.server()
    auction_pb2_grpc.add_AuctionServiceServicer_to_server(AuctionServiceServicer(), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("app server starting on port %s", PORT)
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())

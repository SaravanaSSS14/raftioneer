#!/usr/bin/env python3
"""Concurrency stress test: many bidders hammer ONE auction on ONE app server.

The M1 requirement is that simultaneous bids on the same item never produce a
wrong result. After the storm, checked from the outside using only public RPCs:

  * The auction's final highest bid must equal the highest bid the server
    accepted - a lower bid must never overwrite a higher one, and an accepted
    bid must never be lost.

The count of accepted bids is reported too. With random amounts it stays small
(roughly ln(n)): a bid is only accepted if it beats the current leader, and
most arrive after a higher one has already landed.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/stress_bids.py                       # 20 bidders, 400 bids
    python scripts/stress_bids.py --server localhost:50052 --bidders 50 --bids 1000
"""
import argparse
import asyncio
import random
import sys

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc


async def run(server: str, bidders: int, bids: int) -> int:
    async with grpc.aio.insecure_channel(server) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)

        seller = await stub.Login(auction_pb2.LoginRequest(username="stress_seller", password="stress123"))
        created = await stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller.session_token, title="Stress-test item",
                description="Created by scripts/stress_bids.py", duration_seconds=600, reserve_price=0.0,
            )
        )
        if not created.success:
            print(f"could not create the test auction: {created.error_message}")
            return 1
        auction_id = created.auction_id

        tokens = []
        for i in range(bidders):
            login = await stub.Login(
                auction_pb2.LoginRequest(username=f"stress_bidder_{i}", password="stress123")
            )
            tokens.append(login.session_token)

        async def bid(amount: float):
            resp = await stub.PlaceBid(
                auction_pb2.PlaceBidRequest(
                    session_token=random.choice(tokens), auction_id=auction_id, amount=amount
                )
            )
            return amount, resp.accepted

        print(f"{bidders} bidders, {bids} simultaneous bids on auction {auction_id} @ {server} ...")
        results = await asyncio.gather(*(bid(round(random.uniform(1, 1000), 2)) for _ in range(bids)))

        accepted = [amount for amount, ok in results if ok]
        final = (await stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=auction_id))).auction.current_highest_bid

    print(f"accepted {len(accepted)} of {len(results)} bids")
    print(f"highest accepted bid: {max(accepted):.2f}")
    print(f"auction's final highest bid: {final:.2f}")
    if abs(final - max(accepted)) < 1e-9:
        print("PASS: the final state matches the highest accepted bid - no lost or overwritten bids")
        return 0
    print("FAIL: final highest bid differs from the highest accepted bid")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default="localhost:50051")
    parser.add_argument("--bidders", type=int, default=20)
    parser.add_argument("--bids", type=int, default=400)
    args = parser.parse_args()
    return asyncio.run(run(args.server, args.bidders, args.bids))


if __name__ == "__main__":
    raise SystemExit(main())

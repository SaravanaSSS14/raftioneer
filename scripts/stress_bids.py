#!/usr/bin/env python3
"""Concurrency stress test: many bidders hammer ONE auction through the Raft leader.

Simultaneous bids on the same item must never produce a wrong result, and
every replica must agree on it. After the storm, checked from the outside
using only public RPCs:

  * The auction's final highest bid must equal the highest bid the leader
    accepted - a lower bid must never overwrite a higher one, and an accepted
    bid must never be lost.
  * Every app server must report that same final state once it has caught
    up with the leader's log.

The count of accepted bids is reported too. With random amounts it stays small
(roughly ln(n)): a bid is only accepted if it beats the current highest bid,
and most arrive after a higher one has already landed.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/stress_bids.py                       # 20 bidders, 400 bids, via the leader
    python scripts/stress_bids.py --bidders 50 --bids 1000
"""
import argparse
import asyncio
import collections
import random
import sys

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS, find_leader, wait_until_replicated
import narrate


async def run(server: str, replicas: list[str], bidders: int, bids: int) -> int:
    async with grpc.aio.insecure_channel(server) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)

        seller = await stub.Login(auction_pb2.LoginRequest(username="stress_seller", password="stress123"))
        created = await stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller.session_token, title="Stress-test item",
                # long enough for the storm; short enough not to linger on the board afterwards
                description="Created by scripts/stress_bids.py", duration_seconds=60, reserve_price=0.0,
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
            return amount, resp.accepted, resp.error_message

        print(f"{bidders} bidders, {bids} simultaneous bids on auction {auction_id} @ leader {server} ...")
        # Amounts above the client node's budgets, so its personas don't join in.
        results = await asyncio.gather(*(bid(round(random.uniform(10_000, 20_000), 2)) for _ in range(bids)))

        accepted = [amount for amount, ok, _ in results if ok]
        final = (await stub.GetAuction(auction_pb2.GetAuctionRequest(auction_id=auction_id))).auction.current_highest_bid

    # Rejections should all be "not higher than the current bid"; anything
    # else (a timeout, a lost leader) means the cluster struggled.
    reasons = collections.Counter(message.split(" (")[0] for _, ok, message in results if not ok)
    print(f"accepted {len(accepted)} of {len(results)} bids")
    for reason, count in reasons.most_common():
        print(f"  rejected x{count}: {reason}")
    print(f"highest accepted bid: {max(accepted):.2f}")
    print(f"auction's final highest bid on the leader: {final:.2f}")
    ok = abs(final - max(accepted)) < 1e-9

    replicated = wait_until_replicated(replicas)
    for address in replicas:
        try:
            async with grpc.aio.insecure_channel(address) as channel:
                got = await auction_pb2_grpc.AuctionServiceStub(channel).GetAuction(
                    auction_pb2.GetAuctionRequest(auction_id=auction_id), timeout=3
                )
            same = got.found and abs(got.auction.current_highest_bid - final) < 1e-9
            print(f"  {address}: {got.auction.current_highest_bid:.2f} {'(same)' if same else '(DIFFERENT)'}")
            ok = ok and same
        except grpc.aio.AioRpcError as exc:
            print(f"  {address}: unreachable ({exc.code()})")
    if not replicated:
        print("  (not every app server caught up within 10s)")

    if ok:
        narrate.verdict(True, "final state matches the highest accepted bid on every replica - no lost or overwritten bids")
        narrate.aside("stress", bids=bids)
        return 0
    narrate.verdict(False, "a replica's highest bid differs from the highest accepted bid")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", default=",".join(DEFAULT_SERVERS), help="comma-separated app-server addresses")
    parser.add_argument("--bidders", type=int, default=20)
    parser.add_argument("--bids", type=int, default=400)
    args = parser.parse_args()
    replicas = [s for s in args.servers.split(",") if s]
    return asyncio.run(run(find_leader(replicas), replicas, args.bidders, args.bids))


if __name__ == "__main__":
    raise SystemExit(main())

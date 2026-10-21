#!/usr/bin/env python3
"""Write benchmark: commit latency and throughput through the Raft leader.

The numbers in the README's Performance section come from this script, run
against the Docker deployment before and after group commit.

  1. Latency: bids placed one at a time, each higher than the last, so every
     one is accepted. Each waits for a full commit, which includes the
     leader's disk write, one follower's disk write and the round trip
     between them.
  2. Throughput: bids with random amounts from 10, 50 and 200 concurrent
     clients. Most are refused as too low, but a refused bid is still a log
     entry - it is judged when applied - so this measures committed entries
     per second.

Usage (stack up, ./scripts/generate_protos.sh already run):
    python scripts/benchmark.py
    python scripts/benchmark.py --sequential 100 --bids 500 --concurrency 10 200

It creates one auction that closes two minutes later at a price far above the
personas' budgets; restart the stack (down, then up) before a demo where the
pricing trend in recaps matters.
"""
import argparse
import asyncio
import random
import statistics
import sys
import time

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc
from cluster import DEFAULT_SERVERS, find_leader, statuses


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


async def run(server: str, sequential: int, bids: int, levels: list[int]) -> int:
    async with grpc.aio.insecure_channel(server) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)
        seller = await stub.Login(auction_pb2.LoginRequest(username="bench_seller", password="bench12345"))
        created = await stub.CreateAuction(
            auction_pb2.CreateAuctionRequest(
                session_token=seller.session_token, title="Benchmark item",
                description="Created by scripts/benchmark.py", duration_seconds=120,
            )
        )
        if not created.success:
            print(f"could not create the benchmark auction: {created.error_message}")
            return 1
        tokens = []
        for i in range(20):
            login = await stub.Login(auction_pb2.LoginRequest(username=f"bench_{i}", password="bench12345"))
            tokens.append(login.session_token)

        async def bid(token: str, amount: float) -> float:
            started = time.perf_counter()
            await stub.PlaceBid(
                auction_pb2.PlaceBidRequest(session_token=token, auction_id=created.auction_id, amount=amount),
                timeout=10,
            )
            return (time.perf_counter() - started) * 1000

        amount = 1_000_000.0  # far above the personas' budgets, so they leave this auction alone
        latencies = []
        for _ in range(sequential):
            amount += 1
            latencies.append(await bid(tokens[0], amount))
        print(f"one at a time, {sequential} accepted bids: p50 {percentile(latencies, .5):.1f} ms, "
              f"p95 {percentile(latencies, .95):.1f} ms, p99 {percentile(latencies, .99):.1f} ms "
              f"({1000 / statistics.mean(latencies):.0f} bids/s)")

        for level in levels:
            gate = asyncio.Semaphore(level)
            latencies = []

            async def one() -> None:
                async with gate:
                    latencies.append(await bid(random.choice(tokens), random.uniform(1, 2_000_000)))

            started = time.perf_counter()
            await asyncio.gather(*(one() for _ in range(bids)))
            elapsed = time.perf_counter() - started
            print(f"{level:>4} concurrent: {bids / elapsed:6.0f} log entries/s, "
                  f"latency p50 {percentile(latencies, .5):6.1f} ms, p99 {percentile(latencies, .99):6.1f} ms")

    status = next(s for _, s in statuses(DEFAULT_SERVERS) if s)
    print(f"log length now: {status.last_log_index} entries")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sequential", type=int, default=300, help="bids placed one at a time")
    parser.add_argument("--bids", type=int, default=1500, help="bids per concurrency level")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[10, 50, 200])
    args = parser.parse_args()
    leader = find_leader(DEFAULT_SERVERS)
    print(f"leader: {leader}")
    return asyncio.run(run(leader, args.sequential, args.bids, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())

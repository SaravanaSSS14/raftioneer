#!/usr/bin/env python3
"""Seeds the running stack with real, specific demo data instead of an
empty system: nothing creates an auction on its own - CreateAuction
exists, but nothing calls it automatically. This is that one-off call.

Uses only the AuctionService contract (Login auto-registers a seller on
first use, then CreateAuction) - no new RPCs, no schema changes.

Each app-server replica has its own separate SQLite file (replication
between replicas is Milestone 2), so this seeds every replica in
APP_SERVERS identically - otherwise only whichever replica you happened
to call would ever show these auctions in ListAuctions.

Usage (with the stack already up):
    source venv/bin/activate
    ./scripts/generate_protos.sh
    python scripts/seed_demo_data.py
"""
import sys

import grpc

sys.path.insert(0, "generated")

import auction_pb2
import auction_pb2_grpc

APP_SERVERS = ["localhost:50051", "localhost:50052", "localhost:50053"]
TIMEOUT_SECONDS = 3

# A campus clearance auction: end-of-semester hostel move-out sales.
# Each item is listed by a different seller persona (auto-registered
# on first login, same as any real user) so the demo doesn't look
# like one account listing everything.
ITEMS = [
    dict(
        seller="priya_hostel4",
        password="priya123",
        title="Hero Sprint 27T cycle - monsoon tested",
        description="Daily-driver cycle for the last 2 years. Gears shift clean, "
                     "seat height adjustable, new tube fitted last month. Selling "
                     "because I'm off-campus next sem.",
        duration_seconds=180,
        reserve_price=800.0,
    ),
    dict(
        seller="arjun_hostel7",
        password="arjun123",
        title="Mini fridge - survived 3 finals weeks",
        description="Compact enough for a single room, quiet enough to sleep next "
                     "to. Perfect for late-night Maggi and cold coffee stockpiles.",
        duration_seconds=240,
        reserve_price=500.0,
    ),
    dict(
        seller="neha_ug3",
        password="neha123",
        title="Semester 1 core textbook bundle (Maths, Physics, CS)",
        description="All three, no torn pages, some highlighting in the CS one "
                     "(the useful kind). Would've saved me a week of scrambling.",
        duration_seconds=150,
        reserve_price=0.0,
    ),
    dict(
        seller="rohan_hostel2",
        password="rohan123",
        title="Yamaha F310 acoustic guitar",
        description="Strings changed last month, no dead frets. Learned enough to "
                     "regret not learning more. Case included.",
        duration_seconds=300,
        reserve_price=1500.0,
    ),
    dict(
        seller="sara_ug2",
        password="sara123",
        title="Table fan - beat the hostel summer heat",
        description="3-speed, oscillates, been running every night since August "
                     "without a single complaint.",
        duration_seconds=120,
        reserve_price=0.0,
    ),
    dict(
        seller="vikram_hostel9",
        password="vikram123",
        title="OASIS fest crew hoodie, size L",
        description="Worn exactly once, on the last day of fest when it actually "
                     "got cold enough to need it.",
        duration_seconds=200,
        reserve_price=0.0,
    ),
]


def seed_on(address: str) -> int:
    created = 0
    with grpc.insecure_channel(address) as channel:
        stub = auction_pb2_grpc.AuctionServiceStub(channel)
        for item in ITEMS:
            login = stub.Login(
                auction_pb2.LoginRequest(username=item["seller"], password=item["password"]),
                timeout=TIMEOUT_SECONDS,
            )
            if not login.success:
                print(f"  ! {address}: login failed for {item['seller']} ({login.error_message})")
                continue

            resp = stub.CreateAuction(
                auction_pb2.CreateAuctionRequest(
                    session_token=login.session_token,
                    title=item["title"],
                    description=item["description"],
                    duration_seconds=item["duration_seconds"],
                    reserve_price=item["reserve_price"],
                ),
                timeout=TIMEOUT_SECONDS,
            )
            if resp.success:
                print(f"  + {address}: [{resp.auction_id}] {item['title']} (by {item['seller']})")
                created += 1
            else:
                print(f"  ! {address}: CreateAuction failed for {item['title']!r} ({resp.error_message})")
    return created


def main() -> int:
    total = 0
    for address in APP_SERVERS:
        try:
            print(f"Seeding {address} ...")
            total += seed_on(address)
        except grpc.RpcError as exc:
            print(f"  ! {address}: unreachable ({exc.code()})")
    print(f"\n{total} auction(s) created across {len(APP_SERVERS)} replica(s).")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())

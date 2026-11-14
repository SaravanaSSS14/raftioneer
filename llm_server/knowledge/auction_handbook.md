# Raftioneer Auction House Handbook

The house rules of the Raftioneer campus clearance auction - the end-of-semester
sale where students bid on things leaving the hostels. The LLM node retrieves
the sections relevant to each question and hands them to the model with the
question, so its answers follow these rules rather than general knowledge.
Each "## " heading starts one section. Keep every statement here true to the
code: the figures are the defaults in app_server/config.py and assistant.py.

## Creating an account and signing in
Register once with a username and a password, then sign in. A username is 3 to
32 letters, digits, underscores, hyphens or dots; a password is at least 6
characters. Signing in gives you a session that lasts one hour; signing in
again starts a new session and ends the old one. Signing in never creates an
account - if the name is unknown, register first.

## Your wallet and the welcome credit
Every new account gets a wallet that opens with a welcome credit of Rs 5,000.
All money in Raftioneer is mock money: no real payment is ever taken. You can
top up your wallet with a deposit of up to Rs 1,00,00,000 at a time. The wallet
shows your balance, the money promised to auctions you are winning, the money
available for new bids, and the money held in escrow for auctions you have won.

## How much you can bid (available funds)
A bid needs enough available funds to cover the bid plus the 5% buyer's premium.
Available funds are your wallet balance minus everything you have already
promised to auctions you are currently winning (each winning bid plus its
premium). If someone outbids you, that promise is released and the money is
available again. Raising your own winning bid replaces your earlier promise on
that auction rather than adding to it. A bid you cannot afford is refused with
an "Insufficient funds" message that says how much it needs.

## Placing a bid and what a valid bid is
A bid must be higher than the current highest bid. If the seller set a reserve
price, the bid must also be at least the reserve. Sellers cannot bid on their
own auctions. The amount must be a positive number. Bids are judged in the order
the cluster commits them, so when two people bid at the same moment, one of
them is first and the other is judged against it. An accepted bid cannot be
withdrawn.

## Reserve price
The reserve is the lowest price the seller will accept. The first bid must be
at least the reserve; every bid after that only needs to beat the highest bid.
An auction with no reserve accepts any first bid above Rs 0.

## Anti-sniping: why the end time can move
A bid placed in the last 10 seconds of an auction pushes the end time to 10
seconds after that bid, so other bidders always get a fair chance to answer a
last-second bid. The extensions stop 60 seconds after the original end time,
which is a hard end. The current end time is always shown on the auction.

## When an auction ends and who wins
An auction closes automatically at its end time. The highest bid at that moment
wins. If nobody bid, or no bid met the reserve, the auction closes with no sale
and nobody pays anything.

## Buyer's premium
The winner pays the winning bid plus a 5% buyer's premium. The premium is the
auction house's fee; the seller receives the winning bid.

## Escrow: paying and receiving the item
When an auction closes with a winner, the winning bid plus the premium moves
out of the winner's wallet into escrow, and the settlement shows HELD. Meet the
seller on campus, check the item, and then press Confirm receipt: the escrow is
released, the seller is paid the winning bid, and the settlement shows
RELEASED. Only the winner can confirm receipt, and only once. Do not confirm
before you have the item.

## Selling an item
Any signed-in user can list an item for sale - a cycle, a cooler, books, a
mattress or anything else leaving the hostel. Give a title, an optional reserve price
and a duration (one hour if none is given, at most 30 days). Leave the
description blank and Tapri writes one from the title. The auction starts
immediately. You cannot bid on your own listing.

## After the auction: results and recaps
Every closed auction has a result: the winner, the final price, the buyer's
premium and the escrow status. Tapri also writes a short recap that compares
the sale with the average price of earlier sales.

## If a server fails: is my bid safe?
Raftioneer runs on three replicated servers that agree on every change with
the Raft consensus algorithm. One of them is the leader and takes all bids;
if it crashes, the other two elect a new leader within about a second or two.
A bid that was accepted is never lost. If the cluster could not confirm a bid
in time, the app says so; retrying is safe, because every bid carries a request
id and a retried request is applied at most once.

## Retrying a request
The app tags each bid and deposit with a request id. If it sends the same
request again - after a timeout or a change of leader - the cluster returns
the original answer instead of applying it twice. So a retry never places a
second bid or deposits twice.

## Asking Tapri
Tapri is the assistant. It answers questions about the house rules and about
a specific auction when you ask from that auction's page. Its facts about an
auction come from the replicated auction data, and a fine-print line with the
next valid bid, the bid count and the time left is added under every answer
about an auction. If the assistant is offline, the app answers with the plain
facts instead, and bidding is never affected.

## Hostel hand-over etiquette
Agree a time and a public place on campus for the hand-over. Inspect the item
before confirming receipt. Bring the item in the condition the listing
describes. Cycles, mattresses, coolers and other bulky items usually need a
helper - plan for it before you bid.

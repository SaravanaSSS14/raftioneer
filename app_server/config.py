"""Configuration of an app server, all from environment variables (set per
node in deploy/docker-compose.yml), so the same code runs as any node."""
import os


def parse_peers(value: str) -> dict[str, str]:
    """"app-server-2=app_server_2:50051,..." -> {"app-server-2": "app_server_2:50051", ...}"""
    peers = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        node_id, _, address = item.partition("=")
        peers[node_id] = address or node_id
    return peers


NODE_ID = os.environ.get("NODE_ID", "app-server-unknown")
PORT = os.environ.get("PORT", "50051")

# The other app servers in the Raft cluster. Raft RPCs share the client port.
PEERS = parse_peers(os.environ.get("PEERS", ""))
# How other nodes and clients reach this one; followers hand it out as the
# leader_hint when this node is the leader.
SELF_ADDRESS = os.environ.get("SELF_ADDRESS", f"localhost:{PORT}")
ADDRESSES = {NODE_ID: SELF_ADDRESS, **PEERS}

# Where this node keeps its Raft log and its copy of the auction database.
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", NODE_ID))

# Raft timing. A follower that hears nothing from a leader for a random time
# in [min, max] starts an election; the leader sends heartbeats well inside
# the minimum, so a healthy leader is never suspected.
ELECTION_TIMEOUT = (
    float(os.environ.get("RAFT_ELECTION_TIMEOUT_MIN", "0.8")),
    float(os.environ.get("RAFT_ELECTION_TIMEOUT_MAX", "1.6")),
)
HEARTBEAT_SECONDS = float(os.environ.get("RAFT_HEARTBEAT_SECONDS", "0.2"))
# Pre-vote (thesis section 9.6): a node checks it could win before starting
# an election, so a frozen, cut-off or restarted node can't depose a healthy
# leader. RAFT_PRE_VOTE=0 turns it off (to compare).
PRE_VOTE = os.environ.get("RAFT_PRE_VOTE", "1") != "0"
# How long a write waits to be committed before the client is told it failed.
COMMIT_TIMEOUT_SECONDS = float(os.environ.get("RAFT_COMMIT_TIMEOUT_SECONDS", "2.0"))

# Anti-sniping, applied to auctions as they are listed: a bid in the last
# WINDOW seconds pushes the end to WINDOW seconds after that bid, never more
# than MAX_EXTENSION seconds past the original end. WINDOW=0 turns it off.
ANTI_SNIPE_WINDOW_SECONDS = int(os.environ.get("ANTI_SNIPE_WINDOW_SECONDS", "10"))
ANTI_SNIPE_MAX_EXTENSION_SECONDS = int(os.environ.get("ANTI_SNIPE_MAX_EXTENSION_SECONDS", "60"))

MAX_REQUEST_ID_LENGTH = 64

# Accounts. Usernames are 3-32 letters, digits, '_', '-' or '.'; passwords at
# least 6 characters.
USERNAME_PATTERN = r"[A-Za-z0-9_.-]{3,32}"
MIN_PASSWORD_LENGTH = 6

# Mock payments: every new account's wallet opens with this credit, and one
# deposit can add at most MAX_DEPOSIT. Like the buyer's premium, the leader
# puts the value into the command, so every node credits the same amount.
WELCOME_CREDIT = float(os.environ.get("WELCOME_CREDIT", "5000"))
MAX_DEPOSIT = 10_000_000.0

# How many of its newest log entries each node reports in GetClusterStatus
# (drawn by the demo UI).
STATUS_LOG_TAIL = 14

# The LLM node is optional infrastructure: if it isn't configured, or is slow
# or down, every LLM-backed feature falls back to plain text and bidding is
# unaffected. The timeout is generous because the model runs on CPU.
LLM_SERVER = os.environ.get("LLM_SERVER", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

MAX_AUCTION_SECONDS = 30 * 24 * 3600

COMMIT_TIMEOUT_MESSAGE = (
    "The cluster could not confirm this in time (is a majority of servers down?). "
    "It may still go through - check before retrying."
)

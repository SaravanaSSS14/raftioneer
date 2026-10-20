"""Raft consensus, written from the paper (Ongaro & Ousterhout, "In Search of
an Understandable Consensus Algorithm", Figure 2).

storage.py    the persistent part of a node: current term, vote, log (SQLite)
node.py       the algorithm: leader election, log replication, commit, apply
transport.py  gRPC between nodes (tests swap in an in-process transport)
"""
from .node import NotLeader, ProposalTimeout, RaftNode, Role
from .storage import RaftStorage

__all__ = ["NotLeader", "ProposalTimeout", "RaftNode", "RaftStorage", "Role"]

import asyncio

import grpc

import raft_pb2_grpc

# Reconnect quickly after a peer comes back: gRPC's default backoff grows to
# two minutes, during which a restarted node would hear nothing from the leader
# and keep starting needless elections.
_CHANNEL_OPTIONS = [
    ("grpc.initial_reconnect_backoff_ms", 100),
    ("grpc.min_reconnect_backoff_ms", 100),
    ("grpc.max_reconnect_backoff_ms", 1000),
]


class GrpcTransport:
    """Sends RequestVote / AppendEntries to peers over gRPC.

    `peers` maps node id -> "host:port". A failed call simply raises, which
    the Raft node treats as "no reply" - the same as a lost message, which the
    algorithm already tolerates. After a failure the channel is rebuilt, so a
    peer that restarted (possibly at a new container IP) is found again.
    """

    def __init__(self, peers: dict[str, str], rpc_timeout: float = 0.5):
        self.peers = peers
        self.rpc_timeout = rpc_timeout
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._stubs: dict[str, raft_pb2_grpc.RaftServiceStub] = {}

    def _stub(self, peer_id: str) -> raft_pb2_grpc.RaftServiceStub:
        if peer_id not in self._stubs:
            channel = grpc.aio.insecure_channel(self.peers[peer_id], options=_CHANNEL_OPTIONS)
            self._channels[peer_id] = channel
            self._stubs[peer_id] = raft_pb2_grpc.RaftServiceStub(channel)
        return self._stubs[peer_id]

    def _drop(self, peer_id: str) -> None:
        self._stubs.pop(peer_id, None)
        channel = self._channels.pop(peer_id, None)
        if channel is not None:
            asyncio.ensure_future(channel.close())

    async def _call(self, peer_id: str, method: str, request):
        try:
            return await getattr(self._stub(peer_id), method)(request, timeout=self.rpc_timeout)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.UNAVAILABLE:
                self._drop(peer_id)
            raise

    async def request_vote(self, peer_id: str, request):
        return await self._call(peer_id, "RequestVote", request)

    async def append_entries(self, peer_id: str, request):
        return await self._call(peer_id, "AppendEntries", request)

    async def close(self) -> None:
        for channel in self._channels.values():
            await channel.close()
        self._channels, self._stubs = {}, {}


class RaftServicer(raft_pb2_grpc.RaftServiceServicer):
    """Receives peers' RPCs and hands them to the local Raft node."""

    def __init__(self, node):
        self.node = node

    async def RequestVote(self, request, context):
        return self.node.handle_request_vote(request)

    async def AppendEntries(self, request, context):
        return self.node.handle_append_entries(request)

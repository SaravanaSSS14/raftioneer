"""The assignment's generic client API: post(token, type, data) and
get(token, type, params), served by processBusinessRequest(requestId,
payload, context).

This is a front door, not a second implementation. Each request is routed
by its `type` to the typed RPC that already does the job (PlaceBid,
CreateAuction, GetWallet, ...), so every rule - leader-only writes through
Raft, the bid rules, the funds check, exactly-once request ids, the LLM
fallbacks - applies exactly as it does to the typed calls. The typed reply
comes back as JSON in the generic reply.

    post types:  bid, auction, deposit, confirm_receipt, ask
    get types:   auctions, auction, bids, summary, wallet, transactions, cluster
"""
import json
import logging
import uuid

from google.protobuf import json_format

import auction_pb2

log = logging.getLogger(__name__)

# PostResponse / GetResponse status values
OK, REJECTED, NOT_LEADER, UNAUTHENTICATED, INVALID, NOT_FOUND, ERROR = (
    "OK", "REJECTED", "NOT_LEADER", "UNAUTHENTICATED", "INVALID", "NOT_FOUND", "ERROR",
)


def to_dict(message) -> dict:
    return json_format.MessageToDict(
        message, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )


class BadRequest(ValueError):
    """The type or the data/params of a post/get don't make sense."""


def _need(data: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in data or data[k] in ("", None)]
    if missing:
        raise BadRequest(f"missing {', '.join(missing)}")


class BusinessApi:
    def __init__(self, auction, payments):
        self.auction = auction    # AuctionServiceServicer
        self.payments = payments  # PaymentServiceServicer

    # ---- the two RPCs ----

    async def post(self, request: auction_pb2.PostRequest, context) -> auction_pb2.PostResponse:
        try:
            data = json.loads(request.data) if request.data.strip() else {}
            if not isinstance(data, dict):
                raise BadRequest("data must be a JSON object")
        except (ValueError, BadRequest) as exc:
            return auction_pb2.PostResponse(status=INVALID, message=f"data is not a JSON object: {exc}")
        result = await self.process_business_request(
            request.request_id, {"kind": "post", "type": request.type, "data": data}, {"token": request.token}
        )
        return auction_pb2.PostResponse(**result)

    async def get(self, request: auction_pb2.GetRequest, context) -> auction_pb2.GetResponse:
        result = await self.process_business_request(
            "", {"kind": "get", "type": request.type, "params": dict(request.params)}, {"token": request.token}
        )
        return auction_pb2.GetResponse(
            status=result["status"], message=result.get("message", ""),
            items=[auction_pb2.DataItem(id=i, data=json.dumps(d)) for i, d in result.get("items", [])],
        )

    # ---- processBusinessRequest(requestId, payload, context) ----

    async def process_business_request(self, request_id: str, payload: dict, context: dict) -> dict:
        """Handle one domain operation. payload = {"kind": "post"|"get",
        "type": ..., "data"|"params": {...}}; context = {"token": ...}.
        Writes reach Raft (through the typed RPCs), "ask" reaches the LLM node."""
        kind, kind_type = payload["kind"], payload["type"]
        handlers = self._POSTS if kind == "post" else self._GETS
        handler = handlers.get(kind_type)
        if handler is None:
            return {"status": INVALID, "message": f"unknown {kind} type {kind_type!r} - one of {', '.join(handlers)}"}
        log.info("processBusinessRequest %s %s/%s", request_id or "-", kind, kind_type)
        try:
            return await handler(self, request_id, payload.get("data") or payload.get("params") or {}, context["token"])
        except (BadRequest, ValueError, TypeError) as exc:
            return {"status": INVALID, "message": str(exc)}
        except Exception:
            log.exception("processBusinessRequest failed: %s/%s", kind, kind_type)
            return {"status": ERROR, "message": "Request failed, please try again"}

    # ---- posts: route to the typed write RPCs ----

    @staticmethod
    def _post_reply(reply, ok: bool, error: str, leader_hint: str = "", new_id: str = "") -> dict:
        if ok:
            status = OK
        elif leader_hint:
            status = NOT_LEADER
        elif "session" in error.lower():
            status = UNAUTHENTICATED
        else:
            status = REJECTED
        return {"status": status, "message": error, "id": new_id, "leader_hint": leader_hint,
                "data": json.dumps(to_dict(reply))}

    async def _post_bid(self, request_id, data, token):
        _need(data, "auction_id", "amount")
        reply = await self.auction.PlaceBid(auction_pb2.PlaceBidRequest(
            session_token=token, auction_id=str(data["auction_id"]), amount=float(data["amount"]),
            request_id=request_id,
        ), None)
        return self._post_reply(reply, reply.accepted, reply.error_message, reply.leader_hint,
                                reply.updated_auction.auction_id)

    async def _post_auction(self, request_id, data, token):
        _need(data, "title")
        reply = await self.auction.CreateAuction(auction_pb2.CreateAuctionRequest(
            session_token=token, title=str(data["title"]), description=str(data.get("description", "")),
            duration_seconds=int(data.get("duration_seconds", 0)), reserve_price=float(data.get("reserve_price", 0)),
        ), None)
        return self._post_reply(reply, reply.success, reply.error_message, reply.leader_hint, reply.auction_id)

    async def _post_deposit(self, request_id, data, token):
        _need(data, "amount")
        reply = await self.payments.Deposit(auction_pb2.DepositRequest(
            session_token=token, amount=float(data["amount"]), request_id=request_id,
        ), None)
        return self._post_reply(reply, reply.success, reply.error_message, reply.leader_hint)

    async def _post_confirm_receipt(self, request_id, data, token):
        _need(data, "auction_id")
        reply = await self.payments.ConfirmReceipt(auction_pb2.ConfirmReceiptRequest(
            session_token=token, auction_id=str(data["auction_id"]),
        ), None)
        return self._post_reply(reply, reply.success, reply.error_message, reply.leader_hint)

    async def _post_ask(self, request_id, data, token):
        _need(data, "question")
        reply = await self.auction.AskAssistant(auction_pb2.AskAssistantRequest(
            session_token=token, question=str(data["question"]), auction_id=str(data.get("auction_id", "")),
        ), None)
        return self._post_reply(reply, reply.success, reply.error_message, new_id=request_id or uuid.uuid4().hex[:12])

    _POSTS = {
        "bid": _post_bid,
        "auction": _post_auction,
        "deposit": _post_deposit,
        "confirm_receipt": _post_confirm_receipt,
        "ask": _post_ask,
    }

    # ---- gets: route to the typed read RPCs; items are (id, data) pairs ----

    async def _get_auctions(self, request_id, params, token):
        status = params.get("status", "").upper()
        if status and status not in auction_pb2.AuctionStatus.keys():
            raise BadRequest(f"unknown status {status!r}")
        reply = await self.auction.ListAuctions(auction_pb2.ListAuctionsRequest(
            status_filter=auction_pb2.AuctionStatus.Value(status) if status else 0,
        ), None)
        return {"status": OK, "items": [(a.auction_id, to_dict(a)) for a in reply.auctions]}

    async def _get_auction(self, request_id, params, token):
        _need(params, "auction_id")
        reply = await self.auction.GetAuction(auction_pb2.GetAuctionRequest(auction_id=params["auction_id"]), None)
        if not reply.found:
            return {"status": NOT_FOUND, "message": "No such auction"}
        return {"status": OK, "items": [(reply.auction.auction_id, to_dict(reply.auction))]}

    async def _get_bids(self, request_id, params, token):
        _need(params, "auction_id")
        reply = await self.auction.ListBids(auction_pb2.ListBidsRequest(auction_id=params["auction_id"]), None)
        if not reply.found:
            return {"status": NOT_FOUND, "message": "No such auction"}
        return {"status": OK, "items": [(b.bid_id, to_dict(b)) for b in reply.bids]}

    async def _get_summary(self, request_id, params, token):
        _need(params, "auction_id")
        reply = await self.auction.GetAuctionSummary(
            auction_pb2.GetAuctionSummaryRequest(auction_id=params["auction_id"]), None
        )
        if not reply.found:
            return {"status": NOT_FOUND, "message": "No such auction"}
        return {"status": OK, "items": [(params["auction_id"], to_dict(reply))]}

    async def _get_wallet(self, request_id, params, token):
        reply = await self.payments.GetWallet(auction_pb2.WalletRequest(session_token=token), None)
        if not reply.success:
            return {"status": UNAUTHENTICATED, "message": reply.error_message}
        return {"status": OK, "items": [(reply.user_id, to_dict(reply))]}

    async def _get_transactions(self, request_id, params, token):
        reply = await self.payments.ListTransactions(auction_pb2.WalletRequest(session_token=token), None)
        if not reply.success:
            return {"status": UNAUTHENTICATED, "message": reply.error_message}
        return {"status": OK, "items": [(str(t.txn_id), to_dict(t)) for t in reply.transactions]}

    async def _get_cluster(self, request_id, params, token):
        reply = await self.auction.GetClusterStatus(auction_pb2.ClusterStatusRequest(), None)
        return {"status": OK, "items": [(reply.node_id, to_dict(reply))]}

    _GETS = {
        "auctions": _get_auctions,
        "auction": _get_auction,
        "bids": _get_bids,
        "summary": _get_summary,
        "wallet": _get_wallet,
        "transactions": _get_transactions,
        "cluster": _get_cluster,
    }

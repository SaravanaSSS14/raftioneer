"""The mock payment service: wallets, deposits and the escrow hand-over.

No real money moves anywhere. A wallet is a number in the replicated state;
it changes only when a committed Raft command is applied (REGISTER opens it
with the welcome credit, DEPOSIT tops it up, CLOSE_EXPIRED moves a winner's
price + premium into escrow, CONFIRM_RECEIPT releases it to the seller), so
every node holds identical wallets. Reads are served by any node.

The funds check itself happens where the bid is judged: in the state machine,
at apply time (state_machine._place_bid).
"""
import logging
import math

import assistant
import auction_pb2
import auction_pb2_grpc
from config import COMMIT_TIMEOUT_MESSAGE, MAX_DEPOSIT, MAX_REQUEST_ID_LENGTH
from raft import NotLeader, ProposalTimeout, RaftNode
from service import RaftWriter
from store import database

log = logging.getLogger(__name__)

INVALID_SESSION = "Invalid or expired session token"


class PaymentServiceServicer(RaftWriter, auction_pb2_grpc.PaymentServiceServicer):

    def __init__(self, node: RaftNode):
        self.node = node

    # ---- Reads (any node) ----

    async def GetWallet(self, request, context):
        try:
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.WalletResponse(success=False, error_message=INVALID_SESSION)
            user_id = session["user_id"]
            balance = database.get_wallet_balance(user_id) or 0.0
            committed = database.committed_funds(user_id, assistant.BUYERS_PREMIUM_PERCENT)
            return auction_pb2.WalletResponse(
                success=True, user_id=user_id, balance=balance, committed=committed,
                available=round(balance - committed, 2), in_escrow=database.in_escrow(user_id),
            )
        except Exception:
            log.exception("GetWallet failed")
            return auction_pb2.WalletResponse(success=False, error_message="Could not read the wallet")

    async def ListTransactions(self, request, context):
        try:
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.ListTransactionsResponse(success=False, error_message=INVALID_SESSION)
            return auction_pb2.ListTransactionsResponse(
                success=True,
                transactions=[
                    auction_pb2.Transaction(
                        txn_id=t["txn_id"], kind=t["kind"], amount=float(t["amount"]),
                        auction_id=t["auction_id"] or "", balance_after=float(t["balance_after"]),
                        timestamp_ms=int(t["created_at"]),
                    )
                    for t in database.list_transactions(session["user_id"])
                ],
            )
        except Exception:
            log.exception("ListTransactions failed")
            return auction_pb2.ListTransactionsResponse(success=False, error_message="Could not read the transactions")

    # ---- Writes (leader only, through Raft) ----

    async def Deposit(self, request, context):
        """Mock top-up of the caller's wallet."""
        try:
            if not math.isfinite(request.amount) or request.amount <= 0:
                return auction_pb2.DepositResponse(success=False, error_message="Amount must be a positive number")
            if request.amount > MAX_DEPOSIT:
                return auction_pb2.DepositResponse(
                    success=False, error_message=f"One deposit can be at most {assistant.money(MAX_DEPOSIT)}"
                )
            if len(request.request_id) > MAX_REQUEST_ID_LENGTH:
                return auction_pb2.DepositResponse(success=False, error_message="request_id is too long")
            await self._leader_ready()
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.DepositResponse(success=False, error_message=INVALID_SESSION)
            result = await self._commit(
                "DEPOSIT", user_id=session["user_id"], amount=round(request.amount, 2), request_id=request.request_id
            )
            if not result["ok"]:
                return auction_pb2.DepositResponse(success=False, error_message=result["error"])
            return auction_pb2.DepositResponse(success=True, balance=result["balance"])
        except NotLeader:
            return auction_pb2.DepositResponse(
                success=False, error_message=self._not_leader_message(), leader_hint=self._leader_hint()
            )
        except ProposalTimeout:
            return auction_pb2.DepositResponse(success=False, error_message=COMMIT_TIMEOUT_MESSAGE)
        except Exception:
            log.exception("Deposit failed")
            return auction_pb2.DepositResponse(success=False, error_message="Deposit failed, please try again")

    async def ConfirmReceipt(self, request, context):
        """The winner confirms the hand-over: escrow is released, the seller paid."""
        try:
            await self._leader_ready()
            session = database.validate_session(request.session_token)
            if not session:
                return auction_pb2.ConfirmReceiptResponse(success=False, error_message=INVALID_SESSION)
            result = await self._commit("CONFIRM_RECEIPT", auction_id=request.auction_id, user_id=session["user_id"])
            if not result["ok"]:
                return auction_pb2.ConfirmReceiptResponse(success=False, error_message=result["error"])
            return auction_pb2.ConfirmReceiptResponse(success=True, escrow_status=result["escrow_status"])
        except NotLeader:
            return auction_pb2.ConfirmReceiptResponse(
                success=False, error_message=self._not_leader_message(), leader_hint=self._leader_hint()
            )
        except ProposalTimeout:
            return auction_pb2.ConfirmReceiptResponse(success=False, error_message=COMMIT_TIMEOUT_MESSAGE)
        except Exception:
            log.exception("ConfirmReceipt failed for %s", request.auction_id)
            return auction_pb2.ConfirmReceiptResponse(success=False, error_message="Could not confirm, please try again")

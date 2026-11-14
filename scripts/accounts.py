"""Signing in, for the scripts: register the account if it is new, log in,
and make sure its wallet holds at least `funds` (topping it up with a mock
deposit if not) - so a test can bid far above the welcome credit.

Run against the leader: Register, Login and Deposit are writes.
"""
import sys

sys.path.insert(0, "generated")

import auction_pb2  # noqa: E402
import auction_pb2_grpc  # noqa: E402

TAKEN = "Username already taken"


def sign_in(channel, username: str, password: str, funds: float = 0.0,
            timeout: float = 10) -> auction_pb2.LoginResponse:
    """Blocking version. Returns the LoginResponse (check .success)."""
    auctions = auction_pb2_grpc.AuctionServiceStub(channel)
    registered = auctions.Register(auction_pb2.RegisterRequest(username=username, password=password), timeout=timeout)
    if not registered.success and registered.error_message != TAKEN:
        return auction_pb2.LoginResponse(success=False, error_message=f"register: {registered.error_message}")
    login = auctions.Login(auction_pb2.LoginRequest(username=username, password=password), timeout=timeout)
    if login.success and funds > 0:
        payments = auction_pb2_grpc.PaymentServiceStub(channel)
        wallet = payments.GetWallet(auction_pb2.WalletRequest(session_token=login.session_token), timeout=timeout)
        if wallet.success and wallet.balance < funds:
            payments.Deposit(auction_pb2.DepositRequest(
                session_token=login.session_token, amount=round(funds - wallet.balance, 2)), timeout=timeout)
    return login


async def sign_in_async(channel, username: str, password: str, funds: float = 0.0,
                        timeout: float = 10) -> auction_pb2.LoginResponse:
    """grpc.aio version of sign_in."""
    auctions = auction_pb2_grpc.AuctionServiceStub(channel)
    registered = await auctions.Register(
        auction_pb2.RegisterRequest(username=username, password=password), timeout=timeout
    )
    if not registered.success and registered.error_message != TAKEN:
        return auction_pb2.LoginResponse(success=False, error_message=f"register: {registered.error_message}")
    login = await auctions.Login(auction_pb2.LoginRequest(username=username, password=password), timeout=timeout)
    if login.success and funds > 0:
        payments = auction_pb2_grpc.PaymentServiceStub(channel)
        wallet = await payments.GetWallet(auction_pb2.WalletRequest(session_token=login.session_token), timeout=timeout)
        if wallet.success and wallet.balance < funds:
            await payments.Deposit(auction_pb2.DepositRequest(
                session_token=login.session_token, amount=round(funds - wallet.balance, 2)), timeout=timeout)
    return login

"""The auction clock: closing, settling and recapping auctions - run by
whichever node is currently the Raft leader, through the replicated log."""
import asyncio
import logging

import grpc

import assistant
import state_machine
from config import COMMIT_TIMEOUT_SECONDS
from llm_client import LLMClient
from raft import NotLeader, ProposalTimeout, RaftNode
from service import now_ms
from store import database

log = logging.getLogger(__name__)


TIMER_TICK_SECONDS = 1

# Recaps are written in the background; keep a reference so a running task
# isn't garbage-collected before it finishes.
_background_tasks: set = set()


async def write_recap(node: RaftNode, llm: LLMClient | None, record: dict) -> None:
    """Write the post-auction recap - the LLM node if it answers, a templated
    one if it doesn't - and commit it through Raft so every node stores the
    same text. Runs in the background so a slow model never delays the timer."""
    auction_id = record["auction_id"]
    try:
        winner_id = record["winner_id"]
        final_price = float(record["final_price"])
        amounts = [float(b["amount"]) for b in database.get_bids_for_auction(auction_id)]
        average_price = database.get_average_sale_price(exclude_auction_id=auction_id)
        text, from_fallback = None, True

        if llm is not None:
            try:
                text = await llm.summarize_auction(auction_id, record["title"], amounts, final_price, average_price)
                from_fallback = False
            except grpc.aio.AioRpcError as exc:
                log.warning("LLM node unavailable for the recap of %s (%s) - using template", auction_id, exc.code())
            # The model writes, the code checks: a recap with the wrong number
            # of bids or the wrong price never reaches the log.
            if text is not None and not assistant.recap_matches_facts(text, len(amounts), final_price):
                log.warning(
                    "LLM recap of %s failed the fact check (%d bids, final %.2f) - using the template: %s",
                    auction_id, len(amounts), final_price, text,
                )
                text, from_fallback = None, True

        if text is None:
            winner = winner_id.removeprefix("user_") if winner_id else None
            text = assistant.fallback_summary(record["title"], amounts, final_price, winner, average_price)

        await node.propose(
            state_machine.encode(
                "SAVE_SUMMARY", auction_id=auction_id, summary=text, from_fallback=from_fallback, now_ms=now_ms()
            ),
            timeout=COMMIT_TIMEOUT_SECONDS,
        )
        log.info("recap auction=%s (%s): %s", auction_id, "template" if from_fallback else "LLM", text)
    except (NotLeader, ProposalTimeout):
        log.info("recap for %s not committed (leadership changed) - the next leader will write it", auction_id)
    except Exception:
        log.exception("could not write the recap for auction %s", auction_id)


async def run_auction_timer(node: RaftNode, llm: LLMClient | None = None):
    """The auction clock, run by whichever node is currently the leader.

    Once a second the leader checks for auctions past their end time (or
    closed but unsettled), and if there are any it proposes one CLOSE_EXPIRED
    command stamped with its clock; every node then closes and settles the
    same auctions when it applies that command. It also starts a recap for
    each settled auction that doesn't have one yet.

    Because the decision is a log entry and not local process state, a leader
    crash loses nothing: the next leader sees the same auctions still open (or
    unsettled, or un-recapped) and finishes the job.
    """
    recaps_in_progress: set[str] = set()
    while True:
        await asyncio.sleep(TIMER_TICK_SECONDS)
        if not node.is_leader:
            continue
        try:
            await node.wait_until_ready(COMMIT_TIMEOUT_SECONDS)
            now = now_ms()
            if database.has_expired_active(now) or database.get_closed_unsettled():
                command = state_machine.encode(
                    "CLOSE_EXPIRED", now_ms=now, premium_percent=assistant.BUYERS_PREMIUM_PERCENT
                )
                await node.propose(command, timeout=COMMIT_TIMEOUT_SECONDS)

            for record in database.get_unsummarized():
                auction_id = record["auction_id"]
                if auction_id in recaps_in_progress:
                    continue
                recaps_in_progress.add(auction_id)
                task = asyncio.create_task(write_recap(node, llm, record))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                task.add_done_callback(lambda _t, aid=auction_id: recaps_in_progress.discard(aid))
        except (NotLeader, ProposalTimeout):
            pass  # lost leadership or quorum mid-tick; the next leader carries on
        except Exception:
            # One bad tick must never kill the timer for the life of the process.
            log.exception("auction timer tick failed")

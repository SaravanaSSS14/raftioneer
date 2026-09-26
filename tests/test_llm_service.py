from __future__ import annotations
import asyncio
import llm_pb2
from llm_server.main import (
    LLMServiceServicer,
)


class FakeOllamaClient:
    def __init__(self) -> None:
        self.calls: list[
            tuple[str, str]
        ] = []

    async def generate(
        self,
        prompt: str,
        *,
        system: str,
    ) -> str:

        self.calls.append(
            (
                prompt,
                system,
            )
        )

        return "FAKE LLM RESPONSE"


class FakeContext:
    async def abort(
        self,
        code,
        details,
    ):
        raise AssertionError(
            f"unexpected abort: {code}: {details}"
        )


def test_get_llm_answer_calls_backend_with_question_and_context():
    backend = FakeOllamaClient()

    service = LLMServiceServicer(
        backend
    )

    response = asyncio.run(
        service.GetLLMAnswer(
            llm_pb2.GetLLMAnswerRequest(
                question="How does bidding work?",
                context=(
                    "Bids must exceed the "
                    "current highest bid."
                ),
            ),
            FakeContext(),
        )
    )

    assert response.answer == (
        "FAKE LLM RESPONSE"
    )

    assert len(backend.calls) == 1

    prompt, system = backend.calls[0]

    assert "How does bidding work?" in prompt
    assert "Bids must exceed" in prompt
    assert "Raftioneer" in system


def test_generate_item_description_uses_title_and_keywords():
    backend = FakeOllamaClient()

    service = LLMServiceServicer(
        backend
    )

    response = asyncio.run(
        service.GenerateItemDescription(
            llm_pb2.GenerateItemDescriptionRequest(
                title="Vintage mechanical wristwatch",
                keywords=[
                    "automatic",
                    "stainless steel",
                ],
            ),
            FakeContext(),
        )
    )

    assert response.description == (
        "FAKE LLM RESPONSE"
    )

    prompt, _ = backend.calls[0]

    assert (
        "Vintage mechanical wristwatch"
        in prompt
    )

    assert "automatic" in prompt
    assert "stainless steel" in prompt


def test_summarize_auction_uses_bid_history_and_final_price():
    backend = FakeOllamaClient()

    service = LLMServiceServicer(
        backend
    )

    response = asyncio.run(
        service.SummarizeAuction(
            llm_pb2.SummarizeAuctionRequest(
                auction_id="a1",
                item_title="Camera",
                bid_history=[
                    10.0,
                    12.5,
                    15.0,
                ],
                final_price=15.0,
            ),
            FakeContext(),
        )
    )

    assert response.summary == (
        "FAKE LLM RESPONSE"
    )

    prompt, _ = backend.calls[0]

    assert "a1" in prompt
    assert "Camera" in prompt
    assert (
        "10.00, 12.50, 15.00"
        in prompt
    )
    assert "15.00" in prompt
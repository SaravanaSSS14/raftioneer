from __future__ import annotations

import grpc

import llm_pb2
import llm_pb2_grpc


class LLMClient:
    """App-server-side gRPC client for the independent LLM node."""

    def __init__(
        self,
        address: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = llm_pb2_grpc.LLMServiceStub(
            self._channel
        )

    async def get_answer(
        self,
        question: str,
        context: str = "",
    ) -> str:
        response = await self._stub.GetLLMAnswer(
            llm_pb2.GetLLMAnswerRequest(
                question=question,
                context=context,
            ),
            timeout=self._timeout_seconds,
        )

        return response.answer

    async def generate_item_description(
        self,
        title: str,
        keywords: list[str] | None = None,
    ) -> str:
        response = await self._stub.GenerateItemDescription(
            llm_pb2.GenerateItemDescriptionRequest(
                title=title,
                keywords=keywords or [],
            ),
            timeout=self._timeout_seconds,
        )

        return response.description

    async def summarize_auction(
        self,
        auction_id: str,
        item_title: str,
        bid_history: list[float],
        final_price: float,
    ) -> str:
        response = await self._stub.SummarizeAuction(
            llm_pb2.SummarizeAuctionRequest(
                auction_id=auction_id,
                item_title=item_title,
                bid_history=bid_history,
                final_price=final_price,
            ),
            timeout=self._timeout_seconds,
        )

        return response.summary

    async def close(self) -> None:
        await self._channel.close()
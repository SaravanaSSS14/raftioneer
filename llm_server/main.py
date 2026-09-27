from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
from pathlib import Path

import grpc

# Locally, generated protobuf modules live in repo_root/generated/.
# In Docker, grpc_tools writes llm_pb2.py flat into /app, which is
# already on sys.path - so the stubs are always imported by bare name.
_GENERATED_DIR = Path(__file__).resolve().parents[1] / "generated"

if _GENERATED_DIR.is_dir() and str(_GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(_GENERATED_DIR))

import llm_pb2  # noqa: E402
import llm_pb2_grpc  # noqa: E402

from .ollama_client import (  # noqa: E402
    OllamaClient,
    OllamaClientError,
    OllamaConfig,
)

from .prompts import (  # noqa: E402
    SYSTEM_PROMPT,
    build_answer_prompt,
    build_description_prompt,
    build_summary_prompt,
)



NODE_ID = os.environ.get("NODE_ID", "llm-server")
PORT = int(os.environ.get("PORT", "50060"))

OLLAMA_BASE_URL = os.environ.get(
    "OLLAMA_BASE_URL",
    "http://127.0.0.1:11434",
)

OLLAMA_MODEL = os.environ.get(
    "OLLAMA_MODEL",
    "qwen2.5:1.5b",
)

OLLAMA_TIMEOUT_SECONDS = float(
    os.environ.get("OLLAMA_TIMEOUT_SECONDS", "90")
)

OLLAMA_TEMPERATURE = float(
    os.environ.get("OLLAMA_TEMPERATURE", "0.2")
)

OLLAMA_MAX_OUTPUT_TOKENS = int(
    os.environ.get("OLLAMA_MAX_OUTPUT_TOKENS", "256")
)


logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)

log = logging.getLogger(__name__)


class LLMServiceServicer(
    llm_pb2_grpc.LLMServiceServicer
):
    def __init__(self, llm_client: OllamaClient) -> None:
        self.llm_client = llm_client

    async def _generate(
        self,
        prompt: str,
        context,
    ) -> str:
        try:
            return await self.llm_client.generate(
                prompt,
                system=SYSTEM_PROMPT,
            )

        except OllamaClientError as exc:
            log.exception("Ollama generation failed")

            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                str(exc),
            )

        raise AssertionError(
            "context.abort() should not return"
        )

    async def GetLLMAnswer(
        self,
        request,
        context,
    ):
        question = request.question.strip()

        if not question:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "question is required",
            )

        prompt = build_answer_prompt(
            question,
            request.context,
        )

        answer = await self._generate(
            prompt,
            context,
        )

        log.info(
            "GetLLMAnswer(question=%r) ok",
            question,
        )

        return llm_pb2.GetLLMAnswerResponse(
            answer=answer
        )

    async def GenerateItemDescription(
        self,
        request,
        context,
    ):
        title = request.title.strip()

        if not title:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "title is required",
            )

        keywords = [
            value.strip()
            for value in request.keywords
            if value.strip()
        ]

        prompt = build_description_prompt(
            title,
            keywords,
        )

        description = await self._generate(
            prompt,
            context,
        )

        log.info(
            "GenerateItemDescription(title=%r) ok",
            title,
        )

        return llm_pb2.GenerateItemDescriptionResponse(
            description=description
        )

    async def SummarizeAuction(
        self,
        request,
        context,
    ):
        auction_id = request.auction_id.strip()
        item_title = request.item_title.strip()

        if not auction_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "auction_id is required",
            )

        if not item_title:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "item_title is required",
            )

        bid_history = list(request.bid_history)

        if any(
            not math.isfinite(value) or value < 0
            for value in bid_history
        ):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "bid_history must contain finite non-negative values",
            )

        if (
            not math.isfinite(request.final_price)
            or request.final_price < 0
        ):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "final_price must be a finite non-negative value",
            )

        average_sale_price = None
        if request.HasField("average_sale_price"):
            if not math.isfinite(request.average_sale_price) or request.average_sale_price < 0:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "average_sale_price must be a finite non-negative value",
                )
            average_sale_price = request.average_sale_price

        prompt = build_summary_prompt(
            auction_id=auction_id,
            item_title=item_title,
            bid_history=bid_history,
            final_price=request.final_price,
            average_sale_price=average_sale_price,
        )

        summary = await self._generate(
            prompt,
            context,
        )

        log.info(
            "SummarizeAuction(auction_id=%r) ok",
            auction_id,
        )

        return llm_pb2.SummarizeAuctionResponse(
            summary=summary
        )


async def serve() -> None:
    config = OllamaConfig(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        timeout_seconds=OLLAMA_TIMEOUT_SECONDS,
        temperature=OLLAMA_TEMPERATURE,
        max_output_tokens=OLLAMA_MAX_OUTPUT_TOKENS,
    )

    llm_client = OllamaClient(config)

    server = grpc.aio.server()

    llm_pb2_grpc.add_LLMServiceServicer_to_server(
        LLMServiceServicer(llm_client),
        server,
    )

    bound_port = server.add_insecure_port(
        f"[::]:{PORT}"
    )

    if bound_port != PORT:
        raise RuntimeError(
            f"Could not bind LLM server to port {PORT}; "
            f"got {bound_port}"
        )

    log.info(
        "LLM server starting: grpc_port=%s ollama=%s model=%s",
        PORT,
        OLLAMA_BASE_URL,
        OLLAMA_MODEL,
    )

    await server.start()

    try:
        await server.wait_for_termination()

    finally:
        await llm_client.close()
        await server.stop(grace=2)


if __name__ == "__main__":
    asyncio.run(serve())
from __future__ import annotations
import os
import sys
import grpc

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "generated",
    ),
)


import llm_pb2
import llm_pb2_grpc
LLM_SERVER = os.environ.get(
    "TEST_LLM_SERVER",
    "localhost:50060",
)

TIMEOUT_SECONDS = int(
    os.environ.get(
        "LLM_SMOKE_TIMEOUT_SECONDS",
        "120",
    )
)


def main() -> int:
    try:
        with grpc.insecure_channel(
            LLM_SERVER
        ) as channel:

            stub = llm_pb2_grpc.LLMServiceStub(
                channel
            )

            answer = stub.GetLLMAnswer(
                llm_pb2.GetLLMAnswerRequest(
                    question=(
                        "What happens when an auction closes?"
                    ),
                    context=(
                        "The auction closes at its end time. "
                        "The highest accepted bid is the "
                        "current highest bid."
                    ),
                ),
                timeout=TIMEOUT_SECONDS,
            )

            assert answer.answer.strip(), (
                "GetLLMAnswer returned an empty answer"
            )

            assert (
                "not yet integrated"
                not in answer.answer.lower()
            )

            description = (
                stub.GenerateItemDescription(
                    llm_pb2.GenerateItemDescriptionRequest(
                        title="Vintage mechanical wristwatch",
                        keywords=[
                            "automatic",
                            "stainless steel",
                            "classic design",
                        ],
                    ),
                    timeout=TIMEOUT_SECONDS,
                )
            )

            assert description.description.strip(), (
                "GenerateItemDescription returned "
                "an empty description"
            )

            summary = stub.SummarizeAuction(
                llm_pb2.SummarizeAuctionRequest(
                    auction_id="smoke-auction-001",
                    item_title="Vintage mechanical wristwatch",
                    bid_history=[
                        20.0,
                        25.0,
                        31.5,
                    ],
                    final_price=31.5,
                ),
                timeout=TIMEOUT_SECONDS,
            )

            assert summary.summary.strip(), (
                "SummarizeAuction returned "
                "an empty summary"
            )

        print(
            f"OK   {LLM_SERVER}  "
            f"(all 3 LLM RPCs returned non-empty responses)"
        )

        return 0

    except (
        grpc.RpcError,
        AssertionError,
    ) as exc:

        print(
            f"FAIL {LLM_SERVER}  ({exc})"
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
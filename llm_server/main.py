import asyncio
import logging
import os

import grpc

import llm_pb2
import llm_pb2_grpc

NODE_ID = os.environ.get("NODE_ID", "llm-server")
PORT = os.environ.get("PORT", "50060")

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] node={NODE_ID} %(message)s",
)
log = logging.getLogger(__name__)


class LLMServiceServicer(llm_pb2_grpc.LLMServiceServicer):
    """Week 1 skeleton: proves the frozen .proto contract is wired up
    end-to-end. Real model selection and prompt work lands in Week 3."""

    async def GetLLMAnswer(self, request, context):
        log.info("GetLLMAnswer(question=%s) [stub]", request.question)
        return llm_pb2.GetLLMAnswerResponse(answer="LLM server not yet integrated")

    async def GenerateItemDescription(self, request, context):
        log.info("GenerateItemDescription(title=%s) [stub]", request.title)
        return llm_pb2.GenerateItemDescriptionResponse(description="not yet implemented")

    async def SummarizeAuction(self, request, context):
        log.info("SummarizeAuction(auction_id=%s) [stub]", request.auction_id)
        return llm_pb2.SummarizeAuctionResponse(summary="not yet implemented")


async def serve():
    server = grpc.aio.server()
    llm_pb2_grpc.add_LLMServiceServicer_to_server(LLMServiceServicer(), server)
    server.add_insecure_port(f"[::]:{PORT}")
    log.info("llm server starting on port %s", PORT)
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())

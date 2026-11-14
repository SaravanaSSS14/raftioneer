"""The LLM node's retrieval over the auction handbook (llm_server/retrieval.py)
and how it reaches the model's prompt. No Docker or model needed."""
import asyncio

import llm_pb2
import pytest

from llm_server.main import LLMServiceServicer
from llm_server.retrieval import Handbook, load_sections, tokenize


@pytest.fixture(scope="module")
def handbook():
    return Handbook()


def top(handbook, question):
    found = handbook.search(question)
    return found[0].title if found else None


def test_the_handbook_loads_every_section():
    titles = [s.title for s in load_sections()]
    assert len(titles) >= 12
    assert all(s.text for s in load_sections())
    assert "Escrow: paying and receiving the item" in titles


@pytest.mark.parametrize("question, section", [
    ("How much can I bid with my money?", "How much you can bid (available funds)"),
    ("why did the end time change?", "Anti-sniping: why the end time can move"),
    ("what is the buyers premium", "Buyer's premium"),
    ("what happens if the leader server crashes during my bid", "If a server fails: is my bid safe?"),
    ("how do I sign up", "Creating an account and signing in"),
    ("how do I list my cycle for sale", "Selling an item"),
    ("when should I confirm receipt", "Escrow: paying and receiving the item"),
    ("what if nobody bids", "When an auction ends and who wins"),
    ("how do I top up my wallet", "Your wallet and the welcome credit"),
    ("can I cancel my bid", "Placing a bid and what a valid bid is"),
])
def test_questions_find_the_section_that_answers_them(handbook, question, section):
    assert top(handbook, question) == section


def test_an_off_topic_question_finds_nothing(handbook):
    assert handbook.search("what is the capital of France") == []


def test_tokenize_drops_stopwords_and_folds_variants():
    assert tokenize("How do I top up the wallet?") == ["deposit", "up", "wallet"]
    assert tokenize("bids bidding ended premiums") == ["bid", "bid", "end", "premium"]


class RecordingBackend:
    def __init__(self):
        self.prompts = []

    async def generate(self, prompt, *, system):
        self.prompts.append(prompt)
        return "You need funds for the bid plus the premium."


class NoAbort:
    async def abort(self, code, details):
        raise AssertionError(f"unexpected abort: {code}: {details}")


def test_answers_are_grounded_in_the_handbook_and_echo_the_request_id(handbook):
    backend = RecordingBackend()
    service = LLMServiceServicer(backend, handbook)
    reply = asyncio.run(service.GetLLMAnswer(
        llm_pb2.GetLLMAnswerRequest(question="How much can I bid?", context="", request_id="req-42"),
        NoAbort(),
    ))
    assert reply.request_id == "req-42"
    assert reply.sources[0] == "How much you can bid (available funds)"
    # The retrieved rule text itself is in the prompt the model sees.
    assert "5% buyer's premium" in backend.prompts[0]
    assert "[How much you can bid (available funds)]" in backend.prompts[0]


def test_an_unmatched_question_says_so_in_the_prompt(handbook):
    backend = RecordingBackend()
    reply = asyncio.run(LLMServiceServicer(backend, handbook).GetLLMAnswer(
        llm_pb2.GetLLMAnswerRequest(question="what is the capital of France"), NoAbort(),
    ))
    assert list(reply.sources) == []
    assert "No handbook section matched" in backend.prompts[0]

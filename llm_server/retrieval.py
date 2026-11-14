"""Retrieval over the auction handbook (knowledge/auction_handbook.md): the
"domain-specific" half of the LLM node.

Before the model answers a question, the sections of the house handbook most
relevant to it are found and put into the prompt, so the answer follows
Raftioneer's actual rules (the welcome credit, the premium, anti-sniping,
escrow) instead of whatever a general-purpose model believes about auctions.
This is retrieval-augmented generation in its simplest form.

The ranking is BM25 (Robertson & Zaragoza), the standard keyword-relevance
score used by search engines: a section scores higher the more often it
contains the question's words, with rarer words counting for more and long
sections not winning just for being long. Pure Python - the handbook is a
few kilobytes, so there is no need for an embedding model or a vector
database on a CPU-only node.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HANDBOOK_PATH = Path(__file__).resolve().parent / "knowledge" / "auction_handbook.md"

# Common words that say nothing about which section is relevant.
STOPWORDS = frozenset("""
a an and are as at be been but by can do does for from has have how i if in into is it its me my
no not of on or our so than that the their them then there these they this to too us was we what
when where which who why will with would you your yours i'm it's what's can't don't
get got should want any some about just also
""".split())

# A handful of spelling variants and synonyms bidders actually use.
SYNONYMS = {
    "money": "wallet", "balance": "wallet", "credit": "wallet", "funds": "funds", "afford": "funds",
    "topup": "deposit", "top": "deposit", "recharge": "deposit", "add": "deposit",
    "snipe": "sniping", "sniper": "sniping", "extend": "extension", "extended": "extension",
    "fee": "premium", "commission": "premium", "charge": "premium",
    "pay": "escrow", "payment": "escrow", "paid": "escrow", "refund": "escrow", "release": "escrow",
    "signup": "register", "sign": "register", "login": "sign",
    "crash": "fails", "crashes": "fails", "down": "fails", "failure": "fails", "leader": "fails",
    "win": "wins", "winner": "wins", "won": "wins", "ends": "end", "closes": "end", "close": "end",
    "sell": "selling", "sale": "selling", "list": "selling", "listing": "selling",
    "minimum": "reserve", "lowest": "reserve",
    "cancel": "withdrawn", "withdraw": "withdrawn", "retract": "withdrawn",
}

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", text.lower().replace("-", " "))
    tokens = []
    for word in words:
        if word in STOPWORDS or len(word) < 2:
            continue
        tokens.append(stem(SYNONYMS.get(word, word)))
    return tokens


def stem(word: str) -> str:
    """A light stemmer, enough for a handbook: bids -> bid, bidding -> bid,
    ended -> end, closes -> close."""
    for suffix, min_length in (("ing", 6), ("ed", 5), ("s", 4)):
        if len(word) >= min_length and word.endswith(suffix) and not word.endswith("ss"):
            word = word[: -len(suffix)]
            # bidding -> bidd -> bid
            if suffix != "s" and len(word) > 2 and word[-1] == word[-2] and word[-1] not in "aeiouls":
                word = word[:-1]
            break
    return word


@dataclass(frozen=True)
class Section:
    title: str
    text: str


def load_sections(path: Path = HANDBOOK_PATH) -> list[Section]:
    """Split the handbook into its "## " sections (the preamble is skipped)."""
    sections, title, lines = [], None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if title:
                sections.append(Section(title, " ".join(lines).strip()))
            title, lines = line[3:].strip(), []
        elif title is not None and line.strip():
            lines.append(line.strip())
    if title:
        sections.append(Section(title, " ".join(lines).strip()))
    return sections


class Handbook:
    """BM25 search over the handbook's sections."""

    def __init__(self, sections: list[Section] | None = None):
        self.sections = sections if sections is not None else load_sections()
        # The title is counted twice: it is the best summary of a section.
        self._docs = [tokenize(f"{s.title} {s.title} {s.text}") for s in self.sections]
        self._counts = [Counter(doc) for doc in self._docs]
        self._avg_len = sum(len(d) for d in self._docs) / max(len(self._docs), 1)
        n = len(self._docs)
        df = Counter(term for doc in self._docs for term in set(doc))
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def score(self, query: str, index: int) -> float:
        counts, length = self._counts[index], len(self._docs[index])
        total = 0.0
        for term in set(tokenize(query)):
            tf = counts.get(term, 0)
            if tf:
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self._avg_len))
                total += self._idf.get(term, 0.0) * norm
        return total

    def search(self, query: str, k: int = 2, min_score: float = 1.0, relative: float = 0.5) -> list[Section]:
        """The k most relevant sections, best first. A section must score at
        least min_score (so a question the handbook doesn't cover gets
        nothing, rather than an unrelated section) and at least `relative`
        times the best score (so a weak runner-up doesn't dilute a clear
        answer)."""
        scored = sorted(((self.score(query, i), i) for i in range(len(self.sections))), reverse=True)
        if not scored or scored[0][0] < min_score:
            return []
        floor = max(min_score, scored[0][0] * relative)
        return [self.sections[i] for s, i in scored[:k] if s >= floor]

"""Policy retrieval with BM25 keyword ranking.

The purchasing policy is split into its numbered sections (`### 3.1 Title`), and each section gets
a stable identifier such as `PP-3.1` that the chatbot uses as a citation.

BM25 ranks sections by how often the question's words appear in them, weighted so that rare words
("threshold") count more than common ones ("invoice"). It needs no embedding model or vector
database, which keeps Milestone 1 free to run. `PolicyRetriever` is the interface a future Azure AI
Search implementation would satisfy.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SECTION_ID_PREFIX = "PP"

_CHAPTER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_SECTION_RE = re.compile(r"^###\s+(\d+\.\d+)\s+(.+?)\s*$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    """a an and are as at be been by can could do does did for from has have how i if in into is it
    its me my no not of on or our should so than that the their them then there these this those
    to was we were what when where which who why will with would you your about any all also
    must may per""".split()  # noqa: SIM905 - a word list is easier to read than a list literal
)


@dataclass(frozen=True)
class PolicySection:
    section_id: str  # e.g. "PP-3.1"
    number: str  # e.g. "3.1"
    title: str
    chapter: str
    text: str


@dataclass(frozen=True)
class RetrievedPassage:
    section: PolicySection
    score: float


class PolicyRetriever(Protocol):
    def search(self, query: str, top_k: int = 3) -> list[RetrievedPassage]: ...

    def get_section(self, section_id: str) -> PolicySection | None: ...

    @property
    def sections(self) -> list[PolicySection]: ...


def _stem(token: str) -> str:
    """Light normalisation so 'approvals'/'approval', 'discrepancies'/'discrepancy' match."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def parse_policy_markdown(markdown: str) -> list[PolicySection]:
    sections: list[PolicySection] = []
    chapter = ""
    current: tuple[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            number, title = current
            sections.append(
                PolicySection(
                    section_id=f"{SECTION_ID_PREFIX}-{number}",
                    number=number,
                    title=title,
                    chapter=chapter,
                    text=" ".join(line.strip() for line in body if line.strip()),
                )
            )

    for line in markdown.splitlines():
        if match := _SECTION_RE.match(line):
            flush()
            current, body = (match.group(1), match.group(2)), []
        elif match := _CHAPTER_RE.match(line):
            flush()
            current, body = None, []
            chapter = match.group(2)
        elif current is not None:
            body.append(line)
    flush()

    ids = [s.section_id for s in sections]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate policy section numbers")
    return sections


class BM25PolicyRetriever:
    """Okapi BM25 over policy sections. Section titles are counted twice to boost them."""

    def __init__(
        self,
        sections: list[PolicySection],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        min_score: float = 0.5,
    ) -> None:
        if not sections:
            raise ValueError("At least one policy section is required")
        self._sections = sections
        self._by_id = {s.section_id: s for s in sections}
        self._k1 = k1
        self._b = b
        self._min_score = min_score

        self._doc_tokens = [Counter(tokenize(f"{s.title} {s.title} {s.text}")) for s in sections]
        self._doc_lengths = [sum(c.values()) for c in self._doc_tokens]
        self._avg_length = sum(self._doc_lengths) / len(self._doc_lengths)
        doc_freq: Counter[str] = Counter()
        for counts in self._doc_tokens:
            doc_freq.update(counts.keys())
        n = len(sections)
        # BM25 IDF with +1 inside the log so it is never negative.
        self._idf = {
            term: math.log((n - df + 0.5) / (df + 0.5) + 1.0) for term, df in doc_freq.items()
        }

    @classmethod
    def from_file(cls, path: Path, **kwargs: float) -> BM25PolicyRetriever:
        return cls(parse_policy_markdown(path.read_text(encoding="utf-8")), **kwargs)

    @property
    def sections(self) -> list[PolicySection]:
        return list(self._sections)

    def get_section(self, section_id: str) -> PolicySection | None:
        return self._by_id.get(section_id)

    def _score(self, index: int, query_terms: list[str]) -> float:
        counts = self._doc_tokens[index]
        norm = self._k1 * (1 - self._b + self._b * self._doc_lengths[index] / self._avg_length)
        score = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if tf:
                score += self._idf[term] * tf * (self._k1 + 1) / (tf + norm)
        return score

    def search(self, query: str, top_k: int = 3) -> list[RetrievedPassage]:
        """Return up to `top_k` sections scoring at least `min_score`, best first.

        An empty list means no sufficiently relevant policy evidence was found.
        """
        terms = list(dict.fromkeys(tokenize(query)))  # de-duplicate, keep order
        if not terms or top_k <= 0:
            return []
        scored = [
            RetrievedPassage(section=section, score=round(self._score(i, terms), 4))
            for i, section in enumerate(self._sections)
        ]
        relevant = [p for p in scored if p.score >= self._min_score]
        # Sort by score, then by section order for deterministic ties.
        order = {s.section_id: i for i, s in enumerate(self._sections)}
        relevant.sort(key=lambda p: (-p.score, order[p.section.section_id]))
        return relevant[:top_k]

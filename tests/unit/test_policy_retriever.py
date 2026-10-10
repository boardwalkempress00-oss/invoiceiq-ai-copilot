from __future__ import annotations

import pytest

from invoiceiq.core.invoice_validator import DEFAULT_POLICY, RULES
from invoiceiq.core.policy_retriever import BM25PolicyRetriever, parse_policy_markdown, tokenize


def test_sections_have_stable_unique_ids(retriever):
    ids = [s.section_id for s in retriever.sections]
    assert len(ids) == len(set(ids))
    assert ids[:3] == ["PP-1.1", "PP-1.2", "PP-1.3"]
    assert "PP-3.1" in ids and "PP-4.3" in ids
    section = retriever.get_section("PP-3.1")
    assert section is not None
    assert section.title == "Manager approval threshold"
    assert section.chapter == "Approval thresholds"
    assert "20,000.00" in section.text


def test_every_policy_reference_used_by_the_validator_exists(retriever):
    references = {rule.policy_reference for rule in RULES.values()}
    references |= {tier.policy_reference for tier in DEFAULT_POLICY.approval_tiers}
    for reference in references:
        assert retriever.get_section(reference) is not None, reference


@pytest.mark.parametrize(
    ("query", "expected_top"),
    [
        ("Which purchases need manager approval above the threshold?", "PP-3.1"),
        ("What is the rounding tolerance for totals?", "PP-4.3"),
        ("How do I write to the supplier about a discrepancy?", "PP-5.2"),
        ("What VAT rate applies to domestic invoices?", "PP-4.1"),
        ("Invoice in euro, which exchange rate should we use?", "PP-2.3"),
        ("approvals needed by a director", "PP-3.2"),
    ],
)
def test_search_ranks_relevant_section_near_top(retriever, query, expected_top):
    # Neighbouring sections (e.g. 3.1 and 3.2) legitimately compete, so require the top two.
    results = retriever.search(query, top_k=3)
    assert results, query
    assert expected_top in [r.section.section_id for r in results[:2]]


def test_search_respects_top_k_and_orders_by_score(retriever):
    results = retriever.search("invoice approval VAT supplier payment", top_k=4)
    assert len(results) == 4
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("query", ["zebra quantum banana", "", "   ", "what is the and of"])
def test_search_returns_nothing_without_relevant_evidence(retriever, query):
    assert retriever.search(query) == []


def test_unknown_section_returns_none(retriever):
    assert retriever.get_section("PP-9.9") is None


def test_search_is_deterministic(retriever):
    query = "approval threshold"
    assert retriever.search(query) == retriever.search(query)


def test_tokenize_normalises_plurals_and_stopwords():
    assert tokenize("The Approvals and Discrepancies") == ["approval", "discrepancy"]
    assert tokenize("class") == ["class"]


def test_parse_policy_markdown():
    md = "# T\n## 1. Chapter\nintro\n### 1.1 First\nAlpha beta.\n\n### 1.2 Second\nGamma.\n"
    sections = parse_policy_markdown(md)
    assert [(s.section_id, s.title, s.text) for s in sections] == [
        ("PP-1.1", "First", "Alpha beta."),
        ("PP-1.2", "Second", "Gamma."),
    ]


def test_duplicate_section_numbers_rejected():
    with pytest.raises(ValueError):
        parse_policy_markdown("## 1. C\n### 1.1 A\nx\n### 1.1 B\ny\n")


def test_empty_policy_rejected():
    with pytest.raises(ValueError):
        BM25PolicyRetriever([])

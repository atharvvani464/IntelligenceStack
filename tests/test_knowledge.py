"""Tests for governed retrieval over the enterprise knowledge corpus.

These cover the three properties the RAG capability claims: that retrieval
returns genuinely relevant passages, that it *abstains* rather than citing a
weak match, and that a free-text query is governed without breaking natural
language.
"""

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.policy import enforce
from src.lakehouse.knowledge_engine import KnowledgeEngine
from src.lakehouse.local_engine import LakehouseEngine
from src.settings import LANDING_ZONE


@pytest.fixture(scope="module")
def knowledge() -> KnowledgeEngine:
    return KnowledgeEngine().build()


@pytest.fixture(scope="module")
def agent(knowledge) -> MosaicAnalyticsAgent:
    if not LANDING_ZONE.exists() or not any(LANDING_ZONE.glob("*.json")):
        pytest.skip("No telemetry seeded; run synthetic_generator.py --seed-batch first.")
    return MosaicAnalyticsAgent(engine=LakehouseEngine().build(), knowledge=knowledge)


# --------------------------------------------------------------------------- #
# Retrieval quality
# --------------------------------------------------------------------------- #

def test_index_covers_every_document(knowledge):
    summary = knowledge.index_summary()
    assert {d["source"] for d in summary} == {
        "SRE Runbook",
        "Incident Playbook",
        "Customer Tiering Handbook",
        "Data Governance Policy",
    }
    assert all(d["chunks"] > 0 for d in summary)


def test_remediation_query_retrieves_the_runbook(knowledge):
    hits = knowledge.search("What is our remediation procedure for high-latency customers?")
    assert hits, "expected a retrieval hit for a directly covered question"
    assert hits[0]["source"] == "SRE Runbook"
    assert "remediation" in hits[0]["title"].lower()


def test_pii_query_retrieves_the_governance_policy(knowledge):
    """Guards the stemming behaviour: 'payloads' must match 'payload'."""
    hits = knowledge.search("How do we handle PII in customer payloads?")
    assert hits
    assert hits[0]["source"] == "Data Governance Policy"


def test_results_are_ranked_by_similarity(knowledge):
    hits = knowledge.search("When should we open an incident?", k=3)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Abstention -- the guard against spurious citations
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "query",
    [
        "What is our vacation policy?",  # shares only the common word "policy"
        "Who won the world cup?",  # no corpus vocabulary at all
        "recipe for banana bread",
    ],
)
def test_uncovered_questions_abstain(knowledge, query):
    assert knowledge.search(query) == [], f"expected abstention for {query!r}"


def test_agent_reports_abstention_rather_than_guessing(agent):
    result = agent.run("What is our vacation policy?")
    assert result.executed
    assert result.citations == []
    assert "no governed knowledge" in result.answer.lower()


# --------------------------------------------------------------------------- #
# Governance of a free-text parameter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "query",
    [
        "how do we update the runbook?",  # 'update' is a SQL keyword
        "what is the process to create an incident?",  # 'create' is a SQL keyword
        "how do we drop a customer from the tier?",  # 'drop' is a SQL keyword
    ],
)
def test_natural_language_is_not_blocked_by_keyword_interdiction(query):
    """Free-text questions may contain words that are also SQL keywords."""
    decision = enforce("search_knowledge_base", {"query": query})
    assert decision.allowed, f"{query!r} should be granted: {decision.detail}"


def test_control_sequences_still_blocked_in_free_text():
    decision = enforce("search_knowledge_base", {"query": "policy'; DROP TABLE x; --"})
    assert not decision.allowed
    assert decision.control == "SQL_INTERDICTION"


def test_identifier_parameter_keeps_strict_interdiction():
    decision = enforce("get_customer_anomaly_score", {"target_id": "SELECT * FROM t"})
    assert not decision.allowed
    assert decision.control == "SQL_INTERDICTION"


def test_overlong_and_empty_queries_rejected():
    assert not enforce("search_knowledge_base", {"query": "x" * 600}).allowed
    assert not enforce("search_knowledge_base", {"query": "   "}).allowed


def test_hostile_query_cannot_reshape_the_ranking_statement(knowledge):
    """The query is a bound value; a hostile string simply retrieves nothing useful."""
    hits = knowledge.search("' OR 1=1 --")
    assert hits == []
    # The index is intact and still answers a legitimate question afterwards.
    assert knowledge.search("When should we open an incident?")


# --------------------------------------------------------------------------- #
# Hybrid reasoning -- the flagship capability
# --------------------------------------------------------------------------- #

def test_hybrid_query_returns_both_metrics_and_cited_guidance(agent):
    result = agent.run("CUST_404 is flagged - what should we do about it?")
    assert result.executed
    assert result.payload and result.payload[0]["customer_id"] == "CUST_404"
    assert result.citations, "hybrid answer must cite governed guidance"
    assert "CUST_404" in result.answer
    assert "Governed guidance" in result.answer


def test_hybrid_passes_each_call_through_the_boundary(agent):
    """Both tool calls are independently governed and traced."""
    result = agent.run("CUST_404 is flagged - what should we do about it?")
    checks = [t for t in result.trace if t.step == "Governance Boundary Check"]
    assert len(checks) == 2
    assert all(t.status == "Granted" for t in checks)
    steps = {t.step for t in result.trace}
    assert {"Lakehouse Execution", "Vector Search Execution"} <= steps


def test_pure_metrics_query_does_not_trigger_retrieval(agent):
    """A metrics-only question must not be diluted with document retrieval."""
    result = agent.run("Evaluate anomaly parameters for customer CUST_404")
    assert result.executed
    assert result.payload
    assert result.citations == []
    assert not any(t.step == "Vector Search Execution" for t in result.trace)


def test_knowledge_only_query_returns_citations_without_metrics(agent):
    result = agent.run("What is our remediation procedure for high-latency customers?")
    assert result.executed
    assert result.citations
    assert result.payload == []

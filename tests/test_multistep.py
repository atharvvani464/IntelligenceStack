"""Tests for multi-step governed reasoning and latency measurement.

The architectural claim these defend: the governance boundary is a general
control, not a wrapper around one function. N tool calls in a single request
means N independent decisions, N audit records, and one coherent answer.
"""

import json

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import AuditTrail
from src.governance.policy import REGISTERED_FUNCTIONS, enforce
from src.lakehouse.knowledge_engine import KnowledgeEngine
from src.lakehouse.local_engine import LakehouseEngine
from src.settings import LANDING_ZONE


@pytest.fixture(scope="module")
def engine() -> LakehouseEngine:
    if not LANDING_ZONE.exists() or not any(LANDING_ZONE.glob("*.json")):
        pytest.skip("No telemetry seeded; run synthetic_generator.py --seed-batch first.")
    return LakehouseEngine().build()


@pytest.fixture
def agent(engine, tmp_path) -> MosaicAnalyticsAgent:
    return MosaicAnalyticsAgent(
        engine=engine,
        knowledge=KnowledgeEngine().build(),
        audit=AuditTrail(log_path=tmp_path / "audit.jsonl"),
    )


# --------------------------------------------------------------------------- #
# The new capability is registered, not special-cased
# --------------------------------------------------------------------------- #

def test_timeline_is_on_the_allowlist():
    assert "get_customer_timeline" in REGISTERED_FUNCTIONS
    decision = enforce("get_customer_timeline", {"target_id": "CUST_404"})
    assert decision.allowed


def test_timeline_keeps_strict_identifier_interdiction():
    """A third function inherits the same parameter discipline as the first two."""
    decision = enforce("get_customer_timeline", {"target_id": "CUST_404; DROP TABLE x"})
    assert not decision.allowed
    assert decision.control == "SQL_INTERDICTION"


def test_timeline_returns_daily_rows(engine):
    rows = engine.get_customer_timeline("CUST_404")
    assert rows, "the anomaly cohort customer must have observed history"
    assert {"day", "events", "anomalies", "anomaly_rate"} <= set(rows[0])
    # Ordered oldest first, so a trajectory can be read off directly.
    assert [r["day"] for r in rows] == sorted(r["day"] for r in rows)


def test_timeline_binds_its_parameter(engine):
    assert engine.get_customer_timeline("CUST_404' OR '1'='1") == []


# --------------------------------------------------------------------------- #
# N-way chaining
# --------------------------------------------------------------------------- #

def test_comparison_issues_one_governed_call_per_customer(agent):
    result = agent.run("Compare CUST_404 and CUST_417")
    checks = [t for t in result.trace if t.step == "Governance Boundary Check"]
    assert len(checks) == 2, "each customer must clear the boundary independently"
    assert all(t.status == "Granted" for t in checks)
    assert {row["customer_id"] for row in result.payload} == {"CUST_404", "CUST_417"}


def test_comparison_accumulates_rather_than_overwrites(agent):
    """The second call must not discard the first call's rows."""
    result = agent.run("Compare CUST_404, CUST_417 and CUST_431")
    assert len(result.payload) == 3
    assert len({row["customer_id"] for row in result.payload}) == 3


def test_comparison_actually_compares(agent):
    result = agent.run("Compare CUST_404 and CUST_417")
    assert "more serious case" in result.answer
    # Both customers named, and the higher-risk one identified.
    assert "CUST_404" in result.answer and "CUST_417" in result.answer


def test_each_call_is_audited_separately(agent, tmp_path):
    agent.run("Compare CUST_404 and CUST_417")
    log = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    assert {r["parameters"]["target_id"] for r in records} == {"CUST_404", "CUST_417"}
    assert len({r["request_id"] for r in records}) == 1


def test_a_repeated_customer_is_not_queried_twice(agent):
    """Deduplicated: asking about the same customer twice is still one call."""
    result = agent.run("Compare CUST_404 with CUST_404")
    checks = [t for t in result.trace if t.step == "Governance Boundary Check"]
    assert len(checks) == 1


# --------------------------------------------------------------------------- #
# Trend routing
# --------------------------------------------------------------------------- #

def test_trend_question_routes_to_the_timeline_function(agent):
    result = agent.run("Is CUST_404 getting worse over time?")
    assert result.executed
    assert result.governance["function"] == "get_customer_timeline"
    assert len(result.payload) > 1, "a trajectory needs more than one day"


def test_trend_answer_describes_direction(agent):
    answer = agent.run("Show me the CUST_404 trend over time").answer
    assert any(word in answer for word in ("deteriorating", "improving", "stable"))


def test_plain_metric_question_still_uses_the_scoring_function(agent):
    """Adding a third tool must not change the behaviour of the first."""
    result = agent.run("Evaluate anomaly parameters for customer CUST_404")
    assert result.governance["function"] == "get_customer_anomaly_score"
    assert len(result.payload) == 1
    assert result.payload[0]["risk_factor"] == 82.35


# --------------------------------------------------------------------------- #
# Latency is measured
# --------------------------------------------------------------------------- #

def test_request_duration_is_measured(agent):
    result = agent.run("Evaluate anomaly parameters for customer CUST_404")
    assert result.duration_ms > 0
    assert result.duration_ms < 30_000, "sanity bound"


def test_refusals_are_also_timed(agent):
    result = agent.run("Show me revenue by region")
    assert not result.executed
    assert result.duration_ms > 0


def test_duration_is_persisted_per_call(agent, tmp_path):
    agent.run("Compare CUST_404 and CUST_417")
    log = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert all(r["duration_ms"] > 0 for r in records)

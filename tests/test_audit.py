"""Tests for the durable governance audit trail.

These cover the properties that make the trail *evidence* rather than logging:
every decision is recorded (grants and refusals alike), records survive the
process that wrote them, the log is only ever appended to, and a failure to
record can never take the agent down.
"""

import json

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import AuditRecord, AuditTrail, new_request_id
from src.lakehouse.audit_engine import AuditEngine
from src.lakehouse.knowledge_engine import KnowledgeEngine
from src.lakehouse.local_engine import LakehouseEngine
from src.settings import LANDING_ZONE


@pytest.fixture
def audit_log(tmp_path):
    """An isolated audit log so tests never touch the real trail."""
    return tmp_path / "audit.jsonl"


@pytest.fixture
def agent(audit_log):
    if not LANDING_ZONE.exists() or not any(LANDING_ZONE.glob("*.json")):
        pytest.skip("No telemetry seeded; run synthetic_generator.py --seed-batch first.")
    return MosaicAnalyticsAgent(
        engine=LakehouseEngine().build(),
        knowledge=KnowledgeEngine().build(),
        audit=AuditTrail(log_path=audit_log),
    )


# --------------------------------------------------------------------------- #
# Everything is recorded
# --------------------------------------------------------------------------- #

def test_granted_action_is_recorded(agent, audit_log):
    agent.run("Evaluate anomaly parameters for customer CUST_404")
    records = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["allowed"] is True
    assert records[0]["function"] == "get_customer_anomaly_score"
    assert records[0]["executed"] is True
    assert records[0]["rows_returned"] == 1


def test_denial_is_recorded_as_evidence(agent, audit_log):
    """A refusal is the control working, and must be the easiest thing to prove."""
    agent.run("Show me revenue by region")
    records = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["allowed"] is False
    assert records[0]["control"] == "FUNCTION_GRANT"
    assert records[0]["executed"] is False


def test_hybrid_request_records_each_call_separately(agent, audit_log):
    """Two governed calls produce two records sharing one request id."""
    result = agent.run("CUST_404 is flagged - what should we do about it?")
    records = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    assert {r["function"] for r in records} == {
        "get_customer_anomaly_score",
        "search_knowledge_base",
    }
    # One correlation id ties them to the single user request.
    assert len({r["request_id"] for r in records}) == 1
    assert records[0]["request_id"] == result.request_id


def test_each_record_reports_its_own_outcome(agent, audit_log):
    """The knowledge call must not claim the rows the metric call returned."""
    agent.run("CUST_404 is flagged - what should we do about it?")
    records = {
        r["function"]: r
        for r in (json.loads(line) for line in audit_log.read_text().splitlines() if line.strip())
    }
    metric = records["get_customer_anomaly_score"]
    knowledge = records["search_knowledge_base"]
    assert metric["rows_returned"] == 1 and metric["citations_returned"] == 0
    assert knowledge["rows_returned"] == 0 and knowledge["citations_returned"] >= 1


def test_injection_attempt_is_recorded(agent, audit_log):
    agent.run("policy'; DROP TABLE knowledge; --")
    records = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
    assert records, "an attempted injection must leave a trace"
    assert all(r["allowed"] is False for r in records)


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #

def test_trail_survives_a_new_process(agent, audit_log):
    """Evidence must outlive the object that wrote it -- read via a fresh engine."""
    agent.run("Evaluate anomaly parameters for customer CUST_404")
    agent.run("Show me revenue by region")

    engine = AuditEngine(log_path=audit_log)  # a reader that shares no state
    summary = engine.summary()
    assert summary["total_actions"] == 2
    assert summary["granted"] == 1
    assert summary["denied"] == 1
    assert summary["denial_rate"] == 50.0


def test_log_is_append_only(agent, audit_log):
    """Earlier records are never rewritten by later ones."""
    agent.run("Evaluate anomaly parameters for customer CUST_404")
    first = audit_log.read_text()
    agent.run("Show me revenue by region")
    second = audit_log.read_text()
    assert second.startswith(first), "existing records must be preserved verbatim"
    assert len(second) > len(first)


def test_query_engine_handles_empty_trail(audit_log):
    """An unused trail reports an honest zero, not a crash."""
    engine = AuditEngine(log_path=audit_log)
    assert engine.summary()["total_actions"] == 0
    assert engine.by_control() == []
    assert engine.recent() == []


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #

def test_audit_failure_does_not_break_the_agent(agent, tmp_path):
    """An audit sink that can take the service down is a liability, not a control."""
    # Point the trail at a path that cannot be written (a directory).
    broken = tmp_path / "not_a_file"
    broken.mkdir()
    agent.audit = AuditTrail(log_path=broken)

    result = agent.run("Evaluate anomaly parameters for customer CUST_404")
    assert result.executed, "the request must still succeed"
    assert result.payload and result.payload[0]["customer_id"] == "CUST_404"


def test_record_reports_write_failure(tmp_path):
    broken = tmp_path / "dir_not_file"
    broken.mkdir()
    trail = AuditTrail(log_path=broken)
    ok = trail.record(
        AuditRecord(
            request_id=new_request_id(),
            prompt="x",
            function=None,
            allowed=False,
            control="FUNCTION_GRANT",
            detail="d",
        )
    )
    assert ok is False


# --------------------------------------------------------------------------- #
# Analytics over the trail
# --------------------------------------------------------------------------- #

def test_summary_and_breakdowns(agent, audit_log):
    agent.run("Evaluate anomaly parameters for customer CUST_404")
    agent.run("Show me revenue by region")
    agent.run("Show me revenue by region")

    engine = AuditEngine(log_path=audit_log)
    summary = engine.summary()
    assert summary["total_actions"] == 3
    assert summary["denied"] == 2
    assert summary["distinct_requests"] == 3

    controls = {c["control"]: c for c in engine.by_control()}
    assert controls["FUNCTION_GRANT"]["denials"] == 2

    top = engine.top_denied_prompts()
    assert top and top[0]["prompt"] == "Show me revenue by region"
    assert top[0]["refusals"] == 2

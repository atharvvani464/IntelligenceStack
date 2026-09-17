"""Tests for resource-quota governance (the RESOURCE_QUOTA control).

The property under test: entitlement bounds *what* a caller may touch, but a
caller left unbounded on *how much* can still turn one question into an
unreasonable amount of lakehouse work. These tests cover the two independent
budgets -- per-request fan-out and per-minute rate -- and that both are
auditable, fail-closed, and hold even across a fresh reader of the trail.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import AuditTrail
from src.governance.identity import PRINCIPALS, SERVICE_PRINCIPAL
from src.governance.quota import enforce_quota
from src.lakehouse.audit_engine import AuditEngine
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
# Fan-out budget
# --------------------------------------------------------------------------- #

def test_fanout_within_budget_is_not_a_quota_objection(tmp_path):
    decision = enforce_quota(PRINCIPALS["analyst.chen"], 2, tmp_path / "audit.jsonl")
    assert decision is None


def test_fanout_exceeding_budget_is_denied(tmp_path):
    decision = enforce_quota(PRINCIPALS["analyst.chen"], 3, tmp_path / "audit.jsonl")
    assert decision is not None
    assert not decision.allowed
    assert decision.control == "RESOURCE_QUOTA"
    assert "budgeted for at most 2" in decision.detail


def test_unrestricted_principal_has_no_fanout_limit(tmp_path):
    decision = enforce_quota(SERVICE_PRINCIPAL, 50, tmp_path / "audit.jsonl")
    assert decision is None


def test_oversized_comparison_is_refused_before_the_engine(agent):
    """Three customers exceeds the analyst's fan-out budget of two."""
    result = agent.run(
        "Compare CUST_404, CUST_405 and CUST_406", principal=PRINCIPALS["analyst.chen"]
    )
    assert not result.executed
    assert result.payload == []
    assert result.governance["control"] == "RESOURCE_QUOTA"
    assert any(t.step == "Execution" and t.status == "Blocked" for t in result.trace)


def test_fanout_check_precedes_entitlement(agent):
    """An over-fan-out request is refused for budget, not scope -- even when
    every named customer would otherwise be out of scope too."""
    result = agent.run(
        "Compare CUST_431, CUST_440 and CUST_450", principal=PRINCIPALS["analyst.chen"]
    )
    assert result.governance["control"] == "RESOURCE_QUOTA"


def test_comparison_within_fanout_budget_still_runs(agent):
    """Two customers is exactly the analyst's budget, so it proceeds to the
    per-call boundary as before."""
    result = agent.run("Compare CUST_404 and CUST_405", principal=PRINCIPALS["analyst.chen"])
    assert result.executed
    assert {row["customer_id"] for row in result.payload} == {"CUST_404", "CUST_405"}


def test_quota_denial_is_recorded_in_the_audit_trail(agent, tmp_path):
    agent.run(
        "Compare CUST_404, CUST_405 and CUST_406", principal=PRINCIPALS["analyst.chen"]
    )
    log = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["allowed"] is False
    assert records[0]["control"] == "RESOURCE_QUOTA"
    assert records[0]["principal_id"] == "analyst.chen"
    assert records[0]["executed"] is False


# --------------------------------------------------------------------------- #
# Rate budget, backed by a SQL aggregate over the audit trail
# --------------------------------------------------------------------------- #

def test_count_granted_since_ignores_denied_and_stale_calls(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    trail = AuditTrail(log_path=log_path)
    now = datetime.now(timezone.utc)

    def write(allowed, executed, age_seconds):
        from src.governance.audit import AuditRecord

        record = AuditRecord(
            request_id="r",
            prompt="p",
            function="get_customer_anomaly_score",
            allowed=allowed,
            control="FUNCTION_GRANT",
            detail="d",
            principal_id="analyst.chen",
            principal_role="Customer Analyst",
            executed=executed,
        )
        record.recorded_at = (now - timedelta(seconds=age_seconds)).isoformat()
        trail.record(record)

    write(allowed=True, executed=True, age_seconds=10)   # counts
    write(allowed=True, executed=True, age_seconds=30)   # counts
    write(allowed=False, executed=False, age_seconds=5)  # denied -- excluded
    write(allowed=True, executed=True, age_seconds=120)  # outside the window -- excluded

    count = AuditEngine(log_path=log_path).count_granted_since(
        "analyst.chen", now - timedelta(minutes=1)
    )
    assert count == 2


def test_rate_limit_denies_once_the_budget_is_spent(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    trail = AuditTrail(log_path=log_path)
    analyst = PRINCIPALS["analyst.chen"]  # budgeted for 10 calls/minute
    now = datetime.now(timezone.utc)

    from src.governance.audit import AuditRecord

    for i in range(analyst.rate_limit_per_minute):
        record = AuditRecord(
            request_id=f"r{i}",
            prompt="p",
            function="get_customer_anomaly_score",
            allowed=True,
            control="FUNCTION_GRANT",
            detail="d",
            principal_id=analyst.principal_id,
            principal_role=analyst.role,
            executed=True,
        )
        record.recorded_at = (now - timedelta(seconds=1)).isoformat()
        trail.record(record)

    decision = enforce_quota(analyst, 1, log_path)
    assert decision is not None
    assert not decision.allowed
    assert decision.control == "RESOURCE_QUOTA"
    assert "per minute" in decision.detail


def test_rate_limit_is_silent_when_under_budget(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    decision = enforce_quota(PRINCIPALS["analyst.chen"], 1, log_path)
    assert decision is None


def test_unrestricted_principal_has_no_rate_limit(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    trail = AuditTrail(log_path=log_path)
    from src.governance.audit import AuditRecord

    for i in range(100):
        trail.record(
            AuditRecord(
                request_id=f"r{i}",
                prompt="p",
                function="get_customer_anomaly_score",
                allowed=True,
                control="FUNCTION_GRANT",
                detail="d",
                principal_id=SERVICE_PRINCIPAL.principal_id,
                principal_role=SERVICE_PRINCIPAL.role,
                executed=True,
            )
        )

    decision = enforce_quota(SERVICE_PRINCIPAL, 1, log_path)
    assert decision is None

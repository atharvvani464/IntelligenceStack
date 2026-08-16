"""Tests for identity-aware governance.

The property under test: the boundary bounds not only *what the agent may do*
but *what this caller may ask it to do, against which rows*. Two people asking
the same question must get different, correct outcomes.
"""

import json

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import AuditTrail
from src.governance.identity import (
    PRINCIPALS,
    SERVICE_PRINCIPAL,
    resolve_principal,
)
from src.governance.policy import enforce
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
# Resolving a caller
# --------------------------------------------------------------------------- #

def test_no_caller_resolves_to_the_service_identity():
    assert resolve_principal(None) is SERVICE_PRINCIPAL
    assert resolve_principal("") is SERVICE_PRINCIPAL


def test_unknown_caller_fails_closed():
    """A typo in a caller header must never escalate to unrestricted access."""
    unknown = resolve_principal("nobody.here")
    assert unknown.allowed_functions == frozenset()
    assert not unknown.may_see_customer("CUST_404")
    assert not unknown.can_view_audit


def test_scope_boundaries_are_inclusive():
    analyst = PRINCIPALS["analyst.chen"]
    assert analyst.may_see_customer("CUST_400")
    assert analyst.may_see_customer("CUST_419")
    assert not analyst.may_see_customer("CUST_420")
    assert not analyst.may_see_customer("CUST_399")


def test_unparseable_identifier_is_refused_not_assumed():
    analyst = PRINCIPALS["analyst.chen"]
    assert not analyst.may_see_customer("not-a-customer")


# --------------------------------------------------------------------------- #
# The ENTITLEMENT control
# --------------------------------------------------------------------------- #

def test_row_scope_refuses_a_customer_outside_the_book():
    decision = enforce(
        "get_customer_anomaly_score",
        {"target_id": "CUST_431"},
        PRINCIPALS["analyst.chen"],
    )
    assert not decision.allowed
    assert decision.control == "ENTITLEMENT"
    assert "outside their assigned scope" in decision.detail


def test_same_call_is_granted_inside_the_book():
    decision = enforce(
        "get_customer_anomaly_score",
        {"target_id": "CUST_404"},
        PRINCIPALS["analyst.chen"],
    )
    assert decision.allowed
    assert decision.principal_id == "analyst.chen"


def test_auditor_may_not_touch_customer_data():
    decision = enforce(
        "get_customer_anomaly_score",
        {"target_id": "CUST_404"},
        PRINCIPALS["auditor.silva"],
    )
    assert not decision.allowed
    assert decision.control == "ENTITLEMENT"
    assert "not entitled to invoke" in decision.detail


def test_auditor_may_still_read_the_trail():
    """Oversight without exposure: no data access, full audit access."""
    assert PRINCIPALS["auditor.silva"].can_view_audit
    assert PRINCIPALS["auditor.silva"].allowed_functions == frozenset()


def test_sre_is_unrestricted_across_customers():
    for customer in ("CUST_400", "CUST_431", "CUST_459"):
        decision = enforce(
            "get_customer_anomaly_score", {"target_id": customer}, PRINCIPALS["sre.okafor"]
        )
        assert decision.allowed, customer


def test_entitlement_is_checked_after_injection():
    """A hostile value is reported as injection, not as an authorisation problem."""
    decision = enforce(
        "get_customer_anomaly_score",
        {"target_id": "CUST_431; DROP TABLE x"},
        PRINCIPALS["analyst.chen"],
    )
    assert not decision.allowed
    assert decision.control == "SQL_INTERDICTION"


# --------------------------------------------------------------------------- #
# End to end through the agent
# --------------------------------------------------------------------------- #

def test_same_question_two_callers_two_outcomes(agent):
    """The headline behaviour: entitlement changes the answer, correctly."""
    question = "Evaluate anomaly parameters for customer CUST_431"

    sre = agent.run(question, principal=PRINCIPALS["sre.okafor"])
    analyst = agent.run(question, principal=PRINCIPALS["analyst.chen"])

    assert sre.executed and sre.payload[0]["customer_id"] == "CUST_431"
    assert not analyst.executed
    assert analyst.governance["control"] == "ENTITLEMENT"


def test_out_of_scope_customer_reaches_no_engine(agent):
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_431",
        principal=PRINCIPALS["analyst.chen"],
    )
    assert result.payload == []
    assert any(t.step == "Execution" and t.status == "Blocked" for t in result.trace)


def test_comparison_is_refused_when_one_customer_is_out_of_scope(agent):
    """Fail closed: a partially-entitled comparison returns nothing, not half."""
    result = agent.run(
        "Compare CUST_404 and CUST_431", principal=PRINCIPALS["analyst.chen"]
    )
    assert not result.executed
    assert result.payload == []
    assert result.governance["control"] == "ENTITLEMENT"


def test_callerless_request_still_works(agent):
    """Backward compatibility: the sandbox's previous behaviour is preserved."""
    result = agent.run("Evaluate anomaly parameters for customer CUST_431")
    assert result.executed
    assert result.principal_id == SERVICE_PRINCIPAL.principal_id


# --------------------------------------------------------------------------- #
# The trail now answers "who?"
# --------------------------------------------------------------------------- #

def test_audit_records_the_caller(agent, tmp_path):
    agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    )
    log = next(tmp_path.glob("*.jsonl"))
    record = json.loads(log.read_text().splitlines()[0])
    assert record["principal_id"] == "analyst.chen"
    assert record["principal_role"] == "Customer Analyst"


def test_entitlement_denials_are_recorded(agent, tmp_path):
    agent.run(
        "Evaluate anomaly parameters for customer CUST_431",
        principal=PRINCIPALS["analyst.chen"],
    )
    log = next(tmp_path.glob("*.jsonl"))
    record = json.loads(log.read_text().splitlines()[0])
    assert record["allowed"] is False
    assert record["control"] == "ENTITLEMENT"
    assert record["principal_id"] == "analyst.chen"


def test_audit_engine_groups_by_principal(agent, tmp_path):
    agent.run("Evaluate anomaly parameters for CUST_404", principal=PRINCIPALS["sre.okafor"])
    agent.run("Evaluate anomaly parameters for CUST_431", principal=PRINCIPALS["analyst.chen"])

    rows = {r["principal"]: r for r in AuditEngine(log_path=next(tmp_path.glob("*.jsonl"))).by_principal()}
    assert rows["sre.okafor"]["granted"] == 1
    assert rows["analyst.chen"]["denied"] == 1

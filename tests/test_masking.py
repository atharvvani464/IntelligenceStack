"""Tests for column-level masking (the third axis of least privilege).

The property under test: a masked column is redacted everywhere a caller
could read it back -- the raw payload *and* the synthesized answer -- not
just the table the UI happens to render. A mask that only hides the payload
while the narrative still quotes the real number would be theatre, not
governance.
"""

import json

import pytest

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import AuditTrail
from src.governance.identity import PRINCIPALS, SERVICE_PRINCIPAL
from src.governance.masking import MASK_SENTINEL, mask_rows
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
# mask_rows: the pure transform
# --------------------------------------------------------------------------- #

def test_no_masked_columns_returns_rows_unchanged():
    rows = [{"a": 1, "b": 2}]
    assert mask_rows(rows, frozenset()) == rows


def test_masked_columns_are_redacted():
    rows = [{"a": 1, "b": 2}]
    masked = mask_rows(rows, frozenset({"b"}))
    assert masked == [{"a": 1, "b": MASK_SENTINEL}]


def test_mask_rows_does_not_mutate_the_input():
    rows = [{"a": 1, "b": 2}]
    mask_rows(rows, frozenset({"b"}))
    assert rows == [{"a": 1, "b": 2}], "the original row must be untouched"


# --------------------------------------------------------------------------- #
# Wired into the agent
# --------------------------------------------------------------------------- #

def test_unmasked_principal_sees_raw_counts(agent):
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["sre.okafor"],
    )
    row = result.payload[0]
    assert isinstance(row["total_events"], int)
    assert isinstance(row["total_anomalies"], int)


def test_masked_principal_sees_redacted_counts_not_raw_ones(agent):
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    )
    row = result.payload[0]
    assert row["total_events"] == MASK_SENTINEL
    assert row["total_anomalies"] == MASK_SENTINEL
    # Unrelated columns are untouched -- masking is scoped, not blanket.
    assert row["customer_id"] == "CUST_404"
    assert isinstance(row["risk_factor"], float)
    assert isinstance(row["mean_latency_ms"], float)


def test_masking_does_not_change_the_actual_risk_figure(agent):
    """The masked analyst still sees the real, correct risk factor."""
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    )
    assert result.payload[0]["risk_factor"] == 82.35


def test_timeline_columns_are_not_masked_by_the_same_rule(agent):
    """`events`/`anomalies` (timeline) share no name with `total_events`/
    `total_anomalies` (anomaly score), so the analyst's mask has no effect
    on the timeline function -- masking is scoped by column name, not by
    caller-wide blanket redaction."""
    result = agent.run(
        "Is CUST_404 getting worse over time?", principal=PRINCIPALS["analyst.chen"]
    )
    assert result.executed
    assert all(isinstance(row["events"], int) for row in result.payload)
    assert all(isinstance(row["anomalies"], int) for row in result.payload)


def test_masking_is_recorded_in_the_trace(agent):
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    )
    masking_steps = [t for t in result.trace if t.step == "Column Masking"]
    assert len(masking_steps) == 1
    assert masking_steps[0].status == "Applied"
    assert "total_anomalies" in masking_steps[0].detail
    assert "total_events" in masking_steps[0].detail


def test_unmasked_principal_has_no_masking_trace_step(agent):
    result = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["sre.okafor"],
    )
    assert not any(t.step == "Column Masking" for t in result.trace)


def test_masking_is_recorded_in_the_audit_trail(agent, tmp_path):
    agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    )
    log = next(tmp_path.glob("*.jsonl"))
    record = json.loads(log.read_text().splitlines()[0])
    assert sorted(record["masked_columns"]) == ["total_anomalies", "total_events"]


def test_unmasked_call_records_an_empty_list(agent, tmp_path):
    agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["sre.okafor"],
    )
    log = next(tmp_path.glob("*.jsonl"))
    record = json.loads(log.read_text().splitlines()[0])
    assert record["masked_columns"] == []


# --------------------------------------------------------------------------- #
# The narrative must not leak what the payload redacts
# --------------------------------------------------------------------------- #

def test_synthesised_answer_redacts_the_same_figure(agent):
    """A mask that only hid the payload table while the prose still quoted
    the real count would be theatre, not governance."""
    sre_answer = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["sre.okafor"],
    ).answer
    analyst_answer = agent.run(
        "Evaluate anomaly parameters for customer CUST_404",
        principal=PRINCIPALS["analyst.chen"],
    ).answer

    assert "51 observed events" in sre_answer
    assert "51 observed events" not in analyst_answer
    assert MASK_SENTINEL in analyst_answer
    # The risk factor is not masked, so it must still appear identically.
    assert "82.35" in sre_answer and "82.35" in analyst_answer


def test_comparison_narrative_also_respects_masking(agent):
    answer = agent.run(
        "Compare CUST_404 and CUST_405", principal=PRINCIPALS["analyst.chen"]
    ).answer
    assert MASK_SENTINEL in answer


def test_callerless_request_is_unrestricted_by_masking(agent):
    result = agent.run("Evaluate anomaly parameters for customer CUST_404")
    assert result.principal_id == SERVICE_PRINCIPAL.principal_id
    assert isinstance(result.payload[0]["total_events"], int)

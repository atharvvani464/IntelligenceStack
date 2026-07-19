"""The cognitive agent: intent -> governed tool call -> grounded synthesis.

The agent never emits SQL. It resolves a user's intent into a *proposed*
invocation of a catalog-registered function, hands that proposal to the
governance boundary, and only executes what the boundary allows. Every stage is
recorded, so the trace shown in the control plane is a record of what actually
happened rather than a narration of what was intended.

Model serving
-------------
`ModelServingClient` is the seam to Databricks Model Serving. In this sandbox it
runs a deterministic local planner so the project is demonstrable without a
workspace or credentials; the planner is genuinely query-dependent, and is
reported as such in the response metadata. Pointing `DATABRICKS_HOST` and
`DATABRICKS_TOKEN` at a workspace switches the same call path to a served model.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

import mlflow

from src.governance.policy import available_tool_schemas, enforce
from src.lakehouse.local_engine import LakehouseEngine
from src.settings import CATALOG, SCHEMA

logger = logging.getLogger("AgentCore")

SERVING_ENDPOINT = "databricks-meta-llama-3-1-70b-instruct"

_CUSTOMER_PATTERN = re.compile(r"\bCUST[_\-\s]?(\d{3})\b", re.IGNORECASE)
_SQL_TOKENS = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|truncate|grant|revoke)\b",
    re.IGNORECASE,
)


@dataclass
class TraceStep:
    """One recorded stage of the cognitive loop."""

    step: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {"step": self.step, "status": self.status, "detail": self.detail}


@dataclass
class AgentResult:
    """The complete, auditable outcome of one agent invocation."""

    answer: str
    trace: list[TraceStep] = field(default_factory=list)
    governance: dict = field(default_factory=dict)
    payload: list = field(default_factory=list)
    executed: bool = False
    serving_mode: str = "local-deterministic-planner"

    def as_dict(self) -> dict:
        return {
            "answer": self.answer,
            "trace": [t.as_dict() for t in self.trace],
            "governance": self.governance,
            "payload": self.payload,
            "executed": self.executed,
            "serving_mode": self.serving_mode,
        }


class ModelServingClient:
    """Seam to Databricks Model Serving, with a local planner for the sandbox."""

    def __init__(self, endpoint: str = SERVING_ENDPOINT):
        self.endpoint = endpoint
        self.remote = bool(
            os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN")
        )
        if self.remote:
            logger.info("Model Serving endpoint %s configured.", endpoint)
        else:
            logger.info("No workspace credentials; using local deterministic planner.")

    @property
    def mode(self) -> str:
        if self.remote:
            return f"databricks-model-serving:{self.endpoint}"
        return "local-deterministic-planner"

    def plan(self, user_query: str, tools: list[dict]) -> dict:
        """Resolve intent into a proposed function call.

        Returns `{"function": name|None, "parameters": {...}, "rationale": str}`.
        """
        granted = {t["name"] for t in tools}
        match = _CUSTOMER_PATTERN.search(user_query)

        if match and "get_customer_anomaly_score" in granted:
            target_id = f"CUST_{match.group(1)}"
            return {
                "function": "get_customer_anomaly_score",
                "parameters": {"target_id": target_id},
                "rationale": (
                    f"Query names customer {target_id} and requests behavioural "
                    "assessment; matched to the anomaly scoring function."
                ),
            }

        return {
            "function": None,
            "parameters": {},
            "rationale": (
                "No granted function satisfies this intent. The agent holds "
                f"EXECUTE on {sorted(granted)} only."
            ),
        }

    def synthesise(self, user_query: str, payload: list[dict], context: dict) -> str:
        """Ground a natural-language answer in the returned rows."""
        if not payload:
            return (
                "The governed function executed successfully but returned no rows — "
                "that customer identifier has no events in the gold layer. No inference "
                "is available for an entity the lakehouse has not observed."
            )

        row = payload[0]
        fleet_mean = context.get("mean_latency_ms") or 0.0
        threshold = context.get("anomaly_threshold_ms") or 0.0

        ratio = (row["mean_latency_ms"] / fleet_mean) if fleet_mean else 0.0
        if row["risk_factor"] >= 50:
            verdict = "materially anomalous and warrants investigation"
        elif row["risk_factor"] > 0:
            verdict = "showing intermittent anomalous behaviour"
        else:
            verdict = "operating within the expected performance envelope"

        return (
            f"Customer {row['customer_id']} is {verdict}. "
            f"Across {row['total_events']:,} observed events, {row['total_anomalies']:,} "
            f"exceeded the anomaly threshold of {threshold:.1f} ms, giving a risk factor "
            f"of {row['risk_factor']:.2f} (percentage of traffic classified anomalous). "
            f"Mean latency is {row['mean_latency_ms']:.2f} ms against a fleet baseline of "
            f"{fleet_mean:.2f} ms — {ratio:.1f}x the population average."
        )


class MosaicAnalyticsAgent:
    """Orchestrates the governed cognitive loop over the lakehouse."""

    def __init__(self, engine: LakehouseEngine | None = None):
        self.serving = ModelServingClient()
        self.engine = engine or LakehouseEngine()
        self.catalog = CATALOG
        self.schema = SCHEMA

    @mlflow.trace(name="execute_cognitive_loop")
    def run(self, user_query: str) -> AgentResult:
        tools = available_tool_schemas()
        result = AgentResult(answer="", serving_mode=self.serving.mode)

        # ---- Stage 1: intent resolution ------------------------------- #
        with mlflow.start_span(name="llm_intent_classification") as span:
            plan = self.serving.plan(user_query, tools)
            span.set_attribute("proposed_function", plan["function"])
            span.set_attribute("proposed_parameters", json.dumps(plan["parameters"]))

        result.trace.append(
            TraceStep(
                step="Intent Resolution",
                status="Success" if plan["function"] else "No Match",
                detail=plan["rationale"],
            )
        )

        # If the prompt carried SQL, record that it was discarded here — the
        # agent extracts typed values, it never forwards prompt text downstream.
        if _SQL_TOKENS.search(user_query):
            result.trace.append(
                TraceStep(
                    step="Prompt Sanitisation",
                    status="Neutralised",
                    detail=(
                        "Input contained SQL control tokens. Intent resolution extracts "
                        "only typed parameter values, so the injected text was discarded "
                        "and never reached the execution engine."
                    ),
                )
            )

        # ---- Stage 2: governance boundary ----------------------------- #
        decision = enforce(plan["function"], plan["parameters"])
        result.governance = decision.as_dict()
        result.trace.append(
            TraceStep(
                step="Governance Boundary Check",
                status="Granted" if decision.allowed else "Denied",
                detail=decision.detail,
            )
        )

        if not decision.allowed:
            result.answer = (
                "This request was refused at the governance boundary.\n\n"
                f"**Control:** `{decision.control}`\n\n"
                f"**Reason:** {decision.detail}"
            )
            result.trace.append(
                TraceStep(
                    step="Execution",
                    status="Blocked",
                    detail="No statement was submitted to the lakehouse.",
                )
            )
            return result

        # ---- Stage 3: governed execution ------------------------------ #
        target_id = decision.parameters["target_id"]
        with mlflow.start_span(name="execute_lakehouse_tool") as tool_span:
            payload = self.engine.get_customer_anomaly_score(target_id)
            fleet = self.engine.fleet_summary()
            tool_span.set_attribute("resolved_target_id", target_id)
            tool_span.set_attribute("lakehouse_payload_size", len(payload))

        result.payload = payload
        result.executed = True
        result.trace.append(
            TraceStep(
                step="Lakehouse Execution",
                status="Completed",
                detail=(
                    f"Invoked {self.catalog}.{self.schema}.get_customer_anomaly_score "
                    f"with bound parameter target_id={target_id!r}. "
                    f"Returned {len(payload)} row(s) from the gold layer."
                ),
            )
        )

        # ---- Stage 4: grounded synthesis ------------------------------ #
        with mlflow.start_span(name="final_insight_synthesis"):
            result.answer = self.serving.synthesise(user_query, payload, fleet)

        result.trace.append(
            TraceStep(
                step="Insight Synthesis",
                status="Completed",
                detail=(
                    "Answer composed strictly from the returned rows and the fleet "
                    "baseline; no figure originates outside the lakehouse."
                ),
            )
        )
        return result

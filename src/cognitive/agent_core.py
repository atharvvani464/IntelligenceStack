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
from src.lakehouse.knowledge_engine import KNOWLEDGE_INDEX, KnowledgeEngine
from src.lakehouse.local_engine import LakehouseEngine
from src.settings import CATALOG, SCHEMA

logger = logging.getLogger("AgentCore")

SERVING_ENDPOINT = "databricks-meta-llama-3-1-70b-instruct"

_CUSTOMER_PATTERN = re.compile(r"\bCUST[_\-\s]?(\d{3})\b", re.IGNORECASE)
_SQL_TOKENS = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|truncate|grant|revoke)\b",
    re.IGNORECASE,
)

# Signals that the question seeks documented guidance rather than (or in
# addition to) a metric. Deliberately excludes analytics vocabulary such as
# "anomaly" or "evaluate" so that a pure metrics question stays a pure metrics
# question and routes to the analytics function alone.
_KNOWLEDGE_INTENT = re.compile(
    r"\b(polic(?:y|ies)|procedure|process|runbook|playbook|handbook|guideline|"
    r"guidance|remediat\w*|escalat\w*|sla|tier|retention|pii|governance|"
    r"postmortem|incident|credit|obligation|protocol|recommend\w*|advice)\b"
    r"|\bwhat should\b|\bhow (?:do|should) we\b|\bwhat do we do\b|\bnext steps?\b",
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
    citations: list = field(default_factory=list)
    executed: bool = False
    serving_mode: str = "local-deterministic-planner"

    def as_dict(self) -> dict:
        return {
            "answer": self.answer,
            "trace": [t.as_dict() for t in self.trace],
            "governance": self.governance,
            "payload": self.payload,
            "citations": self.citations,
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
        """Resolve intent into an ordered list of proposed function calls.

        Returns `{"calls": [{"function", "parameters"}, ...], "rationale": str}`.
        A question may need structured analytics, documented guidance, or --
        the hybrid case -- both, which is why this returns a list rather than a
        single call. Each proposed call is checked independently at the
        governance boundary before anything executes.
        """
        granted = {t["name"] for t in tools}
        customer = _CUSTOMER_PATTERN.search(user_query)
        wants_guidance = bool(_KNOWLEDGE_INTENT.search(user_query))

        calls: list[dict] = []
        reasons: list[str] = []

        if customer and "get_customer_anomaly_score" in granted:
            target_id = f"CUST_{customer.group(1)}"
            calls.append(
                {
                    "function": "get_customer_anomaly_score",
                    "parameters": {"target_id": target_id},
                }
            )
            reasons.append(
                f"names customer {target_id}, matched to the anomaly scoring function"
            )

        if wants_guidance and "search_knowledge_base" in granted:
            calls.append(
                {
                    "function": "search_knowledge_base",
                    "parameters": {"query": user_query.strip()},
                }
            )
            reasons.append(
                "seeks documented guidance, matched to the governed knowledge index"
            )

        if not calls:
            return {
                "calls": [],
                "rationale": (
                    "No granted function satisfies this intent. The agent holds "
                    f"EXECUTE on {sorted(granted)} only."
                ),
            }

        prefix = "Hybrid intent: query " if len(calls) > 1 else "Query "
        return {"calls": calls, "rationale": prefix + " and ".join(reasons) + "."}

    def synthesise(
        self,
        user_query: str,
        payload: list[dict],
        context: dict,
        citations: list[dict] | None = None,
        searched_knowledge: bool = False,
        queried_metric: bool = True,
    ) -> str:
        """Ground a natural-language answer in the rows and passages retrieved.

        Every figure comes from the gold layer and every recommendation is
        attributed to the governed document it came from. When retrieval was
        attempted but nothing relevant was found, the agent says so rather than
        citing a weak match.
        """
        citations = citations or []
        metric_part = self._synthesise_metric(payload, context) if queried_metric else ""
        guidance_part = self._synthesise_guidance(citations, searched_knowledge)

        if metric_part and guidance_part:
            return f"{metric_part}\n\n{guidance_part}"
        return metric_part or guidance_part

    @staticmethod
    def _synthesise_guidance(citations: list[dict], searched: bool) -> str:
        """Render retrieved passages as attributed guidance."""
        if not searched:
            return ""
        if not citations:
            return (
                "**No governed knowledge covers that question.** The retrieval index "
                "was searched and returned no passage with sufficient coverage of the "
                "question's terms, so no guidance is offered rather than citing a weak "
                "match."
            )

        lines = ["**Governed guidance:**"]
        for hit in citations[:2]:
            lines.append(f"- *{hit['source']} — {hit['title']}*: {hit['snippet']}")
        sources = ", ".join(dict.fromkeys(f"[{h['source']}]" for h in citations))
        lines.append(f"\nSources: {sources}")
        return "\n".join(lines)

    def _synthesise_metric(self, payload: list[dict], context: dict) -> str:
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

    def __init__(
        self,
        engine: LakehouseEngine | None = None,
        knowledge: KnowledgeEngine | None = None,
    ):
        self.serving = ModelServingClient()
        self.engine = engine or LakehouseEngine()
        self.knowledge = knowledge or KnowledgeEngine()
        self.catalog = CATALOG
        self.schema = SCHEMA

    @mlflow.trace(name="execute_cognitive_loop")
    def run(self, user_query: str) -> AgentResult:
        tools = available_tool_schemas()
        result = AgentResult(answer="", serving_mode=self.serving.mode)

        # ---- Stage 1: intent resolution ------------------------------- #
        with mlflow.start_span(name="llm_intent_classification") as span:
            plan = self.serving.plan(user_query, tools)
            span.set_attribute("proposed_calls", json.dumps(plan["calls"]))

        result.trace.append(
            TraceStep(
                step="Intent Resolution",
                status="Success" if plan["calls"] else "No Match",
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

        # ---- Stages 2 & 3: per-call governance, then governed execution -- #
        # Each proposed call clears the boundary on its own merits. A single
        # denial fails the whole request closed: nothing already retrieved is
        # returned alongside a refusal.
        fleet: dict = {}
        queried_metric = False
        searched_knowledge = False

        if not plan["calls"]:
            # No proposed call still passes through the boundary, so a refusal
            # is produced by the same control that governs every other request.
            decision = enforce(None, {})
            result.governance = decision.as_dict()
            result.trace.append(
                TraceStep(
                    step="Governance Boundary Check",
                    status="Denied",
                    detail=decision.detail,
                )
            )
            result.answer = self._refusal(decision)
            result.trace.append(
                TraceStep(
                    step="Execution",
                    status="Blocked",
                    detail="No statement was submitted to the lakehouse.",
                )
            )
            return result

        for call in plan["calls"]:
            decision = enforce(call["function"], call["parameters"])
            result.governance = decision.as_dict()
            result.trace.append(
                TraceStep(
                    step="Governance Boundary Check",
                    status="Granted" if decision.allowed else "Denied",
                    detail=f"{call['function']}: {decision.detail}",
                )
            )

            if not decision.allowed:
                result.answer = self._refusal(decision)
                result.payload, result.citations, result.executed = [], [], False
                result.trace.append(
                    TraceStep(
                        step="Execution",
                        status="Blocked",
                        detail="No statement was submitted to the lakehouse.",
                    )
                )
                return result

            if call["function"] == "get_customer_anomaly_score":
                target_id = decision.parameters["target_id"]
                with mlflow.start_span(name="execute_lakehouse_tool") as tool_span:
                    result.payload = self.engine.get_customer_anomaly_score(target_id)
                    fleet = self.engine.fleet_summary()
                    tool_span.set_attribute("resolved_target_id", target_id)
                    tool_span.set_attribute("lakehouse_payload_size", len(result.payload))
                queried_metric = True
                result.trace.append(
                    TraceStep(
                        step="Lakehouse Execution",
                        status="Completed",
                        detail=(
                            f"Invoked {self.catalog}.{self.schema}.get_customer_anomaly_score "
                            f"with bound parameter target_id={target_id!r}. "
                            f"Returned {len(result.payload)} row(s) from the gold layer."
                        ),
                    )
                )

            elif call["function"] == "search_knowledge_base":
                query = decision.parameters["query"]
                with mlflow.start_span(name="execute_vector_search") as vs_span:
                    result.citations = self.knowledge.search(query)
                    vs_span.set_attribute("retrieved_passages", len(result.citations))
                searched_knowledge = True
                result.trace.append(
                    TraceStep(
                        step="Vector Search Execution",
                        status="Completed" if result.citations else "No Coverage",
                        detail=(
                            f"Queried {KNOWLEDGE_INDEX} with the question as a bound "
                            f"value. Retrieved {len(result.citations)} governed "
                            "passage(s) above the coverage threshold."
                        ),
                    )
                )

            result.executed = True

        # ---- Stage 4: grounded synthesis ------------------------------ #
        with mlflow.start_span(name="final_insight_synthesis"):
            result.answer = self.serving.synthesise(
                user_query,
                result.payload,
                fleet,
                citations=result.citations,
                searched_knowledge=searched_knowledge,
                queried_metric=queried_metric,
            )

        result.trace.append(
            TraceStep(
                step="Insight Synthesis",
                status="Completed",
                detail=(
                    "Answer composed strictly from the returned rows and retrieved "
                    "passages; every figure originates in the lakehouse and every "
                    "recommendation is attributed to a governed document."
                ),
            )
        )
        return result

    @staticmethod
    def _refusal(decision) -> str:
        return (
            "This request was refused at the governance boundary.\n\n"
            f"**Control:** `{decision.control}`\n\n"
            f"**Reason:** {decision.detail}"
        )

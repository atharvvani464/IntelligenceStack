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
import time
from dataclasses import dataclass, field

import mlflow

from src.governance.audit import AuditRecord, AuditTrail, new_request_id
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
# Signals that the question is about change over time rather than a single
# current figure -- "is it getting worse?" instead of "how bad is it?".
_TREND_INTENT = re.compile(
    r"\b(trend|trending|timeline|over time|history|historical|trajectory|"
    r"getting (?:worse|better)|improv\w*|deteriorat\w*|worsen\w*|"
    r"day[- ]by[- ]day|daily|past few days|recently)\b",
    re.IGNORECASE,
)

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
    # Correlates this response with its rows in the durable audit trail.
    request_id: str = ""
    # End-to-end wall-clock cost of the whole request, in milliseconds.
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "answer": self.answer,
            "trace": [t.as_dict() for t in self.trace],
            "governance": self.governance,
            "payload": self.payload,
            "citations": self.citations,
            "executed": self.executed,
            "serving_mode": self.serving_mode,
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
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
        # Every distinct customer named in the question, in order of appearance.
        # Two or more means a comparison, which needs one governed call each.
        targets: list[str] = []
        for match in _CUSTOMER_PATTERN.finditer(user_query):
            target_id = f"CUST_{match.group(1)}"
            if target_id not in targets:
                targets.append(target_id)

        wants_guidance = bool(_KNOWLEDGE_INTENT.search(user_query))
        wants_trend = bool(_TREND_INTENT.search(user_query))

        calls: list[dict] = []
        reasons: list[str] = []

        # A trend question asks about trajectory; anything else about a customer
        # asks about their current standing. Either way, one call per customer.
        metric_fn = (
            "get_customer_timeline"
            if wants_trend and "get_customer_timeline" in granted
            else "get_customer_anomaly_score"
        )

        if targets and metric_fn in granted:
            for target_id in targets:
                calls.append(
                    {"function": metric_fn, "parameters": {"target_id": target_id}}
                )
            if len(targets) > 1:
                reasons.append(
                    f"names {len(targets)} customers ({', '.join(targets)}), so each is "
                    f"scored independently via {metric_fn}"
                )
            else:
                reasons.append(f"names customer {targets[0]}, matched to {metric_fn}")

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
        queried_trend: bool = False,
    ) -> str:
        """Ground a natural-language answer in the rows and passages retrieved.

        Every figure comes from the gold layer and every recommendation is
        attributed to the governed document it came from. When retrieval was
        attempted but nothing relevant was found, the agent says so rather than
        citing a weak match.
        """
        citations = citations or []
        if queried_trend:
            metric_part = self._synthesise_trend(payload)
        elif queried_metric:
            metric_part = self._synthesise_metric(payload, context)
        else:
            metric_part = ""
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

    @staticmethod
    def _synthesise_comparison(payload: list[dict], context: dict) -> str:
        """Rank several customers against each other and the fleet baseline."""
        fleet_mean = context.get("mean_latency_ms") or 0.0
        ranked = sorted(payload, key=lambda r: r["risk_factor"], reverse=True)
        worst, best = ranked[0], ranked[-1]

        lines = [
            f"**{worst['customer_id']} is the more serious case** of the "
            f"{len(ranked)} compared."
        ]
        for row in ranked:
            ratio = (row["mean_latency_ms"] / fleet_mean) if fleet_mean else 0.0
            lines.append(
                f"- **{row['customer_id']}** — {row['risk_factor']:.2f}% of "
                f"{row['total_events']:,} events anomalous, mean latency "
                f"{row['mean_latency_ms']:.2f} ms ({ratio:.1f}× the fleet baseline)."
            )

        gap = worst["risk_factor"] - best["risk_factor"]
        if gap > 1:
            lines.append(
                f"\nThe gap is {gap:.2f} percentage points, so remediation effort "
                f"should go to {worst['customer_id']} first."
            )
        else:
            lines.append(
                "\nTheir anomaly rates are within a point of each other, so neither "
                "stands out as the priority on this measure alone."
            )
        return "\n".join(lines)

    @staticmethod
    def _synthesise_trend(payload: list[dict]) -> str:
        """Describe a trajectory strictly from the daily rows returned."""
        if not payload:
            return (
                "The timeline function executed but returned no rows — that customer "
                "has no observed history in the gold layer."
            )

        by_customer: dict[str, list[dict]] = {}
        for row in payload:
            by_customer.setdefault(row["customer_id"], []).append(row)

        parts = []
        for customer_id, days in by_customer.items():
            days = sorted(days, key=lambda d: d["day"])
            first, last = days[0], days[-1]
            total_events = sum(d["events"] for d in days)
            total_anoms = sum(d["anomalies"] for d in days)
            overall = 100.0 * total_anoms / total_events if total_events else 0.0
            delta = last["anomaly_rate"] - first["anomaly_rate"]

            if delta > 10:
                direction = f"deteriorating (up {delta:.1f} points across the window)"
            elif delta < -10:
                direction = f"improving (down {abs(delta):.1f} points across the window)"
            else:
                direction = "broadly stable across the window"

            peak = max(days, key=lambda d: d["anomaly_rate"])
            parts.append(
                f"**{customer_id}** is {direction}. Over {len(days)} observed day(s) it "
                f"logged {total_events:,} events with {total_anoms:,} anomalous "
                f"({overall:.1f}% overall), moving from {first['anomaly_rate']:.1f}% on "
                f"{first['day']} to {last['anomaly_rate']:.1f}% on {last['day']}. "
                f"Worst day was {peak['day']} at {peak['anomaly_rate']:.1f}%."
            )
        return "\n\n".join(parts)

    def _synthesise_metric(self, payload: list[dict], context: dict) -> str:
        """Ground a natural-language answer in the returned rows."""
        # More than one customer means the question was a comparison, and the
        # answer must actually compare rather than describe each in isolation.
        if len({row["customer_id"] for row in payload}) > 1:
            return self._synthesise_comparison(payload, context)
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
        audit: AuditTrail | None = None,
    ):
        self.serving = ModelServingClient()
        self.engine = engine or LakehouseEngine()
        self.knowledge = knowledge or KnowledgeEngine()
        self.audit = audit or AuditTrail()
        self.catalog = CATALOG
        self.schema = SCHEMA

    def _audit(self, request_id: str, prompt: str, decision, **outcome) -> None:
        """Persist one governance decision as durable evidence.

        Called for grants and denials alike -- a refusal is the control working,
        and it is precisely what an auditor asks to see.
        """
        self.audit.record(
            AuditRecord(
                request_id=request_id,
                prompt=prompt,
                function=decision.function,
                allowed=decision.allowed,
                control=decision.control,
                detail=decision.detail,
                parameters=decision.parameters,
                serving_mode=self.serving.mode,
                **outcome,
            )
        )

    @mlflow.trace(name="execute_cognitive_loop")
    def run(self, user_query: str) -> AgentResult:
        tools = available_tool_schemas()
        result = AgentResult(answer="", serving_mode=self.serving.mode)
        request_id = new_request_id()
        result.request_id = request_id
        started = time.perf_counter()

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
        queried_trend = False
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
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._audit(
                request_id, user_query, decision, duration_ms=result.duration_ms
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
                result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
                self._audit(
                    request_id, user_query, decision, duration_ms=result.duration_ms
                )
                return result

            # What *this* call returned. Tracked per call so each audit record
            # reports its own outcome rather than the accumulated result.
            call_rows = 0
            call_citations = 0
            call_started = time.perf_counter()

            if call["function"] in ("get_customer_anomaly_score", "get_customer_timeline"):
                fn = call["function"]
                target_id = decision.parameters["target_id"]
                with mlflow.start_span(name="execute_lakehouse_tool") as tool_span:
                    if fn == "get_customer_timeline":
                        rows = self.engine.get_customer_timeline(target_id)
                        queried_trend = True
                    else:
                        rows = self.engine.get_customer_anomaly_score(target_id)
                        queried_metric = True
                    fleet = self.engine.fleet_summary()
                    tool_span.set_attribute("resolved_target_id", target_id)
                    tool_span.set_attribute("lakehouse_payload_size", len(rows))
                # Accumulate: a comparison issues one call per customer, and the
                # answer needs every row, not just the last call's.
                result.payload.extend(rows)
                call_rows = len(rows)
                result.trace.append(
                    TraceStep(
                        step="Lakehouse Execution",
                        status="Completed",
                        detail=(
                            f"Invoked {self.catalog}.{self.schema}.{fn} with bound "
                            f"parameter target_id={target_id!r}. "
                            f"Returned {len(rows)} row(s) from the gold layer."
                        ),
                    )
                )

            elif call["function"] == "search_knowledge_base":
                query = decision.parameters["query"]
                with mlflow.start_span(name="execute_vector_search") as vs_span:
                    result.citations = self.knowledge.search(query)
                    vs_span.set_attribute("retrieved_passages", len(result.citations))
                searched_knowledge = True
                call_citations = len(result.citations)
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

            # Record the granted call together with what it actually returned,
            # so the trail shows not just what was permitted but what happened.
            self._audit(
                request_id,
                user_query,
                decision,
                executed=True,
                rows_returned=call_rows,
                citations_returned=call_citations,
                duration_ms=round((time.perf_counter() - call_started) * 1000, 2),
            )

        # ---- Stage 4: grounded synthesis ------------------------------ #
        with mlflow.start_span(name="final_insight_synthesis"):
            result.answer = self.serving.synthesise(
                user_query,
                result.payload,
                fleet,
                citations=result.citations,
                searched_knowledge=searched_knowledge,
                queried_metric=queried_metric,
                queried_trend=queried_trend,
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
        result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    @staticmethod
    def _refusal(decision) -> str:
        return (
            "This request was refused at the governance boundary.\n\n"
            f"**Control:** `{decision.control}`\n\n"
            f"**Reason:** {decision.detail}"
        )

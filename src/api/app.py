"""FastAPI backend hosting the Lakehouse Agent API.

The endpoint is a thin transport layer over the agent. Every field it returns —
the answer, the trace, the governance decision — originates from the agent's
actual execution, so what the control plane renders is a faithful record rather
than a scripted narrative.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.cognitive.agent_core import MosaicAnalyticsAgent
from src.governance.audit import ensure_audit_dir
from src.governance.identity import directory, resolve_principal
from src.lakehouse.audit_engine import AuditEngine

agent_engine = MosaicAnalyticsAgent()
audit_engine = AuditEngine()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Materialise the medallion layers and the vector index once at startup so
    # the first request is not penalised by the build, and any data or corpus
    # problem surfaces immediately rather than mid-demo.
    agent_engine.engine.build()
    agent_engine.knowledge.build()
    # Create the audit directory up front so the first decision cannot fail to
    # be recorded because the path does not exist yet.
    ensure_audit_dir()
    yield


app = FastAPI(title="IntelligenceStack Lakehouse Agent API", lifespan=lifespan)


class AnalyticsRequest(BaseModel):
    prompt: str
    # Authenticated caller. Omitted means the unrestricted service identity --
    # see identity.SERVICE_PRINCIPAL; production would reject instead.
    principal_id: str | None = None


class TraceEntry(BaseModel):
    step: str
    status: str
    detail: str


class AnalyticsResponse(BaseModel):
    status: str
    answer: str
    executed: bool
    serving_mode: str
    governance: dict
    payload: list
    citations: list
    trace_log: list[TraceEntry]
    # Correlation id for this request's rows in the durable audit trail.
    request_id: str
    # The caller this answer was produced for.
    principal: dict
    # Measured end-to-end cost of this request, in milliseconds.
    duration_ms: float


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "lakehouse": agent_engine.engine.fleet_summary(),
        "knowledge_index": agent_engine.knowledge.index_summary(),
    }


@app.post("/api/v1/agent/explore", response_model=AnalyticsResponse)
async def explore_lakehouse_metrics(payload: AnalyticsRequest) -> AnalyticsResponse:
    try:
        principal = resolve_principal(payload.principal_id)
        result = agent_engine.run(user_query=payload.prompt, principal=principal)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
        raise HTTPException(status_code=500, detail=f"Core execution engine fault: {exc}")

    if result.executed:
        status = "SUCCESS"
    elif not result.governance.get("allowed", True):
        status = "REFUSED"
    else:
        status = "NO_ACTION"

    return AnalyticsResponse(
        status=status,
        answer=result.answer,
        executed=result.executed,
        serving_mode=result.serving_mode,
        governance=result.governance,
        payload=result.payload,
        citations=result.citations,
        trace_log=[TraceEntry(**step.as_dict()) for step in result.trace],
        request_id=result.request_id,
        principal=principal.as_dict(),
        duration_ms=result.duration_ms,
    )


@app.get("/api/v1/identities")
async def identities() -> dict:
    """The demo principal directory, for the control plane's caller switcher."""
    return {"principals": directory()}


@app.get("/api/v1/audit/summary")
async def audit_summary() -> dict:
    """Governance posture across every action ever recorded."""
    return {
        "summary": audit_engine.summary(),
        "by_control": audit_engine.by_control(),
        "by_function": audit_engine.by_function(),
        "by_principal": audit_engine.by_principal(),
        "top_denied": audit_engine.top_denied_prompts(),
    }


@app.get("/api/v1/audit/recent")
async def audit_recent(limit: int = 25) -> dict:
    """The most recent governance decisions, newest first."""
    return {"decisions": audit_engine.recent(limit=limit)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

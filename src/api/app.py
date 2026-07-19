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

agent_engine = MosaicAnalyticsAgent()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Materialise the medallion layers and the vector index once at startup so
    # the first request is not penalised by the build, and any data or corpus
    # problem surfaces immediately rather than mid-demo.
    agent_engine.engine.build()
    agent_engine.knowledge.build()
    yield


app = FastAPI(title="IntelligenceStack Lakehouse Agent API", lifespan=lifespan)


class AnalyticsRequest(BaseModel):
    prompt: str


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
        result = agent_engine.run(user_query=payload.prompt)
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
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

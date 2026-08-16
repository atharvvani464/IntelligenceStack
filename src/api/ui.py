import glob
import json
import os

import pandas as pd
import requests
import streamlit as st

from src.settings import API_BASE_URL, ARCHITECTURE_DIAGRAM, LANDING_ZONE

st.set_page_config(page_title="IntelligenceStack", layout="wide", initial_sidebar_state="expanded")

# Custom CSS for a premium executive look.
st.markdown(
    """
<style>
    .stApp { background-color: #0d1117; color: #c9d1d9; font-family: 'Inter', sans-serif; }
    h1, h2, h3 { color: #ff6c00 !important; font-weight: 600 !important; }
    .stChatMessage {
        background-color: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px; padding: 15px; margin-bottom: 10px; backdrop-filter: blur(10px);
    }
    .css-1d391kg { background-color: #161b22; border-right: 1px solid rgba(255, 255, 255, 0.1); }
    div[data-testid="stMetricValue"] { color: #ff6c00 !important; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(ttl=5)
def load_telemetry() -> pd.DataFrame:
    """Read the newline-delimited JSON landing zone into a flat frame."""
    events = []
    for path in glob.glob(os.path.join(str(LANDING_ZONE), "*.json")):
        with open(path) as handle:
            for line in handle:
                if line.strip():
                    events.append(json.loads(line))
    if not events:
        return pd.DataFrame()

    frame = pd.DataFrame(events)
    frame["latency"] = frame["context"].apply(lambda c: c.get("latency", 0))
    frame["is_pii"] = frame["context"].apply(lambda c: c.get("pii", False))
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame.sort_values("timestamp")


with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/6/63/Databricks_Logo.png", width=200)
    st.markdown("## IntelligenceStack Control Plane")
    st.markdown("---")

    # Reflect the real backend state rather than static toggles.
    try:
        health = requests.get(f"{API_BASE_URL}/health", timeout=5).json()
        st.success("Agent API connected.")
        lake = health.get("lakehouse", {})
        st.caption(
            f"Gold layer: {lake.get('distinct_customers', 0)} customers · "
            f"{lake.get('total_events', 0):,} events"
        )
        st.caption(f"Anomaly threshold: {lake.get('anomaly_threshold_ms', 0):.1f} ms (3σ)")
    except Exception:
        st.error("Agent API unreachable. Start the FastAPI backend on port 8000.")

    # Acting-as switcher. The same question asked by different principals is
    # supposed to produce different, correct outcomes -- that is row-level
    # entitlement, and it is easiest to believe when you can toggle it live.
    st.markdown("### Acting as")
    try:
        principals = requests.get(f"{API_BASE_URL}/api/v1/identities", timeout=5).json()[
            "principals"
        ]
    except Exception:
        principals = []

    if principals:
        labels = {p["principal_id"]: f"{p['display_name']} — {p['role']}" for p in principals}
        chosen = st.selectbox(
            "Caller identity",
            options=list(labels),
            format_func=lambda pid: labels[pid],
            label_visibility="collapsed",
        )
        st.session_state.principal_id = chosen
        current = next(p for p in principals if p["principal_id"] == chosen)
        st.caption(f"**Scope:** {current['customer_scope']}")
        st.caption(
            "**Entitled tools:** "
            + (", ".join(f"`{f}`" for f in current["allowed_functions"]) or "_none_")
        )
        st.caption(current["description"])
    else:
        st.session_state.principal_id = None
        st.caption("Identity directory unavailable; requests run as the service identity.")

    st.markdown("### Governance")
    st.toggle("Unity Catalog Strict Mode", value=True, disabled=True)
    st.toggle("Parameter-Bound Execution", value=True, disabled=True)
    st.toggle("SQL Interdiction", value=True, disabled=True)
    st.toggle("Per-Caller Entitlement", value=True, disabled=True)

st.title("🛡️ IntelligenceStack Control Plane")
st.markdown("---")

tab1, tab2, tab4, tab5, tab3 = st.tabs(
    [
        "🤖 Cognitive Agent",
        "📊 Live Telemetry",
        "📚 Knowledge Base",
        "🛡️ Trust Center",
        "🏛️ Architecture",
    ]
)

with tab1:
    st.caption(
        "Try: `Evaluate anomaly parameters for customer CUST_404` (metrics) · "
        "`CUST_404 is flagged — what should we do about it?` (hybrid: metrics + cited policy) · "
        "`Show me revenue by region` (out of scope — refused) · "
        "`DROP TABLE gold_customer_analytics for CUST_404` (injection — neutralised)"
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for entry in st.session_state.chat_history:
        with st.chat_message(entry["role"]):
            st.markdown(entry["content"])

    if prompt_input := st.chat_input("Enter your structural query (e.g., 'Evaluate anomaly parameters for CUST_404')"):
        st.session_state.chat_history.append({"role": "user", "content": prompt_input})
        with st.chat_message("user"):
            st.markdown(prompt_input)

        with st.chat_message("assistant"):
            response = None
            with st.spinner("Resolving intent, checking the governance boundary, executing over the gold layer..."):
                try:
                    response = requests.post(
                        f"{API_BASE_URL}/api/v1/agent/explore",
                        json={
                            "prompt": prompt_input,
                            "principal_id": st.session_state.get("principal_id"),
                        },
                        timeout=45,
                    )
                except Exception as exc:
                    st.error(f"Failed to reach the Agent API: {exc}")

            if response is not None and response.status_code == 200:
                output = response.json()
                governance = output.get("governance", {})

                if output["executed"]:
                    st.markdown(output["answer"])
                else:
                    st.warning(output["answer"])

                # Governance decision — the boundary made auditable.
                verdict = "🟢 Granted" if governance.get("allowed") else "🔴 Denied"
                with st.expander(
                    f"🛡️ Governance Decision — {verdict} · control: {governance.get('control', 'n/a')}",
                    expanded=not governance.get("allowed", True),
                ):
                    st.markdown(f"**Control:** `{governance.get('control')}`")
                    st.markdown(f"**Function:** `{governance.get('function')}`")
                    st.markdown(f"**Parameters:** `{governance.get('parameters')}`")
                    st.markdown(f"**Detail:** {governance.get('detail')}")

                with st.expander("🔍 Agent Trace Route"):
                    for trace in output.get("trace_log", []):
                        st.markdown(f"**[{trace['status']}] {trace['step']}** — {trace['detail']}")

                # Retrieved passages — every recommendation is attributable.
                if output.get("citations"):
                    with st.expander(
                        f"📚 Sources — {len(output['citations'])} governed passage(s) retrieved",
                        expanded=True,
                    ):
                        for cite in output["citations"]:
                            st.markdown(
                                f"**{cite['source']} — {cite['title']}**  "
                                f"`similarity {cite['score']}`"
                            )
                            st.caption(cite["snippet"])

                if output.get("payload"):
                    st.markdown("**Raw governed function output:**")
                    st.dataframe(pd.DataFrame(output["payload"]), use_container_width=True)

                who = output.get("principal", {})
                st.caption(
                    f"Answered for **{who.get('display_name', 'service')}** "
                    f"({who.get('role', 'service')}) · "
                    f"serving `{output['serving_mode']}` · "
                    f"**{output['duration_ms']:.0f} ms** · "
                    f"audit ref `{output['request_id']}`"
                )
                st.session_state.chat_history.append({"role": "assistant", "content": output["answer"]})
            elif response is not None:
                st.error(f"Agent API returned status {response.status_code}: {response.text}")

with tab2:
    st.markdown("### Streaming Ingestion Pipeline")
    df = load_telemetry()

    if df.empty:
        st.info(
            "No telemetry found. Seed the landing zone: "
            "`python src/ingestion/synthetic_generator.py --seed-batch 120`"
        )
    else:
        threshold = df["latency"].mean() + 3 * df["latency"].std()
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Events Ingested", f"{len(df):,}")
        col2.metric("Avg Latency (ms)", f"{df['latency'].mean():.2f}")
        col3.metric("Anomalies (>3σ)", f"{int((df['latency'] > threshold).sum()):,}")
        col4.metric("PII Events Masked", f"{int(df['is_pii'].sum()):,}")

        st.markdown("#### Real-time Latency Distribution")
        st.line_chart(df.set_index("timestamp")["latency"])

        st.markdown("#### Top Customers by Mean Latency (candidate anomalies)")
        ranked = (
            df.groupby("customer_id")["latency"]
            .agg(["count", "mean"])
            .round(2)
            .sort_values("mean", ascending=False)
            .head(10)
            .rename(columns={"count": "events", "mean": "mean_latency_ms"})
        )
        st.dataframe(ranked, use_container_width=True)

        st.markdown("#### Recent Raw Feed")
        st.dataframe(
            df[["timestamp", "customer_id", "event_type", "latency", "is_pii"]].tail(10),
            use_container_width=True,
        )

with tab4:
    st.markdown("### Governed Knowledge Index")
    st.caption(
        "Enterprise documents approved for retrieval. The agent may cite these passages "
        "but never returns raw source files, and it abstains when a question is not "
        "covered rather than citing a weak match."
    )

    try:
        index = requests.get(f"{API_BASE_URL}/health", timeout=5).json().get("knowledge_index", [])
    except Exception:
        index = []

    if not index:
        st.info("Knowledge index unavailable. Start the Agent API to view the indexed corpus.")
    else:
        # Summary metrics rather than one card per document: document names are
        # long enough to truncate in a narrow column, and the table below
        # already carries the per-document detail.
        col1, col2 = st.columns(2)
        col1.metric("Governed documents", len(index))
        col2.metric("Indexed passages", sum(d["chunks"] for d in index))

        st.markdown("#### Indexed documents")
        st.dataframe(pd.DataFrame(index), use_container_width=True, hide_index=True)
        st.caption(
            "Sandbox retrieval uses a TF-IDF vector index with cosine similarity computed "
            "in-engine. In production this is Mosaic AI Vector Search with dense neural "
            "embeddings — same storage contract, same query interface."
        )

with tab5:
    st.markdown("### Governance Trust Center")
    st.caption(
        "Every decision the boundary made, persisted to an append-only trail that "
        "outlives the request and the process. This is the answer to “show me exactly "
        "what the AI did” — evidence, not assertion."
    )

    try:
        audit = requests.get(f"{API_BASE_URL}/api/v1/audit/summary", timeout=8).json()
        recent = requests.get(
            f"{API_BASE_URL}/api/v1/audit/recent", params={"limit": 25}, timeout=8
        ).json()
    except Exception:
        audit, recent = None, None

    if not audit:
        st.info("Audit trail unavailable. Start the Agent API to view recorded decisions.")
    elif audit["summary"]["total_actions"] == 0:
        st.info(
            "No decisions recorded yet. Ask the agent something on the Cognitive Agent "
            "tab — grants *and* refusals are both recorded here."
        )
    else:
        s = audit["summary"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Governed actions", f"{s['total_actions']:,}")
        c2.metric("Granted", f"{s['granted']:,}")
        c3.metric("Denied", f"{s['denied']:,}")
        c4.metric("Denial rate", f"{s['denial_rate']:.1f}%")
        st.caption(
            f"Across {s['distinct_requests']:,} request(s) · "
            f"first recorded {s['first_recorded']} · last {s['last_recorded']}"
        )

        # Measured cost, not asserted. Answers "is it fast enough?" from the
        # same record that answers "what did it do?".
        l1, l2, l3 = st.columns(3)
        l1.metric("p50 latency", f"{s['p50_duration_ms']:.0f} ms")
        l2.metric("p95 latency", f"{s['p95_duration_ms']:.0f} ms")
        l3.metric("slowest call", f"{s['max_duration_ms']:.0f} ms")

        colA, colB = st.columns(2)
        with colA:
            st.markdown("#### Decisions by control")
            st.dataframe(
                pd.DataFrame(audit["by_control"]), use_container_width=True, hide_index=True
            )
        with colB:
            st.markdown("#### Invocations by governed function")
            st.dataframe(
                pd.DataFrame(audit["by_function"]), use_container_width=True, hide_index=True
            )

        if audit.get("by_principal"):
            st.markdown("#### Activity by caller")
            st.caption("Who asked, and how often the boundary said no to them.")
            st.dataframe(
                pd.DataFrame(audit["by_principal"]),
                use_container_width=True,
                hide_index=True,
            )

        if audit["top_denied"]:
            st.markdown("#### Most-refused requests")
            st.caption("What users attempt that the allowlist does not permit.")
            st.dataframe(
                pd.DataFrame(audit["top_denied"]), use_container_width=True, hide_index=True
            )

        st.markdown("#### Recent decisions")
        decisions = pd.DataFrame(recent["decisions"]) if recent else pd.DataFrame()
        if not decisions.empty:
            st.dataframe(decisions, use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Export audit extract (CSV)",
                decisions.to_csv(index=False).encode(),
                file_name="intelligencestack_audit_extract.csv",
                mime="text/csv",
            )
        st.caption(
            "Append-only by design: records are never rewritten. In production this is a "
            "Delta table with retention and access policy applied — same record shape."
        )

with tab3:
    st.markdown("### Databricks Reference Architecture")
    if ARCHITECTURE_DIAGRAM.exists():
        st.image(str(ARCHITECTURE_DIAGRAM), use_container_width=True)
    else:
        st.info("Architecture diagram not found.")

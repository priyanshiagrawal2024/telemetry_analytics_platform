"""Telemetry Analytics Platform — analytics workspace (Streamlit).

A **pure presentation client**. It visualizes outputs from existing components
only — the analytics HTTP API, the deterministic analytics agent, and the PDF
report generator. It performs **no analytics calculation** and duplicates **no**
business logic; the engine/service/agent remain the single source of truth.

Sourcing
--------
* ``GET /analytics/summary``               -> KPI band, dataset context
* ``GET /analytics/customer/{customerId}``  -> Customer Intelligence (metrics/scores/insights)
* ``GET /analytics/campaigns``             -> Campaign Intelligence
* in-process ``TelemetryAgent.ask``         -> Ask Analytics Agent panel
* in-process ``generate_pdf_report``        -> Reports

Run
---
    uvicorn api.app:app --host 127.0.0.1 --port 8000     # API
    streamlit run dashboard/streamlit_app.py             # this app
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

# Project root on path so in-process components import under `streamlit run`.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

DEFAULT_API_URL = os.getenv("ANALYTICS_API_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT = 30  # seconds

# Suggested analytics queries (labels -> query text sent to the agent).
SUGGESTED_QUERIES: Tuple[Tuple[str, str], ...] = (
    ("Help", "help"),
    ("Show Findings", "show findings"),
    ("Show Evidence", "show evidence"),
    ("Dataset Summary", "dataset summary"),
    ("List Available Metrics", "list available metrics"),
    ("Which Campaign Reached The Most Customers", "which campaign reached the most customers"),
)

# Curated behavioural metrics shown on the Customer Intelligence "Metrics" card.
_CUSTOMER_METRIC_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("Impressions", "total_impressions"),
    ("Clicks", "total_clicks"),
    ("Skips", "total_skips"),
    ("CTR (%)", "ctr"),
    ("Skip rate (%)", "skip_rate"),
    ("Repeat impression rate (%)", "repeat_impression_rate"),
    ("Avg session depth", "avg_session_depth"),
    ("Exploration score", "exploration_score"),
)


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<style>
:root {
  --bg:#F6F7F9; --card:#FFFFFF; --border:#E6E8EB;
  --text:#1F2933; --muted:#647382; --accent:#1F3B57;
}
.stApp { background-color: var(--bg); }
html, body, [class*="css"] {
  font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:var(--text);
}
/* Quiet the default Streamlit chrome for a product feel */
#MainMenu, footer, [data-testid="stToolbar"] { visibility:hidden; }
[data-testid="stHeader"] { background:transparent; }
.block-container { padding-top:2.2rem; max-width:1300px; }

/* Bordered containers become cards */
[data-testid="stVerticalBlockBorderWrapper"] {
  background:var(--card); border:1px solid var(--border) !important; border-radius:12px;
  box-shadow:0 1px 3px rgba(16,24,40,.06),0 1px 2px rgba(16,24,40,.04);
}

/* Header */
.tap-title { font-size:1.95rem; font-weight:700; letter-spacing:-.01em; margin:0; }
.tap-subtitle { font-size:1.0rem; color:var(--muted); margin:2px 0 0 0; }

/* KPI cards */
.kpi { background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 18px; box-shadow:0 1px 2px rgba(16,24,40,.05); }
.kpi-label { font-size:.72rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); font-weight:700; }
.kpi-value { font-size:1.8rem; font-weight:700; margin-top:6px; line-height:1.1; }

/* Section headers */
.section-eyebrow { font-size:.72rem; text-transform:uppercase; letter-spacing:.09em;
  color:var(--accent); font-weight:700; }
.section-title { font-size:1.25rem; font-weight:700; margin:1px 0 0 0; }
.section-desc { color:var(--muted); font-size:.88rem; margin-top:1px; }

/* Card internals */
.card-title { font-size:.95rem; font-weight:700; margin-bottom:2px; }
.card-sub { font-size:.78rem; color:var(--muted); }
.score-row { display:flex; justify-content:space-between; font-size:.86rem; margin:10px 0 2px; }
.score-name { color:var(--text); font-weight:600; }
.score-val { color:var(--muted); font-variant-numeric:tabular-nums; }
.insight-title { font-weight:700; font-size:.92rem; margin:0; }
.insight-text { color:var(--muted); font-size:.86rem; margin:2px 0 0 0; }

/* Confidence badges */
.badge { display:inline-block; padding:3px 12px; border-radius:999px; font-weight:700;
  font-size:.72rem; letter-spacing:.04em; }
.badge-high { background:#E7F4EC; color:#1A7F37; border:1px solid #BBE3C9; }
.badge-medium { background:#FBF1DA; color:#8A6100; border:1px solid #EAD6A6; }
.badge-low { background:#FBE7E7; color:#B3261E; border:1px solid #F0C2C0; }

/* Answer card (query result) */
.answer-card { background:var(--card); border:1px solid var(--border);
  border-left:3px solid var(--accent); border-radius:10px; padding:16px 18px;
  font-size:1.04rem; line-height:1.55; box-shadow:0 1px 2px rgba(16,24,40,.05); }

/* Status pill */
.status { display:inline-block; padding:3px 12px; border-radius:999px; font-size:.74rem;
  font-weight:700; background:#EEF1F4; color:var(--muted); border:1px solid var(--border); }
.status-ready { background:#E7F4EC; color:#1A7F37; border-color:#BBE3C9; }
</style>
"""


def inject_css() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# API client + in-process components (no analytics logic here)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def api_get(base_url: str, path: str) -> Tuple[Optional[Any], Optional[int], Optional[str]]:
    """GET ``base_url + path`` -> ``(json, status, error)``; never raises."""
    url = base_url.rstrip("/") + path
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, f"Could not reach the API at {url} ({exc})."
    if resp.status_code == 200:
        return resp.json(), resp.status_code, None
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    return None, resp.status_code, detail


@st.cache_resource(show_spinner=False)
def get_agent() -> Any:
    """Build the deterministic agent once (existing component; invoked only)."""
    from agent.telemetry_agent import TelemetryAgent

    return TelemetryAgent()


@st.cache_data(ttl=300, show_spinner=False)
def available_customers() -> List[str]:
    """Customer ids for the selector (read-only, from the existing agent tools)."""
    try:
        return [str(c) for c in get_agent().tools.list_customers()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Formatting / small render helpers (display only)
# ---------------------------------------------------------------------------


def fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def section_header(eyebrow: str, title: str, desc: str = "") -> None:
    html = (
        f"<div class='section-eyebrow'>{eyebrow}</div>"
        f"<div class='section-title'>{title}</div>"
    )
    if desc:
        html += f"<div class='section-desc'>{desc}</div>"
    st.markdown(html, unsafe_allow_html=True)


def kpi_card(label: str, value: str) -> str:
    return (
        f"<div class='kpi'><div class='kpi-label'>{label}</div>"
        f"<div class='kpi-value'>{value}</div></div>"
    )


def confidence_badge(confidence: str) -> str:
    level = (confidence or "low").lower()
    cls = {"high": "badge-high", "medium": "badge-medium", "low": "badge-low"}.get(
        level, "badge-low"
    )
    return f"<span class='badge {cls}'>{level.upper()}</span>"


def _as_response_dict(response: Any) -> Dict[str, Any]:
    if hasattr(response, "to_dict"):
        return response.to_dict()
    if isinstance(response, dict):
        return response
    return {
        "answer": getattr(response, "answer", ""),
        "evidence": getattr(response, "evidence", []),
        "confidence": getattr(response, "confidence", "low"),
    }


def connection_help(base_url: str) -> None:
    st.error(
        f"Unable to reach the analytics API at **{base_url}**. Start it with "
        "`uvicorn api.app:app --host 127.0.0.1 --port 8000`, or update the URL "
        "under **Connection** in the sidebar."
    )


def evidence_dataframe(evidence: List[Any]) -> pd.DataFrame:
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            rows.append({"Key": "", "Value": fmt(item), "Source": ""})
            continue
        value = item.get("value")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        rows.append(
            {
                "Key": item.get("key", ""),
                "Value": value if value is not None else "N/A",
                "Source": item.get("source", ""),
            }
        )
    return pd.DataFrame(rows, columns=["Key", "Value", "Source"])


# ---------------------------------------------------------------------------
# Header + KPI band
# ---------------------------------------------------------------------------


def total_generated_insights(base_url: str, customer_ids: Tuple[str, ...]) -> Optional[int]:
    if not customer_ids:
        return None
    total = 0
    for cid in customer_ids:
        data, _, _ = api_get(base_url, f"/analytics/customer/{cid}")
        if data:
            total += len(data.get("insights") or [])
    return total


def render_header_and_kpis(base_url: str, customer_ids: Tuple[str, ...]) -> None:
    st.markdown(
        "<div class='tap-title'>Telemetry Analytics Platform</div>"
        "<div class='tap-subtitle'>Evidence-based customer telemetry intelligence</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    summary, status, error = api_get(base_url, "/analytics/summary")
    if error or summary is None:
        connection_help(base_url) if status is None else st.error(error)
        summary = {}

    insights_total = total_generated_insights(base_url, customer_ids)
    cards = [
        ("Total Events", fmt(summary.get("n_events")) if summary else "—"),
        ("Total Customers", fmt(summary.get("n_customers")) if summary else "—"),
        ("Campaigns", fmt(summary.get("n_campaigns")) if summary else "—"),
        ("Generated Insights", fmt(insights_total) if insights_total is not None else "—"),
    ]
    cols = st.columns(4, gap="medium")
    for col, (label, value) in zip(cols, cards):
        col.markdown(kpi_card(label, value), unsafe_allow_html=True)

    if summary:
        st.caption(
            f"Dataset `{summary.get('dataset')}` · source `{summary.get('source')}` · "
            f"generated {summary.get('generated_at')}"
        )


# ---------------------------------------------------------------------------
# Section 1 — Customer Intelligence
# ---------------------------------------------------------------------------


def render_customer_intelligence(base_url: str, customer_id: str) -> None:
    section_header(
        "Section 1",
        "Customer Intelligence",
        "Behavioural metrics, composite scores, and generated insights for the selected customer.",
    )
    st.write("")

    cid = (customer_id or "").strip()
    if not cid:
        st.info("Select a customer in the sidebar to view their intelligence profile.")
        return

    data, status, error = api_get(base_url, f"/analytics/customer/{cid}")
    if status == 404:
        st.warning(f"No analytics found for customer `{cid}`.")
        return
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    behavioural = (data.get("metrics") or {}).get("behavioural") or {}
    scores = data.get("scores") or {}
    insights = data.get("insights") or []

    top = st.columns(2, gap="medium")
    # Metrics card
    with top[0]:
        with st.container(border=True):
            st.markdown("<div class='card-title'>Metrics</div>", unsafe_allow_html=True)
            st.markdown(
                f"<div class='card-sub'>Customer {cid}</div>", unsafe_allow_html=True
            )
            m = st.columns(3)
            m[0].metric("Impressions", fmt(behavioural.get("total_impressions")))
            m[1].metric("Clicks", fmt(behavioural.get("total_clicks")))
            m[2].metric("CTR (%)", fmt(behavioural.get("ctr")))
            rows = [
                {"Metric": label, "Value": fmt(behavioural.get(key))}
                for label, key in _CUSTOMER_METRIC_FIELDS
            ]
            st.dataframe(
                pd.DataFrame(rows), hide_index=True, use_container_width=True
            )
    # Scores card
    with top[1]:
        with st.container(border=True):
            st.markdown("<div class='card-title'>Scores</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='card-sub'>Composite scores (0–1)</div>",
                unsafe_allow_html=True,
            )
            if not scores:
                st.caption("No scores returned.")
            for name, value in scores.items():
                label = name.replace("_", " ").title()
                st.markdown(
                    f"<div class='score-row'><span class='score-name'>{label}</span>"
                    f"<span class='score-val'>{fmt(value)}</span></div>",
                    unsafe_allow_html=True,
                )
                if isinstance(value, (int, float)):
                    st.progress(min(max(float(value), 0.0), 1.0))

    # Insights card (full width)
    with st.container(border=True):
        st.markdown("<div class='card-title'>Insights</div>", unsafe_allow_html=True)
        if not insights:
            st.caption("No insights generated for this customer.")
        for item in insights:
            title = item.get("title", "Insight")
            text = item.get("insight", "")
            st.markdown(
                f"<p class='insight-title'>{title}</p>"
                f"<p class='insight-text'>{text}</p>",
                unsafe_allow_html=True,
            )
            evidence = item.get("evidence") or {}
            if evidence:
                with st.expander("Evidence"):
                    st.json(evidence)


# ---------------------------------------------------------------------------
# Section 2 — Campaign Intelligence
# ---------------------------------------------------------------------------


def render_campaign_intelligence(base_url: str) -> None:
    section_header(
        "Section 2",
        "Campaign Intelligence",
        "Distinct customers reached per campaign. Click a column header to sort.",
    )
    st.write("")

    data, status, error = api_get(base_url, "/analytics/campaigns")
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    campaigns = data.get("campaigns") or []
    with st.container(border=True):
        c = st.columns(2)
        c[0].metric("Campaigns", fmt(data.get("n_campaigns")))
        if campaigns:
            reach_values = [int(r.get("customers_reached", 0)) for r in campaigns]
            c[1].metric("Top reach", fmt(max(reach_values)) if reach_values else "—")

        if not campaigns:
            st.caption("No campaign data available.")
            return

        df = pd.DataFrame(campaigns).rename(
            columns={"campaign": "Campaign", "customers_reached": "Customers Reached"}
        )
        max_reach = int(df["Customers Reached"].max()) if not df.empty else 1
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Campaign": st.column_config.TextColumn("Campaign", width="large"),
                "Customers Reached": st.column_config.ProgressColumn(
                    "Customers Reached",
                    format="%d",
                    min_value=0,
                    max_value=max(max_reach, 1),
                ),
            },
        )


def render_campaign_performance() -> None:
    """Per-campaign funnel performance from analytics_service (in-process)."""
    with st.container(border=True):
        st.markdown(
            "<div class='card-title'>Campaign Performance</div>"
            "<div class='card-sub'>Per-campaign funnel metrics. Click a column header to sort.</div>",
            unsafe_allow_html=True,
        )
        try:
            from analytics import analytics_service

            data = analytics_service.get_campaign_performance()
        except Exception as exc:  # service/pipeline unavailable
            st.error(f"Could not load campaign performance: {exc}")
            return

        rows = data.get("campaigns") or []
        if not rows:
            st.info("No campaign performance data available.")
            return

        columns = [
            "campaign",
            "impressions",
            "clicks",
            "skips",
            "ctr",
            "skip_rate",
            "exposure_frequency",
            "reach",
        ]
        df = pd.DataFrame(rows).reindex(columns=columns)
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "campaign": st.column_config.TextColumn("Campaign", width="large"),
                "impressions": st.column_config.NumberColumn("Impressions", format="%d"),
                "clicks": st.column_config.NumberColumn("Clicks", format="%d"),
                "skips": st.column_config.NumberColumn("Skips", format="%d"),
                # CTR / skip rate are already 0-100; show as percentages but keep
                # the underlying number so the column sorts numerically.
                "ctr": st.column_config.NumberColumn("CTR", format="%.2f%%"),
                "skip_rate": st.column_config.NumberColumn("Skip Rate", format="%.2f%%"),
                "exposure_frequency": st.column_config.NumberColumn(
                    "Exposure Frequency", format="%.2f"
                ),
                "reach": st.column_config.NumberColumn("Reach", format="%d"),
            },
        )


# ---------------------------------------------------------------------------
# Section 3 — Ask Analytics Agent (analytics query panel; not a chatbot)
# ---------------------------------------------------------------------------


def _run_agent_query(question: str, customer_id: str) -> None:
    try:
        agent = get_agent()
    except Exception as exc:
        st.error(f"Could not load the analytics agent: {exc}")
        return

    response = agent.ask(question=question.strip(), customer_id=customer_id or None)
    data = _as_response_dict(response)
    answer = (data.get("answer") or "(no answer)").replace("\n", "<br>")
    confidence = str(data.get("confidence", "low"))
    evidence = data.get("evidence", []) or []

    st.write("")
    st.markdown(
        f"<span class='card-sub'>Confidence</span>&nbsp;&nbsp;{confidence_badge(confidence)}",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='answer-card'>{answer}</div>", unsafe_allow_html=True)

    if confidence == "low":
        st.caption(
            "This query is outside the supported analytics set. Use a suggested "
            "query above for a grounded result."
        )

    st.markdown("<div class='card-title'>Evidence</div>", unsafe_allow_html=True)
    ev_df = evidence_dataframe(evidence)
    if ev_df.empty:
        st.caption("No supporting evidence for this query.")
    else:
        st.caption(f"{len(ev_df)} sourced fact(s)")
        st.dataframe(ev_df, hide_index=True, use_container_width=True)


def render_agent_panel(customer_id: str) -> None:
    section_header(
        "Section 3",
        "Ask Analytics Agent",
        "Deterministic, evidence-grounded analytics queries (no LLM). "
        "Every answer is backed by sourced telemetry facts.",
    )
    st.write("")

    cid = (customer_id or "").strip()

    with st.container(border=True):
        st.markdown(
            "<div class='card-title'>Supported Analytics Queries</div>"
            "<div class='card-sub'>Select a query to run it, or type your own below.</div>",
            unsafe_allow_html=True,
        )
        st.write("")
        chip_cols = st.columns(3, gap="small")
        for i, (label, query) in enumerate(SUGGESTED_QUERIES):
            if chip_cols[i % 3].button(label, key=f"sq_{i}", use_container_width=True):
                st.session_state["agent_query"] = query
                st.session_state["agent_run"] = True

        with st.form("agent_query_form"):
            query = st.text_input(
                "Analytics query",
                key="agent_query",
                placeholder="e.g. explain engagement score for the selected customer",
            )
            submitted = st.form_submit_button("Run query", type="primary")

        run = submitted or st.session_state.pop("agent_run", False)
        if run:
            if not (query or "").strip():
                st.warning("Enter an analytics query to run.")
            else:
                _run_agent_query(query, cid)


# ---------------------------------------------------------------------------
# Section 4 — Reports
# ---------------------------------------------------------------------------


def render_reports(base_url: str, customer_id: str) -> None:
    section_header(
        "Section 4",
        "Reports",
        "Generate a management-ready PDF analytics report from the current dataset.",
    )
    st.write("")

    with st.container(border=True):
        generated_at = st.session_state.get("report_generated_at")
        pdf_bytes = st.session_state.get("report_pdf")

        status_html = (
            f"<span class='status status-ready'>Ready · generated {generated_at}</span>"
            if pdf_bytes
            else "<span class='status'>Not generated</span>"
        )
        st.markdown(
            f"<div class='card-title'>Report status</div>{status_html}",
            unsafe_allow_html=True,
        )
        st.write("")

        cols = st.columns([1, 1, 3])
        if cols[0].button("Generate report", type="primary", use_container_width=True):
            try:
                from reports.pdf_report_generator import generate_pdf_report

                with st.spinner("Generating PDF report…"):
                    out_path = generate_pdf_report()
                    st.session_state["report_pdf"] = Path(out_path).read_bytes()
                    st.session_state["report_generated_at"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M"
                    )
                st.rerun()
            except Exception as exc:
                st.error(f"Report generation failed: {exc}")

        if pdf_bytes:
            cols[1].download_button(
                "Download PDF",
                data=pdf_bytes,
                file_name="analytics_report.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.caption("Click **Generate report** to build the PDF, then download it.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def render_sidebar() -> Tuple[str, str]:
    with st.sidebar:
        st.markdown(
            "<div class='card-title'>Telemetry Analytics</div>"
            "<div class='card-sub'>Workspace</div>",
            unsafe_allow_html=True,
        )
        st.divider()

        customers = available_customers()
        if customers:
            customer_id = st.selectbox("Customer", customers, index=0)
        else:
            customer_id = st.text_input(
                "Customer ID", value="", placeholder="e.g. 1015289504"
            )

        base_url = DEFAULT_API_URL
        with st.expander("Connection"):
            base_url = st.text_input("Analytics API URL", value=DEFAULT_API_URL)
            if st.button("Refresh data", use_container_width=True):
                st.cache_data.clear()
        st.caption("Read-only view. The engine is the single source of truth.")
    return base_url, customer_id


def main() -> None:
    st.set_page_config(page_title="Telemetry Analytics Platform", layout="wide")
    inject_css()

    base_url, customer_id = render_sidebar()
    customer_ids = tuple(available_customers())

    render_header_and_kpis(base_url, customer_ids)
    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(
        [
            "Customer Intelligence",
            "Campaign Intelligence",
            "Ask Analytics Agent",
            "Reports",
        ]
    )
    with tab1:
        render_customer_intelligence(base_url, customer_id)
    with tab2:
        render_campaign_intelligence(base_url)
        st.write("")
        render_campaign_performance()
    with tab3:
        render_agent_panel(customer_id)
    with tab4:
        render_reports(base_url, customer_id)


if __name__ == "__main__":
    main()

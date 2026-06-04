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
* in-process ``analytics_service``          -> Campaign Performance
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    ("Show findings", "show findings"),
    ("Show evidence", "show evidence"),
    ("Dataset summary", "dataset summary"),
    ("List metrics", "list available metrics"),
    ("Top campaign by reach", "which campaign reached the most customers"),
)

# Curated behavioural metrics shown on the Customer Intelligence "Metrics" card.
_CUSTOMER_METRIC_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("Repeat impression rate (%)", "repeat_impression_rate"),
    ("Avg session depth", "avg_session_depth"),
    ("Exploration score", "exploration_score"),
)

_OVERVIEW_CAPABILITIES: Tuple[Tuple[str, str], ...] = (
    ("Analytics Engine", "Raw telemetry → evidence-based metrics, scores and insights. Capability-gated; never fabricated."),
    ("Campaign Intelligence", "Per-campaign reach and funnel performance — impressions, clicks, skips, CTR, skip rate."),
    ("Analytics Agent", "Deterministic, no-LLM query layer. Answers grounded strictly in telemetry, with sourced evidence."),
    ("Reports", "One-click, management-ready PDF report generated from the current dataset."),
)


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --bg:#FAFAFC; --surface:#FFFFFF; --surface-2:#F3F4F7; --border:#ECEDF1;
  --ink:#161B22; --muted:#6E7681; --accent:#4338CA; --accent-soft:#EEF0FB;
  --ok:#15803D; --warn:#B45309; --bad:#B42318;
}
html, body, [class*="css"], .stApp, button, input, textarea, select {
  font-family:'Inter',-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
.stApp { background:var(--bg); color:var(--ink); }

/* Quiet default Streamlit chrome */
#MainMenu, footer, [data-testid="stToolbar"] { visibility:hidden; }
[data-testid="stHeader"] { background:transparent; height:0; }
.block-container { padding-top:1.6rem; padding-bottom:3rem; max-width:1240px; }

/* Bordered containers -> cards with a quiet hover lift */
[data-testid="stVerticalBlockBorderWrapper"] {
  background:var(--surface); border:1px solid var(--border) !important; border-radius:16px;
  box-shadow:0 1px 2px rgba(16,24,40,.04); transition:box-shadow .18s ease, border-color .18s ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  box-shadow:0 8px 24px rgba(16,24,40,.07); border-color:#E1E3EA !important;
}

/* Brand / header */
.brandrow { display:flex; align-items:center; gap:14px; }
.brand-mark { width:42px; height:42px; border-radius:11px; background:var(--accent); color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:700; font-size:1rem;
  letter-spacing:.02em; box-shadow:0 4px 12px rgba(67,56,202,.28); }
.app-title { font-size:1.5rem; font-weight:700; letter-spacing:-.02em; line-height:1.1; }
.app-sub { color:var(--muted); font-size:.9rem; margin-top:1px; }

/* Section titles */
.eyebrow { font-size:.68rem; font-weight:700; letter-spacing:.13em; text-transform:uppercase; color:var(--accent); }
.h-title { font-size:1.2rem; font-weight:700; letter-spacing:-.01em; margin-top:2px; }
.h-desc  { color:var(--muted); font-size:.86rem; margin-top:2px; }

/* KPI */
.kpi { position:relative; background:var(--surface); border:1px solid var(--border); border-radius:16px;
  padding:18px 20px 16px; box-shadow:0 1px 2px rgba(16,24,40,.04); transition:.18s; overflow:hidden; }
.kpi:hover { transform:translateY(-2px); box-shadow:0 10px 26px rgba(16,24,40,.08); }
.kpi::before { content:""; position:absolute; left:0; top:14px; bottom:14px; width:3px;
  border-radius:3px; background:var(--accent); }
.kpi-label { font-size:.7rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:600; }
.kpi-value { font-size:2rem; font-weight:700; margin-top:8px; letter-spacing:-.02em; line-height:1; }

/* Capability cards */
.cap { background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:16px 18px;
  height:100%; transition:.18s; }
.cap:hover { transform:translateY(-2px); box-shadow:0 10px 24px rgba(16,24,40,.07); }
.cap-name { font-weight:700; font-size:.95rem; display:flex; align-items:center; gap:9px; }
.cap-dot { width:7px; height:7px; border-radius:50%; background:var(--accent); }
.cap-desc { color:var(--muted); font-size:.82rem; line-height:1.55; margin-top:9px; }

/* Context chip (active customer) */
.chip { display:inline-flex; align-items:center; gap:8px; background:var(--surface-2);
  border:1px solid var(--border); border-radius:999px; padding:5px 13px; font-size:.84rem; font-weight:600; }
.chip-dot { width:8px; height:8px; border-radius:50%; }
.dot-ok { background:#22C55E; } .dot-bad { background:#EF4444; }
.chip-muted { color:var(--muted); font-weight:500; }

/* Card internals */
.card-title { font-size:.95rem; font-weight:700; }
.card-sub { font-size:.78rem; color:var(--muted); }
.insight-title { font-weight:700; font-size:.92rem; margin:0; }
.insight-text { color:var(--muted); font-size:.86rem; margin:3px 0 0 0; line-height:1.5; }

/* Custom bars (funnel / scores) */
.bars { margin-top:6px; }
.bar-row { display:flex; align-items:center; gap:12px; margin:9px 0; }
.bar-label { width:130px; font-size:.82rem; color:var(--ink); font-weight:500; }
.bar-track { flex:1; height:9px; background:var(--surface-2); border-radius:999px; overflow:hidden; }
.bar-fill { height:100%; background:var(--accent); border-radius:999px; }
.bar-val { width:64px; text-align:right; font-size:.82rem; color:var(--muted); font-variant-numeric:tabular-nums; }

/* Confidence badges */
.badge { display:inline-block; padding:3px 12px; border-radius:999px; font-weight:700; font-size:.72rem; letter-spacing:.04em; }
.badge-high { background:#E7F4EC; color:#15803D; border:1px solid #C3E6CE; }
.badge-medium { background:#FBF1DA; color:#B45309; border:1px solid #EAD6A6; }
.badge-low { background:#FBE7E7; color:#B42318; border:1px solid #F0C2C0; }

/* Answer card */
.answer-card { background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:12px; padding:18px 20px; font-size:1.02rem; line-height:1.6; }

/* Status pill */
.status { display:inline-block; padding:3px 12px; border-radius:999px; font-size:.74rem; font-weight:700;
  background:var(--surface-2); color:var(--muted); border:1px solid var(--border); }
.status-ready { background:#E7F4EC; color:#15803D; border-color:#C3E6CE; }

/* Tabs */
[data-baseweb="tab-list"] { gap:6px; border-bottom:1px solid var(--border); }
button[data-baseweb="tab"] { font-weight:600; color:var(--muted); }
button[data-baseweb="tab"][aria-selected="true"] { color:var(--accent); }
[data-baseweb="tab-highlight"] { background:var(--accent); height:2.5px; }

/* Buttons */
.stButton > button { border-radius:10px; font-weight:600; border:1px solid var(--border); }
.stButton > button:hover { border-color:var(--accent); color:var(--accent); }
.stButton > button[kind="primary"], [data-testid="baseButton-primary"] {
  background:var(--accent); border-color:var(--accent); color:#fff; }

/* Inputs / progress accents */
[data-baseweb="select"] > div, .stTextInput input { border-radius:10px; }
[data-testid="stProgress"] > div > div > div > div { background-color:var(--accent); }
</style>
"""


def inject_css() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Data access (no analytics logic here — only fetch/format)
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
# Formatting / render helpers (display only)
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
    html = f"<div class='eyebrow'>{eyebrow}</div><div class='h-title'>{title}</div>"
    if desc:
        html += f"<div class='h-desc'>{desc}</div>"
    st.markdown(html, unsafe_allow_html=True)


def confidence_badge(confidence: str) -> str:
    level = (confidence or "low").lower()
    cls = {"high": "badge-high", "medium": "badge-medium", "low": "badge-low"}.get(level, "badge-low")
    return f"<span class='badge {cls}'>{level.upper()}</span>"


def bar_chart(pairs: Sequence[Tuple[str, Any]], max_value: Optional[float] = None) -> str:
    """Render a compact, custom horizontal bar chart (label · bar · value)."""
    numeric = [float(v) for _, v in pairs if isinstance(v, (int, float))]
    top = max_value if max_value is not None else (max(numeric) if numeric else 1.0)
    top = top or 1.0
    rows = ""
    for label, value in pairs:
        v = float(value) if isinstance(value, (int, float)) else 0.0
        pct = max(0.0, min(100.0, v / top * 100.0))
        rows += (
            f"<div class='bar-row'><span class='bar-label'>{label}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%'></div></div>"
            f"<span class='bar-val'>{fmt(value)}</span></div>"
        )
    return f"<div class='bars'>{rows}</div>"


def active_chip(customer_id: str) -> None:
    cid = (customer_id or "").strip()
    text = f"Customer {cid}" if cid else "No customer selected"
    st.markdown(
        f"<span class='chip'><span class='chip-dot dot-ok'></span>{text}</span>",
        unsafe_allow_html=True,
    )


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
        "`uvicorn api.app:app --host 127.0.0.1 --port 8000`, or update the URL in the sidebar."
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
# Header · context bar · KPI band · capabilities
# ---------------------------------------------------------------------------


def render_topbar(base_url: str) -> None:
    left, right = st.columns([3, 1])
    with left:
        st.markdown(
            "<div class='brandrow'><div class='brand-mark'>TA</div>"
            "<div><div class='app-title'>Telemetry Analytics Platform</div>"
            "<div class='app-sub'>Evidence-based customer telemetry intelligence</div></div></div>",
            unsafe_allow_html=True,
        )
    with right:
        _, status, _ = api_get(base_url, "/analytics/summary")
        ok = status == 200
        dot, label = ("dot-ok", "API connected") if ok else ("dot-bad", "API offline")
        st.markdown(
            f"<div style='text-align:right;margin-top:8px'><span class='chip'>"
            f"<span class='chip-dot {dot}'></span>{label}</span></div>",
            unsafe_allow_html=True,
        )


def render_context_bar(customers: List[str]) -> str:
    """First-class customer selector — always visible above the tabs."""
    with st.container(border=True):
        cols = st.columns([2, 3])
        with cols[0]:
            if customers:
                customer_id = st.selectbox(
                    "Active customer", customers, key="active_customer"
                )
            else:
                customer_id = st.text_input(
                    "Active customer ID", key="active_customer",
                    placeholder="e.g. 1015289504",
                )
        with cols[1]:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            cid = (customer_id or "").strip()
            chip = (
                f"<span class='chip'><span class='chip-dot dot-ok'></span>Customer {cid}</span>"
                if cid else
                "<span class='chip'><span class='chip-dot dot-bad'></span>None selected</span>"
            )
            st.markdown(
                f"{chip}&nbsp;&nbsp;<span class='chip-muted' style='font-size:.82rem'>"
                "Drives Customer Intelligence, Insights, Evidence &amp; Agent queries.</span>",
                unsafe_allow_html=True,
            )
    return customer_id or ""


def total_generated_insights(base_url: str, customer_ids: Tuple[str, ...]) -> Optional[int]:
    if not customer_ids:
        return None
    total = 0
    for cid in customer_ids:
        data, _, _ = api_get(base_url, f"/analytics/customer/{cid}")
        if data:
            total += len(data.get("insights") or [])
    return total


def render_kpis(base_url: str, customer_ids: Tuple[str, ...]) -> None:
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
        col.markdown(
            f"<div class='kpi'><div class='kpi-label'>{label}</div>"
            f"<div class='kpi-value'>{value}</div></div>",
            unsafe_allow_html=True,
        )


def render_capabilities() -> None:
    section_header("Overview", "Platform Capabilities")
    st.write("")
    cols = st.columns(len(_OVERVIEW_CAPABILITIES), gap="medium")
    for col, (name, desc) in zip(cols, _OVERVIEW_CAPABILITIES):
        col.markdown(
            f"<div class='cap'><div class='cap-name'><span class='cap-dot'></span>{name}</div>"
            f"<div class='cap-desc'>{desc}</div></div>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Customer Intelligence
# ---------------------------------------------------------------------------


def render_customer_intelligence(base_url: str, customer_id: str) -> None:
    section_header("Customer", "Customer Intelligence",
                   "Behavioural funnel, composite scores, and generated insights.")
    st.write("")

    cid = (customer_id or "").strip()
    if not cid:
        st.info("Select a customer in the bar above to view their profile.")
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
    # Funnel + metrics
    with top[0]:
        with st.container(border=True):
            st.markdown("<div class='card-title'>Engagement funnel</div>"
                        f"<div class='card-sub'>Customer {cid}</div>", unsafe_allow_html=True)
            funnel = [
                ("Impressions", behavioural.get("total_impressions")),
                ("Clicks", behavioural.get("total_clicks")),
                ("Skips", behavioural.get("total_skips")),
            ]
            st.markdown(bar_chart(funnel), unsafe_allow_html=True)
            st.write("")
            m = st.columns(2)
            m[0].metric("CTR", f"{fmt(behavioural.get('ctr'))}%")
            m[1].metric("Skip rate", f"{fmt(behavioural.get('skip_rate'))}%")
            rows = [{"Metric": label, "Value": fmt(behavioural.get(key))}
                    for label, key in _CUSTOMER_METRIC_FIELDS]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    # Scores
    with top[1]:
        with st.container(border=True):
            st.markdown("<div class='card-title'>Composite scores</div>"
                        "<div class='card-sub'>Normalised 0–1</div>", unsafe_allow_html=True)
            if not scores:
                st.caption("No scores returned.")
            else:
                pairs = [(name.replace("_", " ").title(), value) for name, value in scores.items()]
                st.markdown(bar_chart(pairs, max_value=1.0), unsafe_allow_html=True)

    # Insights
    with st.container(border=True):
        st.markdown("<div class='card-title'>Insights</div>", unsafe_allow_html=True)
        if not insights:
            st.caption("No insights generated for this customer.")
        for item in insights:
            st.markdown(
                f"<p class='insight-title'>{item.get('title', 'Insight')}</p>"
                f"<p class='insight-text'>{item.get('insight', '')}</p>",
                unsafe_allow_html=True,
            )
            evidence = item.get("evidence") or {}
            if evidence:
                with st.expander("Evidence"):
                    st.json(evidence)


# ---------------------------------------------------------------------------
# Campaign Intelligence
# ---------------------------------------------------------------------------


def render_campaign_intelligence(base_url: str) -> None:
    section_header("Campaigns", "Campaign Intelligence",
                   "Reach and funnel performance per campaign. Click a column header to sort.")
    st.write("")

    data, status, error = api_get(base_url, "/analytics/campaigns")
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    campaigns = data.get("campaigns") or []
    with st.container(border=True):
        st.markdown("<div class='card-title'>Customers reached</div>", unsafe_allow_html=True)
        c = st.columns(2)
        c[0].metric("Campaigns", fmt(data.get("n_campaigns")))
        if campaigns:
            reach = [int(r.get("customers_reached", 0)) for r in campaigns]
            c[1].metric("Top reach", fmt(max(reach)) if reach else "—")
        if not campaigns:
            st.caption("No campaign data available.")
        else:
            df = pd.DataFrame(campaigns).rename(
                columns={"campaign": "Campaign", "customers_reached": "Customers Reached"})
            max_reach = int(df["Customers Reached"].max()) if not df.empty else 1
            st.dataframe(
                df, hide_index=True, use_container_width=True,
                column_config={
                    "Campaign": st.column_config.TextColumn("Campaign", width="large"),
                    "Customers Reached": st.column_config.ProgressColumn(
                        "Customers Reached", format="%d", min_value=0, max_value=max(max_reach, 1)),
                },
            )

    st.write("")
    render_campaign_performance()


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

        columns = ["campaign", "impressions", "clicks", "skips", "ctr",
                   "skip_rate", "exposure_frequency", "reach"]
        df = pd.DataFrame(rows).reindex(columns=columns)
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "campaign": st.column_config.TextColumn("Campaign", width="large"),
                "impressions": st.column_config.NumberColumn("Impressions", format="%d"),
                "clicks": st.column_config.NumberColumn("Clicks", format="%d"),
                "skips": st.column_config.NumberColumn("Skips", format="%d"),
                # Already 0-100; shown as % but kept numeric so the column sorts.
                "ctr": st.column_config.NumberColumn("CTR", format="%.2f%%"),
                "skip_rate": st.column_config.NumberColumn("Skip Rate", format="%.2f%%"),
                "exposure_frequency": st.column_config.NumberColumn("Exposure Freq.", format="%.2f"),
                "reach": st.column_config.NumberColumn("Reach", format="%d"),
            },
        )


# ---------------------------------------------------------------------------
# Ask Analytics Agent (analytics query panel; not a chatbot)
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
        st.caption("Outside the supported analytics set — try a suggested query for a grounded result.")

    st.markdown("<div class='card-title' style='margin-top:6px'>Evidence</div>", unsafe_allow_html=True)
    ev_df = evidence_dataframe(evidence)
    if ev_df.empty:
        st.caption("No supporting evidence for this query.")
    else:
        st.caption(f"{len(ev_df)} sourced fact(s)")
        st.dataframe(ev_df, hide_index=True, use_container_width=True)


def render_agent_panel(customer_id: str) -> None:
    section_header("Agent", "Ask Analytics Agent",
                   "Deterministic, evidence-grounded queries — no LLM, no guessing.")
    st.write("")
    cid = (customer_id or "").strip()
    cols = st.columns([3, 1])
    with cols[0]:
        st.markdown(
            "<div class='help-text' style='font-size:.85rem;color:#6E7681'>"
            "Ask about the dataset, the selected customer, campaigns, findings, or evidence. "
            "Customer-specific questions use the active customer above. Unsupported questions "
            "return a clear \"insufficient evidence\" reply rather than a guess.</div>",
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.markdown("<div style='text-align:right'>", unsafe_allow_html=True)
        active_chip(cid)
        st.markdown("</div>", unsafe_allow_html=True)
    st.write("")

    with st.container(border=True):
        st.markdown("<div class='card-title'>Suggested queries</div>", unsafe_allow_html=True)
        st.write("")
        chip_cols = st.columns(3, gap="small")
        for i, (label, query) in enumerate(SUGGESTED_QUERIES):
            if chip_cols[i % 3].button(label, key=f"sq_{i}", use_container_width=True):
                st.session_state["agent_query"] = query
                st.session_state["agent_run"] = True

        with st.form("agent_query_form"):
            query = st.text_input(
                "Analytics query", key="agent_query",
                placeholder="e.g. explain the engagement score for the active customer",
            )
            submitted = st.form_submit_button("Run query", type="primary")

        run = submitted or st.session_state.pop("agent_run", False)
        if run:
            if not (query or "").strip():
                st.warning("Enter an analytics query to run.")
            else:
                _run_agent_query(query, cid)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def render_reports() -> None:
    section_header("Reports", "Analytics Report",
                   "Generate a management-ready PDF from the current dataset.")
    st.write("")

    with st.container(border=True):
        generated_at = st.session_state.get("report_generated_at")
        pdf_bytes = st.session_state.get("report_pdf")

        if pdf_bytes:
            size_kb = len(pdf_bytes) / 1024
            status_html = "<span class='status status-ready'>Ready</span>"
            detail = f"Generated {generated_at} · {size_kb:,.0f} KB · ready to download."
        else:
            status_html = "<span class='status'>Not generated</span>"
            detail = "No report has been generated in this session yet."

        st.markdown(
            f"<div class='card-title'>Report status</div>{status_html}"
            f"<div class='card-sub' style='margin-top:6px'>{detail}</div>",
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
                    st.session_state["report_generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                st.rerun()
            except Exception as exc:
                st.error(f"Report generation failed: {exc}")

        if pdf_bytes:
            cols[1].download_button(
                "Download PDF", data=pdf_bytes, file_name="analytics_report.pdf",
                mime="application/pdf", use_container_width=True,
            )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def render_sidebar() -> str:
    """Minimal sidebar: connection settings only (customer selection is up top)."""
    with st.sidebar:
        st.markdown("<div class='card-title'>Workspace</div>"
                    "<div class='card-sub'>Connection & data</div>", unsafe_allow_html=True)
        st.divider()
        base_url = st.text_input("Analytics API URL", value=DEFAULT_API_URL)
        if st.button("Refresh data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Read-only view. The analytics engine is the single source of truth.")
    return base_url


def main() -> None:
    st.set_page_config(
        page_title="Telemetry Analytics Platform",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    inject_css()

    base_url = render_sidebar()
    customers = available_customers()
    customer_ids = tuple(customers)

    render_topbar(base_url)
    st.write("")
    customer_id = render_context_bar(customers)  # first-class, persistent selector
    st.write("")
    render_kpis(base_url, customer_ids)
    st.write("")
    render_capabilities()
    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Customer Intelligence", "Campaign Intelligence", "Ask Analytics Agent", "Reports"]
    )
    with tab1:
        render_customer_intelligence(base_url, customer_id)
    with tab2:
        render_campaign_intelligence(base_url)
    with tab3:
        render_agent_panel(customer_id)
    with tab4:
        render_reports()


if __name__ == "__main__":
    main()

"""Demo dashboard for the MyJio Floater Analytics Platform.

A **pure presentation client**: it only calls the existing analytics HTTP
endpoints and visualizes their JSON. It performs **no analytics calculation**,
no aggregation, and duplicates **no business logic** — every value shown comes
verbatim from the API (the engine remains the single source of truth).

Endpoints consumed
------------------
* ``GET /analytics/summary``               -> Dataset Summary, Campaign reach
* ``GET /analytics/customer/{customerId}``  -> Customer Analytics, Scores, Insights
* ``GET /analytics/campaigns``             -> Campaign Analytics

Run
---
    # 1. start the API (separate terminal):
    uvicorn api.app:app --host 127.0.0.1 --port 8000
    # 2. start the dashboard:
    streamlit run dashboard/streamlit_app.py
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

DEFAULT_API_URL = os.getenv("ANALYTICS_API_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# API client (HTTP only — no analytics logic lives here)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def api_get(base_url: str, path: str) -> Tuple[Optional[Any], Optional[int], Optional[str]]:
    """GET ``base_url + path``.

    Returns ``(json_or_None, status_code_or_None, error_or_None)``. Network
    failures and non-200 responses are returned as data, never raised, so the
    UI can render a friendly message.
    """
    url = base_url.rstrip("/") + path
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, f"Could not reach the API at {url} ({exc})."

    if resp.status_code == 200:
        return resp.json(), resp.status_code, None

    # Surface FastAPI's {"detail": ...} when present.
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    return None, resp.status_code, detail


# ---------------------------------------------------------------------------
# Small rendering helpers (formatting only — no computation)
# ---------------------------------------------------------------------------


def fmt(value: Any) -> str:
    """Human-readable cell value; capability-gated placeholders -> 'N/A'."""
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def kv_table(data: Dict[str, Any]) -> pd.DataFrame:
    """Render a flat dict as a two-column (Field, Value) table."""
    rows = [{"Field": k, "Value": fmt(v)} for k, v in data.items()]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def connection_help(base_url: str) -> None:
    st.error(
        f"Unable to reach the analytics API at **{base_url}**.\n\n"
        "Start it with `uvicorn api.app:app --host 127.0.0.1 --port 8000` "
        "or set the correct URL in the sidebar."
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def render_dataset_summary(base_url: str) -> None:
    st.subheader("Dataset Summary")
    data, status, error = api_get(base_url, "/analytics/summary")
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Customers", fmt(data.get("n_customers")))
    c2.metric("Events", fmt(data.get("n_events")))
    c3.metric("Sessions", fmt(data.get("n_sessions")))
    c4.metric("Campaigns", fmt(data.get("n_campaigns")))

    st.caption(
        f"Dataset: `{data.get('dataset')}` · source: `{data.get('source')}` · "
        f"generated at {data.get('generated_at')}"
    )

    dist = data.get("event_distribution") or {}
    if dist:
        st.markdown("**Event distribution** (share of events by role)")
        st.bar_chart(pd.Series(dist, name="share").sort_values(ascending=False))

    left, right = st.columns(2)
    with left:
        averages = data.get("metric_averages") or {}
        if averages:
            st.markdown("**Metric averages**")
            st.dataframe(kv_table(averages), hide_index=True, use_container_width=True)
    with right:
        caps = data.get("capabilities") or {}
        if caps:
            st.markdown("**Capabilities**")
            st.dataframe(kv_table(caps), hide_index=True, use_container_width=True)

    unavailable = data.get("unavailable_metrics") or []
    if unavailable:
        st.info(
            "Metrics unavailable for this dataset (emitted as placeholders, "
            "never fabricated): " + ", ".join(unavailable)
        )

    with st.expander("Raw response"):
        st.json(data)


def _require_customer(customer_id: str) -> bool:
    if not customer_id.strip():
        st.info("Enter a customer ID in the sidebar to view this section.")
        return False
    return True


def _fetch_customer(
    base_url: str, customer_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str]]:
    return api_get(base_url, f"/analytics/customer/{customer_id.strip()}")


def render_customer_analytics(base_url: str, customer_id: str) -> None:
    st.subheader("Customer Analytics")
    if not _require_customer(customer_id):
        return
    data, status, error = _fetch_customer(base_url, customer_id)
    if status == 404:
        st.warning(error or f"No analytics found for customer '{customer_id}'.")
        return
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    st.markdown(f"**Customer** `{data.get('customer_id', customer_id)}`")
    metrics = data.get("metrics") or {}
    behavioural = metrics.get("behavioural") or {}
    descriptive = metrics.get("descriptive") or {}

    # Highlight a few headline behavioural metrics (rendered as-is).
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Impressions", fmt(behavioural.get("total_impressions")))
    h2.metric("Clicks", fmt(behavioural.get("total_clicks")))
    h3.metric("CTR (%)", fmt(behavioural.get("ctr")))
    h4.metric("Skip rate (%)", fmt(behavioural.get("skip_rate")))

    left, right = st.columns(2)
    with left:
        st.markdown("**Behavioural profile**")
        if behavioural:
            st.dataframe(kv_table(behavioural), hide_index=True, use_container_width=True)
    with right:
        st.markdown("**Descriptive metrics**")
        if descriptive:
            st.dataframe(kv_table(descriptive), hide_index=True, use_container_width=True)

    summary_lines = data.get("dashboard_summary") or []
    if summary_lines:
        st.markdown("**Summary**")
        for line in summary_lines:
            st.markdown(f"- {line}")

    with st.expander("Raw response"):
        st.json(data)


def render_scores(base_url: str, customer_id: str) -> None:
    st.subheader("Scores")
    if not _require_customer(customer_id):
        return
    data, status, error = _fetch_customer(base_url, customer_id)
    if status == 404:
        st.warning(error or f"No analytics found for customer '{customer_id}'.")
        return
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    scores = data.get("scores") or {}
    if not scores:
        st.info("No scores returned for this customer.")
        return

    cols = st.columns(len(scores))
    for col, (name, value) in zip(cols, scores.items()):
        label = name.replace("_", " ").title()
        col.metric(label, fmt(value))
        if isinstance(value, (int, float)):
            col.progress(min(max(float(value), 0.0), 1.0))

    numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
    if numeric:
        st.markdown("**Scores (0–1)**")
        st.bar_chart(pd.Series(numeric, name="score"))

    with st.expander("Raw response"):
        st.json(scores)


def render_campaign_analytics(base_url: str) -> None:
    st.subheader("Campaign Analytics")
    data, status, error = api_get(base_url, "/analytics/campaigns")
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    st.metric("Campaigns", fmt(data.get("n_campaigns")))
    campaigns = data.get("campaigns") or []
    if not campaigns:
        st.info("No campaigns returned.")
        return

    df = pd.DataFrame(campaigns)
    if {"campaign", "customers_reached"}.issubset(df.columns):
        st.markdown("**Customers reached by campaign**")
        chart_df = df.set_index("campaign")["customers_reached"].sort_values(ascending=False)
        st.bar_chart(chart_df)
    st.dataframe(df, hide_index=True, use_container_width=True)

    with st.expander("Raw response"):
        st.json(data)


def render_insights(base_url: str, customer_id: str) -> None:
    st.subheader("Insights")
    if not _require_customer(customer_id):
        return
    data, status, error = _fetch_customer(base_url, customer_id)
    if status == 404:
        st.warning(error or f"No analytics found for customer '{customer_id}'.")
        return
    if error or data is None:
        connection_help(base_url) if status is None else st.error(error)
        return

    insights = data.get("insights") or []
    if not insights:
        st.info("No insights returned for this customer.")
        return

    for item in insights:
        title = item.get("title", "Insight")
        st.markdown(f"#### {title}")
        st.write(item.get("insight", ""))
        evidence = item.get("evidence") or {}
        if evidence:
            with st.expander("Evidence"):
                st.json(evidence)
        st.divider()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="MyJio Floater Analytics", layout="wide")
    st.title("MyJio Floater Analytics — Demo Dashboard")
    st.caption("Read-only view of the analytics API. No calculations happen here.")

    with st.sidebar:
        st.header("Settings")
        base_url = st.text_input("Analytics API URL", value=DEFAULT_API_URL)
        customer_id = st.text_input(
            "Customer ID",
            value="",
            placeholder="e.g. 1015289504",
            help="Used by the Customer Analytics, Scores, and Insights tabs.",
        )
        if st.button("Refresh data"):
            st.cache_data.clear()

    tab_summary, tab_customer, tab_scores, tab_campaigns, tab_insights = st.tabs(
        [
            "Dataset Summary",
            "Customer Analytics",
            "Scores",
            "Campaign Analytics",
            "Insights",
        ]
    )

    with tab_summary:
        render_dataset_summary(base_url)
    with tab_customer:
        render_customer_analytics(base_url, customer_id)
    with tab_scores:
        render_scores(base_url, customer_id)
    with tab_campaigns:
        render_campaign_analytics(base_url)
    with tab_insights:
        render_insights(base_url, customer_id)


if __name__ == "__main__":
    main()

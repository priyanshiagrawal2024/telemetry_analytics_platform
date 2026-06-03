"""Analytics tools — the agent's only gateway to analytics data.

Each method answers one supported question by calling
:mod:`analytics.analytics_service`, then phrasing the returned values into an
:class:`AgentResponse` ``{answer, evidence[], confidence}``.

Strict boundaries:
* reads **only** through ``analytics_service`` (never raw telemetry / compute modules);
* performs **no** metric or score calculation — it reads and describes values the
  pipeline already produced;
* makes **no** recommendation, intent prediction, or accidental-click claim;
* returns the fixed insufficient-evidence fallback whenever data is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Project root on path so `analytics` and `agent` packages import cleanly.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.agent_models import (  # noqa: E402
    AgentResponse,
    Capability,
    Confidence,
    evidence_item,
)
from analytics import analytics_service as svc  # noqa: E402

__all__ = ["AnalyticsTools"]

# Source tags for evidence provenance.
_SRC_DATASET = "analytics_service.get_dataset_summary"
_SRC_CAMPAIGN = "analytics_service.get_campaign_summary"
_SRC_CUSTOMER = "analytics_service.get_customer_analytics"
_SRC_IDS = "analytics_service.available_customer_ids"

# Descriptive metadata (NOT computation): what each score blends / means.
_SCORE_INPUTS: Dict[str, List[str]] = {
    "engagement_score": ["ctr", "interaction_frequency", "click_rate"],
    "exploration_score": ["unique_campaign_count", "campaign_diversity"],
    "campaign_receptiveness_score": ["ctr", "interaction_frequency"],
}
_SCORE_MEANING: Dict[str, str] = {
    "engagement_score": "how actively the customer interacts with floaters relative to exposure",
    "exploration_score": "the breadth of distinct campaigns the customer engages with",
    "campaign_receptiveness_score": "how responsive the customer is to campaign exposure",
}
_METRIC_LABEL: Dict[str, str] = {
    "ctr": "CTR",
    "interaction_frequency": "interaction frequency per session",
    "click_rate": "click rate",
}

_CUSTOMER_METRICS = [
    "impression_count", "click_count", "skip_count", "ctr", "session_count",
    "unique_campaign_count", "exposure_frequency", "interaction_frequency",
    "campaign_diversity",
]
_CUSTOMER_SCORES = ["engagement_score", "exploration_score", "campaign_receptiveness_score"]


def _fmt(value: Any, *, pct: bool = False, ndigits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{ndigits}f}%" if pct else f"{value:.{ndigits}f}"
    return f"{value}{'%' if pct else ''}"


class AnalyticsTools:
    """Question-aligned, read-only wrappers over the analytics service."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        dataset: Optional[str] = None,
    ) -> None:
        self.path = path
        self.dataset = dataset

    # -- helpers -----------------------------------------------------------

    def list_customers(self) -> List[str]:
        return svc.available_customer_ids(path=self.path, dataset=self.dataset)

    def _customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        return svc.get_customer_analytics(
            customer_id, path=self.path, dataset=self.dataset
        )

    def _unknown_customer(self, customer_id: Optional[str]) -> AgentResponse:
        return AgentResponse.insufficient(
            evidence=[
                evidence_item("requested_customer_id", customer_id, _SRC_CUSTOMER),
                evidence_item("available_customer_ids", self.list_customers(), _SRC_IDS),
            ]
        )

    # -- 4. dataset summary ------------------------------------------------

    def explain_dataset_summary(self) -> AgentResponse:
        ds = svc.get_dataset_summary(path=self.path, dataset=self.dataset)
        unavailable = ds.get("unavailable_metrics") or []
        answer = (
            f"Dataset '{ds.get('dataset')}' contains {_fmt(ds.get('n_customers'))} "
            f"customer(s), {_fmt(ds.get('n_events'))} telemetry event(s), "
            f"{_fmt(ds.get('n_campaigns'))} campaign(s), and "
            f"{_fmt(ds.get('n_sessions'))} session(s)."
        )
        if unavailable:
            answer += (
                " Metrics not available from this telemetry (reported as null, not "
                f"fabricated): {', '.join(unavailable)}."
            )
        evidence = [
            evidence_item("customers", ds.get("n_customers"), _SRC_DATASET),
            evidence_item("events", ds.get("n_events"), _SRC_DATASET),
            evidence_item("campaigns", ds.get("n_campaigns"), _SRC_DATASET),
            evidence_item("sessions", ds.get("n_sessions"), _SRC_DATASET),
            evidence_item("unavailable_metrics", unavailable, _SRC_DATASET),
        ]
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.HIGH)

    # -- 7. list available metrics ----------------------------------------

    def list_available_metrics(self) -> AgentResponse:
        ds = svc.get_dataset_summary(path=self.path, dataset=self.dataset)
        available = sorted((ds.get("metric_averages") or {}).keys())
        unavailable = ds.get("unavailable_metrics") or []
        if not available:
            return AgentResponse.insufficient(
                evidence=[evidence_item("metric_averages", {}, _SRC_DATASET)]
            )
        answer = (
            f"{len(available)} metric(s) are available from this telemetry: "
            f"{', '.join(available)}."
        )
        if unavailable:
            answer += f" Not available: {', '.join(unavailable)}."
        evidence = [evidence_item("available_metrics", available, _SRC_DATASET)]
        if unavailable:
            evidence.append(evidence_item("unavailable_metrics", unavailable, _SRC_DATASET))
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.HIGH)

    # -- 3. campaign analytics --------------------------------------------

    def explain_campaign_analytics(self) -> AgentResponse:
        cs = svc.get_campaign_summary(path=self.path, dataset=self.dataset)
        rows = cs.get("campaigns") or []
        if not rows:
            return AgentResponse.insufficient(
                evidence=[evidence_item("campaigns", [], _SRC_CAMPAIGN)]
            )
        reach = "; ".join(
            f"{r.get('campaign')} = {_fmt(r.get('customers_reached'))}" for r in rows
        )
        answer = (
            f"The dataset has {_fmt(cs.get('n_campaigns'))} campaign(s). "
            f"Campaign reach (distinct customers reached): {reach}. "
            "Campaign interaction is recorded at customer grain, not per campaign, "
            "so no per-campaign interaction figure is reported."
        )
        evidence = [
            evidence_item(
                r.get("campaign"), {"customers_reached": r.get("customers_reached")},
                _SRC_CAMPAIGN,
            )
            for r in rows
        ]
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.MEDIUM)

    # -- 1. summarize customer behaviour ----------------------------------

    def summarize_customer_behavior(self, customer_id: str) -> AgentResponse:
        rec = self._customer(customer_id)
        if not rec:
            return self._unknown_customer(customer_id)
        d = (rec.get("metrics") or {}).get("descriptive") or {}
        s = rec.get("scores") or {}
        answer = (
            f"Customer {customer_id}: {_fmt(d.get('impression_count'))} impression(s), "
            f"{_fmt(d.get('click_count'))} click(s), {_fmt(d.get('skip_count'))} skip(s); "
            f"CTR {_fmt(d.get('ctr'), pct=True, ndigits=1)}. "
            f"Reached {_fmt(d.get('unique_campaign_count'))} campaign(s) over "
            f"{_fmt(d.get('session_count'))} session(s); exposure "
            f"{_fmt(d.get('exposure_frequency'))}/session, interaction "
            f"{_fmt(d.get('interaction_frequency'))}/session, campaign diversity "
            f"{_fmt(d.get('campaign_diversity'))}. Engagement score "
            f"{_fmt(s.get('engagement_score'), ndigits=3)} (0-1). "
            "These are descriptive measurements, not predictions or recommendations."
        )
        evidence = [evidence_item(k, d.get(k), _SRC_CUSTOMER) for k in _CUSTOMER_METRICS]
        evidence += [evidence_item(k, s.get(k), _SRC_CUSTOMER) for k in _CUSTOMER_SCORES]
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.HIGH)

    # -- 2. explain engagement score --------------------------------------

    def explain_engagement_score(self, customer_id: str) -> AgentResponse:
        rec = self._customer(customer_id)
        if not rec:
            return self._unknown_customer(customer_id)
        d = (rec.get("metrics") or {}).get("descriptive") or {}
        s = rec.get("scores") or {}
        value = s.get("engagement_score")
        inputs = _SCORE_INPUTS["engagement_score"]
        phrases = [
            f"{_METRIC_LABEL.get(m, m)} = "
            f"{_fmt(d.get(m), pct=(m == 'ctr'), ndigits=(1 if m == 'ctr' else 2))}"
            for m in inputs
        ]
        answer = (
            f"The engagement score for customer {customer_id} is "
            f"{_fmt(value, ndigits=3)} on a 0-1 scale. It is a weighted blend of "
            f"measured telemetry signals ({', '.join(phrases)}) and reflects "
            f"{_SCORE_MEANING['engagement_score']}. A higher value means more of those "
            "signals are present. It is a descriptive indicator derived from observed "
            "events — not a prediction, intent estimate, or recommendation."
        )
        evidence = [evidence_item("engagement_score", value, _SRC_CUSTOMER)]
        evidence += [evidence_item(m, d.get(m), _SRC_CUSTOMER) for m in inputs]
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.MEDIUM)

    # -- 6. show generated findings ---------------------------------------

    def show_findings(self, customer_id: str) -> AgentResponse:
        rec = self._customer(customer_id)
        if not rec:
            return self._unknown_customer(customer_id)
        insights = rec.get("insights") or []
        if not insights:
            return AgentResponse.insufficient(
                evidence=[evidence_item("insights", [], _SRC_CUSTOMER)]
            )
        # One "Finding / Summary" block per generated finding (title + the
        # finding's own description — no new analytics, service data only).
        blocks, evidence = [], []
        for ins in insights:
            title = ins.get("title", "")
            summary = ins.get("insight", "")
            blocks.append(f"Finding: {title}\nSummary: {summary}")
            evidence.append(evidence_item(title, summary, _SRC_CUSTOMER))
        answer = (
            f"Generated findings for customer {customer_id}:\n\n"
            + "\n\n".join(blocks)
        )
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.MEDIUM)

    # -- 5. evidence supporting an insight --------------------------------

    def show_insight_evidence(self, customer_id: str) -> AgentResponse:
        rec = self._customer(customer_id)
        if not rec:
            return self._unknown_customer(customer_id)
        insights = rec.get("insights") or []
        if not insights:
            return AgentResponse.insufficient(
                evidence=[evidence_item("insights", [], _SRC_CUSTOMER)]
            )
        parts = []
        evidence = []
        for ins in insights:
            ev = ins.get("evidence") or {}
            parts.append(
                f"{ins.get('title')} ({', '.join(f'{k}={v}' for k, v in ev.items())})"
            )
            evidence.append(evidence_item(ins.get("title"), ev, _SRC_CUSTOMER))
        answer = (
            f"Supporting evidence for customer {customer_id}'s findings: "
            + "; ".join(parts) + "."
        )
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.MEDIUM)

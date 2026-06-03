"""Deterministic telemetry analytics agent (no LLM).

Provides a conversational backend over the analytics platform WITHOUT any LLM
(no Gemini / OpenAI / Claude). It routes a natural-language question to exactly
one supported capability using fixed keyword rules, invokes the matching
:class:`AnalyticsTools` method, and returns the structured response
``{answer, evidence, confidence}``.

Guarantees:
* deterministic — identical question + data -> identical response (rule-based
  routing; the analytics service is cached/deterministic);
* grounded — every answer's facts come from ``analytics_service`` and are listed
  in ``evidence``;
* read-only — never reads raw telemetry, never computes a metric/score, never
  recommends, predicts intent, or infers accidental clicks;
* graceful — unsupported questions (or missing/unknown customers) return
  "Telemetry does not contain sufficient evidence." with low confidence.

CLI::

    python agent/telemetry_agent.py "summarize behaviour of customer 1015289504"
    python agent/telemetry_agent.py "explain the engagement score for 1015289504"
    python agent/telemetry_agent.py "explain campaign analytics"
    python agent/telemetry_agent.py "dataset summary"
    python agent/telemetry_agent.py "show evidence for 1015289504"
    python agent/telemetry_agent.py "show findings for 1015289504"
    python agent/telemetry_agent.py "list available metrics"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional, Union

# Project root on path for clean package imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.agent_models import (  # noqa: E402
    EXAMPLE_QUESTIONS,
    SUPPORTED_CAPABILITIES,
    AgentResponse,
    Capability,
    Confidence,
    evidence_item,
)
from agent.analytics_tools import AnalyticsTools  # noqa: E402

__all__ = ["TelemetryAgent"]

# Keyword sets for deterministic routing (checked in the order applied below).
_HELP_KW = ("help", "what can you do", "what can you", "supported question",
            "available command", "commands", "capabilit")
_METRICS_KW = ("metric", "metrics")
_METRICS_LIST_KW = ("list", "available", "which", "what")
_EVIDENCE_KW = ("evidence", "support", "why", "justif", "proof", "reason", "because")
_FINDINGS_KW = ("finding", "findings", "insight", "insights")
_ENGAGEMENT_KW = ("engagement", "score")
_CAMPAIGN_KW = ("campaign",)
_DATASET_KW = ("dataset", "overall", "platform", "population", "totals")
_CUSTOMER_KW = ("customer", "user", "behaviour", "behavior", "profile",
                "summary", "summarize", "summarise")
# Ranking-style analytics queries: an operator + an entity must both appear.
_RANK_OPS = ("most", "least", "highest", "lowest", "best", "worst", "top", "bottom")
_RANK_ENTITY_KW = ("campaign", "click", "clicked", "impression", "impressions",
                   "shown", "engagement", "interaction", "interact", "exposure", "reach")


class TelemetryAgent:
    """Deterministic, no-LLM router over :class:`AnalyticsTools`."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        dataset: Optional[str] = None,
    ) -> None:
        self.tools = AnalyticsTools(path=path, dataset=dataset)

    # -- public API --------------------------------------------------------

    def ask(self, question: str, customer_id: Optional[str] = None) -> AgentResponse:
        """Answer ``question`` deterministically, grounded in the service."""
        capability, cid = self.classify(question, customer_id)

        if capability == Capability.HELP:
            return self._help_response()
        if capability == Capability.ANALYTICS_QUERY:
            return self.tools.analytics_query(question)
        if capability == Capability.LIST_METRICS:
            return self.tools.list_available_metrics()
        if capability == Capability.DATASET_SUMMARY:
            return self.tools.explain_dataset_summary()
        if capability == Capability.CAMPAIGN_ANALYTICS:
            return self.tools.explain_campaign_analytics()

        if capability in Capability.REQUIRES_CUSTOMER:
            if not cid:
                # Supported question, but no customer to ground it in.
                return AgentResponse.insufficient(
                    evidence=[
                        evidence_item(
                            "available_customer_ids",
                            self.tools.list_customers(),
                            "analytics_service.available_customer_ids",
                        )
                    ]
                )
            if capability == Capability.ENGAGEMENT_SCORE:
                return self.tools.explain_engagement_score(cid)
            if capability == Capability.INSIGHT_EVIDENCE:
                return self.tools.show_insight_evidence(cid)
            if capability == Capability.FINDINGS:
                return self.tools.show_findings(cid)
            return self.tools.summarize_customer_behavior(cid)

        # Unsupported question -> fixed fallback.
        return AgentResponse.insufficient()

    # -- routing (exposed for the dashboard / tests) ----------------------

    def classify(self, question: str, customer_id: Optional[str] = None):
        """Return ``(capability, customer_id)`` for a question (deterministic)."""
        text = (question or "").strip()
        cid = (
            str(customer_id) if customer_id is not None
            else self._extract_customer_id(text)
        )
        return self._route(text.lower(), cid), cid

    def route(self, question: str, customer_id: Optional[str] = None) -> str:
        """Return just the routed capability (convenience for tests / UI)."""
        return self.classify(question, customer_id)[0]

    def supported_questions(self) -> list:
        """Example questions the agent supports (for dashboard display)."""
        return list(EXAMPLE_QUESTIONS)

    def _help_response(self) -> AgentResponse:
        """List supported capabilities (agent self-description; high confidence)."""
        numbered = "\n".join(
            f"{i}. {cap}" for i, cap in enumerate(SUPPORTED_CAPABILITIES, start=1)
        )
        answer = (
            "I can answer the following questions, grounded only in the analytics "
            "service:\n" + numbered + "\n\nExample questions: "
            + "; ".join(EXAMPLE_QUESTIONS) + "."
        )
        evidence = [
            evidence_item("capabilities", list(SUPPORTED_CAPABILITIES), "agent.telemetry_agent"),
            evidence_item("example_questions", list(EXAMPLE_QUESTIONS), "agent.telemetry_agent"),
        ]
        return AgentResponse(answer=answer, evidence=evidence, confidence=Confidence.HIGH)

    # -- deterministic routing --------------------------------------------

    @staticmethod
    def _has(lowered: str, keywords) -> bool:
        return any(k in lowered for k in keywords)

    def _route(self, lowered: str, cid: Optional[str]) -> str:
        """Fixed-precedence keyword routing (deterministic)."""
        # Help — listing the agent's own capabilities (highest priority).
        if self._has(lowered, _HELP_KW):
            return Capability.HELP
        # Ranking-style analytics query: a ranking operator + a ranking entity.
        # Checked before campaign/engagement so "which campaign reached the most
        # customers" is treated as a ranking, not a generic campaign explanation.
        if self._has(lowered, _RANK_OPS) and self._has(lowered, _RANK_ENTITY_KW):
            return Capability.ANALYTICS_QUERY
        # 7. list metrics — only when phrased as a listing request.
        if self._has(lowered, _METRICS_KW) and self._has(lowered, _METRICS_LIST_KW):
            return Capability.LIST_METRICS
        # 2. engagement score — PRIORITISED over evidence so questions like
        #    "why is engagement score low" route to ENGAGEMENT_SCORE.
        if self._has(lowered, _ENGAGEMENT_KW):
            return Capability.ENGAGEMENT_SCORE
        # 5. evidence supporting an insight.
        if self._has(lowered, _EVIDENCE_KW):
            return Capability.INSIGHT_EVIDENCE
        # 6. generated findings.
        if self._has(lowered, _FINDINGS_KW):
            return Capability.FINDINGS
        # 3. campaign analytics.
        if self._has(lowered, _CAMPAIGN_KW):
            return Capability.CAMPAIGN_ANALYTICS
        # 4. dataset summary (when not clearly about one customer).
        if self._has(lowered, _DATASET_KW) and not cid:
            return Capability.DATASET_SUMMARY
        # 1. customer behaviour.
        if cid or self._has(lowered, _CUSTOMER_KW):
            return Capability.CUSTOMER_BEHAVIOR
        if self._has(lowered, _DATASET_KW):
            return Capability.DATASET_SUMMARY
        return Capability.UNSUPPORTED

    def _extract_customer_id(self, question: str) -> Optional[str]:
        """Resolve a customer id from the question (prefers a known id)."""
        ids = set(self.tools.list_customers())
        for token in re.findall(r"[A-Za-z0-9_]+", question):
            if token in ids:
                return token
        digit_runs = re.findall(r"\d{3,}", question)
        for run in digit_runs:
            if run in ids:
                return run
        # A bare numeric token still routes to a deterministic unknown-customer result.
        return digit_runs[0] if digit_runs else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    question = " ".join(sys.argv[1:]).strip() or "dataset summary"
    response = TelemetryAgent().ask(question)
    print(json.dumps(response.to_dict(), indent=2, ensure_ascii=False))

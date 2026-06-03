"""Tests for the deterministic telemetry analytics agent.

Verifies (per the demo-readiness brief):
* HELP capability works,
* engagement routing precedence (engagement beats evidence),
* findings response contains per-finding summaries,
* supported-question examples are returned,
* deterministic behaviour is unchanged,
* the fallback response is unchanged.

Run directly (``python agent/test_telemetry_agent.py``) or under pytest.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.agent_models import (  # noqa: E402
    EXAMPLE_QUESTIONS,
    SUPPORTED_CAPABILITIES,
    Capability,
    Confidence,
    FALLBACK_ANSWER,
)
from agent.telemetry_agent import TelemetryAgent  # noqa: E402

logging.disable(logging.CRITICAL)

_AGENT = TelemetryAgent()
_CID = (_AGENT.tools.list_customers() or ["1015289504"])[0]


def test_help_capability() -> None:
    for q in ("help", "what can you do", "supported questions", "available commands"):
        assert _AGENT.route(q) == Capability.HELP, q
    r = _AGENT.ask("help")
    assert r.confidence == Confidence.HIGH
    # Every capability is listed in the answer.
    for cap in SUPPORTED_CAPABILITIES:
        assert cap in r.answer, cap
    # Evidence carries the capability list.
    keys = {e["key"] for e in r.evidence}
    assert "capabilities" in keys
    caps_value = next(e["value"] for e in r.evidence if e["key"] == "capabilities")
    assert list(caps_value) == list(SUPPORTED_CAPABILITIES)


def test_engagement_routing_precedence() -> None:
    # Engagement words win even when an evidence word ("why") is present.
    assert _AGENT.route("why is engagement score low") == Capability.ENGAGEMENT_SCORE
    assert _AGENT.route("explain the engagement score") == Capability.ENGAGEMENT_SCORE
    # Pure evidence questions still route to INSIGHT_EVIDENCE.
    assert _AGENT.route(f"show evidence for {_CID}") == Capability.INSIGHT_EVIDENCE
    assert _AGENT.route(f"why for customer {_CID}") == Capability.INSIGHT_EVIDENCE


def test_existing_routes_unbroken() -> None:
    assert _AGENT.route("list available metrics") == Capability.LIST_METRICS
    assert _AGENT.route("dataset summary") == Capability.DATASET_SUMMARY
    assert _AGENT.route("explain campaign analytics") == Capability.CAMPAIGN_ANALYTICS
    assert _AGENT.route(f"show findings for {_CID}") == Capability.FINDINGS
    assert _AGENT.route(f"summarize customer {_CID}") == Capability.CUSTOMER_BEHAVIOR


def test_findings_contains_summaries() -> None:
    r = _AGENT.ask(f"show findings for {_CID}")
    assert "Finding:" in r.answer
    assert "Summary:" in r.answer
    assert len(r.evidence) >= 1
    assert r.confidence == Confidence.MEDIUM


def test_supported_questions_returned() -> None:
    examples = _AGENT.supported_questions()
    assert isinstance(examples, list)
    assert examples == list(EXAMPLE_QUESTIONS)
    assert len(examples) == 7


def test_deterministic_behaviour() -> None:
    for q in ("help", "dataset summary", f"summarize customer {_CID}",
              f"show findings for {_CID}", "list available metrics"):
        assert _AGENT.ask(q).to_dict() == _AGENT.ask(q).to_dict(), q


def test_fallback_unchanged() -> None:
    r = _AGENT.ask("tell me a joke")
    assert r.answer == FALLBACK_ANSWER
    assert r.confidence == Confidence.LOW
    assert r.evidence == []


def test_response_model_contract() -> None:
    for q in ("help", "dataset summary", f"summarize customer {_CID}", "xyz"):
        assert set(_AGENT.ask(q).to_dict().keys()) == {"answer", "evidence", "confidence"}, q


def test_ranking_routes_to_analytics_query() -> None:
    for q in (
        "which campaign reached the most customers",
        "top campaign",
        "which campaign performed best",
        "which campaign had the highest engagement",
        "what was clicked most often",
        "what did the customer interact with least",
    ):
        assert _AGENT.route(q) == Capability.ANALYTICS_QUERY, q


def test_supported_ranking_campaign_reach() -> None:
    # Campaign reach IS exposed by analytics_service -> a grounded answer.
    r = _AGENT.ask("which campaign reached the most customers")
    assert r.answer != FALLBACK_ANSWER
    assert r.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert len(r.evidence) >= 1                       # evidence included
    assert any("customers_reached" in str(e.get("value")) for e in r.evidence)
    assert "reach" in r.answer.lower()


def test_unsupported_ranking_returns_insufficient() -> None:
    for q in (
        "which campaign performed best",
        "which campaign had the highest engagement",
        "which campaign got the most clicks",
        "what was clicked most often",
        "what was shown least often",
        "what did the customer interact with most",
    ):
        r = _AGENT.ask(q)
        assert r.answer == FALLBACK_ANSWER, q
        assert r.confidence == Confidence.LOW, q


def test_ranking_deterministic() -> None:
    for q in ("which campaign reached the most customers", "which campaign performed best"):
        assert _AGENT.ask(q).to_dict() == _AGENT.ask(q).to_dict(), q


def run() -> int:
    tests = [
        test_help_capability,
        test_engagement_routing_precedence,
        test_existing_routes_unbroken,
        test_findings_contains_summaries,
        test_supported_questions_returned,
        test_deterministic_behaviour,
        test_fallback_unchanged,
        test_response_model_contract,
        test_ranking_routes_to_analytics_query,
        test_supported_ranking_campaign_reach,
        test_unsupported_ranking_returns_insufficient,
        test_ranking_deterministic,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print("\nAGENT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())

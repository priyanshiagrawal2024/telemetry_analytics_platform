"""Data models for the deterministic telemetry analytics agent.

Pure data containers — no analytics, no service access, no computation. Shared
by ``analytics_tools.py`` (produces :class:`AgentResponse`) and
``telemetry_agent.py`` (routes questions to tools).

Response contract (stable)::

    {
      "answer": str,
      "evidence": [ {...sourced facts...} ],
      "confidence": "high" | "medium" | "low"
    }
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


class Capability:
    """The questions the agent supports (string constants)."""

    CUSTOMER_BEHAVIOR = "customer_behavior"      # 1. summarize customer behaviour
    ENGAGEMENT_SCORE = "engagement_score"        # 2. explain engagement score
    CAMPAIGN_ANALYTICS = "campaign_analytics"    # 3. explain campaign analytics
    DATASET_SUMMARY = "dataset_summary"          # 4. explain dataset summary
    INSIGHT_EVIDENCE = "insight_evidence"        # 5. evidence supporting an insight
    FINDINGS = "findings"                        # 6. show generated findings
    LIST_METRICS = "list_metrics"                # 7. list available metrics
    ANALYTICS_QUERY = "analytics_query"          # ranking-style analytics question
    HELP = "help"                                # list supported capabilities
    UNSUPPORTED = "unsupported"                  # fallback

    #: Capabilities that require a specific customer id.
    REQUIRES_CUSTOMER = frozenset(
        {CUSTOMER_BEHAVIOR, ENGAGEMENT_SCORE, INSIGHT_EVIDENCE, FINDINGS}
    )


class Confidence:
    """Allowed confidence levels."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Fixed answer returned whenever the telemetry cannot ground a response.
FALLBACK_ANSWER = "Telemetry does not contain sufficient evidence."

#: Human-readable list of supported capabilities (used by HELP and the dashboard).
SUPPORTED_CAPABILITIES = (
    "Summarize customer behavior",
    "Explain engagement score",
    "Explain campaign analytics",
    "Explain dataset summary",
    "Show evidence supporting findings",
    "Show generated findings",
    "List available metrics",
)

#: Example questions the agent can answer (for HELP and dashboard display).
EXAMPLE_QUESTIONS = (
    "summarize customer 1015289504",
    "explain engagement score for 1015289504",
    "explain campaign analytics",
    "dataset summary",
    "show evidence for 1015289504",
    "show findings for 1015289504",
    "list available metrics",
)


def evidence_item(key: str, value: Any, source: str) -> Dict[str, Any]:
    """Build one sourced evidence fact (keeps evidence auditable & uniform)."""
    return {"key": key, "value": value, "source": source}


@dataclass
class AgentResponse:
    """The agent's structured answer (the public response contract)."""

    answer: str
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    confidence: str = Confidence.LOW

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def insufficient(cls, evidence: List[Dict[str, Any]] | None = None) -> "AgentResponse":
        """Standard low-confidence fallback response."""
        return cls(answer=FALLBACK_ANSWER, evidence=evidence or [], confidence=Confidence.LOW)

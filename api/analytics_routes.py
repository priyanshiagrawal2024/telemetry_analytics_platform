"""Analytics serving endpoints for the Telemetry Analytics Platform.

This module is the **HTTP serving surface** for analytics. It exposes read-only
endpoints that return per-customer analytics and a dataset summary. It does NOT
compute analytics itself — computation is the Analytics Engine's responsibility.

Integration seam
----------------
Route handlers delegate to an :class:`AnalyticsProvider`. Today a
:class:`MockAnalyticsProvider` returns placeholder data so the API contract and
Swagger docs are stable immediately. When the analytics runner is ready, swap it
in via :func:`set_analytics_provider` — **no route or response-shape changes**.

Contract alignment
------------------
Response shapes mirror ``contracts/analytics_contract.md``: ``metrics`` / ``scores``
use the v2 *supported* metric names (§3), and fields the contract marks as
*placeholders* (§4 — ``conversion_rate``, ``fatigue_score``, segmentation) are
returned as ``null`` rather than fabricated values. Insights are
descriptive/diagnostic only (platform guardrail), never prescriptive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models (mirror analytics_contract.md §3/§4/§6)
# ---------------------------------------------------------------------------


class CustomerMetrics(BaseModel):
    """Supported per-customer metrics (analytics_contract.md §3).

    Placeholder metrics (§4) are typed ``Optional`` and default to ``None`` —
    they signal *missing capability*, never a misleading zero.
    """

    total_impressions: int
    total_clicks: int
    total_skips: int
    ctr: Optional[float] = None  # %
    skip_rate: Optional[float] = None  # %
    repeat_impression_rate: Optional[float] = None  # %
    avg_time_to_click_sec: Optional[float] = None
    avg_time_to_skip_sec: Optional[float] = None
    conversion_rate: Optional[float] = None  # §4 placeholder (no conversion telemetry)


class CustomerScores(BaseModel):
    """Per-customer scores (0–1 unless noted). Placeholders default to ``None``."""

    attention_score: Optional[float] = None  # clicks / (clicks + skips)
    exploration_score: Optional[float] = None
    campaign_diversity_score: Optional[float] = None
    fatigue_score: Optional[float] = None  # §4 placeholder (needs population/temporal)


class Insight(BaseModel):
    """Descriptive/diagnostic insight (analytics_contract.md guardrails).

    Explains observed behaviour with cited evidence — never prescribes actions.
    """

    insight_id: str
    category: str
    severity: str  # info | low | medium | high
    headline: str
    explanation: str
    evidence: dict = Field(default_factory=dict)


class CustomerAnalyticsResponse(BaseModel):
    customerId: str
    metrics: CustomerMetrics
    scores: CustomerScores
    insights: list[Insight] = Field(default_factory=list)
    primary_segment: Optional[str] = None  # §4 placeholder (needs population)
    source: str = "mock"  # "mock" until the analytics runner is integrated
    generated_at: datetime


class DatasetStatistics(BaseModel):
    total_events: int
    normalized_events: int
    quarantined_events: int
    impressions: int
    clicks: int
    skips: int


class AnalyticsSummaryResponse(BaseModel):
    dataset_statistics: DatasetStatistics
    customer_count: int
    campaign_count: int
    source: str = "mock"
    generated_at: datetime


# ---------------------------------------------------------------------------
# Provider seam — swap MockAnalyticsProvider for the real runner later
# ---------------------------------------------------------------------------


class AnalyticsProvider(Protocol):
    """Interface the serving layer depends on. The analytics runner implements this."""

    def customer_analytics(self, customer_id: str) -> CustomerAnalyticsResponse: ...

    def summary(self) -> AnalyticsSummaryResponse: ...


class MockAnalyticsProvider:
    """Placeholder provider.

    Returns deterministic, contract-shaped mock data grounded in the validated
    sample reality (analytics_contract.md §7 / event_schema.md §12: 194 rows →
    7 impressions / 3 clicks / 3 skips, 1 customer, 1 campaign). This makes the
    API usable and demonstrable before the analytics runner exists.
    """

    def customer_analytics(self, customer_id: str) -> CustomerAnalyticsResponse:
        metrics = CustomerMetrics(
            total_impressions=7,
            total_clicks=3,
            total_skips=3,
            ctr=42.86,
            skip_rate=42.86,
            repeat_impression_rate=0.0,
            avg_time_to_click_sec=4.2,
            avg_time_to_skip_sec=2.7,
            conversion_rate=None,  # §4 placeholder
        )
        scores = CustomerScores(
            attention_score=0.5,  # 3 / (3 + 3)
            exploration_score=1.0,
            campaign_diversity_score=1.0,
            fatigue_score=None,  # §4 placeholder
        )
        insights = [
            Insight(
                insight_id="mock_ins_0001",
                category="engagement",
                severity="info",
                headline="Balanced engagement on observed floaters",
                explanation=(
                    "Of the customer's reactions, half were clicks and half were "
                    "skips (attention score 0.5), on a CTR of 42.9%. "
                    "[MOCK DATA - illustrative only]"
                ),
                evidence={"ctr": 42.86, "skip_rate": 42.86, "attention_score": 0.5},
            )
        ]
        return CustomerAnalyticsResponse(
            customerId=customer_id,
            metrics=metrics,
            scores=scores,
            insights=insights,
            primary_segment=None,  # §4 placeholder (needs population)
            source="mock",
            generated_at=datetime.now(timezone.utc),
        )

    def summary(self) -> AnalyticsSummaryResponse:
        return AnalyticsSummaryResponse(
            dataset_statistics=DatasetStatistics(
                total_events=194,
                normalized_events=13,
                quarantined_events=181,
                impressions=7,
                clicks=3,
                skips=3,
            ),
            customer_count=1,
            campaign_count=1,
            source="mock",
            generated_at=datetime.now(timezone.utc),
        )


# Module-level provider. Replace at runtime when the runner is ready.
_provider: AnalyticsProvider = MockAnalyticsProvider()


def set_analytics_provider(provider: AnalyticsProvider) -> None:
    """Wire in the real analytics runner (future integration point)."""
    global _provider
    logger.info("Analytics provider set to %s", type(provider).__name__)
    _provider = provider


def get_analytics_provider() -> AnalyticsProvider:
    """FastAPI dependency returning the active analytics provider."""
    return _provider


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/customer/{customerId}",
    response_model=CustomerAnalyticsResponse,
    summary="Per-customer analytics (metrics, scores, insights)",
)
def customer_analytics(
    customerId: str,
    provider: AnalyticsProvider = Depends(get_analytics_provider),
) -> CustomerAnalyticsResponse:
    """Return analytics for a single customer.

    NOTE: currently backed by mock data (``source = "mock"``) until the
    analytics runner is integrated.
    """
    return provider.customer_analytics(customerId)


@router.get(
    "/summary",
    response_model=AnalyticsSummaryResponse,
    summary="Dataset-level analytics summary",
)
def analytics_summary(
    provider: AnalyticsProvider = Depends(get_analytics_provider),
) -> AnalyticsSummaryResponse:
    """Return dataset statistics plus customer and campaign counts.

    NOTE: currently backed by mock data (``source = "mock"``).
    """
    return provider.summary()


__all__ = [
    "router",
    "AnalyticsProvider",
    "MockAnalyticsProvider",
    "set_analytics_provider",
    "get_analytics_provider",
    "CustomerAnalyticsResponse",
    "AnalyticsSummaryResponse",
]

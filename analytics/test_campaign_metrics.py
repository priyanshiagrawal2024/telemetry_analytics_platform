"""Tests for campaign-grain funnel metrics (additive feature).

Verifies:
* per-campaign impressions/clicks/skips/CTR/skip_rate/exposure_frequency/reach,
* only funnel campaigns appear (served-only campaigns excluded; no fake CTR),
* analytics_service.get_campaign_performance exposes the table,
* backward compatibility (customer metrics + get_campaign_summary unchanged),
* deterministic behaviour.

Run directly (``python analytics/test_campaign_metrics.py``) or under pytest.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics import analytics_service as svc  # noqa: E402
from analytics.feature_extractor import (  # noqa: E402
    CAMPAIGN_METRIC_COLUMNS,
    MetricCalculator,
    MetricResult,
    TelemetryLoader,
    load_semantic_schema,
)

logging.disable(logging.CRITICAL)

_SAMPLE = _PROJECT_ROOT / "sample_data" / "telemetry_sample.csv"
_FUNNEL_CAMPAIGN = "PLANEXPIRY01"
# Served-only campaigns (FloaterResponse) must NOT appear in the funnel table.
_SERVED_ONLY = "200PlanFloater_forGeminien_US"


def _result() -> MetricResult:
    raw = TelemetryLoader().load(_SAMPLE)
    return MetricCalculator(load_semantic_schema()).compute(raw)


def _row(df, campaign):
    sub = df[df["campaign"] == campaign]
    return sub.iloc[0] if len(sub) else None


def test_metric_result_has_campaign_metrics() -> None:
    res = _result()
    assert hasattr(res, "campaign_metrics")
    assert list(res.campaign_metrics.columns) == list(CAMPAIGN_METRIC_COLUMNS)
    assert not res.campaign_metrics.empty


def test_campaign_funnel_values() -> None:
    cm = _result().campaign_metrics
    row = _row(cm, _FUNNEL_CAMPAIGN)
    assert row is not None, "funnel campaign missing"
    assert int(row["impressions"]) == 7
    assert int(row["clicks"]) == 3
    assert int(row["skips"]) == 3
    assert int(row["reach"]) == 1
    assert math.isclose(float(row["ctr"]), 3 / 7 * 100, rel_tol=1e-6)
    assert math.isclose(float(row["skip_rate"]), 3 / 7 * 100, rel_tol=1e-6)
    assert float(row["exposure_frequency"]) > 0


def test_served_only_campaign_excluded() -> None:
    cm = _result().campaign_metrics
    assert _FUNNEL_CAMPAIGN in set(cm["campaign"])
    assert _SERVED_ONLY not in set(cm["campaign"])  # no fabricated CTR for it


def test_service_exposes_campaign_performance() -> None:
    perf = svc.get_campaign_performance(path=str(_SAMPLE))
    assert perf["n_campaigns_with_funnel"] >= 1
    names = {c["campaign"] for c in perf["campaigns"]}
    assert _FUNNEL_CAMPAIGN in names
    row = next(c for c in perf["campaigns"] if c["campaign"] == _FUNNEL_CAMPAIGN)
    for key in ("impressions", "clicks", "skips", "ctr", "skip_rate",
                "exposure_frequency", "reach"):
        assert key in row


def test_backward_compatibility() -> None:
    # Customer metrics unchanged (still present with ctr).
    res = _result()
    assert "ctr" in res.customer_metrics.columns
    assert len(res.customer_metrics) >= 1
    # Reach summary unchanged in shape.
    summary = svc.get_campaign_summary(path=str(_SAMPLE))
    assert "campaigns" in summary
    assert all("customers_reached" in c for c in summary["campaigns"])
    # Two-arg MetricResult construction still valid (defaulted campaign_metrics).
    import pandas as pd
    mr = MetricResult(pd.DataFrame(), {})
    assert list(mr.campaign_metrics.columns) == list(CAMPAIGN_METRIC_COLUMNS)


def test_deterministic() -> None:
    a = _result().campaign_metrics
    b = _result().campaign_metrics
    assert a.equals(b)
    assert svc.get_campaign_performance(path=str(_SAMPLE)) == svc.get_campaign_performance(path=str(_SAMPLE))


def run() -> int:
    tests = [
        test_metric_result_has_campaign_metrics,
        test_campaign_funnel_values,
        test_served_only_campaign_excluded,
        test_service_exposes_campaign_performance,
        test_backward_compatibility,
        test_deterministic,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print("\nCAMPAIGN METRICS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())

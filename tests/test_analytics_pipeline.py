"""End-to-end regression tests for the analytics pipeline.

Exercises the full chain — load -> classify -> extract -> metrics -> scores ->
insights -> assemble — via the analytics runner and the analytics service facade,
and locks the *structure* of the assembled output. These are structural checks
(key presence + types), not value assertions, so they protect against accidental
breakage of the pipeline contract without being brittle to legitimate analytics
tuning.

Run:
    pytest tests/test_analytics_pipeline.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

# Make the project root importable regardless of where pytest is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

from analytics import analytics_service
from analytics.analytics_runner import run_analytics

# Expected, configured composite score names (configs/analytics_thresholds.yaml).
EXPECTED_SCORE_KEYS = {
    "engagement_score",
    "exploration_score",
    "campaign_receptiveness_score",
}

# Stable keys of each assembled insight (analytics.insight_generator).
EXPECTED_INSIGHT_KEYS = {"title", "insight", "evidence"}


# ---------------------------------------------------------------------------
# Fixtures — reuse the bundled sample telemetry; run the pipeline once each.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_path() -> Path:
    """Path to the validated sample telemetry (reused by every test)."""
    path = analytics_service.DEFAULT_DATASET_PATH
    if not Path(path).is_file():
        pytest.skip(f"Sample telemetry not found at {path}")
    return Path(path)


@pytest.fixture(scope="module")
def pipeline_result(sample_path: Path) -> Dict[str, Any]:
    """Assembled output of a direct analytics_runner execution."""
    return run_analytics(sample_path)


@pytest.fixture(scope="module")
def customer_id() -> str:
    """A real customer id from the analyzed dataset (deterministic)."""
    ids = analytics_service.available_customer_ids()
    if not ids:
        pytest.skip("No customers available in the analytics dataset.")
    return str(ids[0])


@pytest.fixture(scope="module")
def customer_record(customer_id: str) -> Dict[str, Any]:
    """One customer's analytics record via the service facade."""
    record = analytics_service.get_customer_analytics(customer_id)
    assert record is not None, f"service returned no record for {customer_id}"
    return record


# ---------------------------------------------------------------------------
# Test 1 — analytics_runner executes successfully
# ---------------------------------------------------------------------------


def test_analytics_runner_executes(pipeline_result: Dict[str, Any]) -> None:
    result = pipeline_result

    assert isinstance(result, dict)
    for key in ("dataset", "source", "generated_at", "dataset_summary", "customers"):
        assert key in result, f"runner output missing top-level key: {key}"

    customers = result["customers"]
    assert isinstance(customers, list)
    assert len(customers) >= 1, "pipeline produced no customer records"

    # Each assembled customer record carries the four pipeline sections.
    first = customers[0]
    for key in ("customer_id", "metrics", "scores", "insights"):
        assert key in first, f"customer record missing section: {key}"


# ---------------------------------------------------------------------------
# Test 2 — analytics_service returns metrics, scores, insights
# ---------------------------------------------------------------------------


def test_service_returns_metrics_scores_insights(
    customer_record: Dict[str, Any]
) -> None:
    for key in ("metrics", "scores", "insights"):
        assert key in customer_record, f"service record missing: {key}"

    assert isinstance(customer_record["metrics"], dict)
    assert isinstance(customer_record["scores"], dict)
    assert isinstance(customer_record["insights"], list)


# ---------------------------------------------------------------------------
# Test 3 — metrics structure remains stable
# ---------------------------------------------------------------------------


def test_metrics_structure_stable(customer_record: Dict[str, Any]) -> None:
    metrics = customer_record["metrics"]
    assert isinstance(metrics, dict)

    # Two-part structure: directly-calculable + behavioural (contract §6) profile.
    assert "descriptive" in metrics and isinstance(metrics["descriptive"], dict)
    assert "behavioural" in metrics and isinstance(metrics["behavioural"], dict)

    # Stable behavioural (§6) keys — removal/rename is a regression.
    for key in (
        "total_impressions",
        "total_clicks",
        "ctr",
        "skip_rate",
        "repeat_impression_rate",
        "exploration_score",
    ):
        assert key in metrics["behavioural"], f"missing behavioural metric: {key}"

    # Stable generic descriptive keys.
    for key in ("event_count", "impression_count", "click_count", "ctr"):
        assert key in metrics["descriptive"], f"missing descriptive metric: {key}"


# ---------------------------------------------------------------------------
# Test 4 — scores structure remains stable
# ---------------------------------------------------------------------------


def test_scores_structure_stable(customer_record: Dict[str, Any]) -> None:
    scores = customer_record["scores"]
    assert isinstance(scores, dict)

    # All configured composite scores are present.
    assert EXPECTED_SCORE_KEYS.issubset(scores.keys()), (
        f"missing score(s): {EXPECTED_SCORE_KEYS - set(scores.keys())}"
    )

    # Scores are 0..1 floats (or None when not computable). Never out of range.
    for name, value in scores.items():
        assert value is None or (
            isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0
        ), f"score {name} out of range / wrong type: {value!r}"


# ---------------------------------------------------------------------------
# Test 5 — insights structure remains stable
# ---------------------------------------------------------------------------


def test_insights_structure_stable(customer_record: Dict[str, Any]) -> None:
    insights = customer_record["insights"]
    assert isinstance(insights, list)
    assert len(insights) >= 1, "expected at least one insight for the sample customer"

    for insight in insights:
        assert isinstance(insight, dict)
        assert EXPECTED_INSIGHT_KEYS.issubset(insight.keys()), (
            f"insight missing key(s): {EXPECTED_INSIGHT_KEYS - set(insight.keys())}"
        )
        assert isinstance(insight["title"], str) and insight["title"]
        assert isinstance(insight["insight"], str) and insight["insight"]
        assert isinstance(insight["evidence"], dict)

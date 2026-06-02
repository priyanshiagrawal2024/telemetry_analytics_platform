"""Smoke / integration test for ``analytics.feature_extractor``.

Runs the full pipeline against the FROZEN validated sample
(``sample_data/telemetry_sample.csv`` — an XLSX workbook despite its ``.csv``
name) and asserts the customer profile comes out with the expected shape and
core funnel columns.

Run directly::

    python analytics/test_feature_extractor.py

or under pytest::

    pytest analytics/test_feature_extractor.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

# Ensure the project root is importable whether run as a script
# (``python analytics/test_feature_extractor.py``) or under pytest.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics.feature_extractor import (  # noqa: E402
    EventClassifier,
    FeatureExtractor,
    TelemetryLoader,
)

# Project root = parent of this file's directory (analytics/..).
SAMPLE_PATH = _PROJECT_ROOT / "sample_data" / "telemetry_sample.csv"


def _aggregate(series: pd.Series) -> int:
    """Sum a nullable-int count column, treating <NA> as 0 (capability-gated)."""
    return int(series.fillna(0).sum())


def _fmt(value: object, suffix: str = "") -> str:
    """Format a possibly-NaN/NA metric for display."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
        return "N/A (placeholder)"
    if isinstance(value, float):
        return f"{value:.4f}{suffix}"
    return f"{value}{suffix}"


def run() -> int:
    """Execute the pipeline, print a report, assert invariants. Returns 0 on success."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("=" * 72)
    print("FEATURE EXTRACTOR - INTEGRATION TEST")
    print("=" * 72)
    print(f"Sample file: {SAMPLE_PATH}")

    # 1. Load -----------------------------------------------------------------
    raw = TelemetryLoader().load(SAMPLE_PATH)

    # 2. Classify -------------------------------------------------------------
    classified = EventClassifier().classify(raw)

    # 3. Classification report ------------------------------------------------
    print("\n[1] CLASSIFICATION")
    print(f"    total raw rows        : {len(raw)}")
    print(f"    total classified rows : {len(classified)}")
    print("    event distribution    :")
    if classified.empty:
        print("        (none)")
    else:
        for event, count in classified["event"].value_counts().items():
            print(f"        {event:<12}: {count}")

    # 4. Extract --------------------------------------------------------------
    profiles = FeatureExtractor().extract(classified)

    # 5. Key metrics ----------------------------------------------------------
    # Aggregated across all customer profiles (recomputed from totals so the
    # rates stay correct regardless of how many customers are present).
    impressions = _aggregate(profiles["total_impressions"])
    clicks = _aggregate(profiles["total_clicks"])
    skips = _aggregate(profiles["total_skips"])
    has_skips = profiles["total_skips"].notna().any()

    ctr = (clicks / impressions * 100) if impressions else float("nan")
    skip_rate = (skips / impressions * 100) if (impressions and has_skips) else float("nan")
    attention = (clicks / (clicks + skips)) if (has_skips and (clicks + skips)) else float("nan")
    avg_impr_before_click = profiles["avg_impressions_before_click"].mean()

    print("\n[2] KEY METRICS")
    print(f"    Impressions                 : {impressions}")
    print(f"    Clicks                      : {clicks}")
    print(f"    Skips                       : {skips if has_skips else _fmt(pd.NA)}")
    print(f"    CTR                         : {_fmt(ctr, '%')}")
    print(f"    Skip Rate                   : {_fmt(skip_rate, '%')}")
    print(f"    Attention Score             : {_fmt(attention)}")
    print(f"    Avg Impressions Before Click: {_fmt(avg_impr_before_click)}")

    # 6. Full profile dataframe ----------------------------------------------
    print("\n[3] CUSTOMER PROFILE DATAFRAME")
    print(f"    shape   : {profiles.shape}")
    print(f"    columns : {list(profiles.columns)}")
    print()
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(profiles.to_string(index=False))

    # 7. Assertions -----------------------------------------------------------
    print("\n[4] ASSERTIONS")
    assert profiles.shape[0] >= 1, "expected at least one customer profile"
    assert "customerId" in profiles.columns, "missing column: customerId"
    assert "ctr" in profiles.columns, "missing column: ctr"
    assert "skip_rate" in profiles.columns, "missing column: skip_rate"
    assert "attention_score" in profiles.columns, "missing column: attention_score"
    print("    all assertions passed.")

    # 8. Success --------------------------------------------------------------
    print("\n" + "=" * 72)
    print("FEATURE EXTRACTION TEST PASSED")
    print("=" * 72)
    return 0


def test_feature_extractor_pipeline() -> None:
    """pytest entrypoint (assertions raise on failure)."""
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())

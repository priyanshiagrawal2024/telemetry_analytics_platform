"""End-to-end analytics runner for the MyJio Floater Analytics Platform.

This is a **thin orchestrator**: it wires the existing analytics modules
together and assembles their outputs into a single per-customer record. It
performs **no metric computation of its own** and never recomputes a value that
a module already produced.

Pipeline (each stage = one existing module)::

    TelemetryLoader.load(path)                 -> raw telemetry (read once)
      |- EventClassifier.classify(raw)         -> classified funnel events
      |     -> FeatureExtractor.extract(...)   -> behavioural profile (contract §6)
      |- MetricCalculator.compute(raw)         -> generic metrics + dataset_summary
            -> ScoreCalculator.compute(metrics)        -> composite scores
            -> AnalysisEngine.analyze(metrics, summary) -> evidence-based insights

``ScoreCalculator`` and ``AnalysisEngine`` consume the **already-computed**
generic metrics (they take a metrics frame as input), so scores and insights
are derived from — and traceable to — the exact metrics emitted in the output.

Final output (per customer)::

    {
      "customer_id": ...,
      "metrics":  {"descriptive": {...generic...}, "behavioural": {...§6...}},
      "scores":   {engagement_score, exploration_score, campaign_receptiveness_score},
      "insights": [ {statement, supporting_metrics, evidence, confidence}, ... ]
    }

Reuses (no duplication of logic):
* :class:`analytics.feature_extractor.TelemetryLoader`
* :class:`analytics.feature_extractor.EventClassifier`
* :class:`analytics.feature_extractor.FeatureExtractor`
* :class:`analytics.feature_extractor.MetricCalculator`
* :class:`analytics.score_calculator.ScoreCalculator`
* :class:`analytics.analysis_engine.AnalysisEngine`
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

# Make the package importable when run as a script or under pytest.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics.feature_extractor import (  # noqa: E402
    EventClassifier,
    EventClassifierConfig,
    FeatureExtractor,
    FeatureExtractorConfig,
    MetricCalculator,
    SemanticSchema,
    TelemetryLoader,
    load_semantic_schema,
)
from analytics.insight_generator import InsightGenerator  # noqa: E402
from analytics.score_calculator import ScoreCalculator, ScoreConfig  # noqa: E402

__all__ = ["AnalyticsRunner", "run_analytics"]

logger = logging.getLogger(__name__)

#: Keys of the dataset summary surfaced at the top level for auditability.
_SUMMARY_KEYS = (
    "n_customers",
    "n_events",
    "n_sessions",
    "n_campaigns",
    "capabilities",
    "unavailable_metrics",
    "event_distribution",
    "campaign_reach",
    "metric_averages",
)


# ---------------------------------------------------------------------------
# JSON-safety helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Coerce pandas/numpy values into JSON-serialisable Python types.

    ``<NA>`` / ``NaN`` / ``NaT`` -> ``None`` (an unsupported metric stays
    unavailable, never a fake 0); numpy scalars -> native; Timestamp -> ISO.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, np.ndarray)):
        return [_jsonable(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    # Scalar missing-value check (guarded: pd.isna on arrays is ambiguous).
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, float):
        return None if math.isnan(value) else value
    return value


def _row_to_dict(row: pd.Series) -> Dict[str, Any]:
    """Convert a DataFrame row to a JSON-safe dict."""
    return {str(k): _jsonable(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class AnalyticsRunner:
    """Orchestrates the existing analytics modules end-to-end.

    All configuration is optional; sensible, schema-driven defaults are used.
    Nothing here computes metrics — it only sequences modules and assembles
    their results.
    """

    dataset: Optional[str] = None
    semantic_mappings_path: Optional[Union[str, Path]] = None
    thresholds_path: Optional[Union[str, Path]] = None
    feature_config: Optional[FeatureExtractorConfig] = None

    def __post_init__(self) -> None:
        self.schema: SemanticSchema = (
            load_semantic_schema(self.semantic_mappings_path, self.dataset)
            if self.semantic_mappings_path
            else load_semantic_schema(dataset=self.dataset)
        )
        # Each module is constructed once and reused for every run.
        self._loader = TelemetryLoader()
        self._classifier = EventClassifier(
            EventClassifierConfig.from_schema(self.schema)
        )
        self._extractor = FeatureExtractor(self.feature_config)
        self._metrics = MetricCalculator(self.schema)
        score_cfg = (
            ScoreConfig.load(self.thresholds_path) if self.thresholds_path else None
        )
        self._scorer = ScoreCalculator(score_cfg)
        self._insighter = InsightGenerator()

    # -- public API --------------------------------------------------------

    def run(self, path: Union[str, Path]) -> Dict[str, Any]:
        """Run the full pipeline and return the assembled analytics output."""
        # 1. Load (once).
        raw = self._loader.load(path)

        # 2-3. Classify -> behavioural profile (contract §6).
        classified = self._classifier.classify(raw)
        profile = self._extractor.extract(classified)

        # (parallel) generic, directly-calculable metrics + dataset context.
        metric_result = self._metrics.compute(raw)
        metrics = metric_result.customer_metrics
        summary = metric_result.dataset_summary

        # 4. Scores from the already-computed metrics (no recompute).
        scores = self._scorer.compute(metrics)

        # 5. Business-facing insights from the same metrics + behavioural
        #    profile (no recompute; single-customer, fact-based).
        insights = self._insighter.generate(metrics, profile)

        # 6. Assemble.
        customers = self._assemble(metrics, profile, scores, insights)
        logger.info("Assembled analytics output for %d customer(s).", len(customers))

        return {
            "dataset": self.schema.dataset,
            "source": str(path),
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "dataset_summary": {
                k: _jsonable(summary.get(k)) for k in _SUMMARY_KEYS if k in summary
            },
            "customers": customers,
        }

    # -- assembly ----------------------------------------------------------

    @staticmethod
    def _assemble(
        metrics: pd.DataFrame,
        profile: pd.DataFrame,
        scores: pd.DataFrame,
        insights: Dict[str, List[Any]],
    ) -> List[Dict[str, Any]]:
        """Join the per-customer outputs into the final record list."""
        if metrics.empty:
            return []

        descriptive = metrics.set_index(metrics["customerId"].astype(str))
        behavioural = (
            profile.set_index(profile["customerId"].astype(str))
            if not profile.empty
            else profile
        )
        score_idx = (
            scores.set_index(scores["customerId"].astype(str))
            if not scores.empty and "customerId" in scores.columns
            else scores
        )
        score_cols = [
            c for c in score_idx.columns
            if c != "customerId" and not c.startswith("norm__")
        ]

        records: List[Dict[str, Any]] = []
        for cid in descriptive.index:
            desc = _row_to_dict(descriptive.loc[cid].drop(labels=["customerId"], errors="ignore"))
            beh = (
                _row_to_dict(behavioural.loc[cid].drop(labels=["customerId"], errors="ignore"))
                if cid in getattr(behavioural, "index", [])
                else {}
            )
            scr = (
                _row_to_dict(score_idx.loc[cid][score_cols])
                if cid in getattr(score_idx, "index", [])
                else {}
            )
            cust_insights = insights.get(cid, {})
            records.append(
                {
                    "customer_id": cid,
                    "metrics": {"descriptive": desc, "behavioural": beh},
                    "scores": {k: _jsonable(v) for k, v in scr.items()},
                    "insights": _jsonable(cust_insights.get("insights", [])),
                    "dashboard_summary": _jsonable(
                        cust_insights.get("dashboard_summary", [])
                    ),
                }
            )
        return records


def run_analytics(
    path: Union[str, Path],
    *,
    dataset: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience one-call entrypoint (default configs)."""
    return AnalyticsRunner(dataset=dataset).run(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run the floater analytics pipeline.")
    parser.add_argument(
        "path",
        nargs="?",
        default="sample_data/telemetry_sample.csv",
        help="Telemetry export (XLSX-disguised-as-CSV supported).",
    )
    parser.add_argument("--dataset", default=None, help="Semantic dataset key.")
    parser.add_argument("--out", default=None, help="Write JSON to this file.")
    parser.add_argument("--quiet", action="store_true", help="Suppress module logs.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    result = AnalyticsRunner(dataset=args.dataset).run(args.path)
    payload = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"Wrote analytics output for {len(result['customers'])} customer(s) to {args.out}")
    else:
        print(payload)

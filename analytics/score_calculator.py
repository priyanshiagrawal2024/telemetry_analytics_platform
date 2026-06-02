"""Composite score calculation for the configurable analytics framework.

Layer position::

    MetricCalculator (directly-calculable metrics)
        -> ScoreCalculator   <-- THIS MODULE
        -> AnalysisEngine

Turns the directly-calculable per-customer metrics from
:class:`analytics.feature_extractor.MetricCalculator` into **configurable
composite scores** (0..1). Every weight, normalization reference and method
lives in ``configs/analytics_thresholds.yaml`` — nothing is hardcoded here.

Philosophy (project context)
----------------------------
Metrics are evidence; scores are *combinations* of evidence. We never collapse
a single metric into a behavioural label (``if ctr > x: high_intent``). A score
is a weighted blend of normalized, directly-measured metrics, and the weights
are owned by config so analysts can tune without touching code.

This is rule-based and explainable: :meth:`ScoreCalculator.compute` can also
return the normalized component breakdown behind each score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import numpy as np
import pandas as pd
import yaml

__all__ = ["ScoreCalculator", "ScoreConfig"]

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLDS = (
    Path(__file__).resolve().parents[1] / "configs" / "analytics_thresholds.yaml"
)


@dataclass(frozen=True)
class ScoreConfig:
    """Parsed scoring configuration (from ``analytics_thresholds.yaml``)."""

    normalization: Mapping[str, Mapping[str, Any]]
    scores: Mapping[str, Mapping[str, Any]]

    @classmethod
    def load(cls, path: Union[str, Path] = _DEFAULT_THRESHOLDS) -> "ScoreConfig":
        with Path(path).open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        return cls(
            normalization=doc.get("normalization", {}) or {},
            scores=doc.get("scores", {}) or {},
        )


class ScoreCalculator:
    """Compute configurable composite scores from per-customer metrics."""

    def __init__(self, config: Optional[ScoreConfig] = None) -> None:
        self.config = config or ScoreConfig.load()

    # -- public API --------------------------------------------------------

    def compute(
        self, customer_metrics: pd.DataFrame, with_components: bool = False
    ) -> pd.DataFrame:
        """Return per-customer scores (0..1), one column per configured score.

        Parameters
        ----------
        customer_metrics:
            Output of ``MetricCalculator.compute().customer_metrics``.
        with_components:
            When True, also include the normalized input metrics (prefixed
            ``norm__``) for full explainability of each score.
        """
        if not isinstance(customer_metrics, pd.DataFrame):
            raise TypeError("`customer_metrics` must be a pandas DataFrame.")
        if customer_metrics.empty:
            logger.warning("Empty metrics frame; returning empty scores.")
            cols = ["customerId", *self.config.scores]
            return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})

        df = customer_metrics.reset_index(drop=True)

        # Normalize every metric referenced by any score, once.
        needed = {
            metric
            for spec in self.config.scores.values()
            for metric in (spec.get("weights") or {})
        }
        normalized: Dict[str, pd.Series] = {}
        for metric in sorted(needed):
            if metric not in df.columns:
                logger.warning(
                    "Score input metric '%s' absent from metrics frame; "
                    "it will be dropped from any score that uses it.",
                    metric,
                )
                continue
            normalized[metric] = self._normalize_series(metric, df[metric])

        out = pd.DataFrame()
        out["customerId"] = df.get("customerId", pd.Series(range(len(df))))

        for score_name, spec in self.config.scores.items():
            out[score_name] = self._weighted_score(spec.get("weights") or {}, normalized, len(df))

        if with_components:
            for metric, series in normalized.items():
                out[f"norm__{metric}"] = series.to_numpy()
        return out

    # -- internals ---------------------------------------------------------

    def _weighted_score(
        self,
        weights: Mapping[str, float],
        normalized: Mapping[str, pd.Series],
        n_rows: int,
    ) -> pd.Series:
        """Weighted mean of available normalized metrics; weights renormalized."""
        available = {m: float(w) for m, w in weights.items() if m in normalized}
        total = sum(available.values())
        if not available or total <= 0:
            return pd.Series([np.nan] * n_rows)
        acc = np.zeros(n_rows, dtype="float64")
        for metric, weight in available.items():
            acc = acc + normalized[metric].fillna(0.0).to_numpy() * (weight / total)
        return pd.Series(acc).clip(0.0, 1.0)

    def _normalize_series(self, metric: str, series: pd.Series) -> pd.Series:
        """Scale a raw metric column to 0..1 per its normalization config."""
        cfg = self.config.normalization.get(metric, {})
        method = str(cfg.get("method", "reference"))
        invert = bool(cfg.get("invert", False))
        values = pd.to_numeric(series, errors="coerce").astype("float64")

        if method == "minmax":
            lo, hi = values.min(), values.max()
            if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
                # Degenerate population (e.g. single customer) -> neutral 0.5.
                norm = pd.Series(np.where(values.notna(), 0.5, np.nan), index=values.index)
            else:
                norm = (values - lo) / (hi - lo)
        else:  # reference (default) — robust for tiny populations
            reference = float(cfg.get("reference", 1.0)) or 1.0
            norm = values / reference

        norm = norm.clip(0.0, 1.0)
        if invert:
            norm = 1.0 - norm
        return norm


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analytics.feature_extractor import (  # noqa: E402
        MetricCalculator,
        TelemetryLoader,
        load_semantic_schema,
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sample = sys.argv[1] if len(sys.argv) > 1 else "sample_data/telemetry_sample.csv"

    raw = TelemetryLoader().load(sample)
    metrics = MetricCalculator(load_semantic_schema()).compute(raw)
    scores = ScoreCalculator().compute(metrics.customer_metrics, with_components=True)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print("\nComposite scores (0..1):")
    print(scores.to_string(index=False))

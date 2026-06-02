"""Evidence-based observation generation for the analytics framework.

Layer position::

    MetricCalculator -> ScoreCalculator -> AnalysisEngine   <-- THIS MODULE

Produces **explainable observations** about each customer, built strictly from
directly-measured metrics. Each observation carries:

* ``supporting_metrics`` — the metric values it is built on,
* ``evidence`` — the comparison/quantities that justify it,
* ``confidence`` — 0..1, honestly discounted by population size and effect.

Guardrails (project context)
----------------------------
Observations are descriptive and evidence-grounded. They describe **what the
telemetry shows**, never inferred mental states. Permitted (good)::

    "User interacts with campaigns more frequently than the dataset average."
    "User engages with a diverse set of campaigns."
    "Campaign exposure is high while interaction frequency is relatively low."

Forbidden (bad — unsupported by telemetry)::

    "User was distracted."  "User accidentally clicked."  "User read carefully."

Two observation families are produced:

* **Comparative** — customer vs. dataset average. Confidence is scaled down when
  the population is small (a single-customer dataset yields near-zero
  confidence, so these are suppressed — by design, not by accident).
* **Structural** — relationships *within* one customer's own metrics
  (population-free), so they remain valid even on a tiny dataset.

Thresholds and confidence parameters live in
``configs/analytics_thresholds.yaml`` — nothing is hardcoded here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import numpy as np
import pandas as pd
import yaml

__all__ = ["AnalysisEngine", "Observation", "ObservationConfig"]

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLDS = (
    Path(__file__).resolve().parents[1] / "configs" / "analytics_thresholds.yaml"
)


@dataclass
class Observation:
    """A single evidence-backed observation about a customer."""

    observation_id: str
    statement: str
    supporting_metrics: Dict[str, float] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservationConfig:
    """Parsed observation configuration (from ``analytics_thresholds.yaml``)."""

    high_ratio: float = 1.20
    low_ratio: float = 0.80
    diversity_floor: float = 0.40
    min_unique_campaigns: int = 2
    exposure_interaction_gap: float = 1.30
    min_population: int = 30
    min_confidence_to_report: float = 0.05
    base_confidence: float = 0.90

    @classmethod
    def load(cls, path: Union[str, Path] = _DEFAULT_THRESHOLDS) -> "ObservationConfig":
        with Path(path).open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        obs = doc.get("observations", {}) or {}
        conf = obs.get("confidence", {}) or {}
        return cls(
            high_ratio=float(obs.get("high_ratio", 1.20)),
            low_ratio=float(obs.get("low_ratio", 0.80)),
            diversity_floor=float(obs.get("diversity_floor", 0.40)),
            min_unique_campaigns=int(obs.get("min_unique_campaigns", 2)),
            exposure_interaction_gap=float(obs.get("exposure_interaction_gap", 1.30)),
            min_population=int(obs.get("min_population", 30)),
            min_confidence_to_report=float(obs.get("min_confidence_to_report", 0.05)),
            base_confidence=float(conf.get("base", 0.90)),
        )


class AnalysisEngine:
    """Generate evidence-based observations from metrics + dataset summary."""

    def __init__(self, config: Optional[ObservationConfig] = None) -> None:
        self.config = config or ObservationConfig.load()

    # -- public API --------------------------------------------------------

    def analyze(
        self,
        customer_metrics: pd.DataFrame,
        dataset_summary: Mapping[str, Any],
    ) -> Dict[str, List[Observation]]:
        """Return ``{customerId: [Observation, ...]}`` for every customer."""
        if customer_metrics.empty:
            return {}
        averages = dict(dataset_summary.get("metric_averages", {}))
        n_population = int(dataset_summary.get("n_customers", len(customer_metrics)))

        results: Dict[str, List[Observation]] = {}
        for _, row in customer_metrics.iterrows():
            observations: List[Observation] = []
            observations += self._comparative(row, averages, n_population)
            observations += self._structural(row)
            observations += self._diversity(row)
            kept = [
                o for o in observations
                if o.confidence >= self.config.min_confidence_to_report
            ]
            results[str(row["customerId"])] = sorted(
                kept, key=lambda o: o.confidence, reverse=True
            )
        return results

    # -- confidence model --------------------------------------------------

    def _population_factor(self, n_population: int) -> float:
        """1.0 with enough customers; linearly small for tiny populations."""
        if self.config.min_population <= 0:
            return 1.0
        return float(min(1.0, n_population / self.config.min_population))

    def _effect_factor(self, ratio: float) -> float:
        """Scales 0..1 with how far ``ratio`` sits beyond the high/low band."""
        margin = max(self.config.high_ratio - 1.0, 1e-9)
        return float(min(1.0, abs(ratio - 1.0) / margin))

    # -- comparative (customer vs dataset average) -------------------------

    def _comparative(
        self, row: pd.Series, averages: Mapping[str, float], n_population: int
    ) -> List[Observation]:
        pop_factor = self._population_factor(n_population)
        specs = [
            ("interaction_frequency", "interacts with campaigns"),
            ("ctr", "clicks through campaigns"),
            ("exposure_frequency", "is exposed to campaigns"),
            ("unique_campaign_count", "is reached by distinct campaigns"),
        ]
        out: List[Observation] = []
        for metric, phrase in specs:
            value = _num(row.get(metric))
            avg = _num(averages.get(metric))
            if np.isnan(value) or np.isnan(avg) or avg <= 0:
                continue
            ratio = value / avg
            if ratio >= self.config.high_ratio:
                direction = "more frequently than" if "frequenc" in metric or metric == "interaction_frequency" else "more than"
            elif ratio <= self.config.low_ratio:
                direction = "less frequently than" if metric == "interaction_frequency" else "less than"
            else:
                continue
            confidence = round(self.config.base_confidence * pop_factor * self._effect_factor(ratio), 4)
            out.append(
                Observation(
                    observation_id=f"cmp_{metric}",
                    statement=f"User {phrase} {direction} the dataset average.",
                    supporting_metrics={metric: round(value, 4)},
                    evidence={
                        "metric": metric,
                        "customer_value": round(value, 4),
                        "dataset_average": round(avg, 4),
                        "ratio_to_average": round(ratio, 4),
                        "population_size": n_population,
                    },
                    confidence=confidence,
                )
            )
        return out

    # -- structural (within one customer; population-free) -----------------

    def _structural(self, row: pd.Series) -> List[Observation]:
        out: List[Observation] = []
        exposure = _num(row.get("exposure_frequency"))
        interaction = _num(row.get("interaction_frequency"))
        if not np.isnan(exposure) and not np.isnan(interaction) and interaction > 0:
            ratio = exposure / interaction
            if ratio >= self.config.exposure_interaction_gap:
                out.append(
                    Observation(
                        observation_id="struct_exposure_gt_interaction",
                        statement=(
                            "Campaign exposure is high while interaction frequency "
                            "is relatively low."
                        ),
                        supporting_metrics={
                            "exposure_frequency": round(exposure, 4),
                            "interaction_frequency": round(interaction, 4),
                        },
                        evidence={
                            "exposure_to_interaction_ratio": round(ratio, 4),
                            "threshold": self.config.exposure_interaction_gap,
                        },
                        # Structural: not population-limited; scaled by effect only.
                        confidence=round(
                            self.config.base_confidence
                            * min(1.0, (ratio - 1.0) / max(self.config.exposure_interaction_gap - 1.0, 1e-9)),
                            4,
                        ),
                    )
                )
        return out

    # -- diversity (absolute threshold; population-free) -------------------

    def _diversity(self, row: pd.Series) -> List[Observation]:
        diversity = _num(row.get("campaign_diversity"))
        unique = _num(row.get("unique_campaign_count"))
        if (
            np.isnan(diversity)
            or unique < self.config.min_unique_campaigns
            or diversity < self.config.diversity_floor
        ):
            return []
        return [
            Observation(
                observation_id="div_diverse_campaigns",
                statement="User engages with a diverse set of campaigns.",
                supporting_metrics={
                    "campaign_diversity": round(diversity, 4),
                    "unique_campaign_count": int(unique),
                },
                evidence={
                    "campaign_diversity": round(diversity, 4),
                    "diversity_floor": self.config.diversity_floor,
                    "unique_campaign_count": int(unique),
                },
                confidence=round(
                    self.config.base_confidence
                    * min(1.0, diversity / max(self.config.diversity_floor, 1e-9) - 0.0),
                    4,
                ),
            )
        ]


def _num(value: Any) -> float:
    """Coerce to float, mapping None/NA to NaN."""
    if value is None or value is pd.NA:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
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
    result = MetricCalculator(load_semantic_schema()).compute(raw)
    observations = AnalysisEngine().analyze(result.customer_metrics, result.dataset_summary)

    print("\nDataset summary:")
    print(json.dumps(
        {k: v for k, v in result.dataset_summary.items() if k != "metric_averages"},
        indent=2, default=str,
    ))
    print("\nObservations:")
    for customer, obs in observations.items():
        print(f"\ncustomer {customer}: {len(obs)} observation(s)")
        for o in obs:
            print(f"  - [{o.confidence:.2f}] {o.statement}")
            print(f"      evidence: {o.evidence}")
    if not any(observations.values()):
        print(
            "  (no observations above the confidence floor — expected on a "
            "single-customer / single-campaign sample; not a failure)"
        )

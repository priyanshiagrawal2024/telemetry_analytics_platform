"""Business-facing insight generation (single-customer, fact-only).

Layer position::

    MetricCalculator / FeatureExtractor -> InsightGenerator   <-- THIS MODULE

Turns a customer's already-computed metrics into a small set of **consolidated,
business-readable** insights plus a manager-friendly ``dashboard_summary``.
Every statement is a plain restatement of measured telemetry, so it works for a
**single customer** (no population comparison required).

Output per customer::

    {
      "insights": [ {"title": ..., "insight": ..., "evidence": {...}}, ... ],  # 4-6 items
      "dashboard_summary": [ "concise manager line", ... ]                     # ~3 lines
    }

Design (per the refactor brief):
* Related metrics are MERGED into one insight (e.g. CTR + skip rate + click/skip
  split -> a single "Engagement with floaters" insight) — no metric-by-metric
  restatement.
* 4-6 high-value insights, not 10 metric-level ones.
* ``evidence`` is retained on every insight for auditability.
* ``dashboard_summary`` gives 2-4 punchy lines for a manager view.

Hard guardrails (do NOT): predict intent, infer accidental clicks, infer
fatigue, infer attention/emotions, or reconstruct user-journey behaviour.

This module computes nothing new — it reuses values from
:class:`analytics.feature_extractor.MetricCalculator` (generic metrics) and, for
``avg_impressions_before_click``, :class:`FeatureExtractor` (behavioural profile).
Every insight is gated on data availability; a missing metric simply drops its
contribution rather than being fabricated.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

__all__ = ["InsightGenerator"]

logger = logging.getLogger(__name__)


def _num(value: Any) -> Optional[float]:
    """Return a float, or None for missing/NaN/<NA> (so insights can be gated)."""
    if value is None or value is pd.NA:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _int(value: Any) -> Optional[int]:
    f = _num(value)
    return None if f is None else int(round(f))


def _r(value: Optional[float], ndigits: int = 2) -> Optional[float]:
    """Round for compact, business-readable evidence (None-safe)."""
    return None if value is None else round(float(value), ndigits)


class InsightGenerator:
    """Generate consolidated, fact-based, single-customer business insights."""

    def generate(
        self,
        metrics: pd.DataFrame,
        profile: Optional[pd.DataFrame] = None,
    ) -> Dict[str, Dict[str, List[Any]]]:
        """Return ``{customer_id: {"insights": [...], "dashboard_summary": [...]}}``.

        Parameters
        ----------
        metrics:
            ``MetricCalculator.compute(...).customer_metrics`` (generic metrics).
        profile:
            Optional ``FeatureExtractor.extract(...)`` output; supplies
            ``avg_impressions_before_click`` (not in the generic metric set).
        """
        if metrics is None or metrics.empty:
            return {}

        gm = metrics.set_index(metrics["customerId"].astype(str))
        bp = (
            profile.set_index(profile["customerId"].astype(str))
            if profile is not None and not profile.empty
            else None
        )

        out: Dict[str, Dict[str, List[Any]]] = {}
        for cid in gm.index:
            row = gm.loc[cid].to_dict()
            if bp is not None and cid in bp.index:
                row["avg_impressions_before_click"] = bp.loc[cid].get(
                    "avg_impressions_before_click"
                )
            insights, summary = self._for_customer(row)
            out[cid] = {"insights": insights, "dashboard_summary": summary}
            logger.info(
                "Generated %d consolidated insight(s) for customer %s.",
                len(insights),
                cid,
            )
        return out

    # -- per-customer consolidated insight set ----------------------------

    def _for_customer(
        self, m: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        insights: List[Dict[str, Any]] = []
        summary: List[str] = []

        impressions = _int(m.get("impression_count"))
        clicks = _int(m.get("click_count"))
        skips = _int(m.get("skip_count"))
        ctr = _num(m.get("ctr"))
        sessions = _int(m.get("session_count"))
        events = _int(m.get("event_count"))
        aeps = _num(m.get("average_events_per_session"))
        reach = _int(m.get("campaigns_reached"))
        diversity = _num(m.get("campaign_diversity"))
        unique_campaigns = _int(m.get("unique_campaign_count"))
        interaction_freq = _num(m.get("interaction_frequency"))
        exposure_freq = _num(m.get("exposure_frequency"))
        aibc = _num(m.get("avg_impressions_before_click"))
        # New metrics
        peak_click_hour = m.get("peak_click_hour")
        weekend_jump = _num(m.get("weekend_activity_jump"))
        weekend_jump = _num(m.get("weekend_activity_jump"))

        # 1. ENGAGEMENT — merges CTR + skip rate + click/skip split -----------
        if impressions and clicks is not None and ctr is not None:
            text = f"Of {impressions} floaters shown, the customer clicked {clicks} ({ctr:.0f}% CTR)"
            evidence: Dict[str, Any] = {
                "ctr_pct": _r(ctr, 1),
                "clicks": clicks,
                "impressions": impressions,
            }
            split_word = None
            if skips is not None:
                skip_rate = skips / impressions * 100 if impressions else None
                reactions = clicks + skips
                split_word = (
                    "even" if clicks == skips
                    else "click-leaning" if clicks > skips
                    else "skip-leaning"
                )
                article = "an" if split_word[0] in "aeiou" else "a"
                text += (
                    f" and skipped {skips} ({skip_rate:.0f}% skip rate) — "
                    f"{article} {split_word} click/skip split"
                )
                evidence.update(
                    {
                        "skip_rate_pct": _r(skip_rate, 1),
                        "skips": skips,
                        "click_share_of_reactions": _r(clicks / reactions, 3) if reactions else None,
                    }
                )
            insights.append(self._mk("Engagement with floaters", text + ".", evidence))
            split_phrase = ""
            if split_word:
                article = "an" if split_word[0] in "aeiou" else "a"
                split_phrase = f" with {article} {split_word} click/skip split"
            summary.append(f"{ctr:.0f}% CTR{split_phrase} across {impressions} floaters.")

        # 2. CAMPAIGN REACH & DIVERSITY — merges reach + diversity ------------
        if reach:
            shape = "varied across campaigns" if (diversity or 0) >= 0.5 else "concentrated on a few campaigns"
            text = f"Reached by {reach} distinct campaign(s)"
            evidence = {"campaigns_reached": reach}
            if diversity is not None and unique_campaigns:
                text += f"; exposure is {shape} (diversity {diversity:.2f})"
                evidence.update(
                    {"campaign_diversity": _r(diversity, 3), "unique_campaign_count": unique_campaigns}
                )
            insights.append(self._mk("Campaign reach & diversity", text + ".", evidence))
            summary.append(
                f"Reached {reach} campaign(s)"
                + (f"; exposure {shape}." if diversity is not None else ".")
            )

        # 3. EXPOSURE vs INTERACTION — merges exposure + interaction freq ------
        if exposure_freq is not None and interaction_freq is not None and sessions:
            if exposure_freq > interaction_freq:
                gap = "more floaters were shown than were acted on"
            else:
                gap = "almost every shown floater drew a reaction"
            insights.append(
                self._mk(
                    "Exposure vs interaction",
                    f"Across {sessions} session(s), the customer saw ~{exposure_freq:.1f} "
                    f"floater(s) per session and acted on ~{interaction_freq:.1f}; {gap}.",
                    {
                        "exposure_frequency": _r(exposure_freq, 2),
                        "interaction_frequency": _r(interaction_freq, 2),
                        "session_count": sessions,
                        "impressions": impressions,
                    },
                )
            )
            summary.append(
                f"~{exposure_freq:.1f} floaters/session over {sessions} session(s); {gap}."
            )

        # 4. SESSION ACTIVITY -------------------------------------------------
        if sessions and events is not None:
            aeps_txt = f", averaging {aeps:.0f} per session" if aeps is not None else ""
            insights.append(
                self._mk(
                    "Session activity",
                    f"Logged {events} events across {sessions} session(s){aeps_txt}.",
                    {
                        "event_count": events,
                        "session_count": sessions,
                        "average_events_per_session": _r(aeps, 2),
                    },
                )
            )

        # 5. CLICKS AFTER REPEAT EXPOSURE (factual; no intent/fatigue) ---------
        if aibc is not None:
            insights.append(
                self._mk(
                    "Clicks after repeat exposure",
                    f"On average a campaign was shown ~{aibc:.1f} time(s) before its first click.",
                    {"avg_impressions_before_click": _r(aibc, 2)},
                )
            )
            summary.append(f"First click came after ~{aibc:.1f} exposure(s) of a campaign.")


        # 6. PEAK CLICK TIME
        if peak_click_hour is not None:
            insights.append(
                self._mk(
                    "Peak Click Activity",
                    f"Most clicks occurred around {peak_click_hour}:00.",
                    {
                        "peak_click_hour": peak_click_hour
                    }
                )
            )   
            

        if weekend_jump is not None and not pd.isna(weekend_jump):

            insights.append(
                self._mk(
                    "Weekend activity",
                    f"Weekend CTR differed from weekday CTR by {weekend_jump:.1f} percentage points.",
                    {
                    "weekend_activity_jump": round(
                    weekend_jump,
                    2
                    )
                    }
                )
            )


        return insights, summary[:4]

    @staticmethod
    def _mk(title: str, insight: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        return {"title": title, "insight": insight, "evidence": evidence}


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analytics.feature_extractor import (  # noqa: E402
        EventClassifier,
        EventClassifierConfig,
        FeatureExtractor,
        MetricCalculator,
        TelemetryLoader,
        load_semantic_schema,
    )

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sample = sys.argv[1] if len(sys.argv) > 1 else "sample_data/telemetry_sample.csv"

    schema = load_semantic_schema()
    raw = TelemetryLoader().load(sample)
    metrics = MetricCalculator(schema).compute(raw).customer_metrics
    profile = FeatureExtractor().extract(
        EventClassifier(EventClassifierConfig.from_schema(schema)).classify(raw)
    )
    result = InsightGenerator().generate(metrics, profile)

    for customer, payload in result.items():
        print(f"\n=== customer {customer}: {len(payload['insights'])} insight(s) ===")
        print(json.dumps(payload, indent=2, ensure_ascii=False))

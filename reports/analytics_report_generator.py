"""Human-readable analytics report generator (presentation layer).

Position in the pipeline::

    Telemetry -> FeatureExtractor -> ScoreCalculator -> AnalysisEngine
              -> analytics_service (facade)
              -> AnalyticsReportGenerator   <-- THIS MODULE

This module is a **pure presentation layer**. It reads the already-computed
results exclusively through :mod:`analytics.analytics_service` and formats them
into a Markdown report for manager review. It performs **no metric/score/insight
computation** and duplicates **no** analytics logic — every number and statement
originates from the pipeline via the service (which runs the pipeline once and
caches the result).

Guardrails: the report only restates metrics and insights the pipeline already
produced. It introduces no inference of accidental clicks, attention, dwell
time, emotion, intent, or user journeys. Missing values render as ``N/A`` (never
a fabricated ``0``).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Make the package importable when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics import analytics_service as svc  # noqa: E402

__all__ = ["AnalyticsReportGenerator", "generate_report"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting helpers (display only)
# ---------------------------------------------------------------------------


def _fmt(value: Any, *, pct: bool = False, ndigits: int = 2, suffix: str = "") -> str:
    """Render a value for the report; missing -> 'N/A' (never a fake 0)."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int,)) and not pct:
        return f"{value}{suffix}"
    if isinstance(value, float):
        text = f"{value:.{ndigits}f}"
        return f"{text}%" if pct else f"{text}{suffix}"
    return f"{value}{suffix}"


def _get(d: Optional[Dict[str, Any]], *keys: str) -> Any:
    """Safe nested lookup."""
    cur: Any = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

#: (label, descriptive-metric key, formatting kwargs) for the per-customer table.
_CUSTOMER_METRICS = [
    ("Impressions", "impression_count", {}),
    ("Clicks", "click_count", {}),
    ("Skips", "skip_count", {}),
    ("CTR", "ctr", {"pct": True, "ndigits": 1}),
    ("Sessions", "session_count", {}),
    ("Campaigns reached", "unique_campaign_count", {}),
    ("Exposure frequency (per session)", "exposure_frequency", {"ndigits": 2}),
    ("Interaction frequency (per session)", "interaction_frequency", {"ndigits": 2}),
    ("Campaign diversity (0-1)", "campaign_diversity", {"ndigits": 2}),
]

_SCORES = [
    ("Engagement score (0-1)", "engagement_score"),
    ("Exploration score (0-1)", "exploration_score"),
    ("Campaign receptiveness score (0-1)", "campaign_receptiveness_score"),
]

_TOP_METRICS = [
    ("Click-through rate (CTR)", "ctr", {"pct": True, "ndigits": 1}),
    ("Campaign diversity (0-1)", "campaign_diversity", {"ndigits": 2}),
    ("Exposure frequency (impressions/session)", "exposure_frequency", {"ndigits": 2}),
    ("Interaction frequency (per session)", "interaction_frequency", {"ndigits": 2}),
]


class AnalyticsReportGenerator:
    """Builds a Markdown analytics report from the analytics service."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        dataset: Optional[str] = None,
    ) -> None:
        self.path = path
        self.dataset = dataset

    # -- public API --------------------------------------------------------

    def generate(self) -> str:
        """Return the full report as a Markdown string."""
        summary = svc.get_dataset_summary(path=self.path, dataset=self.dataset)
        campaigns = svc.get_campaign_summary(path=self.path, dataset=self.dataset)
        customer_ids = svc.available_customer_ids(path=self.path, dataset=self.dataset)
        records = [
            svc.get_customer_analytics(cid, path=self.path, dataset=self.dataset)
            for cid in customer_ids
        ]
        records = [r for r in records if r]

        lines: List[str] = []
        lines += self._header(summary)
        lines += self._section_dataset_summary(summary)
        lines += self._section_customer_summary(records)
        lines += self._section_campaign_summary(campaigns, summary)
        lines += self._section_top_metrics(summary)
        lines += self._section_top_insights(records)
        lines += self._section_observations(summary, records)
        return "\n".join(lines).rstrip() + "\n"

    def write(self, out_path: Union[str, Path]) -> Path:
        """Generate the report and write it to ``out_path``; return the path."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(self.generate(), encoding="utf-8")
        logger.info("Wrote analytics report to %s.", out_path)
        return out_path

    # -- sections ----------------------------------------------------------

    @staticmethod
    def _header(summary: Dict[str, Any]) -> List[str]:
        return [
            "# MyJio Floater Analytics — Report",
            "",
            f"**Dataset:** {summary.get('dataset', 'N/A')}  ",
            f"**Source:** {summary.get('source', 'N/A')}  ",
            f"**Generated at:** {summary.get('generated_at', 'N/A')}",
            "",
            "> Descriptive analytics report. All figures are produced by the analytics "
            "pipeline and restated here as-is; unavailable metrics are shown as `N/A`. "
            "No intent, attention, dwell-time, emotion, accidental-click, or user-journey "
            "inference is made.",
            "",
            "---",
            "",
        ]

    @staticmethod
    def _section_dataset_summary(summary: Dict[str, Any]) -> List[str]:
        return [
            "## 1. Dataset Summary",
            "",
            "| Measure | Value |",
            "|---------|-------|",
            f"| Customers | {_fmt(summary.get('n_customers'))} |",
            f"| Events | {_fmt(summary.get('n_events'))} |",
            f"| Campaigns | {_fmt(summary.get('n_campaigns'))} |",
            f"| Sessions | {_fmt(summary.get('n_sessions'))} |",
            "",
        ]

    @staticmethod
    def _section_customer_summary(records: List[Dict[str, Any]]) -> List[str]:
        out = ["## 2. Customer Analytics Summary", ""]
        if not records:
            return out + ["_No customers available._", ""]

        for rec in records:
            desc = _get(rec, "metrics", "descriptive") or {}
            scores = rec.get("scores") or {}
            out.append(f"### Customer `{rec.get('customer_id')}`")
            out.append("")
            out.append("**Key metrics**")
            out.append("")
            out.append("| Metric | Value |")
            out.append("|--------|-------|")
            for label, key, kw in _CUSTOMER_METRICS:
                out.append(f"| {label} | {_fmt(desc.get(key), **kw)} |")
            out.append("")
            out.append("**Key scores**")
            out.append("")
            out.append("| Score | Value |")
            out.append("|-------|-------|")
            for label, key in _SCORES:
                out.append(f"| {label} | {_fmt(scores.get(key), ndigits=3)} |")
            out.append("")
        return out

    @staticmethod
    def _section_campaign_summary(
        campaigns: Dict[str, Any], summary: Dict[str, Any]
    ) -> List[str]:
        out = ["## 3. Campaign Analytics Summary", ""]
        rows = campaigns.get("campaigns") or []
        out.append(f"**Campaigns:** {_fmt(campaigns.get('n_campaigns'))}")
        out.append("")
        out.append("**Campaign reach** (distinct customers reached per campaign)")
        out.append("")
        if rows:
            out.append("| Campaign | Customers reached |")
            out.append("|----------|-------------------|")
            for row in rows:
                out.append(
                    f"| {row.get('campaign')} | {_fmt(row.get('customers_reached'))} |"
                )
        else:
            out.append("_No campaign reach data._")
        out.append("")
        # Campaign interaction: the pipeline computes interaction at customer
        # grain, not per campaign — report the dataset-level average for context
        # rather than fabricating a per-campaign figure.
        avg_interaction = _get(summary, "metric_averages", "interaction_frequency")
        out.append(
            "**Campaign interaction:** per-campaign interaction is not produced at "
            "campaign grain in the current pipeline. Customer-grain interaction "
            f"frequency averages **{_fmt(avg_interaction, ndigits=2)}** interaction(s) "
            "per session across the dataset (see §4)."
        )
        out.append("")
        return out

    @staticmethod
    def _section_top_metrics(summary: Dict[str, Any]) -> List[str]:
        averages = summary.get("metric_averages") or {}
        out = [
            "## 4. Top Metrics",
            "",
            "_Dataset averages (equal to the single customer's values when the "
            "population is one)._",
            "",
            "| Metric | Value |",
            "|--------|-------|",
        ]
        for label, key, kw in _TOP_METRICS:
            out.append(f"| {label} | {_fmt(averages.get(key), **kw)} |")
        out.append("")
        return out

    @staticmethod
    def _section_top_insights(records: List[Dict[str, Any]]) -> List[str]:
        out = ["## 5. Top Insights", ""]
        any_insight = False
        for rec in records:
            insights = rec.get("insights") or []
            if not insights:
                continue
            any_insight = True
            out.append(f"**Customer `{rec.get('customer_id')}`**")
            out.append("")
            for ins in insights:
                title = ins.get("title", "Insight")
                text = ins.get("insight", "")
                out.append(f"- **{title}** — {text}")
            out.append("")
        if not any_insight:
            out.append(
                "_No insights met the reporting threshold for the available data._"
            )
            out.append("")
        return out

    @staticmethod
    def _section_observations(
        summary: Dict[str, Any], records: List[Dict[str, Any]]
    ) -> List[str]:
        out = ["## 6. Analytics Observations", ""]

        # Manager-friendly dashboard lines already generated per customer.
        for rec in records:
            ds = rec.get("dashboard_summary") or []
            if ds:
                out.append(f"**Customer `{rec.get('customer_id')}` — at a glance**")
                out.append("")
                for line in ds:
                    out.append(f"- {line}")
                out.append("")

        # Capability / availability notes (drawn from the pipeline, not inferred).
        unavailable = summary.get("unavailable_metrics") or []
        if unavailable:
            out.append(
                "**Unavailable metrics (reported as `null`, not fabricated):** "
                + ", ".join(f"`{m}`" for m in unavailable)
            )
            out.append("")

        # Data-sufficiency caveat for tiny populations.
        n_customers = summary.get("n_customers") or 0
        n_campaigns = summary.get("n_campaigns") or 0
        if n_customers <= 1 or n_campaigns <= 1:
            out.append(
                "**Data sufficiency:** this run validates platform capability rather "
                "than business performance. Population benchmarking needs multiple "
                "customers, campaign comparison needs multiple campaigns, segmentation "
                "needs a larger population, and conversion analytics need conversion "
                "events. Figures here are illustrative of the pipeline, not a "
                "representative business read."
            )
            out.append("")
        return out


def generate_report(
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
) -> str:
    """Convenience one-call entrypoint returning the Markdown report string."""
    return AnalyticsReportGenerator(path=path, dataset=dataset).generate()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Generate a human-readable analytics report.")
    parser.add_argument("--path", default=None, help="Telemetry source (defaults to sample).")
    parser.add_argument("--dataset", default=None, help="Semantic dataset key.")
    parser.add_argument(
        "--out",
        nargs="?",
        const="reports/analytics_report.md",
        default=None,
        help="Write the report to a file (default path: reports/analytics_report.md).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress pipeline logs.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    generator = AnalyticsReportGenerator(path=args.path, dataset=args.dataset)
    if args.out:
        written = generator.write(args.out)
        print(f"Report written to {written}")
    else:
        print(generator.generate())

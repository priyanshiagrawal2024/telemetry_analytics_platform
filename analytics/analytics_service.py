"""Read-facade service over the analytics pipeline.

This module exposes simple, stable accessor functions for consumers (API layer,
dashboard, agent) without re-implementing any analytics. It is a **thin facade**
over :class:`analytics.analytics_runner.AnalyticsRunner`:

* the full pipeline (load -> classify -> extract -> metrics -> scores ->
  insights) runs **once** per ``(path, dataset)`` and the result is cached;
* each accessor returns a **slice** of that already-assembled result.

No metric / score / insight logic lives here — that all stays in the existing
modules the runner orchestrates (no duplicated logic).

Functions
---------
* :func:`get_customer_analytics` — one customer's metrics, scores, insights,
  dashboard_summary.
* :func:`get_dataset_summary` — population-level counts, capabilities, averages,
  event distribution, campaign reach.
* :func:`get_campaign_summary` — per-campaign reach (the only campaign-grained
  output the pipeline currently produces), reshaped as a ranked list.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Make the package importable when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics.analytics_runner import AnalyticsRunner  # noqa: E402

__all__ = [
    "get_customer_analytics",
    "get_dataset_summary",
    "get_campaign_summary",
    "available_customer_ids",
    "clear_cache",
    "DEFAULT_DATASET_PATH",
]

logger = logging.getLogger(__name__)

#: Default telemetry source (the validated sample). Override per call.
DEFAULT_DATASET_PATH = _PROJECT_ROOT / "sample_data" / "telemetry_sample.csv"

#: Process-level cache: (resolved path, dataset key) -> assembled run result.
_CACHE: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Internal: run-once-and-cache
# ---------------------------------------------------------------------------


def _result(
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
    *,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Return the assembled pipeline result for ``(path, dataset)``, cached.

    The pipeline is executed (once) by :class:`AnalyticsRunner`; subsequent
    calls with the same key are served from the cache. Use ``refresh=True`` to
    force re-execution (e.g. after the source file changes).
    """
    resolved = Path(path) if path else DEFAULT_DATASET_PATH
    key = (str(resolved), dataset)
    if refresh or key not in _CACHE:
        logger.info("Running analytics pipeline for %s (dataset=%s).", resolved, dataset)
        _CACHE[key] = AnalyticsRunner(dataset=dataset).run(resolved)
    return _CACHE[key]


def _customers(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return result.get("customers", [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_customer_analytics(
    customer_id: Union[str, int],
    *,
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
    refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Return one customer's full analytics record, or ``None`` if not found.

    The record is the runner's per-customer object verbatim::

        {
          "customer_id": ...,
          "metrics":  {"descriptive": {...}, "behavioural": {...}},
          "scores":   {...},
          "insights": [ {title, insight, evidence}, ... ],
          "dashboard_summary": [ "...", ... ]
        }
    """
    result = _result(path, dataset, refresh=refresh)
    target = str(customer_id)
    for record in _customers(result):
        if str(record.get("customer_id")) == target:
            return record
    logger.warning(
        "Customer %s not found (available: %s).",
        target,
        available_customer_ids(path=path, dataset=dataset),
    )
    return None


def get_dataset_summary(
    *,
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Return the population-level summary plus run context.

    Reuses the runner's ``dataset_summary`` (counts, ``capabilities``,
    ``unavailable_metrics``, ``event_distribution``, ``campaign_reach``,
    ``metric_averages``) — nothing is recomputed here.
    """
    result = _result(path, dataset, refresh=refresh)
    return {
        "dataset": result.get("dataset"),
        "source": result.get("source"),
        "generated_at": result.get("generated_at"),
        "n_customers": len(_customers(result)),
        **result.get("dataset_summary", {}),
    }


def get_campaign_summary(
    *,
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Return a per-campaign summary, ranked by distinct customers reached.

    Built from the runner's ``dataset_summary.campaign_reach`` — the only
    campaign-grained output the pipeline currently produces. No per-campaign
    metric is invented; when a campaign-grain aggregator is added upstream, this
    accessor surfaces it without an API change.
    """
    result = _result(path, dataset, refresh=refresh)
    summary = result.get("dataset_summary", {})
    reach: Dict[str, int] = summary.get("campaign_reach", {}) or {}
    campaigns = [
        {"campaign": campaign, "customers_reached": int(count)}
        for campaign, count in sorted(
            reach.items(), key=lambda kv: (-kv[1], str(kv[0]))
        )
    ]
    return {
        "dataset": result.get("dataset"),
        "n_campaigns": summary.get("n_campaigns", len(campaigns)),
        "campaigns": campaigns,
    }


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def available_customer_ids(
    *,
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
    refresh: bool = False,
) -> List[str]:
    """List the customer ids available for ``(path, dataset)``."""
    return [
        str(r.get("customer_id")) for r in _customers(_result(path, dataset, refresh=refresh))
    ]


def clear_cache() -> None:
    """Drop all cached pipeline results (forces re-run on next access)."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    print("=== dataset summary ===")
    ds = get_dataset_summary()
    print(json.dumps({k: ds[k] for k in ("dataset", "n_customers", "n_events", "n_campaigns")}, indent=2))

    print("\n=== campaign summary ===")
    print(json.dumps(get_campaign_summary(), indent=2, ensure_ascii=False))

    ids = available_customer_ids()
    print(f"\n=== customer analytics ({ids[0] if ids else 'n/a'}) ===")
    if ids:
        rec = get_customer_analytics(ids[0])
        print("scores:", json.dumps(rec["scores"], indent=2))
        print("dashboard_summary:")
        for line in rec["dashboard_summary"]:
            print("  -", line)

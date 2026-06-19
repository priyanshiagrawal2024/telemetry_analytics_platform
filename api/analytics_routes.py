"""Analytics serving endpoints for the Telemetry Analytics Platform.

This module is the **HTTP serving surface** for analytics. It is a thin adapter:
it maps URLs to the analytics read-facade and translates errors into HTTP
responses. It contains **no analytics logic** — every metric, score, and insight
is produced by :mod:`analytics.analytics_service` (which itself is a facade over
the analytics runner). Routes only invoke service methods and return their
result verbatim.

Endpoints
---------
* ``GET /analytics/customer/{customerId}`` -> ``analytics_service.get_customer_analytics``
* ``GET /analytics/summary``               -> ``analytics_service.get_dataset_summary``
* ``GET /analytics/campaigns``             -> ``analytics_service.get_campaign_summary``

Responses are the service's own (capability-gated) shapes, passed through
unchanged so the API never fabricates or reshapes analytics output.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException, status

from analytics import analytics_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _service_call(operation: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Invoke a service function, mapping failures to HTTP errors.

    Pure error-handling plumbing (no analytics logic): a missing dataset becomes
    ``503``; any other pipeline failure becomes ``500``. Successful results are
    returned untouched.
    """
    try:
        return fn(*args)
    except FileNotFoundError as exc:
        logger.exception("Analytics dataset unavailable during %s", operation)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytics dataset is unavailable.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface any pipeline failure as 500
        logger.exception("Analytics pipeline failed during %s", operation)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute {operation}.",
        ) from exc


@router.get(
    "/customer/{customerId}",
    response_model=Dict[str, Any],
    summary="Per-customer analytics (metrics, scores, insights)",
    responses={
        404: {"description": "No analytics found for the given customer"},
        503: {"description": "Analytics dataset unavailable"},
        500: {"description": "Analytics computation failed"},
    },
)
def get_customer_analytics(customerId: str) -> Dict[str, Any]:
    """Return one customer's analytics record.

    Delegates to :func:`analytics.analytics_service.get_customer_analytics`. The
    response is the service's per-customer object (``customer_id``, ``metrics``,
    ``scores``, ``insights``, ``dashboard_summary``). Returns ``404`` when the
    customer is not present in the analyzed dataset.
    """
    record = _service_call(
        "customer analytics", analytics_service.get_customer_analytics, customerId
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analytics found for customer '{customerId}'.",
        )
    return record


@router.get(
    "/summary",
    response_model=Dict[str, Any],
    summary="Dataset-level analytics summary",
    responses={
        503: {"description": "Analytics dataset unavailable"},
        500: {"description": "Analytics computation failed"},
    },
)
def get_dataset_summary() -> Dict[str, Any]:
    """Return the population-level dataset summary.

    Delegates to :func:`analytics.analytics_service.get_dataset_summary`
    (counts, capabilities, event distribution, campaign reach, metric averages).
    """
    return _service_call("dataset summary", analytics_service.get_dataset_summary)


@router.get(
    "/campaigns",
    response_model=Dict[str, Any],
    summary="Per-campaign analytics summary",
    responses={
        503: {"description": "Analytics dataset unavailable"},
        500: {"description": "Analytics computation failed"},
    },
)
def get_campaign_summary() -> Dict[str, Any]:
    """Return the per-campaign summary, ranked by customers reached.

    Delegates to :func:`analytics.analytics_service.get_campaign_summary`.
    """
    return _service_call("campaign summary", analytics_service.get_campaign_summary)


__all__ = ["router"]

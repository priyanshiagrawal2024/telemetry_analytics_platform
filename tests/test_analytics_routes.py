"""Regression tests for the analytics serving endpoints.

Locks the HTTP contract of:
    GET /analytics/summary
    GET /analytics/campaigns
    GET /analytics/customer/{customerId}

These are contract-level checks (status codes, structure, key presence) — not
assertions on specific metric values, so they protect against accidental route /
service breakage without being brittle to legitimate analytics updates.

Run:
    pytest tests/test_analytics_routes.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable regardless of where pytest is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient

from analytics import analytics_service
from api.app import app

#: An id that must never appear in the analyzed dataset (for the 404 case).
UNKNOWN_CUSTOMER_ID = "definitely-not-a-real-customer-000"


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Module-scoped TestClient.

    Created without the lifespan context manager: the analytics routes do not
    use the database, so we skip startup to keep tests fast and DB-independent.
    """
    return TestClient(app)


@pytest.fixture(scope="module")
def valid_customer_id() -> str:
    """A real customer id taken from the analyzed dataset (deterministic)."""
    ids = analytics_service.available_customer_ids()
    if not ids:
        pytest.skip("No customers available in the analytics dataset.")
    return str(ids[0])


# ---------------------------------------------------------------------------
# Test 1 — GET /analytics/summary
# ---------------------------------------------------------------------------


def test_summary_returns_dataset_statistics(client: TestClient) -> None:
    response = client.get("/analytics/summary")

    assert response.status_code == 200

    body = response.json()
    assert isinstance(body, dict)

    # Dataset statistics are present and well-typed.
    for key in ("n_customers", "n_events", "n_campaigns"):
        assert key in body, f"missing dataset statistic: {key}"
        assert isinstance(body[key], int)

    # Structure is valid: the population breakdown is a mapping.
    assert "event_distribution" in body
    assert isinstance(body["event_distribution"], dict)


# ---------------------------------------------------------------------------
# Test 2 — GET /analytics/campaigns
# ---------------------------------------------------------------------------


def test_campaigns_returns_campaign_analytics(client: TestClient) -> None:
    response = client.get("/analytics/campaigns")

    assert response.status_code == 200

    body = response.json()
    assert isinstance(body, dict)
    assert "n_campaigns" in body and isinstance(body["n_campaigns"], int)

    # Campaign analytics: a list of well-formed per-campaign entries.
    assert "campaigns" in body
    campaigns = body["campaigns"]
    assert isinstance(campaigns, list)
    for entry in campaigns:
        assert isinstance(entry, dict)
        assert "campaign" in entry
        assert "customers_reached" in entry
        assert isinstance(entry["customers_reached"], int)


# ---------------------------------------------------------------------------
# Test 3 — GET /analytics/customer/{valid_customer}
# ---------------------------------------------------------------------------


def test_customer_returns_metrics_scores_insights(
    client: TestClient, valid_customer_id: str
) -> None:
    response = client.get(f"/analytics/customer/{valid_customer_id}")

    assert response.status_code == 200

    body = response.json()
    assert isinstance(body, dict)

    # Required sections of a customer analytics record.
    for key in ("metrics", "scores", "insights"):
        assert key in body, f"missing customer analytics section: {key}"

    assert isinstance(body["metrics"], dict)
    assert isinstance(body["scores"], dict)
    assert isinstance(body["insights"], list)


# ---------------------------------------------------------------------------
# Test 4 — GET /analytics/customer/{invalid_customer}
# ---------------------------------------------------------------------------


def test_unknown_customer_returns_404(client: TestClient) -> None:
    # Guard: the id genuinely does not exist in the dataset.
    assert UNKNOWN_CUSTOMER_ID not in analytics_service.available_customer_ids()

    response = client.get(f"/analytics/customer/{UNKNOWN_CUSTOMER_ID}")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Test 5 — Swagger / OpenAPI registration intact
# ---------------------------------------------------------------------------


def test_openapi_registration_intact(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200

    paths = response.json()["paths"]
    for route in (
        "/analytics/summary",
        "/analytics/campaigns",
        "/analytics/customer/{customerId}",
    ):
        assert route in paths, f"route missing from OpenAPI schema: {route}"
        assert "get" in paths[route], f"GET not registered for {route}"

    # Swagger UI page is served.
    assert client.get("/docs").status_code == 200

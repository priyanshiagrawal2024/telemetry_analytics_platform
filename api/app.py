"""FastAPI application for the Telemetry Analytics Platform ingestion service.

This is the HTTP entry point of the **Ingestion Layer** (``docs/project_context``
§7). It is deliberately thin: it owns the application lifecycle (database pool
connect/disconnect), exposes a health endpoint, and registers the telemetry
ingestion router. All event handling lives in ``ingestion`` / ``preprocessing``.

Run locally::

    uvicorn api.app:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI
from pydantic import BaseModel

from api.analytics_routes import router as analytics_router
from api.config_routes import router as config_router
from database.connection import database
from ingestion.telemetry_receiver import router as telemetry_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

SERVICE_NAME = "myjio-floater-telemetry-ingestion"
SERVICE_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    service: str = SERVICE_NAME
    version: str = SERVICE_VERSION
    database: str  # "connected" | "unavailable"
    timestamp: datetime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect the DB pool on startup, release it on shutdown.

    Database connectivity is best-effort: if the pool cannot be created the
    service still starts and reports ``degraded`` health, which is the correct
    behaviour for a resilient ingestion edge.
    """
    logger.info("Starting %s v%s", SERVICE_NAME, SERVICE_VERSION)
    try:
        await database.connect()
    except Exception:
        logger.exception("Database unavailable at startup; continuing degraded")
    try:
        yield
    finally:
        await database.disconnect()
        logger.info("Stopped %s", SERVICE_NAME)


def create_app() -> FastAPI:
    """Application factory — builds and wires the FastAPI instance."""
    app = FastAPI(
        title="MyJio Floater Telemetry Ingestion",
        version=SERVICE_VERSION,
        description=(
            "Infrastructure layer: receives, validates, and normalizes floater "
            "telemetry events for the MyJio Floater Analytics Platform."
        ),
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        db_ok = await database.healthcheck()
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            database="connected" if db_ok else "unavailable",
            timestamp=datetime.now(timezone.utc),
        )

    # Telemetry endpoint registration (Ingestion Layer).
    app.include_router(telemetry_router)
    # Configuration endpoints (semantic column registry).
    app.include_router(config_router)
    # Analytics serving endpoints (mock-backed until the runner is integrated).
    app.include_router(analytics_router)

    return app


app = create_app()


__all__ = ["app", "create_app", "HealthResponse"]

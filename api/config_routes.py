"""Configuration endpoints for the Telemetry Analytics Platform.

Exposes the **semantic column registry** so any consumer (the future
feature-extraction / analytics layers, a dashboard, or an operator) can
discover how raw source columns map to domain-neutral semantic roles. This is
read-only configuration metadata; it does not touch ingestion behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from preprocessing.event_normalizer import COLUMN_REGISTRY_PATH, load_column_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])


@router.get(
    "/column-registry",
    summary="Get the semantic column registry",
    response_model=dict,
)
def get_column_registry() -> dict[str, Any]:
    """Return the parsed contents of ``contracts/column_registry.yaml``.

    Each entry maps a raw source column to its semantic role, description,
    data type, and required flag.
    """
    try:
        return load_column_registry()
    except FileNotFoundError:
        logger.error("Column registry not found at %s", COLUMN_REGISTRY_PATH)
        raise HTTPException(status_code=404, detail="Column registry not found.")
    except Exception as exc:  # malformed YAML / missing parser
        logger.exception("Failed to load column registry")
        raise HTTPException(
            status_code=500, detail=f"Failed to load column registry: {exc}"
        )


__all__ = ["router", "get_column_registry"]

"""Telemetry ingestion: raw event model, receiver service, and HTTP router.

This is the **Ingestion Layer** (``docs/project_context.md`` §7): it receives
floater telemetry from the MyJio app, validates the request shape, normalizes
each event via the Preprocessing Layer, and persists the result (best-effort).

The raw request model intentionally mirrors ``contracts/event_schema.md`` (the
*raw* client shape: epoch-ms timestamps, ``label``, ``newscreen_name``, raw
event names) and is permissive — unknown extra fields are preserved so nothing
is silently lost before quarantine routing. Conversion to the canonical
analytics shape (``analytics_contract.md`` §3) happens in the normalizer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from database.connection import DatabaseManager, get_database
from preprocessing.event_normalizer import (
    EventNormalizer,
    NormalizationResult,
    NormalizationStatus,
    NormalizedEvent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RawTelemetryEvent(BaseModel):
    """Raw floater telemetry event as emitted by the MyJio client.

    Permissive by design: alias-tolerant (accepts both the ``event_schema.md``
    raw field names and the canonical names) and ``extra="allow"`` so any
    additional client metadata survives into normalization/quarantine.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    customerId: str = Field(validation_alias=AliasChoices("customerId", "customer_id"))
    sessionId: str = Field(validation_alias=AliasChoices("sessionId", "session_id"))
    event_type: str = Field(
        validation_alias=AliasChoices("event_type", "eventName", "raw_event")
    )

    # Optional — resolved/validated during normalization.
    # NOTE: per event_schema.md v2 §3/§6, `label` is the chosen action (carries
    # the skip signal), NOT the campaign — the campaign is `click_action`. It is
    # passed through untouched so the normalizer can derive click vs skip.
    label: Optional[str] = None
    campaign: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("campaign", "campaign_id")
    )
    timestamp: Optional[Any] = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "event_timestamp", "timestamp_ist"),
    )
    screen_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("screen_name", "newscreen_name")
    )
    click_action: Optional[str] = None
    platform: Optional[str] = None
    os: Optional[str] = None

    def to_raw_dict(self) -> dict[str, Any]:
        """Flatten declared fields + preserved extras into one payload dict."""
        data = self.model_dump(exclude_none=True)
        if self.model_extra:
            data.update({k: v for k, v in self.model_extra.items() if v is not None})
        return data


class EventReceipt(BaseModel):
    """Per-event ingestion outcome returned to the client."""

    status: NormalizationStatus
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    reason: Optional[str] = None


class IngestResponse(BaseModel):
    """Response envelope for a single event."""

    accepted: int
    quarantined: int
    receipt: EventReceipt
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class BatchIngestResponse(BaseModel):
    """Response envelope for a batch of events."""

    total: int
    accepted: int
    quarantined: int
    receipts: list[EventReceipt]
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Receiver service
# ---------------------------------------------------------------------------


class TelemetryReceiver:
    """Application service: normalize incoming telemetry and persist results.

    Persistence is best-effort and structural: if no live database pool is
    available the events are logged and still acknowledged, keeping the
    ingestion edge resilient. The DDL the persistence methods expect is kept
    alongside them for reference.
    """

    INSERT_EVENT_SQL = """
        INSERT INTO telemetry_events (
            event_id, customer_id, session_id, campaign, event_type, ts,
            screen_name, click_action, raw_event, conversion_type, event_date,
            mapping_version, ingested_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
        )
        ON CONFLICT (event_id) DO NOTHING;
    """  # idempotent de-dup by event_id (analytics_contract.md §3.3)

    INSERT_QUARANTINE_SQL = """
        INSERT INTO telemetry_quarantine (reason, payload, quarantined_at)
        VALUES ($1, $2, $3);
    """

    def __init__(
        self,
        normalizer: Optional[EventNormalizer] = None,
        database: Optional[DatabaseManager] = None,
    ) -> None:
        self.normalizer = normalizer or EventNormalizer()
        self.database = database

    async def receive(self, event: RawTelemetryEvent) -> NormalizationResult:
        """Normalize and persist a single raw event."""
        result = self.normalizer.normalize(event.to_raw_dict())
        await self._persist(result)
        return result

    async def receive_batch(
        self, events: list[RawTelemetryEvent]
    ) -> list[NormalizationResult]:
        """Normalize and persist a batch; one bad event never fails the rest."""
        results: list[NormalizationResult] = []
        for event in events:
            results.append(await self.receive(event))
        return results

    # -- persistence (best-effort) ----------------------------------------

    async def _persist(self, result: NormalizationResult) -> None:
        if self.database is None or not self.database.is_connected:
            logger.debug(
                "No database connection; skipping persistence (status=%s)",
                result.status.value,
            )
            return
        try:
            if result.is_normalized and result.event is not None:
                await self._store_event(result.event)
            else:
                await self._store_quarantine(result)
        except Exception:
            # Never let a storage hiccup break acknowledgement of ingestion.
            logger.exception("Failed to persist event (status=%s)", result.status.value)

    async def _store_event(self, event: NormalizedEvent) -> None:
        async with self.database.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                self.INSERT_EVENT_SQL,
                event.event_id,
                event.customerId,
                event.sessionId,
                event.campaign,
                event.event_type,
                event.timestamp,
                event.screen_name,
                event.click_action,
                event.raw_event,
                event.conversion_type,
                event.event_date,
                event.mapping_version,
                event.ingested_at,
            )

    async def _store_quarantine(self, result: NormalizationResult) -> None:
        async with self.database.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                self.INSERT_QUARANTINE_SQL,
                result.reason,
                result.raw,
                datetime.now(timezone.utc),
            )


def _to_receipt(result: NormalizationResult) -> EventReceipt:
    if result.is_normalized and result.event is not None:
        return EventReceipt(
            status=result.status,
            event_id=result.event.event_id,
            event_type=str(result.event.event_type),
        )
    return EventReceipt(status=result.status, reason=result.reason)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def get_receiver(
    database: DatabaseManager = Depends(get_database),
) -> TelemetryReceiver:
    """FastAPI dependency: build a receiver bound to the shared DB manager.

    The normalizer is stateless, so constructing a receiver per request is
    cheap; the database manager and its pool are shared singletons.
    """
    return TelemetryReceiver(normalizer=EventNormalizer(), database=database)


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single floater telemetry event",
)
async def ingest_event(
    event: RawTelemetryEvent,
    receiver: TelemetryReceiver = Depends(get_receiver),
) -> IngestResponse:
    result = await receiver.receive(event)
    accepted = 1 if result.is_normalized else 0
    return IngestResponse(
        accepted=accepted,
        quarantined=1 - accepted,
        receipt=_to_receipt(result),
    )


@router.post(
    "/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of floater telemetry events",
)
async def ingest_batch(
    events: list[RawTelemetryEvent],
    receiver: TelemetryReceiver = Depends(get_receiver),
) -> BatchIngestResponse:
    results = await receiver.receive_batch(events)
    receipts = [_to_receipt(r) for r in results]
    accepted = sum(1 for r in results if r.is_normalized)
    return BatchIngestResponse(
        total=len(results),
        accepted=accepted,
        quarantined=len(results) - accepted,
        receipts=receipts,
    )


__all__ = [
    "RawTelemetryEvent",
    "EventReceipt",
    "IngestResponse",
    "BatchIngestResponse",
    "TelemetryReceiver",
    "router",
    "get_receiver",
]

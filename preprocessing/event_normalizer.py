"""Event normalization for the MyJio Floater Analytics ingestion pipeline.

This module is the **Preprocessing Layer** entry point. It converts a raw
client telemetry event (shape defined in ``contracts/event_schema.md``) into the
canonical normalized event record (shape defined in
``contracts/analytics_contract.md`` §3).

Responsibilities (and *only* these — this is infrastructure, not analytics):

* **Event-type normalization** — map raw client event names to the four
  canonical event types using the authoritative mapping table
  (``analytics_contract.md`` §2). Unmapped events are *quarantined*, never
  counted (§1).
* **Timestamp normalization** — accept epoch-ms / epoch-s / ISO-8601 / datetime
  and emit a timezone-aware UTC ``datetime`` (§13 "Time fields are UTC").
* **Field validation** — enforce presence/typing of the canonical required
  fields and apply the 5-minute future-skew guard (§3.3).

Stateful enrichment (``impression_seq``, ``is_repeat_impression``,
``time_since_impression_ms``) and conversion attribution (§4) require
cross-event state and are performed downstream; they are emitted here as
``None`` placeholders so the record already matches the contract shape.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract constants (analytics_contract.md §1, §2, §4)
# ---------------------------------------------------------------------------

#: Version of the event-mapping table applied. Bumped whenever the mapping or
#: canonical taxonomy changes (analytics_contract.md §2 "Mapping is versioned").
MAPPING_VERSION = "2026.05-v1"

#: Clock-skew tolerance for future timestamps (analytics_contract.md §3.3).
FUTURE_SKEW_TOLERANCE = timedelta(minutes=5)


class CanonicalEventType(str, Enum):
    """The four canonical event types (analytics_contract.md §1)."""

    IMPRESSION = "impression"
    CLICK = "click"
    SKIP = "skip"
    CONVERSION = "conversion"


class ConversionType(str, Enum):
    """Conversion sub-types (analytics_contract.md §4.1)."""

    RECHARGE = "recharge"
    OTT_SUBSCRIPTION = "ott_subscription"


#: Authoritative raw -> canonical mapping (analytics_contract.md §2) merged with
#: the raw event names observed in contracts/event_schema.md §4. Keys are
#: matched case-insensitively after stripping surrounding whitespace.
EVENT_TYPE_MAPPING: dict[str, CanonicalEventType] = {
    # analytics_contract.md §2 (authoritative)
    "floater_impression": CanonicalEventType.IMPRESSION,
    "floater_click": CanonicalEventType.CLICK,
    "floater_skip": CanonicalEventType.SKIP,
    "dismiss_popup": CanonicalEventType.SKIP,
    "recharge_success": CanonicalEventType.CONVERSION,
    "ott_subscription_success": CanonicalEventType.CONVERSION,
    # event_schema.md §4 (observed raw names)
    "recharge floater impression": CanonicalEventType.IMPRESSION,
    "recharge floater clicks": CanonicalEventType.CLICK,
}

#: Raw conversion event -> conversion_type (analytics_contract.md §4.1).
CONVERSION_TYPE_MAPPING: dict[str, ConversionType] = {
    "recharge_success": ConversionType.RECHARGE,
    "ott_subscription_success": ConversionType.OTT_SUBSCRIPTION,
}

#: Canonical fields that must be resolvable for an event to be analytics-ready
#: (analytics_contract.md §3.1 required columns, minus screen_name which is
#: defaulted rather than dropped — see ``DEFAULT_SCREEN_NAME``).
REQUIRED_CANONICAL_FIELDS = ("customerId", "sessionId", "campaign")

DEFAULT_SCREEN_NAME = "unknown"


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class NormalizedEvent(BaseModel):
    """Canonical normalized telemetry record (analytics_contract.md §3).

    The contract between the ingestion/preprocessing layers and everything
    downstream. Percentage/derived analytics are *not* computed here.
    """

    # §3.1 core fields
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    customerId: str
    sessionId: str
    campaign: str
    event_type: CanonicalEventType
    timestamp: datetime
    screen_name: str
    click_action: Optional[str] = None

    # §3.2 derived / enrichment fields populated at this layer
    raw_event: str
    conversion_type: Optional[ConversionType] = None
    event_date: date
    mapping_version: str = MAPPING_VERSION
    ingested_at: datetime

    # §3.2 derived fields requiring cross-event state — filled downstream.
    impression_seq: Optional[int] = None
    is_repeat_impression: Optional[bool] = None
    time_since_impression_ms: Optional[int] = None

    model_config = {"use_enum_values": True}


class NormalizationStatus(str, Enum):
    NORMALIZED = "normalized"
    QUARANTINED = "quarantined"


class NormalizationResult(BaseModel):
    """Outcome of normalizing a single raw event.

    A quarantined result carries the reason and the original payload so the
    ingestion layer can route it to the quarantine store (analytics_contract.md
    §1: unmapped/invalid events are *never* counted in metrics).
    """

    status: NormalizationStatus
    event: Optional[NormalizedEvent] = None
    reason: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_normalized(self) -> bool:
        return self.status is NormalizationStatus.NORMALIZED


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class EventNormalizer:
    """Stateless transformer: raw telemetry payload -> ``NormalizationResult``.

    Stateless and side-effect free, so it is safe to share a single instance
    across requests/workers.
    """

    def __init__(
        self,
        *,
        event_type_mapping: Optional[Mapping[str, CanonicalEventType]] = None,
        conversion_type_mapping: Optional[Mapping[str, ConversionType]] = None,
        mapping_version: str = MAPPING_VERSION,
        future_skew_tolerance: timedelta = FUTURE_SKEW_TOLERANCE,
    ) -> None:
        # Normalize mapping keys to lower-case for case-insensitive lookup.
        self._event_type_mapping = {
            k.strip().lower(): v
            for k, v in (event_type_mapping or EVENT_TYPE_MAPPING).items()
        }
        self._conversion_type_mapping = {
            k.strip().lower(): v
            for k, v in (conversion_type_mapping or CONVERSION_TYPE_MAPPING).items()
        }
        self.mapping_version = mapping_version
        self.future_skew_tolerance = future_skew_tolerance

    # -- public API --------------------------------------------------------

    def normalize(self, raw: Mapping[str, Any]) -> NormalizationResult:
        """Normalize one raw telemetry payload.

        Never raises on bad data — invalid/unmapped events are returned as a
        quarantined result so a single bad event cannot break a batch.
        """
        payload = dict(raw)
        try:
            return self._normalize(payload)
        except _Quarantine as exc:
            logger.info("Quarantining event: %s", exc.reason)
            return NormalizationResult(
                status=NormalizationStatus.QUARANTINED,
                reason=exc.reason,
                raw=payload,
            )
        except Exception:  # defensive: never let preprocessing crash ingestion
            logger.exception("Unexpected error normalizing event")
            return NormalizationResult(
                status=NormalizationStatus.QUARANTINED,
                reason="internal_normalization_error",
                raw=payload,
            )

    # -- internals ---------------------------------------------------------

    def _normalize(self, raw: dict[str, Any]) -> NormalizationResult:
        raw_event = self._first_str(raw, "event_type", "raw_event", "eventName")
        if not raw_event:
            raise _Quarantine("missing_required_field:event_type")

        # Event-type normalization (analytics_contract.md §2).
        canonical = self._event_type_mapping.get(raw_event.strip().lower())
        if canonical is None:
            raise _Quarantine(f"unmapped_event_type:{raw_event}")

        # Field validation (analytics_contract.md §3.1).
        customer_id = self._first_str(raw, "customerId", "customer_id")
        session_id = self._first_str(raw, "sessionId", "session_id")
        campaign = self._first_str(raw, "campaign", "label", "campaign_id")

        for field_name, value in (
            ("customerId", customer_id),
            ("sessionId", session_id),
            ("campaign", campaign),
        ):
            if not value:
                raise _Quarantine(f"missing_required_field:{field_name}")

        # Timestamp normalization -> tz-aware UTC (analytics_contract.md §13).
        raw_ts = self._first_present(
            raw, "timestamp", "event_timestamp", "timestamp_ist"
        )
        timestamp = self._normalize_timestamp(raw_ts)

        ingested_at = datetime.now(timezone.utc)
        # Future-skew guard (analytics_contract.md §3.3).
        if timestamp > ingested_at + self.future_skew_tolerance:
            raise _Quarantine("timestamp_in_future")

        screen_name = (
            self._first_str(raw, "screen_name", "newscreen_name") or DEFAULT_SCREEN_NAME
        )
        click_action = self._first_str(raw, "click_action")

        conversion_type = None
        if canonical is CanonicalEventType.CONVERSION:
            conversion_type = self._conversion_type_mapping.get(
                raw_event.strip().lower()
            )

        event = NormalizedEvent(
            customerId=customer_id,
            sessionId=session_id,
            campaign=campaign,
            event_type=canonical,
            timestamp=timestamp,
            screen_name=screen_name,
            click_action=click_action,
            raw_event=raw_event,
            conversion_type=conversion_type,
            event_date=timestamp.date(),
            mapping_version=self.mapping_version,
            ingested_at=ingested_at,
        )
        logger.debug("Normalized %s -> %s", raw_event, canonical.value)
        return NormalizationResult(
            status=NormalizationStatus.NORMALIZED, event=event, raw=raw
        )

    # -- timestamp handling ------------------------------------------------

    def _normalize_timestamp(self, value: Any) -> datetime:
        """Coerce a raw timestamp into a timezone-aware UTC ``datetime``.

        Accepts epoch milliseconds (per event_schema.md), epoch seconds,
        ISO-8601 strings, and ``datetime`` objects.
        """
        if value is None:
            raise _Quarantine("missing_required_field:timestamp")

        if isinstance(value, datetime):
            return self._to_utc(value)

        if isinstance(value, bool):  # bool is an int subclass — reject explicitly
            raise _Quarantine("invalid_timestamp")

        if isinstance(value, (int, float)):
            return self._epoch_to_utc(float(value))

        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise _Quarantine("missing_required_field:timestamp")
            # Numeric string -> epoch.
            if text.lstrip("-").isdigit():
                return self._epoch_to_utc(float(text))
            try:
                # Support trailing 'Z' (Python <3.11 fromisoformat rejects it).
                return self._to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
            except ValueError:
                raise _Quarantine(f"invalid_timestamp:{value!r}")

        raise _Quarantine(f"invalid_timestamp_type:{type(value).__name__}")

    @staticmethod
    def _epoch_to_utc(value: float) -> datetime:
        # Heuristic: values >= 1e12 are milliseconds (year ~2001+ in ms),
        # otherwise seconds. event_schema.md specifies epoch milliseconds.
        seconds = value / 1000.0 if abs(value) >= 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise _Quarantine(f"invalid_epoch_timestamp:{value!r}")

    @staticmethod
    def _to_utc(dt: datetime) -> datetime:
        # Naive datetimes are assumed UTC (contract stores all times in UTC).
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # -- small helpers -----------------------------------------------------

    @staticmethod
    def _first_present(raw: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in raw and raw[key] is not None:
                return raw[key]
        return None

    @classmethod
    def _first_str(cls, raw: Mapping[str, Any], *keys: str) -> Optional[str]:
        value = cls._first_present(raw, *keys)
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class _Quarantine(Exception):
    """Internal control-flow signal carrying a quarantine reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


__all__ = [
    "CanonicalEventType",
    "ConversionType",
    "EVENT_TYPE_MAPPING",
    "CONVERSION_TYPE_MAPPING",
    "MAPPING_VERSION",
    "NormalizedEvent",
    "NormalizationResult",
    "NormalizationStatus",
    "EventNormalizer",
]

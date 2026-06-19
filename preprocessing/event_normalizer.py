"""Event normalization for the MyJio Floater Analytics ingestion pipeline.

This module is the **Preprocessing Layer** entry point. It converts a raw
client telemetry event (shape defined in ``contracts/event_schema.md`` v2.0)
into the canonical normalized event record.

Responsibilities (and *only* these — this is infrastructure, not analytics):

* **Event-type normalization / canonical derivation** (event_schema §5,
  analytics_contract §1) — the platform derives three behavioural events today:

    - ``Recharge floater impression``                            → ``impression``
    - ``Recharge floater clicks`` + ``label`` has a skip marker   → ``skip``
    - ``Recharge floater clicks`` + ``label`` has no skip marker  → ``click``

  Skip is **label-derived** (there is no native skip event); the skip rule is
  evaluated *before* click. Every other ``event_type`` — API plumbing, app
  lifecycle, navigation, ``Recharge initiated`` — is **quarantined** and can
  never become impression/click/skip (event_schema §7).
* **Campaign identification** (event_schema §6) — ``campaign := click_action``
  (e.g. ``PLANEXPIRY01``), never ``label``. A missing ``click_action`` becomes
  the ``__unknown_campaign__`` sentinel.
* **Timestamp normalization** — accept epoch-ms / epoch-s / ISO-8601 / datetime
  and emit a timezone-aware UTC ``datetime`` (event_schema §9; epoch-ms is the
  real format).
* **Field validation** — enforce presence of the core required fields and apply
  the 5-minute future-skew guard (event_schema §11).

Stateful enrichment (``impression_seq``, ``is_repeat_impression``,
``time_since_impression_ms``) and conversion attribution require cross-event
state and are performed downstream; they are emitted here as ``None``
placeholders so the record already matches the contract shape.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from pydantic import BaseModel, Field

try:  # pragma: no cover - import guard; only needed to read the registry
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract constants (event_schema.md §5–§9, analytics_contract.md §1)
# ---------------------------------------------------------------------------

#: Version of the derivation/mapping rules applied. Bumped to track the v2.0
#: frozen contracts (event_schema.md / analytics_contract.md, 2026-06-01).
MAPPING_VERSION = "2026.06-v2"

#: Clock-skew tolerance for future timestamps (event_schema.md §11).
FUTURE_SKEW_TOLERANCE = timedelta(minutes=5)

#: Campaign sentinel for rows missing ``click_action`` (event_schema.md §6).
#: Such rows stay well-defined but are excluded from per-campaign reporting.
UNKNOWN_CAMPAIGN = "__unknown_campaign__"

#: Default location of the semantic column registry (contracts/column_registry.yaml).
#: Resolved relative to the project root so it works regardless of CWD.
COLUMN_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "column_registry.yaml"
)


def load_column_registry(
    path: Optional[Union[str, Path]] = None,
) -> dict[str, Any]:
    """Load and parse the semantic column registry (``column_registry.yaml``).

    The registry maps each raw source column to a domain-neutral semantic role
    (``entity_id``, ``event_time``, ``group_id`` …). It is the configuration
    that lets the same analytics framework serve different domains without code
    changes. This is read-only metadata and does not affect normalization.

    Raises ``FileNotFoundError`` if the registry is missing, or ``RuntimeError``
    if PyYAML is unavailable.
    """
    registry_path = Path(path) if path else COLUMN_REGISTRY_PATH
    if yaml is None:  # pragma: no cover - environment guard
        raise RuntimeError(
            "PyYAML is required to read the column registry. Install it "
            "(see requirements.txt)."
        )
    with open(registry_path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Column registry at {registry_path} must be a mapping of columns."
        )
    return data


class CanonicalEventType(str, Enum):
    """The canonical behavioural event types (analytics_contract.md §1).

    ``conversion`` is reserved for future telemetry (event_schema.md §10); no
    raw event maps to it in the current export.
    """

    IMPRESSION = "impression"
    CLICK = "click"
    SKIP = "skip"
    CONVERSION = "conversion"


class ConversionType(str, Enum):
    """Conversion sub-types (event_schema.md §10 — future)."""

    RECHARGE = "recharge"
    OTT_SUBSCRIPTION = "ott_subscription"


#: Raw ``event_type`` values (lower-cased) that derive to ``impression``
#: (event_schema.md §5.1).
IMPRESSION_RAW_EVENTS: frozenset[str] = frozenset({"recharge floater impression"})

#: Raw ``event_type`` values (lower-cased) that carry the click/skip funnel and
#: are disambiguated by ``label`` (event_schema.md §5.2–§5.4).
CLICK_SKIP_RAW_EVENTS: frozenset[str] = frozenset({"recharge floater clicks"})

#: Default skip markers searched (case-insensitively) within ``label``
#: (event_schema.md §5.2 / §5.4). Configuration-driven; this is the baseline.
DEFAULT_SKIP_MARKERS: tuple[str, ...] = ("skip", "dismiss")

#: Future conversion raw events -> conversion_type (event_schema.md §10).
#: Absent from today's data; retained for forward-compatibility. ``Recharge
#: initiated`` is intent and is intentionally NOT mapped here.
CONVERSION_TYPE_MAPPING: dict[str, ConversionType] = {
    "recharge_success": ConversionType.RECHARGE,
    "ott_subscription_success": ConversionType.OTT_SUBSCRIPTION,
}

DEFAULT_SCREEN_NAME = "unknown"


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class NormalizedEvent(BaseModel):
    """Canonical normalized telemetry record.

    The contract between the ingestion/preprocessing layers and everything
    downstream. Percentage/derived analytics are *not* computed here.
    """

    # Core fields
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    customerId: str
    sessionId: str
    campaign: str  # := click_action (event_schema.md §6)
    event_type: CanonicalEventType
    timestamp: datetime
    screen_name: str
    # The raw click_action value. Equal to ``campaign`` for funnel rows; kept
    # distinct so a missing value surfaces as ``None`` rather than the sentinel.
    click_action: Optional[str] = None

    # Derived / enrichment fields populated at this layer
    raw_event: str
    label: Optional[str] = None  # chosen action / CTA (event_schema.md §3)
    conversion_type: Optional[ConversionType] = None
    event_date: date
    mapping_version: str = MAPPING_VERSION
    ingested_at: datetime

    # Derived fields requiring cross-event state — filled downstream.
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
    ingestion layer can route it to the quarantine store (event_schema.md §7:
    unmapped/non-funnel events are *never* counted in metrics).
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
        impression_events: Optional[Iterable[str]] = None,
        click_skip_events: Optional[Iterable[str]] = None,
        skip_markers: Optional[Iterable[str]] = None,
        conversion_type_mapping: Optional[Mapping[str, ConversionType]] = None,
        mapping_version: str = MAPPING_VERSION,
        future_skew_tolerance: timedelta = FUTURE_SKEW_TOLERANCE,
        column_registry: Optional[Mapping[str, Any]] = None,
    ) -> None:
        # Case-insensitive matching on trimmed, lower-cased values (§5.4).
        self._impression_events = frozenset(
            e.strip().lower() for e in (impression_events or IMPRESSION_RAW_EVENTS)
        )
        self._click_skip_events = frozenset(
            e.strip().lower() for e in (click_skip_events or CLICK_SKIP_RAW_EVENTS)
        )
        self._skip_markers = tuple(
            m.strip().lower() for m in (skip_markers or DEFAULT_SKIP_MARKERS)
        )
        self._conversion_type_mapping = {
            k.strip().lower(): v
            for k, v in (conversion_type_mapping or CONVERSION_TYPE_MAPPING).items()
        }
        self.mapping_version = mapping_version
        self.future_skew_tolerance = future_skew_tolerance
        # Optional semantic column registry. Read-only metadata: it lets callers
        # resolve column meaning by role and does NOT affect normalization.
        self.column_registry: Optional[dict[str, Any]] = (
            dict(column_registry) if column_registry is not None else None
        )

    @classmethod
    def with_registry(
        cls, path: Optional[Union[str, Path]] = None, **kwargs: Any
    ) -> "EventNormalizer":
        """Construct a normalizer with the semantic column registry loaded."""
        return cls(column_registry=load_column_registry(path), **kwargs)

    # -- semantic registry access (read-only; no effect on normalization) --

    def semantic_role(self, column: str) -> Optional[str]:
        """Return the semantic role configured for a raw ``column``, if any."""
        if not self.column_registry:
            return None
        entry = self.column_registry.get(column)
        if isinstance(entry, Mapping):
            role = entry.get("semantic_role")
            return str(role) if role is not None else None
        return None

    def columns_for_role(self, role: str) -> list[str]:
        """Return all raw column names mapped to the given semantic ``role``."""
        if not self.column_registry:
            return []
        return [
            column
            for column, entry in self.column_registry.items()
            if isinstance(entry, Mapping) and entry.get("semantic_role") == role
        ]

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

        label = self._first_str(raw, "label")

        # Canonical event derivation (event_schema.md §5). Returns None for any
        # non-funnel event so infrastructure/API events can never become a
        # behavioural event — they fall through to quarantine.
        canonical = self._derive_event_type(raw_event, label)
        if canonical is None:
            raise _Quarantine(f"unmapped_event_type:{raw_event}")

        # Field validation — core required identifiers (event_schema.md §2).
        customer_id = self._first_str(raw, "customerId", "customer_id")
        session_id = self._first_str(raw, "sessionId", "session_id")
        for field_name, value in (
            ("customerId", customer_id),
            ("sessionId", session_id),
        ):
            if not value:
                raise _Quarantine(f"missing_required_field:{field_name}")

        # Campaign := click_action, with sentinel fallback (event_schema.md §6).
        click_action = self._first_str(raw, "click_action")
        campaign = click_action or UNKNOWN_CAMPAIGN

        # Timestamp normalization -> tz-aware UTC (event_schema.md §9).
        raw_ts = self._first_present(
            raw, "event_timestamp", "timestamp", "timestamp_ist"
        )
        timestamp = self._normalize_timestamp(raw_ts)

        ingested_at = datetime.now(timezone.utc)
        # Future-skew guard (event_schema.md §11).
        if timestamp > ingested_at + self.future_skew_tolerance:
            raise _Quarantine("timestamp_in_future")

        screen_name = (
            self._first_str(raw, "screen_name", "newscreen_name") or DEFAULT_SCREEN_NAME
        )

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
            label=label,
            conversion_type=conversion_type,
            event_date=timestamp.date(),
            mapping_version=self.mapping_version,
            ingested_at=ingested_at,
        )
        logger.debug("Normalized %s -> %s", raw_event, canonical.value)
        return NormalizationResult(
            status=NormalizationStatus.NORMALIZED, event=event, raw=raw
        )

    # -- canonical derivation (event_schema.md §5) ------------------------

    def _derive_event_type(
        self, raw_event: str, label: Optional[str]
    ) -> Optional[CanonicalEventType]:
        key = raw_event.strip().lower()

        # §5.1 Impression — clean and unambiguous.
        if key in self._impression_events:
            return CanonicalEventType.IMPRESSION

        # §5.2/§5.3 Click vs Skip — same raw event, disambiguated by label.
        # The skip rule is evaluated BEFORE click; a row is never both (§5.4).
        if key in self._click_skip_events:
            if self._has_skip_marker(label):
                # Data-quality signal: a "clicks" row reclassified to skip
                # (event_schema.md §5.4 / §11). Logged so a future change to the
                # label naming convention is detected, not silently mis-counted.
                logger.info(
                    "skip_reclassification: event_type=%r label=%r -> skip",
                    raw_event,
                    label,
                )
                return CanonicalEventType.SKIP
            return CanonicalEventType.CLICK

        # Future conversion events (event_schema.md §10) — absent today.
        if key in self._conversion_type_mapping:
            return CanonicalEventType.CONVERSION

        # Everything else (API plumbing, lifecycle, navigation, intent) is
        # quarantined — never a behavioural event.
        return None

    def _has_skip_marker(self, label: Optional[str]) -> bool:
        if not label:
            return False
        label_lower = label.lower()
        return any(marker in label_lower for marker in self._skip_markers)

    # -- timestamp handling ------------------------------------------------

    def _normalize_timestamp(self, value: Any) -> datetime:
        """Coerce a raw timestamp into a timezone-aware UTC ``datetime``.

        Accepts epoch milliseconds (the real format, event_schema.md §9), epoch
        seconds, ISO-8601 strings, and ``datetime`` objects.
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
        # Heuristic: values >= 1e12 are milliseconds (event_schema.md §9),
        # otherwise seconds.
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
    "IMPRESSION_RAW_EVENTS",
    "CLICK_SKIP_RAW_EVENTS",
    "DEFAULT_SKIP_MARKERS",
    "CONVERSION_TYPE_MAPPING",
    "MAPPING_VERSION",
    "UNKNOWN_CAMPAIGN",
    "COLUMN_REGISTRY_PATH",
    "load_column_registry",
    "NormalizedEvent",
    "NormalizationResult",
    "NormalizationStatus",
    "EventNormalizer",
]

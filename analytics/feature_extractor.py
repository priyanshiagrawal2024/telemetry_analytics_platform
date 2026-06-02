"""Customer-level feature extraction for the MyJio Floater Analytics Platform.

Layer position (see ``CLAUDE.md`` / ``docs/project_context.md`` §7)::

    Telemetry Events
        -> Ingestion Layer        (TelemetryLoader)        <-- THIS MODULE
        -> Preprocessing Layer    (EventClassifier)        <-- THIS MODULE
        -> Feature Extraction     (FeatureExtractor)       <-- THIS MODULE
        -> Analytics Engine
        -> Insight Generation
        -> Dashboard

This module turns a **raw MyJio telemetry export** into a **customer profile**
:class:`pandas.DataFrame` (one row per ``customerId``) whose schema matches
**§6 Customer Profile Schema** of ``contracts/analytics_contract.md`` (v2.0,
FROZEN). It is the primary input to the downstream analyses:
``fatigue_analysis``, ``campaign_analysis``, ``segmentation_analysis``,
``trend_analysis``, ``engagement_analysis``.

Source-of-truth documents (FROZEN — no metric / mapping / schema is invented
outside them):

* ``contracts/event_schema.md`` v2.0 — file format (§1), fields (§2/§3),
  canonical event derivation (§5), campaign identifier (§6), quarantine
  families (§7), customer journey (§8), timestamps (§9), future conversions
  (§10).
* ``contracts/analytics_contract.md`` v2.0 — derivation rules (§1), capability
  gating (§2), supported metrics (§3), unsupported/placeholder metrics (§4),
  future metrics (§5), profile schema (§6), single-customer limits (§7),
  conventions/guardrails (§8).
* ``docs/telemetry_data_findings.md`` — validation evidence for the above.

Key validated realities encoded here
-------------------------------------
* The shipped ``sample_data/telemetry_sample.csv`` is an **XLSX workbook**
  (``PK`` zip signature) despite its ``.csv`` name -> sniff magic bytes.
* ``event_timestamp`` is **epoch milliseconds**, not ISO-8601.
* **Skip is not a native event.** A dismissal is a ``Recharge floater clicks``
  row whose ``label`` contains a ``skip``/``dismiss`` marker (e.g.
  ``Recharge-skip``). Skip is therefore *label-derived*.
* The **campaign key is ``click_action``** (e.g. ``PLANEXPIRY01``), never
  ``label`` (which holds the chosen action on a click).
* **No conversion telemetry** exists yet (``Recharge initiated`` is intent, not
  a completed outcome) -> conversion metrics are placeholders.

Capability gating (§2)
----------------------
Any metric whose source events are absent is emitted as an explicit
**placeholder (``<NA>`` / ``NaN`` / ``None``) with a logged warning**, never a
misleading ``0``. The same code computes the metric unchanged once the
telemetry arrives.

This is an **analytics** module: rule-based, explainable, no recommendations,
no personalisation, no ML (guardrails §8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set, Union

import numpy as np
import pandas as pd
import yaml

__all__ = [
    "TelemetryLoader",
    "SemanticSchema",
    "load_semantic_schema",
    "EventClassifier",
    "EventClassifierConfig",
    "FeatureExtractor",
    "FeatureExtractorConfig",
    "CustomerProfile",
    "MetricCalculator",
    "MetricResult",
    "extract_customer_profiles",
]

logger = logging.getLogger(__name__)


# ===========================================================================
# Canonical taxonomy & contract constants (event_schema.md §4/§5/§7/§10)
# ===========================================================================

EVENT_IMPRESSION = "impression"
EVENT_CLICK = "click"
EVENT_SKIP = "skip"
EVENT_CONVERSION = "conversion"

CANONICAL_EVENTS: Set[str] = {
    EVENT_IMPRESSION,
    EVENT_CLICK,
    EVENT_SKIP,
    EVENT_CONVERSION,
}

#: Raw ``event_type`` -> canonical event (matched case-insensitively). Entries
#: flagged "future" are valid mappings that simply do not appear in the current
#: telemetry (event_schema.md §10). Skip is intentionally NOT here: it is
#: derived from the ``label`` of a click row (§5.2), not from ``event_type``.
DEFAULT_EVENT_MAPPING: Dict[str, str] = {
    # --- present in the validated sample (event_schema.md §4) -------------
    "recharge floater impression": EVENT_IMPRESSION,
    "recharge floater clicks": EVENT_CLICK,
    "recharge floater click": EVENT_CLICK,  # singular tolerance
    # --- future conversion telemetry (event_schema.md §10) ----------------
    "recharge_success": EVENT_CONVERSION,
    "ott_subscription_success": EVENT_CONVERSION,
    "fiber_activation_success": EVENT_CONVERSION,
    "upi_success": EVENT_CONVERSION,
}

#: Known infrastructure / lifecycle / other-surface events (event_schema.md
#: §7). Legitimate telemetry that carries NO floater funnel -> dropped quietly
#: (debug-logged), never quarantined as "unknown". Matched case-insensitively;
#: ``HOME_API_STATUS-*`` / ``ENTERTAINMENT_API_STATUS-*`` handled by prefix.
QUARANTINE_EVENT_TYPES: FrozenSet[str] = frozenset(
    {
        # Floater API plumbing
        "floaterresponse",
        "floater api called",
        "floater api response received",
        "campaign_response_received",
        "campaign_response_received_empty",
        "campaign_saved_in_db",
        # Home / API status
        "homeapi request body",
        "burgermenu api called",
        "burgermenu api called-success--{body_notempty}",
        # App lifecycle
        "app open",
        "app background",
        "app closed",
        # Other surfaces / navigation
        "navigation_superapp",
        "home_superapp",
        "home",
        "jiocloud_login",
        "jiocloud_onboarding",
        "cloud_registered",
        "cloud_not_registered",
        "jiotune activated no",
        # Intent, NOT a conversion (event_schema.md §8/§10)
        "recharge initiated",
    }
)

#: Prefixes for families of status events that should be quarantined wholesale.
QUARANTINE_EVENT_PREFIXES: tuple = ("home_api_status", "entertainment_api_status")

#: Columns the classifier needs to derive canonical events.
REQUIRED_COLUMNS: tuple = ("customerId", "event_type", "event_timestamp")

#: Sentinel for a missing campaign so campaign-scoped logic stays defined (§6).
UNKNOWN_CAMPAIGN = "__unknown_campaign__"

# Tidy column set produced by EventClassifier and consumed by FeatureExtractor.
_CLASSIFIED_COLUMNS = [
    "customerId",
    "sessionId",
    "campaign",
    "event",
    "event_timestamp",
    "impression_seq",
    "is_repeat_impression",
]

#: Default location of the semantic mapping config (relative to project root).
_DEFAULT_SEMANTIC_MAPPINGS = (
    Path(__file__).resolve().parents[1] / "configs" / "semantic_mappings.yaml"
)


def _norm_key(value: object) -> str:
    """Normalise a raw token for case-insensitive lookups."""
    return str(value).strip().lower()


def _safe_div(
    numerator: object, denominator: object, scale: float = 1.0
) -> pd.Series:
    """``num / den * scale`` with a zero/NaN-denominator guard -> NaN."""
    num = pd.Series(numerator, dtype="float64")
    den = pd.Series(denominator, dtype="float64").reindex(num.index)
    result = np.where(den > 0, num / den.where(den > 0, np.nan) * scale, np.nan)
    return pd.Series(result, index=num.index, dtype="float64")


# ===========================================================================
# Semantic schema  (Task 4 — domain-agnostic role <-> column registry)
# ===========================================================================


@dataclass(frozen=True)
class SemanticSchema:
    """Resolves canonical *roles* to a dataset's physical columns & events.

    Loaded from ``configs/semantic_mappings.yaml`` (see
    :func:`load_semantic_schema`). The analytics layers reference roles
    (``customer_id``, ``campaign``, ``event_type`` …) so a new domain is
    onboarded by adding a YAML block — no code change.
    """

    dataset: str
    columns: Mapping[str, str]
    event_roles: Mapping[str, str]
    skip_markers: FrozenSet[str] = frozenset({"skip", "dismiss"})
    interaction_roles: FrozenSet[str] = frozenset({"click", "skip"})
    exposure_roles: FrozenSet[str] = frozenset({"impression"})
    timestamp_unit: str = "ms"

    def col(self, role: str) -> Optional[str]:
        """Physical column for ``role`` (or ``None`` if unmapped)."""
        return self.columns.get(role)

    def require(self, role: str) -> str:
        """Physical column for ``role``; raise if the role is not mapped."""
        column = self.col(role)
        if not column:
            raise KeyError(
                f"Semantic role '{role}' is not mapped for dataset "
                f"'{self.dataset}'. Add it to configs/semantic_mappings.yaml."
            )
        return column


def load_semantic_schema(
    path: Union[str, Path] = _DEFAULT_SEMANTIC_MAPPINGS,
    dataset: Optional[str] = None,
) -> SemanticSchema:
    """Load a :class:`SemanticSchema` for ``dataset`` (default: file default)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    dataset = dataset or doc.get("default_dataset")
    blocks = doc.get("datasets", {})
    if dataset not in blocks:
        raise KeyError(
            f"Dataset '{dataset}' not found in {path}. "
            f"Available: {sorted(blocks)}."
        )
    block = blocks[dataset]
    return SemanticSchema(
        dataset=dataset,
        columns=dict(block["columns"]),
        event_roles={
            _norm_key(k): str(v)
            for k, v in (block.get("event_roles") or {}).items()
        },
        skip_markers=frozenset(
            _norm_key(m) for m in (block.get("skip_markers") or ["skip", "dismiss"])
        ),
        interaction_roles=frozenset(block.get("interaction_roles") or ["click", "skip"]),
        exposure_roles=frozenset(block.get("exposure_roles") or ["impression"]),
        timestamp_unit=str(block.get("timestamp_unit", "ms")),
    )


# ===========================================================================
# 1. TelemetryLoader  (Ingestion Layer — event_schema.md §1)
# ===========================================================================

#: ZIP local-file-header magic. XLSX (OOXML) files are ZIP containers.
_ZIP_MAGIC = b"PK"


class TelemetryLoader:
    """Load a raw telemetry export, auto-detecting XLSX-vs-CSV from content.

    The shipped sample is an XLSX workbook despite its ``.csv`` extension; the
    extension is *not trusted*. The real format is sniffed from the leading
    magic bytes (event_schema.md §1).

    Example
    -------
    >>> df = TelemetryLoader().load("sample_data/telemetry_sample.csv")
    """

    def __init__(self, excel_engine: str = "openpyxl") -> None:
        self.excel_engine = excel_engine

    def load(self, path: Union[str, Path]) -> pd.DataFrame:
        """Return the raw telemetry as a DataFrame (one row per event).

        Raises
        ------
        FileNotFoundError
            If ``path`` does not exist.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Telemetry file not found: {path}")

        with path.open("rb") as fh:
            head = fh.read(2)

        if head == _ZIP_MAGIC:
            logger.info(
                "Detected XLSX content in %s (PK magic); reading via %s.",
                path.name,
                self.excel_engine,
            )
            df = pd.read_excel(path, engine=self.excel_engine)
        else:
            logger.info("Reading %s as delimited text (CSV).", path.name)
            df = pd.read_csv(path)

        logger.info("Loaded %d telemetry rows x %d columns.", len(df), df.shape[1])
        return df


# ===========================================================================
# 2. EventClassifier  (Preprocessing Layer — event_schema.md §5/§6/§7)
# ===========================================================================


@dataclass(frozen=True)
class EventClassifierConfig:
    """Column names and rules for canonical event derivation.

    Defaults match the FROZEN MyJio export schema.

    Attributes
    ----------
    customer_col, session_col, event_type_col, timestamp_col:
        Source column names. ``timestamp_col`` is epoch **milliseconds**.
    campaign_col:
        Campaign identifier column. **``click_action``** per event_schema §6
        (NOT ``label``).
    action_col:
        Column carrying the chosen action, scanned for skip markers
        (``label`` per §5.2).
    skip_markers:
        Substrings that mark a click row as a dismissal/skip (§5.2).
    event_mapping:
        Raw ``event_type`` -> canonical event (case-insensitive).
    """

    customer_col: str = "customerId"
    session_col: str = "sessionId"
    event_type_col: str = "event_type"
    timestamp_col: str = "event_timestamp"
    campaign_col: str = "click_action"
    action_col: str = "label"
    skip_markers: FrozenSet[str] = frozenset({"skip", "dismiss"})
    event_mapping: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_EVENT_MAPPING)
    )

    @classmethod
    def from_schema(cls, schema: "SemanticSchema") -> "EventClassifierConfig":
        """Build a config from a :class:`SemanticSchema` (Task 4 wiring).

        Only ``impression`` / ``click`` / ``conversion`` event roles flow into
        the behavioural mapping; ``skip`` is derived from the action label, and
        infrastructure roles (served/received/saved/other) are ignored by the
        contract classifier.
        """
        behavioural = {EVENT_IMPRESSION, EVENT_CLICK, EVENT_CONVERSION}
        mapping = {
            raw: role
            for raw, role in schema.event_roles.items()
            if role in behavioural
        }
        return cls(
            customer_col=schema.require("customer_id"),
            session_col=schema.col("session_id") or "sessionId",
            event_type_col=schema.require("event_type"),
            timestamp_col=schema.require("timestamp"),
            campaign_col=schema.col("campaign") or "click_action",
            action_col=schema.col("action") or "label",
            skip_markers=schema.skip_markers,
            event_mapping=mapping or dict(DEFAULT_EVENT_MAPPING),
        )


class EventClassifier:
    """Derive canonical floater events from raw telemetry (event_schema §5).

    Produces a tidy frame restricted to behavioural funnel events
    (``impression`` / ``click`` / ``skip`` and, in future, ``conversion``),
    with the campaign resolved from ``click_action`` and impression sequence
    numbers attached. Infrastructure/lifecycle events (§7) are dropped quietly;
    genuinely unknown event types are quarantined and warned about.
    """

    def __init__(self, config: Optional[EventClassifierConfig] = None) -> None:
        self.config = config or EventClassifierConfig()
        self._mapping = {
            self._norm(k): v for k, v in self.config.event_mapping.items()
        }

    # -- public API --------------------------------------------------------

    def classify(self, events: pd.DataFrame) -> pd.DataFrame:
        """Return the tidy classified funnel frame (``_CLASSIFIED_COLUMNS``).

        Raises
        ------
        TypeError
            If ``events`` is not a DataFrame.
        ValueError
            If required columns are missing.
        """
        self._validate(events)
        if events.empty:
            logger.warning("Received empty telemetry frame.")
            return self._empty()

        df = events.copy()
        cfg = self.config

        # --- map event_type -> canonical -----------------------------------
        keys = df[cfg.event_type_col].map(self._norm)
        df["event"] = keys.map(self._mapping)

        # --- label-derived skip (§5.2): reclassify marked clicks -----------
        df = self._apply_skip_rule(df)

        # --- drop quarantine / unknown (§7) --------------------------------
        df = self._drop_non_behavioural(df, keys)
        if df.empty:
            logger.warning("No canonical behavioural events after classification.")
            return self._empty()

        # --- normalise identity / campaign / timestamp --------------------
        df = self._normalise_columns(df)
        df = self._coerce_timestamp(df)
        if df.empty:
            return self._empty()

        # --- impression sequencing (event_schema §8) ----------------------
        df = self._add_impression_sequence(df)

        logger.info(
            "Classified %d behavioural events (%s).",
            len(df),
            df["event"].value_counts().to_dict(),
        )
        return df[_CLASSIFIED_COLUMNS].reset_index(drop=True)

    @staticmethod
    def present_events(classified: pd.DataFrame) -> Set[str]:
        """Canonical events actually present (drives capability gating §2)."""
        if classified.empty:
            return set()
        return set(classified["event"].dropna().unique())

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _norm(value: object) -> str:
        return str(value).strip().lower()

    def _validate(self, events: pd.DataFrame) -> None:
        if not isinstance(events, pd.DataFrame):
            raise TypeError(
                f"`events` must be a pandas DataFrame, got {type(events).__name__}."
            )
        required = set(REQUIRED_COLUMNS) | {
            self.config.customer_col,
            self.config.event_type_col,
            self.config.timestamp_col,
        }
        missing = sorted(required - set(events.columns))
        if missing:
            raise ValueError(
                f"Telemetry frame is missing required column(s): {missing}. "
                f"Present columns: {sorted(events.columns)}."
            )

    def _apply_skip_rule(self, df: pd.DataFrame) -> pd.DataFrame:
        """Reclassify ``click`` rows whose action label marks a skip (§5.2)."""
        cfg = self.config
        if cfg.action_col not in df.columns or not cfg.skip_markers:
            logger.debug("Skip refinement disabled (no action col / markers).")
            return df
        markers = tuple(m.strip().lower() for m in cfg.skip_markers if m)
        action = df[cfg.action_col].map(self._norm)
        looks_skip = action.apply(lambda s: any(m in s for m in markers))
        reclassified = df["event"].eq(EVENT_CLICK) & looks_skip
        n = int(reclassified.sum())
        if n:
            # Data-quality signal (§5.4): surfaces label-convention changes.
            logger.info(
                "Reclassified %d click(s) -> skip via %s marker(s) in `%s`.",
                n,
                list(markers),
                cfg.action_col,
            )
            df.loc[reclassified, "event"] = EVENT_SKIP
        return df

    def _drop_non_behavioural(
        self, df: pd.DataFrame, keys: pd.Series
    ) -> pd.DataFrame:
        """Drop quarantine families quietly; warn on truly-unknown types."""
        unmapped = df["event"].isna()
        if not unmapped.any():
            return df

        is_quarantine = keys.apply(self._is_quarantine)
        n_known = int((unmapped & is_quarantine).sum())
        if n_known:
            logger.debug("Dropping %d infrastructure/lifecycle event(s).", n_known)

        truly_unknown = unmapped & ~is_quarantine
        n_unknown = int(truly_unknown.sum())
        if n_unknown:
            sample = (
                df.loc[truly_unknown, self.config.event_type_col]
                .astype(str)
                .value_counts()
                .head(10)
                .to_dict()
            )
            logger.warning(
                "Quarantining %d event(s) with UNKNOWN raw type (top: %s).",
                n_unknown,
                sample,
            )
        return df.loc[~unmapped].copy()

    @staticmethod
    def _is_quarantine(key: str) -> bool:
        return key in QUARANTINE_EVENT_TYPES or key.startswith(
            QUARANTINE_EVENT_PREFIXES
        )

    def _normalise_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        df["customerId"] = df[cfg.customer_col].astype("string")
        df["sessionId"] = (
            df[cfg.session_col].astype("string")
            if cfg.session_col in df.columns
            else pd.Series(pd.NA, index=df.index, dtype="string")
        )
        campaign = (
            df[cfg.campaign_col].astype("string")
            if cfg.campaign_col in df.columns
            else pd.Series(pd.NA, index=df.index, dtype="string")
        )
        df["campaign"] = campaign.fillna(UNKNOWN_CAMPAIGN)
        return df

    def _coerce_timestamp(self, df: pd.DataFrame) -> pd.DataFrame:
        """Coerce epoch-ms timestamps; drop malformed rows (event_schema §9)."""
        df["event_timestamp"] = pd.to_numeric(
            df[self.config.timestamp_col], errors="coerce"
        )
        bad = int(df["event_timestamp"].isna().sum())
        if bad:
            logger.warning(
                "Dropping %d event(s) with non-numeric `%s`.",
                bad,
                self.config.timestamp_col,
            )
            df = df.loc[df["event_timestamp"].notna()].copy()
        return df

    @staticmethod
    def _add_impression_sequence(df: pd.DataFrame) -> pd.DataFrame:
        """Add ``impression_seq`` / ``is_repeat_impression`` (event_schema §8).

        Per ``(customerId, campaign)`` impressions are numbered 1..N
        chronologically; repeats are every exposure after the first.
        """
        df = df.sort_values("event_timestamp", kind="stable").copy()
        is_impr = df["event"].eq(EVENT_IMPRESSION)
        seq = (
            df.loc[is_impr]
            .groupby(["customerId", "campaign"], sort=False)
            .cumcount()
            + 1
        )
        df["impression_seq"] = seq  # aligns by index; NaN for non-impressions
        df["is_repeat_impression"] = df["impression_seq"].gt(1).fillna(False)
        return df

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in _CLASSIFIED_COLUMNS})


# ===========================================================================
# Customer profile schema  (analytics_contract.md §6 — column order binding)
# ===========================================================================


@dataclass
class CustomerProfile:
    """Per-customer behavioural profile (one output row), schema = §6.

    Percentages are 0-100; ``attention_score`` / ``exploration_score`` /
    ``campaign_diversity_score`` are 0-1. Fields not computable from the
    current telemetry carry ``pd.NA`` / ``NaN`` / ``None`` placeholders (§2/§4).
    """

    customerId: str
    first_seen: Optional[pd.Timestamp] = None
    last_seen: Optional[pd.Timestamp] = None

    # --- Core funnel counts ----------------------------------------------
    total_impressions: int = 0
    total_clicks: int = 0
    total_skips: Optional[int] = None        # label-derived (§3); NA if absent
    total_conversions: Optional[int] = None  # §4 placeholder (no telemetry)
    repeat_impressions: int = 0
    unique_campaigns_seen: int = 0
    unique_campaigns_clicked: int = 0

    # --- Rate metrics (0-100 %) ------------------------------------------
    ctr: float = np.nan
    skip_rate: float = np.nan          # label-derived (§3)
    conversion_rate: float = np.nan    # §4 placeholder
    repeat_impression_rate: float = np.nan

    # --- Time metrics (seconds) ------------------------------------------
    avg_time_to_click_sec: float = np.nan
    avg_time_to_skip_sec: float = np.nan   # label-derived (§3)
    avg_session_depth: float = np.nan

    # --- Fatigue (§4 placeholder: needs population + temporal CTR decline)-
    fatigue_score: float = np.nan

    # --- Engagement ------------------------------------------------------
    # NOTE: despite the contract name, `attention_score` is a behavioural
    # REACTION RATIO = clicks / (clicks + skips). It measures the share of
    # reactions that were clicks; it is NOT a psychological "attention"/dwell
    # signal (the telemetry cannot observe attention). Label-derived via skips.
    attention_score: float = np.nan
    first_impression_success: bool = False

    # --- Exploration -----------------------------------------------------
    exploration_score: float = np.nan  # 0-1

    # --- Segmentation (§4/§5 placeholder: needs population) ---------------
    segments: List[str] = field(default_factory=list)
    primary_segment: Optional[str] = None

    # --- Lineage ---------------------------------------------------------
    profile_updated_at: Optional[pd.Timestamp] = None

    # --- Extended supported metrics (§3; appended AFTER core §6 fields) ---
    avg_impressions_before_click: float = np.nan
    campaign_diversity_score: float = np.nan   # alias of exploration_score
    first_impression_success_rate: float = np.nan  # per-customer (§3 #13)
    delayed_responder_flag: bool = False

    @classmethod
    def column_order(cls) -> List[str]:
        """Binding §6 column order (dataclass field order)."""
        return [f.name for f in fields(cls)]


# ===========================================================================
# 3. FeatureExtractor  (Feature Extraction Layer — analytics_contract §3/§6)
# ===========================================================================


@dataclass(frozen=True)
class FeatureExtractorConfig:
    """Config-driven thresholds & capability switches (analytics_contract §3/§8).

    Attributes
    ----------
    compute_latency_metrics:
        When True, compute ``avg_time_to_click_sec`` / ``avg_time_to_skip_sec``
        — the elapsed time between the impression log and the reaction log.
        This is a measured *event-gap latency*, NOT dwell/attention/read time
        (which the telemetry cannot observe). Disable to avoid surfacing a value
        that is easily mis-read as an attention signal.
    compute_behavioural_flags:
        When True, emit ``delayed_responder_flag``. This is a threshold-derived
        *label* (``impressions_before_click`` / ``time_to_click`` over a cutoff).
        It is OFF by default: such interpretation belongs in the scores /
        observations layer (composite, explainable), not as a fact column on the
        profile. When off, the flag is emitted as ``False`` and a note is logged.
    delayed_response_seconds, delayed_impressions_threshold:
        Cutoffs backing ``delayed_responder_flag`` when it is enabled.
    """

    compute_latency_metrics: bool = True
    compute_behavioural_flags: bool = False
    delayed_response_seconds: float = 60.0
    delayed_impressions_threshold: int = 3


class FeatureExtractor:
    """Build per-customer profiles from classified events (schema = §6).

    Consumes the tidy frame from :class:`EventClassifier` and returns one row
    per ``customerId``. Supported metrics (§3) are computed; unsupported ones
    (§4) are emitted as placeholders with logged warnings (§2).
    """

    def __init__(self, config: Optional[FeatureExtractorConfig] = None) -> None:
        self.config = config or FeatureExtractorConfig()

    # -- public API --------------------------------------------------------

    def extract(self, classified: pd.DataFrame) -> pd.DataFrame:
        """Return the customer profile DataFrame (binding §6 column order)."""
        if not isinstance(classified, pd.DataFrame):
            raise TypeError(
                f"`classified` must be a DataFrame, got {type(classified).__name__}."
            )
        if classified.empty:
            logger.warning("No usable events; returning empty profile frame.")
            return self._empty_profile()

        work = classified  # already tidy & validated by EventClassifier
        present = EventClassifier.present_events(work)
        self._log_capabilities(present)

        counts = self._event_counts(work, present)
        diversity = self._campaign_diversity(work)
        depth = self._session_depth(work)
        timestamps = self._event_timestamps(work)
        if self.config.compute_latency_metrics:
            click_latency = self._reaction_latency(work, EVENT_CLICK)
            skip_latency = self._reaction_latency(work, EVENT_SKIP)
        else:
            logger.info(
                "Latency metrics disabled (avg_time_to_click_sec / "
                "avg_time_to_skip_sec emitted as NaN)."
            )
            click_latency = pd.Series(dtype="float64")
            skip_latency = pd.Series(dtype="float64")

        # First-exposure analysis computed ONCE, reused by three metrics.
        exposure = self._first_click_exposure(work)
        fis = self._first_impression_success(exposure)
        fis_rate = self._first_impression_success_rate(exposure)
        impressions_before_click = self._avg_impressions_before_click(exposure)

        profile = self._assemble(
            present=present,
            counts=counts,
            diversity=diversity,
            depth=depth,
            timestamps=timestamps,
            click_latency=click_latency,
            skip_latency=skip_latency,
            first_impression_success=fis,
            first_impression_success_rate=fis_rate,
            avg_impressions_before_click=impressions_before_click,
        )
        logger.info(
            "Extracted %d customer profile(s) from %d events.", len(profile), len(work)
        )
        return profile

    # -- capability gating (§2/§4) ----------------------------------------

    @staticmethod
    def _log_capabilities(present: Set[str]) -> None:
        """Warn about every contract metric the current telemetry cannot yield."""
        if EVENT_SKIP not in present:
            logger.warning(
                "No `skip` telemetry (no label skip-markers found): total_skips, "
                "skip_rate, avg_time_to_skip_sec, attention_score -> placeholders "
                "(analytics_contract §4)."
            )
        if EVENT_CONVERSION not in present:
            logger.warning(
                "No `conversion` telemetry (event_schema §10 future): "
                "total_conversions, conversion_rate -> placeholders (§4)."
            )
        # Metrics that are unavailable regardless of this snapshot — explained
        # so callers know they are intentionally NOT customer-profile columns.
        logger.warning(
            "Unsupported metrics emitted as placeholders / not on customer grain "
            "(analytics_contract §4): conversion_rate (no conversions); "
            "loyalty_score (no contract formula defined); fatigue_score (needs "
            "population min-max norm + temporal CTR decline — Analytics Engine); "
            "campaign_fatigue_index (campaign grain, needs population); "
            "engagement_momentum (multi-period grain); segmentation outputs "
            "segments/primary_segment (need multi-customer population)."
        )

    # -- component computations (each returns a per-customer frame/series) -

    @staticmethod
    def _event_counts(work: pd.DataFrame, present: Set[str]) -> pd.DataFrame:
        """Funnel counts; absent canonical events -> ``pd.NA`` (not 0)."""
        counts = work.groupby(["customerId", "event"]).size().unstack(fill_value=0)
        counts = counts.reindex(columns=sorted(CANONICAL_EVENTS), fill_value=0)

        out = pd.DataFrame(index=counts.index)
        out.index.name = "customerId"
        out["total_impressions"] = counts[EVENT_IMPRESSION].astype("Int64")
        out["total_clicks"] = counts[EVENT_CLICK].astype("Int64")
        out["total_skips"] = (
            counts[EVENT_SKIP].astype("Int64") if EVENT_SKIP in present else pd.NA
        )
        out["total_conversions"] = (
            counts[EVENT_CONVERSION].astype("Int64")
            if EVENT_CONVERSION in present
            else pd.NA
        )
        out["repeat_impressions"] = (
            work.groupby("customerId", sort=False)["is_repeat_impression"]
            .sum()
            .astype("Int64")
        )
        return out

    @staticmethod
    def _campaign_diversity(work: pd.DataFrame) -> pd.DataFrame:
        """Distinct campaigns seen (impressions) and clicked."""
        seen = (
            work.loc[work["event"].eq(EVENT_IMPRESSION)]
            .groupby("customerId", sort=False)["campaign"]
            .nunique()
        )
        clicked = (
            work.loc[work["event"].eq(EVENT_CLICK)]
            .groupby("customerId", sort=False)["campaign"]
            .nunique()
        )
        out = pd.DataFrame(index=work["customerId"].drop_duplicates())
        out.index.name = "customerId"
        out["unique_campaigns_seen"] = (
            seen.reindex(out.index).fillna(0).astype("Int64")
        )
        out["unique_campaigns_clicked"] = (
            clicked.reindex(out.index).fillna(0).astype("Int64")
        )
        return out

    @classmethod
    def _session_depth(cls, work: pd.DataFrame) -> pd.Series:
        """Avg events per distinct session (analytics_contract §3)."""
        grp = work.groupby("customerId", sort=False)
        depth = cls._safe_ratio(grp["event"].size(), grp["sessionId"].nunique())
        depth.name = "avg_session_depth"
        return depth

    @staticmethod
    def _event_timestamps(work: pd.DataFrame) -> pd.DataFrame:
        """first_seen (first impression) and last_seen (latest event), UTC."""
        impressions = work.loc[work["event"].eq(EVENT_IMPRESSION)]
        first_ms = impressions.groupby("customerId", sort=False)[
            "event_timestamp"
        ].min()
        last_ms = work.groupby("customerId", sort=False)["event_timestamp"].max()

        out = pd.DataFrame(index=work["customerId"].drop_duplicates())
        out.index.name = "customerId"
        out["first_seen"] = pd.to_datetime(
            first_ms.reindex(out.index), unit="ms", utc=True
        )
        out["last_seen"] = pd.to_datetime(
            last_ms.reindex(out.index), unit="ms", utc=True
        )
        return out

    @staticmethod
    def _reaction_latency(work: pd.DataFrame, reaction: str) -> pd.Series:
        """Mean seconds from each click/skip to its parent impression (§1.5).

        Backward ``merge_asof`` keyed by ``(customerId, campaign)`` attaches
        each reaction to the closest preceding impression of the same campaign.
        Orphans yield NaN and drop out of the mean.
        """
        impressions = (
            work.loc[
                work["event"].eq(EVENT_IMPRESSION),
                ["customerId", "campaign", "event_timestamp"],
            ]
            .assign(_impression_ts=lambda d: d["event_timestamp"])
            .sort_values("event_timestamp", kind="stable")
        )
        reactions = work.loc[
            work["event"].eq(reaction),
            ["customerId", "campaign", "event_timestamp"],
        ].sort_values("event_timestamp", kind="stable")
        if reactions.empty or impressions.empty:
            return pd.Series(dtype="float64")

        merged = pd.merge_asof(
            reactions,
            impressions,
            on="event_timestamp",
            by=["customerId", "campaign"],
            direction="backward",
        )
        latency_sec = (merged["event_timestamp"] - merged["_impression_ts"]) / 1000.0
        latency_sec = latency_sec.set_axis(merged["customerId"].to_numpy())
        return latency_sec.groupby(level=0).mean()

    @staticmethod
    def _first_click_exposure(work: pd.DataFrame) -> pd.DataFrame:
        """One row per clicked ``(customerId, campaign)`` describing the FIRST
        click relative to the campaign's exposures (backs three metrics).

        Columns: customerId, campaign, impressions_at_first_click (>=1),
        clicked_on_first (bool). Empty when no clicks/impressions to pair.
        """
        impressions = work.loc[
            work["event"].eq(EVENT_IMPRESSION),
            ["customerId", "campaign", "event_timestamp", "impression_seq"],
        ].sort_values("event_timestamp", kind="stable")
        first_clicks = (
            work.loc[work["event"].eq(EVENT_CLICK)]
            .sort_values("event_timestamp", kind="stable")
            .groupby(["customerId", "campaign"], sort=False, as_index=False)
            .first()[["customerId", "campaign", "event_timestamp"]]
            .sort_values("event_timestamp", kind="stable")
        )
        cols = [
            "customerId",
            "campaign",
            "impressions_at_first_click",
            "clicked_on_first",
        ]
        if first_clicks.empty or impressions.empty:
            return pd.DataFrame(columns=cols)

        merged = pd.merge_asof(
            first_clicks,
            impressions,
            on="event_timestamp",
            by=["customerId", "campaign"],
            direction="backward",
        )
        merged = merged.loc[merged["impression_seq"].notna()].copy()  # drop orphans
        merged["impressions_at_first_click"] = merged["impression_seq"]
        merged["clicked_on_first"] = merged["impression_seq"].eq(1)
        return merged[cols]

    @staticmethod
    def _first_impression_success(exposure: pd.DataFrame) -> pd.Series:
        """True if the customer clicked ANY campaign on its first exposure."""
        if exposure.empty:
            return pd.Series(dtype="boolean")
        return (
            exposure.groupby("customerId", sort=False)["clicked_on_first"]
            .any()
            .astype("boolean")
        )

    @staticmethod
    def _first_impression_success_rate(exposure: pd.DataFrame) -> pd.Series:
        """Per-customer % of CLICKED campaigns clicked on first exposure (§3 #13)."""
        if exposure.empty:
            return pd.Series(dtype="float64")
        return (
            exposure.groupby("customerId", sort=False)["clicked_on_first"].mean()
            * 100.0
        )

    @staticmethod
    def _avg_impressions_before_click(exposure: pd.DataFrame) -> pd.Series:
        """Mean exposures before first click across clicked campaigns (§3 #12)."""
        if exposure.empty:
            return pd.Series(dtype="float64")
        return exposure.groupby("customerId", sort=False)[
            "impressions_at_first_click"
        ].mean()

    # -- ratio helper & assembly ------------------------------------------

    @staticmethod
    def _safe_ratio(
        numerator: pd.Series, denominator: pd.Series, scale: float = 1.0
    ) -> pd.Series:
        """``num / den * scale`` with a zero-denominator guard -> NaN (§8)."""
        num = numerator.astype("float64")
        den = denominator.astype("float64")
        result = np.where(
            den > 0, num / den.where(den > 0, np.nan) * scale, np.nan
        )
        return pd.Series(result, index=numerator.index, dtype="float64")

    def _assemble(
        self,
        present: Set[str],
        counts: pd.DataFrame,
        diversity: pd.DataFrame,
        depth: pd.Series,
        timestamps: pd.DataFrame,
        click_latency: pd.Series,
        skip_latency: pd.Series,
        first_impression_success: pd.Series,
        first_impression_success_rate: pd.Series,
        avg_impressions_before_click: pd.Series,
    ) -> pd.DataFrame:
        """Join components and derive rate / engagement / exploration metrics."""
        profile = (
            counts.join(diversity, how="outer")
            .join(timestamps, how="left")
            .join(depth, how="left")
        )
        impressions = profile["total_impressions"]
        clicks = profile["total_clicks"]

        # --- Rate metrics (§3) -------------------------------------------
        profile["ctr"] = self._safe_ratio(clicks, impressions, scale=100.0)
        profile["repeat_impression_rate"] = self._safe_ratio(
            profile["repeat_impressions"], impressions, scale=100.0
        )

        if EVENT_SKIP in present:
            profile["skip_rate"] = self._safe_ratio(
                profile["total_skips"], impressions, scale=100.0
            )
            profile["attention_score"] = self._safe_ratio(
                clicks, clicks + profile["total_skips"]
            )
            profile["avg_time_to_skip_sec"] = skip_latency.reindex(profile.index)
        else:  # §4 placeholder
            profile["skip_rate"] = np.nan
            profile["attention_score"] = np.nan
            profile["avg_time_to_skip_sec"] = np.nan

        if EVENT_CONVERSION in present:
            profile["conversion_rate"] = self._safe_ratio(
                profile["total_conversions"], clicks, scale=100.0
            )
        else:  # §4 placeholder
            profile["conversion_rate"] = np.nan

        # --- Time-based ---------------------------------------------------
        profile["avg_time_to_click_sec"] = click_latency.reindex(profile.index)

        # --- Engagement / exploration ------------------------------------
        profile["exploration_score"] = self._safe_ratio(
            profile["unique_campaigns_clicked"], profile["unique_campaigns_seen"]
        )
        profile["campaign_diversity_score"] = profile["exploration_score"]  # alias
        profile["first_impression_success"] = (
            first_impression_success.reindex(profile.index)
            .fillna(False)
            .astype(bool)
        )

        # --- Extended supported metrics (§3) -----------------------------
        profile["avg_impressions_before_click"] = (
            avg_impressions_before_click.reindex(profile.index)
        )
        profile["first_impression_success_rate"] = (
            first_impression_success_rate.reindex(profile.index)
        )
        # delayed_responder_flag is a threshold-derived behavioural *label*. It
        # is OFF by default — interpretation belongs in the scores/observations
        # layer, not as a fact column (see FeatureExtractorConfig). When enabled,
        # comparisons against NaN are False, so non-clickers are never flagged.
        if self.config.compute_behavioural_flags:
            ibc = profile["avg_impressions_before_click"]
            ttc = profile["avg_time_to_click_sec"]
            profile["delayed_responder_flag"] = (
                (ibc > self.config.delayed_impressions_threshold)
                | (ttc > self.config.delayed_response_seconds)
            ).astype(bool)
        else:
            logger.info(
                "Behavioural labelling disabled (delayed_responder_flag=False); "
                "use the scores/observations layer for interpretation."
            )
            profile["delayed_responder_flag"] = False

        # --- §4 placeholders owned by other layers -----------------------
        profile["fatigue_score"] = np.nan
        profile["segments"] = [[] for _ in range(len(profile))]
        profile["primary_segment"] = None
        profile["profile_updated_at"] = pd.Timestamp.now(tz="UTC")

        return self._finalise(profile)

    @staticmethod
    def _finalise(profile: pd.DataFrame) -> pd.DataFrame:
        """Reset index and pin the binding §6 column order."""
        profile = profile.reset_index()
        ordered = CustomerProfile.column_order()
        for col in ordered:
            if col not in profile.columns:
                profile[col] = np.nan
        return profile[ordered]

    @staticmethod
    def _empty_profile() -> pd.DataFrame:
        return pd.DataFrame(
            {c: pd.Series(dtype="object") for c in CustomerProfile.column_order()}
        )


# ===========================================================================
# MetricCalculator  (Task 1 — generic, directly-calculable metrics)
# ===========================================================================

#: Per-customer numeric metrics summarised into the dataset averages. These are
#: ALL directly calculable from logged events — no dwell/attention/intent.
GENERIC_NUMERIC_METRICS: tuple = (
    "event_count",
    "impression_count",
    "click_count",
    "skip_count",
    "campaign_served_count",
    "campaign_received_count",
    "session_count",
    "unique_campaign_count",
    "unique_screen_count",
    "ctr",
    "click_rate",
    "exposure_frequency",
    "interaction_frequency",
    "average_events_per_session",
    "campaign_diversity",
    "repeat_interaction_rate",
)

#: Subset of :data:`GENERIC_NUMERIC_METRICS` that are integer counts (the rest
#: are float rates). Used when emitting capability-gated ``<NA>`` placeholders.
_GENERIC_INT_METRICS: FrozenSet[str] = frozenset(
    {
        "event_count",
        "impression_count",
        "click_count",
        "skip_count",
        "campaign_served_count",
        "campaign_received_count",
        "session_count",
        "unique_campaign_count",
        "unique_screen_count",
    }
)


@dataclass
class MetricResult:
    """Output of :class:`MetricCalculator`.

    Attributes
    ----------
    customer_metrics:
        One row per customer; columns are :data:`GENERIC_NUMERIC_METRICS` plus
        ``customerId``, ``campaigns_reached`` and an ``event_distribution`` dict.
    dataset_summary:
        Population-level aggregates: counts, global ``event_distribution``,
        ``campaign_reach`` (campaign -> distinct customers) and
        ``metric_averages`` (mean of each numeric metric) for comparisons.
    """

    customer_metrics: pd.DataFrame
    dataset_summary: Dict[str, Any]


class MetricCalculator:
    """Compute directly-calculable, domain-agnostic metrics over raw telemetry.

    Unlike :class:`FeatureExtractor` (which produces the floater-contract §6
    profile from the behavioural funnel only), this operates on **all** events
    via the :class:`SemanticSchema`, so it generalises to any domain. It emits
    only evidence-grounded metrics (counts, ratios, frequencies, diversity,
    distribution) and never infers attention, dwell, intent or journeys.
    """

    def __init__(self, schema: Optional[SemanticSchema] = None) -> None:
        self.schema = schema or load_semantic_schema()

    # -- public API --------------------------------------------------------

    def compute(self, raw: pd.DataFrame) -> MetricResult:
        """Return per-customer metrics and a dataset summary for ``raw``."""
        if not isinstance(raw, pd.DataFrame):
            raise TypeError(
                f"`raw` must be a pandas DataFrame, got {type(raw).__name__}."
            )
        work = self._normalise(raw)
        if work.empty:
            logger.warning("No usable rows for metric calculation.")
            return MetricResult(self._empty_metrics(), self._empty_summary())

        customer_metrics = self._per_customer(work)
        dataset_summary = self._summary(work, customer_metrics)
        logger.info(
            "Computed generic metrics for %d customer(s) over %d events.",
            len(customer_metrics),
            len(work),
        )
        unavailable = dataset_summary.get("unavailable_metrics", [])
        if unavailable:
            logger.warning(
                "Schema '%s' does not support %d metric(s): %s. They are emitted "
                "as <NA> (not 0). Map the missing role/column in "
                "configs/semantic_mappings.yaml to enable them.",
                self.schema.dataset,
                len(unavailable),
                unavailable,
            )
        return MetricResult(customer_metrics, dataset_summary)

    # -- capability gating -------------------------------------------------

    def _capabilities(self) -> Dict[str, bool]:
        """Which metric inputs the active schema can actually supply.

        Capability is a property of the **schema**, not the data: if a role or
        column is not mapped, the corresponding metric is *unavailable* (emitted
        as ``<NA>``) rather than a misleading ``0``.
        """
        s = self.schema
        roles = set(s.event_roles.values())
        return {
            "impression": "impression" in roles,
            "click": "click" in roles,
            # skip is derivable from a native skip role OR from click+markers.
            "skip": ("skip" in roles) or (bool(s.skip_markers) and "click" in roles),
            "served": "campaign_served" in roles,
            "received": "campaign_received" in roles,
            "session": s.col("session_id") is not None,
            "campaign": s.col("campaign") is not None,
            "screen": s.col("screen") is not None,
            "timestamp": s.col("timestamp") is not None,
        }

    @staticmethod
    def _metric_gates(caps: Mapping[str, bool]) -> Dict[str, bool]:
        """Map each generic metric to whether its inputs are available."""
        return {
            "event_count": True,
            "impression_count": caps["impression"],
            "click_count": caps["click"],
            "skip_count": caps["skip"],
            "campaign_served_count": caps["served"],
            "campaign_received_count": caps["received"],
            "session_count": caps["session"],
            "unique_campaign_count": caps["campaign"],
            "unique_screen_count": caps["screen"],
            "ctr": caps["click"] and caps["impression"],
            "click_rate": caps["click"],
            "exposure_frequency": caps["impression"] and caps["session"],
            "interaction_frequency": (caps["click"] or caps["skip"]) and caps["session"],
            "average_events_per_session": caps["session"],
            "campaign_diversity": caps["campaign"],
            "repeat_interaction_rate": (
                caps["impression"] and caps["campaign"] and caps["timestamp"]
            ),
        }

    # -- normalisation -----------------------------------------------------

    def _normalise(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Project raw telemetry onto canonical role columns (schema-driven)."""
        s = self.schema
        out = pd.DataFrame(index=raw.index)
        out["customerId"] = raw[s.require("customer_id")].astype("string")

        roles = raw[s.require("event_type")].map(_norm_key).map(s.event_roles)
        out["role"] = roles.fillna("other")

        # A click whose action label marks a dismissal is a skip (schema rule).
        action_col = s.col("action")
        if action_col and action_col in raw.columns and s.skip_markers:
            markers = tuple(s.skip_markers)
            action = raw[action_col].map(_norm_key)
            looks_skip = action.apply(lambda a: any(m in a for m in markers))
            out.loc[out["role"].eq("click") & looks_skip, "role"] = "skip"

        session_col = s.col("session_id")
        out["sessionId"] = (
            raw[session_col].astype("string")
            if session_col and session_col in raw.columns
            else pd.Series(pd.NA, index=raw.index, dtype="string")
        )
        campaign_col = s.col("campaign")
        out["campaign"] = (
            raw[campaign_col].astype("string")
            if campaign_col and campaign_col in raw.columns
            else pd.Series(pd.NA, index=raw.index, dtype="string")
        )
        screen_col = s.col("screen")
        out["screen"] = (
            raw[screen_col].astype("string")
            if screen_col and screen_col in raw.columns
            else pd.Series(pd.NA, index=raw.index, dtype="string")
        )
        ts_col = s.col("timestamp")
        out["ts"] = (
            pd.to_numeric(raw[ts_col], errors="coerce")
            if ts_col and ts_col in raw.columns
            else np.nan
        )
        return out.loc[out["customerId"].notna()].copy()

    # -- per-customer metrics ---------------------------------------------

    def _per_customer(self, work: pd.DataFrame) -> pd.DataFrame:
        g = work.groupby("customerId", sort=True)
        index = pd.Index(sorted(work["customerId"].dropna().unique()), name="customerId")
        out = pd.DataFrame(index=index)

        def role_count(role: str) -> pd.Series:
            return (
                work.loc[work["role"].eq(role)]
                .groupby("customerId")
                .size()
                .reindex(index)
                .fillna(0)
                .astype("Int64")
            )

        out["event_count"] = g.size().reindex(index).fillna(0).astype("Int64")
        out["impression_count"] = role_count("impression")
        out["click_count"] = role_count("click")
        out["skip_count"] = role_count("skip")
        out["campaign_served_count"] = role_count("campaign_served")
        out["campaign_received_count"] = role_count("campaign_received")
        out["session_count"] = (
            g["sessionId"].nunique().reindex(index).fillna(0).astype("Int64")
        )

        # Campaign attribution requires a campaign-related event: `click_action`
        # is overloaded (it carries navigation actions / ids on `other` rows),
        # so only recognised roles contribute to campaign metrics.
        campaign_events = work.loc[work["campaign"].notna() & work["role"].ne("other")]
        out["unique_campaign_count"] = (
            campaign_events.groupby("customerId")["campaign"]
            .nunique()
            .reindex(index)
            .fillna(0)
            .astype("Int64")
        )
        out["unique_screen_count"] = (
            work.loc[work["screen"].notna()]
            .groupby("customerId")["screen"]
            .nunique()
            .reindex(index)
            .fillna(0)
            .astype("Int64")
        )

        impressions = out["impression_count"].astype("float64")
        clicks = out["click_count"].astype("float64")
        skips = out["skip_count"].astype("float64")
        events = out["event_count"].astype("float64")
        sessions = out["session_count"].astype("float64")
        campaign_bearing = (
            campaign_events.groupby("customerId")
            .size()
            .reindex(index)
            .fillna(0)
            .astype("float64")
        )

        out["ctr"] = _safe_div(clicks, impressions, scale=100.0)
        out["click_rate"] = _safe_div(clicks, events)               # share 0..1
        out["exposure_frequency"] = _safe_div(impressions, sessions)
        out["interaction_frequency"] = _safe_div(clicks + skips, sessions)
        out["average_events_per_session"] = _safe_div(events, sessions)
        out["campaign_diversity"] = _safe_div(
            out["unique_campaign_count"].astype("float64"), campaign_bearing
        )
        out["repeat_interaction_rate"] = _safe_div(
            self._repeat_impression_counts(work).reindex(index).fillna(0),
            impressions,
            scale=100.0,
        )

        # --- capability gating ------------------------------------------------
        # A metric the active schema cannot support is emitted as <NA>/NaN, never
        # a misleading 0. This keeps the calculator correct today and lets future
        # datasets enable metrics simply by mapping more roles/columns in YAML.
        gates = self._metric_gates(self._capabilities())
        n = len(index)
        for metric, supported in gates.items():
            if supported or metric not in out.columns:
                continue
            out[metric] = (
                pd.array([pd.NA] * n, dtype="Int64")
                if metric in _GENERIC_INT_METRICS
                else pd.Series(np.nan, index=index, dtype="float64")
            )

        out["campaigns_reached"] = out["unique_campaign_count"]
        dist = self._event_distribution_by_customer(work)
        out["event_distribution"] = pd.Series(
            {c: dist.get(c, {}) for c in index}, dtype="object"
        )
        return out.reset_index()

    @staticmethod
    def _repeat_impression_counts(work: pd.DataFrame) -> pd.Series:
        """Per-customer count of repeat exposures (same campaign seen again)."""
        imps = work.loc[
            work["role"].eq("impression") & work["campaign"].notna()
        ].sort_values("ts", kind="stable")
        if imps.empty:
            return pd.Series(dtype="float64")
        seq = imps.groupby(["customerId", "campaign"], sort=False).cumcount() + 1
        imps = imps.assign(_is_repeat=seq.gt(1).to_numpy())
        return imps.groupby("customerId")["_is_repeat"].sum().astype("float64")

    @staticmethod
    def _event_distribution_by_customer(
        work: pd.DataFrame,
    ) -> Dict[str, Dict[str, float]]:
        """Per-customer proportion of each canonical role (sums to 1)."""
        counts = work.groupby(["customerId", "role"]).size()
        result: Dict[str, Dict[str, float]] = {}
        for customer, sub in counts.groupby(level=0):
            total = float(sub.sum())
            result[customer] = {
                role: round(float(c) / total, 4) for (_, role), c in sub.items()
            }
        return result

    # -- dataset summary ---------------------------------------------------

    def _summary(
        self, work: pd.DataFrame, customer_metrics: pd.DataFrame
    ) -> Dict[str, Any]:
        role_counts = work["role"].value_counts()
        total = float(role_counts.sum()) or 1.0
        event_distribution = {
            role: round(float(c) / total, 4) for role, c in role_counts.items()
        }
        campaign_events = work.loc[work["campaign"].notna() & work["role"].ne("other")]
        campaign_reach = (
            campaign_events.groupby("campaign")["customerId"]
            .nunique()
            .astype(int)
            .to_dict()
        )
        caps = self._capabilities()
        gates = self._metric_gates(caps)
        # Averages only for metrics the schema supports (unsupported are <NA>).
        metric_averages = {
            m: float(customer_metrics[m].astype("float64").mean())
            for m in GENERIC_NUMERIC_METRICS
            if m in customer_metrics.columns and gates.get(m, True)
        }
        return {
            "n_customers": int(customer_metrics["customerId"].nunique()),
            "n_events": int(len(work)),
            "n_sessions": int(work["sessionId"].nunique()),
            "n_campaigns": int(campaign_events["campaign"].nunique()),
            "event_distribution": event_distribution,
            "campaign_reach": campaign_reach,
            "metric_averages": metric_averages,
            "capabilities": caps,
            "unavailable_metrics": sorted(m for m, ok in gates.items() if not ok),
        }

    # -- empties -----------------------------------------------------------

    @staticmethod
    def _empty_metrics() -> pd.DataFrame:
        cols = ["customerId", *GENERIC_NUMERIC_METRICS, "campaigns_reached",
                "event_distribution"]
        return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})

    @staticmethod
    def _empty_summary() -> Dict[str, Any]:
        return {
            "n_customers": 0,
            "n_events": 0,
            "n_sessions": 0,
            "n_campaigns": 0,
            "event_distribution": {},
            "campaign_reach": {},
            "metric_averages": {},
            "capabilities": {},
            "unavailable_metrics": [],
        }


# ===========================================================================
# Convenience orchestrator
# ===========================================================================


def extract_customer_profiles(
    path: Union[str, Path],
    *,
    classifier_config: Optional[EventClassifierConfig] = None,
    extractor_config: Optional[FeatureExtractorConfig] = None,
) -> pd.DataFrame:
    """Load -> classify -> extract in one call (end-to-end convenience).

    Parameters
    ----------
    path:
        Path to the raw telemetry export (XLSX-disguised-as-CSV supported).
    classifier_config, extractor_config:
        Optional overrides; FROZEN-schema defaults used when ``None``.

    Returns
    -------
    pandas.DataFrame
        One row per ``customerId``, columns in binding §6 order.
    """
    raw = TelemetryLoader().load(path)
    classified = EventClassifier(classifier_config).classify(raw)
    return FeatureExtractor(extractor_config).extract(classified)


# ===========================================================================
# Example usage
# ===========================================================================

if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    sample = sys.argv[1] if len(sys.argv) > 1 else "sample_data/telemetry_sample.csv"

    # End-to-end:
    profiles = extract_customer_profiles(sample)

    # …or step-by-step (each stage is independently unit-testable):
    #   raw        = TelemetryLoader().load(sample)
    #   classified = EventClassifier().classify(raw)
    #   profiles   = FeatureExtractor().extract(classified)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(f"\nCustomer profiles: {profiles.shape[0]} row(s) x {profiles.shape[1]} cols")
    print(profiles.to_string(index=False))

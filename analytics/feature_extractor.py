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
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Union

import numpy as np
import pandas as pd

__all__ = [
    "TelemetryLoader",
    "EventClassifier",
    "EventClassifierConfig",
    "FeatureExtractor",
    "FeatureExtractorConfig",
    "CustomerProfile",
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
    attention_score: float = np.nan   # label-derived (§3)
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
    """Config-driven thresholds (analytics_contract §3 #14 / §8).

    A customer is a "delayed responder" if they typically need more than
    ``delayed_impressions_threshold`` exposures before clicking, OR take longer
    than ``delayed_response_seconds`` to click on average.
    """

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
        click_latency = self._reaction_latency(work, EVENT_CLICK)
        skip_latency = self._reaction_latency(work, EVENT_SKIP)

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
        ibc = profile["avg_impressions_before_click"]
        ttc = profile["avg_time_to_click_sec"]
        # Comparisons against NaN are False, so non-clickers are never flagged.
        profile["delayed_responder_flag"] = (
            (ibc > self.config.delayed_impressions_threshold)
            | (ttc > self.config.delayed_response_seconds)
        ).astype(bool)

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

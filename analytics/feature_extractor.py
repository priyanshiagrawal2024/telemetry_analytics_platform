"""Customer-level feature extraction for the MyJio Floater Analytics Platform.

Layer position (see ``CLAUDE.md`` architecture):

    Telemetry Events
        -> Ingestion Layer
        -> Preprocessing Layer
        -> Feature Extraction      <-- THIS MODULE
        -> Analytics Engine
        -> Insight Generation
        -> Dashboard

This module consumes a *cleaned* telemetry event :class:`pandas.DataFrame` and
produces a **customer profile** :class:`pandas.DataFrame` whose schema matches
**§5 Customer Profile Schema** of ``contracts/analytics_contract.md`` exactly.
The profile is the primary input to the Segmentation step.

Source-of-truth documents (no metric / mapping / schema is invented outside
them):
* ``contracts/analytics_contract.md`` — metric formulas (§7/§8), profile
  schema (§5), event taxonomy (§1), event mappings (§2), segmentation (§9),
  calculation conventions (§13).
* ``contracts/event_schema.md`` — raw telemetry fields (§1/§2), the raw->
  canonical mapping available in the *current* sample (§4) and the events
  reserved for a **future extension** (§6).

Capability gating
-----------------
``event_schema.md`` §4 only maps ``impression`` and ``click`` today; ``skip``
and ``conversion`` are §6 *future* telemetry. Accordingly, any metric whose
source events are absent from the input is emitted as an explicit **placeholder
(``<NA>`` / ``NaN``) with a logged warning** rather than a misleading zero. When
that telemetry arrives, the same code computes the metric with no changes.

This is an **analytics** module: rule-based, explainable, no recommendations,
no personalisation, no ML.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Dict, List, Mapping, Optional, Set

import numpy as np
import pandas as pd

__all__ = [
    "CustomerProfile",
    "FeatureExtractorConfig",
    "extract_features",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical event taxonomy (analytics_contract.md §1)
# ---------------------------------------------------------------------------

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

#: Raw-event -> canonical-event mapping, merged from analytics_contract.md §2
#: and event_schema.md §4. Matched case-insensitively (see ``_normalise_key``).
#: Entries flagged "future" below are valid mappings that simply do not appear
#: in the current telemetry sample (event_schema.md §6).
DEFAULT_EVENT_MAPPING: Dict[str, str] = {
    # --- present in current sample (event_schema.md §4) -------------------
    "recharge floater impression": EVENT_IMPRESSION,
    "recharge floater clicks": EVENT_CLICK,
    "recharge floater click": EVENT_CLICK,
    # --- analytics_contract.md §2 canonical names -------------------------
    "floater_impression": EVENT_IMPRESSION,
    "floater_click": EVENT_CLICK,
    "floater_skip": EVENT_SKIP,            # future (no skip telemetry yet)
    "dismiss_popup": EVENT_SKIP,           # future
    "recharge_success": EVENT_CONVERSION,  # future (event_schema.md §6)
    "ott_subscription_success": EVENT_CONVERSION,  # future
}

#: Known non-behavioural / lifecycle events (event_schema.md §3/§4). They are
#: legitimate telemetry but do not feed any §5 profile metric, so they are
#: dropped quietly (debug-logged) rather than quarantined as unknown.
KNOWN_NON_BEHAVIOURAL: Set[str] = {
    "floaterresponse",
    "campaign_response_received",
    "campaign_response_received_empty",
    "campaign_saved_in_db",
    "floater api called",
    "floater api response received",
}

#: Columns the extractor requires on the input frame.
REQUIRED_COLUMNS: tuple[str, ...] = ("customerId", "event_type", "event_timestamp")

#: Sentinel for a missing campaign label so campaign-scoped logic stays defined.
_UNKNOWN_CAMPAIGN = "__unknown_campaign__"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureExtractorConfig:
    """Tunable inputs for :func:`extract_features`.

    Attributes
    ----------
    event_mapping:
        Raw-event-name -> canonical-event mapping (matched case-insensitively).
    customer_col, session_col, campaign_col, event_type_col, timestamp_col:
        Column names on the input frame. ``timestamp_col`` must be epoch
        **milliseconds** (event_schema.md §1 ``event_timestamp``).
    """

    event_mapping: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_EVENT_MAPPING)
    )
    customer_col: str = "customerId"
    session_col: str = "sessionId"
    campaign_col: str = "label"
    event_type_col: str = "event_type"
    timestamp_col: str = "event_timestamp"

    # Optional column whose value REFINES a mapped event. In the real MyJio
    # export a dismissal is logged as a "...clicks" event whose action label
    # marks it as a skip (e.g. label "Recharge-skip"). When ``action_col`` is
    # set, any mapped ``click`` whose action value contains a
    # ``skip_label_markers`` substring is reclassified to ``skip``
    # (analytics_contract.md §2: dismiss_popup / floater_skip -> skip).
    action_col: Optional[str] = None
    skip_label_markers: frozenset = frozenset({"skip", "dismiss"})

    # Thresholds backing ``delayed_responder_flag`` (see CustomerProfile). A
    # customer is "delayed" if they typically need more than
    # ``delayed_impressions_threshold`` exposures before clicking, OR take longer
    # than ``delayed_response_seconds`` seconds to click after an impression.
    # Both are config-driven per analytics_contract.md §13 (thresholds are
    # configuration, defaults are the contract baseline).
    delayed_response_seconds: float = 60.0
    delayed_impressions_threshold: int = 3

    @classmethod
    def for_myjio_sample(cls) -> "FeatureExtractorConfig":
        """Preset tuned to the real ``sample_data/telemetry_sample.csv`` export.

        Schema realities this preset encodes (discovered from the actual file,
        which is an XLSX workbook despite the ``.csv`` name):

        * The recharge floater is the well-formed behavioural funnel:
          ``Recharge floater impression`` -> impression and
          ``Recharge floater clicks``     -> click (already in
          :data:`DEFAULT_EVENT_MAPPING`).
        * **Skips are embedded in click rows.** A "Recharge floater clicks"
          event whose ``label`` is "Recharge-skip" is a dismissal -> ``skip``.
          We therefore scan ``label`` (``action_col``) for skip markers.
        * **The stable campaign key is ``click_action``** (e.g. "PLANEXPIRY01").
          On a click the ``label`` holds the ACTION ("Recharge-skip",
          "Recharge-Explore all plans"), NOT the campaign, so ``label`` must not
          be used as the campaign key.
        * Conversion events (recharge_success / ott_subscription_success) are
          absent ("Recharge initiated" is intent, not a completed outcome), so
          conversion metrics remain placeholders (event_schema.md §6).
        """
        return cls(
            event_type_col="event_type",
            campaign_col="click_action",
            action_col="label",
            timestamp_col="event_timestamp",
        )


# ---------------------------------------------------------------------------
# Output schema  (analytics_contract.md §5 — column order is binding)
# ---------------------------------------------------------------------------


@dataclass
class CustomerProfile:
    """Per-customer behavioural profile (one output row), schema = §5.

    Percentages are 0-100; ``attention_score``/``exploration_score`` are 0-1.
    Fields that cannot be computed from the current telemetry sample carry
    ``pd.NA`` / ``NaN`` / ``None`` placeholders (see module docstring).
    """

    customerId: str
    first_seen: Optional[pd.Timestamp] = None
    last_seen: Optional[pd.Timestamp] = None

    # --- Event Counts -----------------------------------------------------
    total_impressions: int = 0
    total_clicks: int = 0
    total_skips: Optional[int] = None        # TODO: needs skip telemetry (§6)
    total_conversions: Optional[int] = None  # TODO: needs conversion telemetry (§6)
    repeat_impressions: int = 0
    unique_campaigns_seen: int = 0
    unique_campaigns_clicked: int = 0

    # --- Rate Metrics (0-100, %) -----------------------------------------
    ctr: float = np.nan
    skip_rate: float = np.nan          # TODO: needs skip telemetry (§6)
    conversion_rate: float = np.nan    # TODO: needs conversion telemetry (§6)
    repeat_impression_rate: float = np.nan

    # --- Time-Based Metrics (seconds) ------------------------------------
    avg_time_to_click_sec: float = np.nan
    avg_time_to_skip_sec: float = np.nan  # TODO: needs skip telemetry (§6)
    avg_session_depth: float = np.nan

    # --- Fatigue Metrics --------------------------------------------------
    # TODO: §7.8 composite needs skip_rate AND temporal ctr_decline
    #       (previous_ctr − current_ctr), the latter owned by the Analytics
    #       Engine (period-over-period). Not computable in a single snapshot.
    fatigue_score: float = np.nan

    # --- Engagement Metrics ----------------------------------------------
    attention_score: float = np.nan  # TODO: clicks/(clicks+skips) needs skips
    first_impression_success: bool = False

    # --- Exploration Metrics ---------------------------------------------
    exploration_score: float = np.nan  # 0-1

    # --- Segmentation (filled by the Segmentation module, §9) -------------
    segments: List[str] = field(default_factory=list)  # TODO: segmentation step
    primary_segment: Optional[str] = None               # TODO: segmentation step

    # --- Lineage ----------------------------------------------------------
    profile_updated_at: Optional[pd.Timestamp] = None

    # --- Extended derived metrics (targeted additions; appended AFTER the §5
    #     schema so the binding §5 column order is preserved exactly) ---------
    # Mean impressions seen before a customer's first click (avg over clicked
    # campaigns). Supports delayed_responder_flag. NaN for non-clickers.
    avg_impressions_before_click: float = np.nan
    # unique_campaigns_clicked / unique_campaigns_seen (0-1). NOTE: by formula
    # this equals exploration_score (§8.8); kept as a business-friendly alias.
    campaign_diversity_score: float = np.nan
    # Per-customer % of CLICKED campaigns clicked on first exposure (0-100).
    # Per-customer analogue of the population rate in §8.5. NaN for non-clickers.
    first_impression_success_rate: float = np.nan
    # True if the customer engages slowly: many exposures before clicking OR a
    # long average time-to-click (thresholds in FeatureExtractorConfig).
    delayed_responder_flag: bool = False

    @classmethod
    def column_order(cls) -> List[str]:
        """Return the §5 column order (dataclass field order)."""
        return [f.name for f in fields(cls)]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def extract_features(
    events: pd.DataFrame,
    config: Optional[FeatureExtractorConfig] = None,
) -> pd.DataFrame:
    """Build per-customer profiles (schema = analytics_contract.md §5).

    Parameters
    ----------
    events:
        Cleaned telemetry, one row per event. Must contain at least
        :data:`REQUIRED_COLUMNS`; ``event_timestamp`` is epoch milliseconds.
    config:
        Optional :class:`FeatureExtractorConfig`; defaults used when ``None``.

    Returns
    -------
    pandas.DataFrame
        One row per customer, columns ordered per
        :meth:`CustomerProfile.column_order`. Empty (correctly-typed) frame
        when there are no usable canonical events.

    Raises
    ------
    TypeError
        If ``events`` is not a :class:`pandas.DataFrame`.
    ValueError
        If required columns are missing.
    """
    config = config or FeatureExtractorConfig()
    _validate_input(events, config)

    if events.empty:
        logger.warning("Received empty events frame; returning empty profile.")
        return _empty_profile_frame()

    work = _normalise_events(events, config)
    if work.empty:
        logger.warning("No canonical behavioural events; returning empty profile.")
        return _empty_profile_frame()

    present = set(work["event"].unique())
    _log_capability(present)

    work = _add_impression_sequence(work)

    counts = _event_counts(work, present)
    diversity = _campaign_diversity(work)
    depth = _session_depth(work)
    timestamps = _event_timestamps(work)
    click_latency = _pair_reactions_to_impressions(work, EVENT_CLICK)
    skip_latency = _pair_reactions_to_impressions(work, EVENT_SKIP)

    # First-exposure click analysis is computed ONCE (one as-of join) and reused
    # for three customer metrics, avoiding duplicate work: the success flag, the
    # success rate (%), and the avg impressions seen before the first click.
    exposure = _first_click_exposure(work)
    fis = _first_impression_success(exposure)
    fis_rate = _first_impression_success_rate(exposure)
    impressions_before_click = _avg_impressions_before_click(exposure)

    profile = _assemble_profiles(
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
        config=config,
    )

    logger.info(
        "Extracted %d customer profiles from %d usable events.",
        len(profile),
        len(work),
    )
    return profile


# ---------------------------------------------------------------------------
# Validation & capability logging
# ---------------------------------------------------------------------------


def _validate_input(events: pd.DataFrame, config: FeatureExtractorConfig) -> None:
    """Strict boundary validation with actionable error messages."""
    if not isinstance(events, pd.DataFrame):
        raise TypeError(
            f"`events` must be a pandas DataFrame, got {type(events).__name__}."
        )
    required = set(REQUIRED_COLUMNS) | {
        config.customer_col,
        config.event_type_col,
        config.timestamp_col,
    }
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(
            f"Input events frame is missing required column(s): {missing}. "
            f"Present columns: {sorted(events.columns)}."
        )


def _log_capability(present: Set[str]) -> None:
    """Warn about contract metrics that the current telemetry cannot produce."""
    if EVENT_SKIP not in present:
        logger.warning(
            "No `skip` telemetry present (event_schema.md §6 future extension): "
            "total_skips, skip_rate, avg_time_to_skip_sec, attention_score and "
            "fatigue_score will be emitted as placeholders."
        )
    if EVENT_CONVERSION not in present:
        logger.warning(
            "No `conversion` telemetry present (event_schema.md §6 future "
            "extension): total_conversions and conversion_rate will be "
            "emitted as placeholders."
        )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise_key(value: object) -> str:
    """Normalise a raw event name for mapping lookups (lower + strip)."""
    return str(value).strip().lower()


def _normalise_events(
    events: pd.DataFrame, config: FeatureExtractorConfig
) -> pd.DataFrame:
    """Return a tidy working frame restricted to canonical behavioural events.

    Adds ``event`` (canonical), ``campaign`` (from label), ``sessionId`` and a
    numeric ``event_timestamp`` (ms). Known lifecycle events are dropped
    quietly; truly unknown raw types are quarantined and counted.
    """
    df = events.copy()
    lookup = {_normalise_key(k): v for k, v in config.event_mapping.items()}

    keys = df[config.event_type_col].map(_normalise_key)
    df["event"] = keys.map(lookup)

    # --- label-based skip refinement (see FeatureExtractorConfig.action_col) ---
    # Real telemetry encodes a dismissal as a *click* event whose action label
    # contains a skip marker (e.g. "Recharge-skip"). Reclassify those to `skip`
    # so skip metrics are computed from data that genuinely exists, rather than
    # being treated as engaged clicks.
    if config.action_col and config.action_col in df.columns and config.skip_label_markers:
        markers = tuple(m.strip().lower() for m in config.skip_label_markers if m)
        action_norm = df[config.action_col].map(_normalise_key)
        looks_skip = action_norm.apply(lambda s: any(m in s for m in markers))
        reclassified = df["event"].eq(EVENT_CLICK) & looks_skip
        n_reclass = int(reclassified.sum())
        if n_reclass:
            logger.info(
                "Reclassified %d click event(s) as `skip` via %s marker(s) in `%s`.",
                n_reclass, list(markers), config.action_col,
            )
            df.loc[reclassified, "event"] = EVENT_SKIP

    unmapped = df["event"].isna()
    if unmapped.any():
        non_behavioural = unmapped & keys.isin(KNOWN_NON_BEHAVIOURAL)
        n_known = int(non_behavioural.sum())
        if n_known:
            logger.debug("Dropping %d known non-behavioural event(s).", n_known)

        truly_unknown = unmapped & ~keys.isin(KNOWN_NON_BEHAVIOURAL)
        n_unknown = int(truly_unknown.sum())
        if n_unknown:
            sample = (
                df.loc[truly_unknown, config.event_type_col]
                .astype(str).value_counts().head(10).to_dict()
            )
            logger.warning(
                "Quarantining %d event(s) with unknown raw types (top: %s).",
                n_unknown, sample,
            )
        df = df.loc[~unmapped].copy()

    if df.empty:
        return df.iloc[0:0]

    df["customerId"] = df[config.customer_col].astype("string")
    df["sessionId"] = (
        df[config.session_col].astype("string")
        if config.session_col in df.columns
        else pd.Series(pd.NA, index=df.index, dtype="string")
    )
    campaign = (
        df[config.campaign_col].astype("string")
        if config.campaign_col in df.columns
        else pd.Series(pd.NA, index=df.index, dtype="string")
    )
    df["campaign"] = campaign.fillna(_UNKNOWN_CAMPAIGN)

    df["event_timestamp"] = pd.to_numeric(df[config.timestamp_col], errors="coerce")
    bad_ts = int(df["event_timestamp"].isna().sum())
    if bad_ts:
        logger.warning(
            "Dropping %d event(s) with non-numeric `%s`.", bad_ts, config.timestamp_col
        )
        df = df.loc[df["event_timestamp"].notna()].copy()

    return df[["customerId", "sessionId", "campaign", "event", "event_timestamp"]]


def _add_impression_sequence(work: pd.DataFrame) -> pd.DataFrame:
    """Add ``impression_seq`` / ``is_repeat_impression`` (analytics §3.2).

    Per ``(customerId, campaign)`` impressions are numbered 1..N chronologically;
    ``is_repeat_impression`` is True for every exposure after the first.
    """
    df = work.sort_values("event_timestamp", kind="stable").copy()
    is_impression = df["event"].eq(EVENT_IMPRESSION)
    seq = (
        df.loc[is_impression]
        .groupby(["customerId", "campaign"], sort=False)
        .cumcount()
        + 1
    )
    df["impression_seq"] = seq  # aligns by index; NaN for non-impressions
    df["is_repeat_impression"] = df["impression_seq"].gt(1).fillna(False)
    return df


# ---------------------------------------------------------------------------
# Component computations (each returns a per-customer frame/Series)
# ---------------------------------------------------------------------------


def _event_counts(work: pd.DataFrame, present: Set[str]) -> pd.DataFrame:
    """Event Counts group: impressions, clicks, skips, conversions, repeats.

    Counts for canonical events absent from ``present`` are returned as ``pd.NA``
    (placeholder), distinguishing "not tracked yet" from a genuine zero.
    """
    counts = (
        work.groupby(["customerId", "event"]).size().unstack(fill_value=0)
    )
    counts = counts.reindex(columns=sorted(CANONICAL_EVENTS), fill_value=0)

    out = pd.DataFrame(index=counts.index)
    out.index.name = "customerId"
    out["total_impressions"] = counts[EVENT_IMPRESSION].astype("Int64")
    out["total_clicks"] = counts[EVENT_CLICK].astype("Int64")
    out["total_skips"] = (
        counts[EVENT_SKIP].astype("Int64") if EVENT_SKIP in present else pd.NA
    )
    out["total_conversions"] = (
        counts[EVENT_CONVERSION].astype("Int64") if EVENT_CONVERSION in present else pd.NA
    )
    out["repeat_impressions"] = (
        work.groupby("customerId", sort=False)["is_repeat_impression"]
        .sum().astype("Int64")
    )
    return out


def _campaign_diversity(work: pd.DataFrame) -> pd.DataFrame:
    """Exploration Counts: distinct campaigns seen and clicked."""
    seen = (
        work.loc[work["event"].eq(EVENT_IMPRESSION)]
        .groupby("customerId", sort=False)["campaign"].nunique()
    )
    clicked = (
        work.loc[work["event"].eq(EVENT_CLICK)]
        .groupby("customerId", sort=False)["campaign"].nunique()
    )
    out = pd.DataFrame(index=work["customerId"].drop_duplicates())
    out.index.name = "customerId"
    out["unique_campaigns_seen"] = seen.reindex(out.index).fillna(0).astype("Int64")
    out["unique_campaigns_clicked"] = (
        clicked.reindex(out.index).fillna(0).astype("Int64")
    )
    return out


def _session_depth(work: pd.DataFrame) -> pd.Series:
    """Avg session depth (§7.7): canonical events per distinct session."""
    grp = work.groupby("customerId", sort=False)
    n_events = grp["event"].size()
    n_sessions = grp["sessionId"].nunique()
    depth = _safe_ratio(n_events, n_sessions)
    depth.name = "avg_session_depth"
    return depth


def _event_timestamps(work: pd.DataFrame) -> pd.DataFrame:
    """first_seen (first impression) and last_seen (latest event), UTC (§13)."""
    impressions = work.loc[work["event"].eq(EVENT_IMPRESSION)]
    first_ms = impressions.groupby("customerId", sort=False)["event_timestamp"].min()
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


def _pair_reactions_to_impressions(
    work: pd.DataFrame, reaction_event: str
) -> pd.Series:
    """Mean latency (seconds) from each reaction to its parent impression.

    ``merge_asof`` (backward) keyed by ``(customerId, campaign)`` attaches each
    click/skip to the closest preceding impression of the same campaign
    (analytics §3.2 parent rule). Orphan reactions yield ``NaN`` and drop out of
    the mean. Returns a per-customer Series; empty when the reaction is absent.
    """
    impressions = (
        work.loc[
            work["event"].eq(EVENT_IMPRESSION),
            ["customerId", "campaign", "event_timestamp"],
        ]
        .assign(_impression_ts=lambda d: d["event_timestamp"])
        .sort_values("event_timestamp", kind="stable")
    )
    reactions = (
        work.loc[
            work["event"].eq(reaction_event),
            ["customerId", "campaign", "event_timestamp"],
        ].sort_values("event_timestamp", kind="stable")
    )
    if reactions.empty or impressions.empty:
        return pd.Series(dtype="float64")

    merged = pd.merge_asof(
        reactions, impressions,
        on="event_timestamp", by=["customerId", "campaign"], direction="backward",
    )
    latency_sec = (merged["event_timestamp"] - merged["_impression_ts"]) / 1000.0
    latency_sec = latency_sec.set_axis(merged["customerId"].to_numpy())
    return latency_sec.groupby(level=0).mean()


def _first_click_exposure(work: pd.DataFrame) -> pd.DataFrame:
    """One row per ``(customerId, campaign)`` the customer clicked, describing
    that customer's FIRST click on the campaign relative to its exposures.

    For every clicked campaign we take the earliest click in time and match it
    (backward as-of join) to the parent impression in effect at that instant
    (analytics_contract.md §3.2 "parent impression" rule). The matched
    ``impression_seq`` is how many times the campaign had been shown up to and
    including that click.

    Returned columns
    ----------------
    customerId, campaign,
    impressions_at_first_click : int  -- exposure count when first clicked (>=1)
    clicked_on_first           : bool -- True iff first click landed on exposure #1

    This single computation backs three customer metrics
    (:func:`_first_impression_success`, :func:`_first_impression_success_rate`,
    :func:`_avg_impressions_before_click`), so the as-of join runs only once.
    Returns an empty frame when there are no clicks/impressions to pair.
    """
    # Impressions carry their per-(customer, campaign) sequence number.
    impressions = (
        work.loc[
            work["event"].eq(EVENT_IMPRESSION),
            ["customerId", "campaign", "event_timestamp", "impression_seq"],
        ].sort_values("event_timestamp", kind="stable")
    )
    # Earliest click per (customer, campaign) == the customer's "first reaction".
    first_clicks = (
        work.loc[work["event"].eq(EVENT_CLICK)]
        .sort_values("event_timestamp", kind="stable")
        .groupby(["customerId", "campaign"], sort=False, as_index=False)
        .first()[["customerId", "campaign", "event_timestamp"]]
        .sort_values("event_timestamp", kind="stable")
    )
    if first_clicks.empty or impressions.empty:
        return pd.DataFrame(
            columns=[
                "customerId",
                "campaign",
                "impressions_at_first_click",
                "clicked_on_first",
            ]
        )

    # Backward as-of join: nearest impression at-or-before each first click.
    merged = pd.merge_asof(
        first_clicks, impressions,
        on="event_timestamp", by=["customerId", "campaign"], direction="backward",
    )
    # Drop orphan clicks (no preceding impression) -- they carry no exposure signal.
    merged = merged.loc[merged["impression_seq"].notna()].copy()
    merged["impressions_at_first_click"] = merged["impression_seq"]
    merged["clicked_on_first"] = merged["impression_seq"].eq(1)
    return merged[
        ["customerId", "campaign", "impressions_at_first_click", "clicked_on_first"]
    ]


def _first_impression_success(exposure: pd.DataFrame) -> pd.Series:
    """first_impression_success (§5): clicked >=1 campaign on its first exposure.

    Per-customer reduction of :func:`_first_click_exposure`: True if ANY clicked
    campaign was clicked on exposure #1. Non-clickers are absent here and are
    filled ``False`` during assembly.
    """
    if exposure.empty:
        return pd.Series(dtype="boolean")
    # .any() over the per-campaign boolean -> "succeeded at least once".
    return (
        exposure.groupby("customerId", sort=False)["clicked_on_first"]
        .any().astype("boolean")
    )


def _first_impression_success_rate(exposure: pd.DataFrame) -> pd.Series:
    """first_impression_success_rate: share of a customer's CLICKED campaigns that
    were clicked on first exposure, as a 0-100 percentage.

    Per-customer analogue of analytics_contract.md §8.5 (which is a population
    rate across users). It grades the boolean ``first_impression_success`` -- e.g.
    clicked 4 campaigns, 3 of them on first sight -> 75.0. A customer with no
    clicked campaigns has a zero denominator -> ``NaN`` (§13 convention).
    """
    if exposure.empty:
        return pd.Series(dtype="float64")
    # mean() of a boolean column == fraction True; *100 -> percentage.
    return (
        exposure.groupby("customerId", sort=False)["clicked_on_first"].mean()
        * 100.0
    )


def _avg_impressions_before_click(exposure: pd.DataFrame) -> pd.Series:
    """Average number of impressions seen before the first click, per customer.

    Mean of ``impressions_at_first_click`` across the customer's clicked
    campaigns. Feeds ``delayed_responder_flag``. ``NaN`` for non-clickers.
    """
    if exposure.empty:
        return pd.Series(dtype="float64")
    return (
        exposure.groupby("customerId", sort=False)["impressions_at_first_click"]
        .mean()
    )


# ---------------------------------------------------------------------------
# Ratio helper & assembly
# ---------------------------------------------------------------------------


def _safe_ratio(
    numerator: pd.Series, denominator: pd.Series, scale: float = 1.0
) -> pd.Series:
    """``numerator / denominator * scale`` with a zero-denominator guard (§13).

    Returns ``NaN`` where the denominator is 0/NaN so such customers are
    excluded from downstream rollups rather than skewed toward zero.
    """
    num = numerator.astype("float64")
    den = denominator.astype("float64")
    result = np.where(den > 0, num / den.where(den > 0, np.nan) * scale, np.nan)
    return pd.Series(result, index=numerator.index, dtype="float64")


def _assemble_profiles(
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
    config: FeatureExtractorConfig,
) -> pd.DataFrame:
    """Join components and derive rate / engagement / exploration metrics."""
    profile = (
        counts.join(diversity, how="outer")
        .join(timestamps, how="left")
        .join(depth, how="left")
    )

    impressions = profile["total_impressions"]
    clicks = profile["total_clicks"]

    # --- Rate Metrics -----------------------------------------------------
    profile["ctr"] = _safe_ratio(clicks, impressions, scale=100.0)
    profile["repeat_impression_rate"] = _safe_ratio(
        profile["repeat_impressions"], impressions, scale=100.0
    )
    if EVENT_SKIP in present:
        profile["skip_rate"] = _safe_ratio(
            profile["total_skips"], impressions, scale=100.0
        )
        profile["attention_score"] = _safe_ratio(
            clicks, clicks + profile["total_skips"]
        )
        profile["avg_time_to_skip_sec"] = skip_latency.reindex(profile.index)
    else:  # TODO: §6 future skip telemetry
        profile["skip_rate"] = np.nan
        profile["attention_score"] = np.nan
        profile["avg_time_to_skip_sec"] = np.nan

    if EVENT_CONVERSION in present:
        profile["conversion_rate"] = _safe_ratio(
            profile["total_conversions"], clicks, scale=100.0
        )
    else:  # TODO: §6 future conversion telemetry
        profile["conversion_rate"] = np.nan

    # --- Time-Based -------------------------------------------------------
    profile["avg_time_to_click_sec"] = click_latency.reindex(profile.index)

    # --- Engagement / Exploration ----------------------------------------
    profile["exploration_score"] = _safe_ratio(
        profile["unique_campaigns_clicked"], profile["unique_campaigns_seen"]
    )
    profile["first_impression_success"] = (
        first_impression_success.reindex(profile.index).fillna(False).astype(bool)
    )

    # --- Extended derived metrics (targeted additions) -------------------
    # campaign_diversity_score = unique_campaigns_clicked / unique_campaigns_seen.
    # NOTE: by formula this is IDENTICAL to exploration_score (§8.8); it is
    # exposed under the requested business-friendly name. Both are retained so
    # neither the existing nor the newly-requested column is removed.
    profile["campaign_diversity_score"] = _safe_ratio(
        profile["unique_campaigns_clicked"], profile["unique_campaigns_seen"]
    )

    # Mean exposures before first click (over clicked campaigns); NaN if never
    # clicked. Reindexed onto the full customer set.
    profile["avg_impressions_before_click"] = avg_impressions_before_click.reindex(
        profile.index
    )

    # % of clicked campaigns clicked on first exposure; NaN for non-clickers.
    profile["first_impression_success_rate"] = first_impression_success_rate.reindex(
        profile.index
    )

    # delayed_responder_flag: the customer engages slowly -- either needs more
    # than ``delayed_impressions_threshold`` exposures before clicking, OR takes
    # longer than ``delayed_response_seconds`` to click on average. Comparisons
    # against NaN evaluate to False, so non-clickers are never flagged.
    ibc = profile["avg_impressions_before_click"]
    ttc = profile["avg_time_to_click_sec"]
    profile["delayed_responder_flag"] = (
        (ibc > config.delayed_impressions_threshold)
        | (ttc > config.delayed_response_seconds)
    ).astype(bool)

    # --- TODO placeholders (owned by other layers) -----------------------
    profile["fatigue_score"] = np.nan       # §7.8: needs skip + temporal ctr_decline
    profile["segments"] = [[] for _ in range(len(profile))]  # §9 segmentation step
    profile["primary_segment"] = None       # §9 segmentation step
    profile["profile_updated_at"] = pd.Timestamp.now(tz="UTC")

    return _finalise_columns(profile)


def _finalise_columns(profile: pd.DataFrame) -> pd.DataFrame:
    """Reset index and pin the §5 column order."""
    profile = profile.reset_index()
    ordered = CustomerProfile.column_order()
    for col in ordered:
        if col not in profile.columns:
            profile[col] = np.nan
    return profile[ordered]


def _empty_profile_frame() -> pd.DataFrame:
    """Empty profile frame carrying the full §5 column schema."""
    return pd.DataFrame(
        {c: pd.Series(dtype="object") for c in CustomerProfile.column_order()}
    )

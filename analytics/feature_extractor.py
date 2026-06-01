"""Customer-level behavioural feature extraction.

This module sits in the **Feature Extraction** layer of the MyJio Floater
Analytics Platform:

    Telemetry Events
        -> Ingestion Layer
        -> Preprocessing Layer
        -> Feature Extraction      <-- THIS MODULE
        -> Analytics Engine
        -> Insight Generation
        -> Dashboard

It consumes a *cleaned* telemetry event :class:`pandas.DataFrame` (one row per
event) and produces a *customer profile* :class:`pandas.DataFrame` (one row per
``customerId``). The profile is the canonical input to the segmentation step of
the Analytics Engine.

Design principles (see ``contracts/analytics_contract.md``):

* **Analytics-first, rule-based, explainable.** No recommendations, no ML, no
  personalisation. Every number here can be traced to a numerator and a
  denominator.
* **Zero-denominator metrics return ``NaN``** (never ``0``) so that downstream
  rollups can exclude them rather than averaging in a misleading zero.
* **Raw -> canonical event normalisation is configurable** and unmapped events
  are quarantined (logged and dropped), never counted.

The metrics produced match the task contract:

    impression_count, click_count, skip_count, ctr, skip_rate,
    repeat_impression_rate, avg_time_to_click, avg_time_to_skip,
    attention_score, exploration_score, loyalty_score,
    avg_impressions_before_click
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

__all__ = [
    "CustomerProfile",
    "FeatureExtractorConfig",
    "extract_features",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical event vocabulary
# ---------------------------------------------------------------------------

EVENT_IMPRESSION = "impression"
EVENT_CLICK = "click"
EVENT_SKIP = "skip"
EVENT_CONVERSION = "conversion"

CANONICAL_EVENTS = frozenset(
    {EVENT_IMPRESSION, EVENT_CLICK, EVENT_SKIP, EVENT_CONVERSION}
)

#: Default raw-event -> canonical-event mapping.
#:
#: Keys are matched **case-insensitively** and after stripping surrounding
#: whitespace (see :func:`_normalise_key`). This table merges the raw event
#: names from both ``contracts/event_schema.md`` (production telemetry) and
#: ``contracts/analytics_contract.md`` (canonical mapping) so the extractor
#: works against either dialect.
DEFAULT_EVENT_MAPPING: Dict[str, str] = {
    # analytics_contract.md
    "floater_impression": EVENT_IMPRESSION,
    "floater_click": EVENT_CLICK,
    "floater_skip": EVENT_SKIP,
    "dismiss_popup": EVENT_SKIP,
    "recharge_success": EVENT_CONVERSION,
    "ott_subscription_success": EVENT_CONVERSION,
    # event_schema.md (raw production telemetry)
    "recharge floater impression": EVENT_IMPRESSION,
    "recharge floater clicks": EVENT_CLICK,
    "recharge floater click": EVENT_CLICK,
}

#: Columns the extractor requires on the input frame.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "customerId",
    "event_type",
    "event_timestamp",
)

#: Placeholder used when a campaign label is missing. Kept as a sentinel so
#: campaign-scoped logic (repeat impressions, exploration) stays well defined.
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
        Raw-event-name -> canonical-event mapping. Matched case-insensitively.
        Defaults to :data:`DEFAULT_EVENT_MAPPING`.
    customer_col:
        Column holding the customer identifier.
    campaign_col:
        Column holding the campaign / floater label. In the raw telemetry
        schema this is ``label``.
    event_type_col:
        Column holding the *raw* event name.
    timestamp_col:
        Column holding the event timestamp as an epoch value in
        **milliseconds** (used for latency maths).
    drop_unmapped:
        When ``True`` (default) events whose raw type is not in
        ``event_mapping`` are quarantined (dropped). Their count is logged.
    """

    event_mapping: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_EVENT_MAPPING)
    )
    customer_col: str = "customerId"
    campaign_col: str = "label"
    event_type_col: str = "event_type"
    timestamp_col: str = "event_timestamp"
    drop_unmapped: bool = True


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass
class CustomerProfile:
    """Behavioural profile for a single customer (one row of the output).

    All percentage metrics are expressed on a 0-100 scale; scores noted as
    ratios are on a 0-1 scale. Ratio metrics are ``NaN`` when their denominator
    is zero (e.g. ``ctr`` is ``NaN`` for a customer with no impressions).
    """

    customerId: str

    # --- raw counts -------------------------------------------------------
    impression_count: int = 0
    click_count: int = 0
    skip_count: int = 0
    unique_campaigns_seen: int = 0
    unique_campaigns_clicked: int = 0

    # --- core rate metrics (0-100, %) -------------------------------------
    ctr: float = np.nan
    skip_rate: float = np.nan
    repeat_impression_rate: float = np.nan

    # --- timing metrics (seconds) -----------------------------------------
    avg_time_to_click: float = np.nan
    avg_time_to_skip: float = np.nan

    # --- advanced behavioural scores --------------------------------------
    attention_score: float = np.nan  # clicks / (clicks + skips), 0-1
    exploration_score: float = np.nan  # unique_clicked / unique_seen, 0-1
    loyalty_score: float = np.nan  # repeat-clicks / clicks, 0-1
    avg_impressions_before_click: float = np.nan

    @classmethod
    def column_order(cls) -> List[str]:
        """Return the canonical output column order (dataclass field order)."""
        return [f.name for f in fields(cls)]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def extract_features(
    events: pd.DataFrame,
    config: Optional[FeatureExtractorConfig] = None,
) -> pd.DataFrame:
    """Build per-customer behavioural profiles from telemetry events.

    Parameters
    ----------
    events:
        Cleaned telemetry events, one row per event. Must contain at least the
        columns in :data:`REQUIRED_COLUMNS`. ``event_timestamp`` must be epoch
        milliseconds.
    config:
        Optional :class:`FeatureExtractorConfig`. Defaults are used when
        ``None``.

    Returns
    -------
    pandas.DataFrame
        One row per customer, columns ordered as
        :meth:`CustomerProfile.column_order`. Returns an empty frame with the
        correct columns when ``events`` is empty or contains no usable
        (canonical) events.

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
        logger.warning(
            "No canonical events remain after normalisation; "
            "returning empty profile."
        )
        return _empty_profile_frame()

    work = _add_impression_sequence(work)

    counts = _compute_counts(work)
    latencies = _compute_latencies(work)
    before_click = _compute_impressions_before_click(work)
    repeat_clicks = _compute_repeat_clicks(work)

    profile = _assemble_profiles(counts, latencies, before_click, repeat_clicks)

    logger.info(
        "Extracted profiles for %d customers from %d usable events.",
        len(profile),
        len(work),
    )
    return profile


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_input(events: pd.DataFrame, config: FeatureExtractorConfig) -> None:
    """Validate the input frame's type and required columns.

    Raises ``TypeError``/``ValueError`` with actionable messages. Validation is
    intentionally strict at the boundary so failures surface here rather than
    as cryptic errors deep in the pipeline.
    """
    if not isinstance(events, pd.DataFrame):
        raise TypeError(
            f"`events` must be a pandas DataFrame, got {type(events).__name__}."
        )

    required = set(REQUIRED_COLUMNS) | {config.customer_col, config.event_type_col, config.timestamp_col}
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(
            "Input events frame is missing required column(s): "
            f"{missing}. Present columns: {sorted(events.columns)}."
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
    """Return a working frame with canonical ``event`` and ``campaign`` columns.

    * Maps raw ``event_type`` to a canonical event via ``config.event_mapping``.
    * Quarantines (drops) rows whose raw type is unmapped, logging the volume.
    * Coerces ``event_timestamp`` to numeric (ms); rows with an unparseable
      timestamp are dropped.
    * Derives a ``campaign`` column from ``config.campaign_col`` (missing
      labels become a sentinel so campaign-scoped logic stays defined).
    """
    df = events.copy()

    # Canonical event column.
    lookup = {_normalise_key(k): v for k, v in config.event_mapping.items()}
    df["event"] = df[config.event_type_col].map(
        lambda raw: lookup.get(_normalise_key(raw))
    )

    unmapped_mask = df["event"].isna()
    n_unmapped = int(unmapped_mask.sum())
    if n_unmapped:
        sample = (
            df.loc[unmapped_mask, config.event_type_col]
            .astype(str)
            .value_counts()
            .head(10)
            .to_dict()
        )
        logger.warning(
            "Quarantining %d event(s) with unmapped raw types (top: %s).",
            n_unmapped,
            sample,
        )
        if config.drop_unmapped:
            df = df.loc[~unmapped_mask].copy()

    # Standardised helper columns.
    df["customerId"] = df[config.customer_col].astype("string")

    if config.campaign_col in df.columns:
        campaign = df[config.campaign_col].astype("string")
    else:
        campaign = pd.Series(pd.NA, index=df.index, dtype="string")
    df["campaign"] = campaign.fillna(_UNKNOWN_CAMPAIGN)

    # Timestamp must be numeric (epoch ms) for latency maths.
    df["event_timestamp"] = pd.to_numeric(
        df[config.timestamp_col], errors="coerce"
    )
    bad_ts = int(df["event_timestamp"].isna().sum())
    if bad_ts:
        logger.warning(
            "Dropping %d event(s) with non-numeric `%s`.",
            bad_ts,
            config.timestamp_col,
        )
        df = df.loc[df["event_timestamp"].notna()].copy()

    return df[["customerId", "campaign", "event", "event_timestamp"]]


def _add_impression_sequence(work: pd.DataFrame) -> pd.DataFrame:
    """Add ``impression_seq`` and ``is_repeat_impression`` to impressions.

    For each ``(customerId, campaign)`` impressions are numbered 1..N in
    chronological order. ``impression_seq`` is the 1-based exposure count and
    ``is_repeat_impression`` is ``True`` for every exposure after the first.

    Non-impression rows receive ``NaN``/``False`` for these columns.
    """
    df = work.sort_values("event_timestamp", kind="stable").copy()

    is_impression = df["event"].eq(EVENT_IMPRESSION)
    seq = (
        df.loc[is_impression]
        .groupby(["customerId", "campaign"], sort=False)
        .cumcount()
        + 1
    )
    df["impression_seq"] = seq  # aligns by index; NaN elsewhere
    df["is_repeat_impression"] = df["impression_seq"].gt(1).fillna(False)
    return df


# ---------------------------------------------------------------------------
# Metric computation (each returns a per-customer frame indexed by customerId)
# ---------------------------------------------------------------------------


def _compute_counts(work: pd.DataFrame) -> pd.DataFrame:
    """Per-customer event counts and campaign-diversity counts.

    Returns a frame indexed by ``customerId`` with: ``impression_count``,
    ``click_count``, ``skip_count``, ``repeat_impression_count``,
    ``unique_campaigns_seen`` and ``unique_campaigns_clicked``.
    """
    g = work.groupby("customerId", sort=False)

    counts = pd.DataFrame(index=g.size().index)
    counts.index.name = "customerId"

    event = work["event"]
    counts["impression_count"] = (
        work.assign(_is=event.eq(EVENT_IMPRESSION))
        .groupby("customerId", sort=False)["_is"]
        .sum()
        .astype(int)
    )
    counts["click_count"] = (
        work.assign(_is=event.eq(EVENT_CLICK))
        .groupby("customerId", sort=False)["_is"]
        .sum()
        .astype(int)
    )
    counts["skip_count"] = (
        work.assign(_is=event.eq(EVENT_SKIP))
        .groupby("customerId", sort=False)["_is"]
        .sum()
        .astype(int)
    )
    counts["repeat_impression_count"] = (
        work.groupby("customerId", sort=False)["is_repeat_impression"]
        .sum()
        .astype(int)
    )

    # Campaign diversity (only campaigns actually seen / clicked count).
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
    counts["unique_campaigns_seen"] = seen.reindex(counts.index).fillna(0).astype(int)
    counts["unique_campaigns_clicked"] = (
        clicked.reindex(counts.index).fillna(0).astype(int)
    )

    return counts


def _pair_reactions_to_impressions(
    work: pd.DataFrame, reaction_event: str
) -> pd.Series:
    """Latency (seconds) from each reaction to its most recent prior impression.

    Uses :func:`pandas.merge_asof` (backward) keyed by
    ``(customerId, campaign)`` to attach, to every ``reaction_event`` (click or
    skip), the timestamp of the closest preceding impression of the *same*
    campaign. Reactions with no prior impression (orphans) yield ``NaN`` and are
    naturally excluded from the mean.

    Returns a per-row Series of latencies in **seconds**, indexed like the
    reaction rows.
    """
    impressions = (
        work.loc[work["event"].eq(EVENT_IMPRESSION), ["customerId", "campaign", "event_timestamp"]]
        .assign(_impression_ts=lambda d: d["event_timestamp"])
        .sort_values("event_timestamp", kind="stable")
    )
    reactions = (
        work.loc[work["event"].eq(reaction_event), ["customerId", "campaign", "event_timestamp"]]
        .sort_values("event_timestamp", kind="stable")
    )

    if reactions.empty or impressions.empty:
        return pd.Series(dtype="float64")

    merged = pd.merge_asof(
        reactions,
        impressions,
        on="event_timestamp",
        by=["customerId", "campaign"],
        direction="backward",
    )
    latency_ms = merged["event_timestamp"] - merged["_impression_ts"]
    latency_sec = latency_ms / 1000.0
    return latency_sec.set_axis(merged["customerId"].to_numpy())


def _compute_latencies(work: pd.DataFrame) -> pd.DataFrame:
    """Per-customer mean time-to-click and time-to-skip (seconds).

    Returns a frame indexed by ``customerId`` with ``avg_time_to_click`` and
    ``avg_time_to_skip``. Customers with no qualifying reactions get ``NaN``.
    """
    click_latency = _pair_reactions_to_impressions(work, EVENT_CLICK)
    skip_latency = _pair_reactions_to_impressions(work, EVENT_SKIP)

    out = pd.DataFrame(
        {
            "avg_time_to_click": _mean_by_customer(click_latency),
            "avg_time_to_skip": _mean_by_customer(skip_latency),
        }
    )
    out.index.name = "customerId"
    return out


def _mean_by_customer(latency: pd.Series) -> pd.Series:
    """Mean latency per customer, ignoring NaN; empty input -> empty Series."""
    if latency.empty:
        return pd.Series(dtype="float64")
    return latency.groupby(level=0).mean()


def _compute_impressions_before_click(work: pd.DataFrame) -> pd.DataFrame:
    """Per-customer average number of impressions seen before the first click.

    For each ``(customerId, campaign)`` the customer clicked, we find the
    impression sequence number in effect at the **first** click on that
    campaign (i.e. how many impressions of that campaign preceded/coincided
    with the click), then average those across the customer's clicked
    campaigns.

    Returns a frame indexed by ``customerId`` with
    ``avg_impressions_before_click``. ``NaN`` for customers who never clicked.
    """
    impressions = (
        work.loc[
            work["event"].eq(EVENT_IMPRESSION),
            ["customerId", "campaign", "event_timestamp", "impression_seq"],
        ].sort_values("event_timestamp", kind="stable")
    )
    first_clicks = (
        work.loc[work["event"].eq(EVENT_CLICK)]
        .sort_values("event_timestamp", kind="stable")
        .groupby(["customerId", "campaign"], sort=False, as_index=False)
        .first()[["customerId", "campaign", "event_timestamp"]]
        .sort_values("event_timestamp", kind="stable")
    )

    if first_clicks.empty or impressions.empty:
        return pd.DataFrame(
            {"avg_impressions_before_click": pd.Series(dtype="float64")}
        ).rename_axis("customerId")

    merged = pd.merge_asof(
        first_clicks,
        impressions[["customerId", "campaign", "event_timestamp", "impression_seq"]],
        on="event_timestamp",
        by=["customerId", "campaign"],
        direction="backward",
    )
    # Clicks with no preceding impression contribute no signal.
    merged = merged.loc[merged["impression_seq"].notna()]

    out = (
        merged.groupby("customerId", sort=False)["impression_seq"]
        .mean()
        .to_frame("avg_impressions_before_click")
    )
    out.index.name = "customerId"
    return out


# ---------------------------------------------------------------------------
# Assembly + ratio metrics
# ---------------------------------------------------------------------------


def _safe_ratio(
    numerator: pd.Series, denominator: pd.Series, scale: float = 1.0
) -> pd.Series:
    """Element-wise ``numerator / denominator * scale`` with a zero guard.

    Where ``denominator`` is 0 (or NaN) the result is ``NaN`` rather than 0, per
    the contract's zero-denominator rule. This keeps such customers out of
    downstream averages instead of dragging them toward zero.
    """
    num = numerator.astype("float64")
    den = denominator.astype("float64")
    result = np.where(den > 0, num / den.where(den > 0, np.nan) * scale, np.nan)
    return pd.Series(result, index=numerator.index, dtype="float64")


def _compute_repeat_clicks(work: pd.DataFrame) -> pd.Series:
    """Count clicks that land on a campaign the customer has clicked before.

    A "repeat click" is the 2nd, 3rd, ... click by a customer on a given
    campaign. The share of repeat clicks among all clicks is the customer's
    ``loyalty_score`` (repeat-engagement depth). Returns a per-customer Series.
    """
    clicks = work.loc[work["event"].eq(EVENT_CLICK)].sort_values(
        "event_timestamp", kind="stable"
    )
    if clicks.empty:
        return pd.Series(dtype="float64")
    click_rank = clicks.groupby(["customerId", "campaign"], sort=False).cumcount()
    is_repeat_click = click_rank > 0
    return (
        is_repeat_click.groupby(clicks["customerId"]).sum().astype("float64")
    )


def _assemble_profiles(
    counts: pd.DataFrame,
    latencies: pd.DataFrame,
    before_click: pd.DataFrame,
    repeat_clicks: pd.Series,
) -> pd.DataFrame:
    """Join component frames and derive the final ratio/score metrics."""
    profile = counts.join(latencies, how="left").join(before_click, how="left")

    impressions = profile["impression_count"]
    clicks = profile["click_count"]
    skips = profile["skip_count"]
    repeat_impr = profile["repeat_impression_count"]

    # Core rates (0-100, %).
    profile["ctr"] = _safe_ratio(clicks, impressions, scale=100.0)
    profile["skip_rate"] = _safe_ratio(skips, impressions, scale=100.0)
    profile["repeat_impression_rate"] = _safe_ratio(
        repeat_impr, impressions, scale=100.0
    )

    # Attention: of customers who reacted, share that engaged (0-1).
    profile["attention_score"] = _safe_ratio(clicks, clicks + skips)

    # Exploration: breadth of interest (0-1).
    profile["exploration_score"] = _safe_ratio(
        profile["unique_campaigns_clicked"], profile["unique_campaigns_seen"]
    )

    # Loyalty: repeat-engagement depth (0-1) = repeat-clicks / total clicks.
    repeat_clicks_aligned = repeat_clicks.reindex(profile.index).fillna(0.0)
    profile["loyalty_score"] = _safe_ratio(repeat_clicks_aligned, clicks)

    return _finalise_columns(profile)


def _finalise_columns(profile: pd.DataFrame) -> pd.DataFrame:
    """Reset index, enforce dtypes and pin the contract column order."""
    profile = profile.reset_index()

    int_cols = [
        "impression_count",
        "click_count",
        "skip_count",
        "unique_campaigns_seen",
        "unique_campaigns_clicked",
    ]
    for col in int_cols:
        if col in profile.columns:
            profile[col] = profile[col].fillna(0).astype(int)

    ordered = CustomerProfile.column_order()
    for col in ordered:
        if col not in profile.columns:
            profile[col] = np.nan
    return profile[ordered]


def _empty_profile_frame() -> pd.DataFrame:
    """Return an empty profile frame with the canonical column schema."""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in CustomerProfile.column_order()})

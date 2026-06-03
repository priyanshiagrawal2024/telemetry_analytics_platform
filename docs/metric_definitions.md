# MyJio Floater Analytics — Metric, Score & Insight Definitions

**Document type:** Technical reference (metric dictionary) for manager review
**Version:** 1.0 · **Date:** 2026-06-02
**Scope:** Every metric, score, and insight the platform actually computes from the validated telemetry.
**Authoritative sources:** `contracts/event_schema.md` v2.0, `contracts/analytics_contract.md` v2.0, `docs/telemetry_data_findings.md`.
**Implemented in:** `analytics/feature_extractor.py` (`MetricCalculator`, `FeatureExtractor`), `analytics/score_calculator.py`, `analytics/analysis_engine.py`, `analytics/insight_generator.py`; tuned via `configs/semantic_mappings.yaml` and `configs/analytics_thresholds.yaml`.

> **Guiding rule:** only calculations supported by the telemetry are documented as *available*. Calculations that the current data cannot support are listed explicitly as **Not available** with the reason — they are never reported as real values.

---

## 0. What the telemetry is (and what it is not)

The platform reads MyJio floater telemetry (delivered as an XLSX workbook despite a `.csv` name). The semantic mapping (`configs/semantic_mappings.yaml`, dataset `myjio_floater`) resolves canonical **roles** to physical columns:

| Role | Physical column | Notes |
|------|-----------------|-------|
| `customer_id` | `customerId` | Customer identifier. |
| `session_id` | `sessionId` | App session (null on many lifecycle rows). |
| `event_type` | `event_type` | Raw client event name. |
| `campaign` | `click_action` | **Campaign key** (e.g. `PLANEXPIRY01`) — *not* `label`. |
| `action` | `label` | Chosen action; carries the skip marker. |
| `screen` | `newscreen_name` | App surface. |
| `timestamp` | `event_timestamp` | **Epoch milliseconds** (UTC). |

**Canonical event derivation** (`event_schema.md` §5):
- `impression` ← `event_type = "Recharge floater impression"`
- `skip` ← `event_type = "Recharge floater clicks"` **and** `label` contains `skip`/`dismiss`
- `click` ← `event_type = "Recharge floater clicks"` **and** not a skip
- `campaign_served` ← `FloaterResponse`; `campaign_received` ← `campaign_response_received[_empty]`
- All other event types are quarantined (`other`) and excluded from campaign/funnel metrics.

### 0.1 Explicitly NOT measured (out of scope, by data limitation)

The telemetry has **no signal** for the following, so the platform makes **no** such claim anywhere:

| Not measured | Why it is impossible from this data |
|--------------|-------------------------------------|
| **Accidental clicks** | A click and an intentional click are indistinguishable in the logs. |
| **Dwell time / read time on content** | No "content visible" or "content closed" timestamps are emitted. |
| **Attention / focus** | No gaze, scroll-into-view, or viewability signal exists. |
| **Detailed user-journey reconstruction** | Only floater funnel events + session ids are present; cross-screen navigation intent is not captured. |
| **Emotion / intent** | Not observable from event logs. |

Where a metric could be *mistaken* for one of these (e.g. `avg_time_to_click_sec`, or the reaction ratio `positive_reaction_ratio`), its definition below states explicitly what it really measures.

---

## 1. Metrics

Two metric families are produced from the same telemetry:

- **§1.1 Descriptive metrics** — domain-agnostic, directly-calculable, computed over **all** events by `MetricCalculator`. These feed the scores and insights.
- **§1.2 Behavioural-profile metrics** — floater-funnel metrics (contract §6) by `FeatureExtractor`.

All percentages are 0–100; ratios/scores noted as 0–1. A zero/absent denominator yields `null` (never 0). A metric whose required role/column is not mapped is emitted as `null` and listed in `dataset_summary.unavailable_metrics` (capability gating).

### 1.1 Descriptive metrics (`MetricCalculator`)

#### `event_count`
- **Definition:** Total telemetry rows attributed to the customer (all event types).
- **Formula:** `count(rows)`
- **Source columns:** `customerId`
- **Required events:** none (all rows)
- **Assumptions:** Includes quarantined/infrastructure events.
- **Interpretation:** Raw activity volume; a denominator for `click_rate`.

#### `impression_count`
- **Definition:** Number of floater impressions shown to the customer.
- **Formula:** `count(role = impression)`
- **Source columns:** `customerId`, `event_type`
- **Required events:** `impression`
- **Assumptions:** Impression = `Recharge floater impression`.
- **Interpretation:** Exposure volume (reach denominator for CTR/skip rate).

#### `click_count`
- **Definition:** Number of genuine floater clicks (skips excluded).
- **Formula:** `count(role = click)`
- **Source columns:** `customerId`, `event_type`, `label`
- **Required events:** `click`
- **Assumptions:** A `…clicks` row whose `label` marks a skip is **not** counted here.
- **Interpretation:** Engagement volume. Not an intent or accidental-click measure.

#### `skip_count`
- **Definition:** Number of floater dismissals.
- **Formula:** `count(role = skip)`
- **Source columns:** `customerId`, `event_type`, `label`
- **Required events:** `skip` (label-derived)
- **Assumptions:** Skip is derived from the `label` marker (`skip`/`dismiss`); if no marker convention exists, this metric is `null` (not 0).
- **Interpretation:** Rejection volume.

#### `campaign_served_count`
- **Definition:** Floater payloads delivered to the client for the customer.
- **Formula:** `count(role = campaign_served)`
- **Source columns:** `customerId`, `event_type`
- **Required events:** `campaign_served` (`FloaterResponse`)
- **Assumptions:** Delivery ≠ impression; a served floater is not necessarily shown.
- **Interpretation:** Upstream delivery volume (operational, not engagement).

#### `campaign_received_count`
- **Definition:** Campaign responses received by the client.
- **Formula:** `count(role = campaign_received)`
- **Source columns:** `customerId`, `event_type`
- **Required events:** `campaign_received`
- **Assumptions:** Includes empty responses (`campaign_response_received_empty`).
- **Interpretation:** Upstream pipeline volume (operational).

#### `session_count`
- **Definition:** Distinct app sessions for the customer.
- **Formula:** `nunique(sessionId)`
- **Source columns:** `customerId`, `sessionId`
- **Required events:** none
- **Assumptions:** Null session ids are not counted.
- **Interpretation:** Breadth of visits; denominator for frequency metrics.

#### `unique_campaign_count`  (alias surfaced as `campaigns_reached`)
- **Definition:** Distinct campaigns the customer was exposed to/acted on.
- **Formula:** `nunique(campaign where role ≠ other)`
- **Source columns:** `customerId`, `click_action`, `event_type`
- **Required events:** any recognised role carrying a campaign
- **Assumptions:** `click_action` on quarantined (`other`) rows is navigation noise and excluded.
- **Interpretation:** Campaign breadth for this customer.

#### `unique_screen_count`
- **Definition:** Distinct app screens generating the customer's events.
- **Formula:** `nunique(newscreen_name)`
- **Source columns:** `customerId`, `newscreen_name`
- **Required events:** none
- **Assumptions:** Screen label as logged; not a journey path.
- **Interpretation:** Surface spread of activity (not journey reconstruction).

#### `ctr`  (click-through rate)
- **Definition:** Share of impressions that resulted in a click.
- **Formula:** `click_count / impression_count × 100`
- **Source columns:** `event_type`, `label`
- **Required events:** `impression`, `click`
- **Assumptions:** Clicks exclude label-derived skips.
- **Interpretation:** Engagement efficiency of shown floaters.

#### `click_rate`
- **Definition:** Share of **all** the customer's events that are floater clicks.
- **Formula:** `click_count / event_count`  (0–1)
- **Source columns:** `event_type`, `label`
- **Required events:** `click`
- **Assumptions:** Denominator includes all event types.
- **Interpretation:** How click-dense the overall activity stream is. Distinct from CTR.

#### `exposure_frequency`
- **Definition:** Average floater impressions per session.
- **Formula:** `impression_count / session_count`
- **Source columns:** `event_type`, `sessionId`
- **Required events:** `impression`
- **Assumptions:** Sessions with null id excluded.
- **Interpretation:** Exposure load per visit.

#### `interaction_frequency`
- **Definition:** Average floater interactions (clicks + skips) per session.
- **Formula:** `(click_count + skip_count) / session_count`
- **Source columns:** `event_type`, `label`, `sessionId`
- **Required events:** `click` and/or `skip`
- **Assumptions:** Interaction = a click or a skip.
- **Interpretation:** How often the customer acts on floaters per visit.

#### `average_events_per_session`
- **Definition:** Average telemetry events per session.
- **Formula:** `event_count / session_count`
- **Source columns:** `customerId`, `sessionId`
- **Required events:** none
- **Assumptions:** All event types included.
- **Interpretation:** Session activity intensity.

#### `campaign_diversity`
- **Definition:** Concentration of campaign exposure (variety vs repetition).
- **Formula:** `unique_campaign_count / count(campaign-bearing events)`  (0–1)
- **Source columns:** `click_action`, `event_type`
- **Required events:** any recognised role with a campaign
- **Assumptions:** Campaign-bearing = recognised-role rows with a non-null campaign.
- **Interpretation:** ~1.0 = mostly distinct campaigns; →0 = repeated exposure to few campaigns.

#### `repeat_interaction_rate`
- **Definition:** Share of impressions that were repeat views of an already-seen campaign.
- **Formula:** `repeat_impressions / impression_count × 100`, where `repeat_impressions = impressions of a (customer, campaign) after the first`.
- **Source columns:** `customerId`, `click_action`, `event_type`, `event_timestamp`
- **Required events:** `impression` (with campaign + timestamp for ordering)
- **Assumptions:** Chronological ordering by `event_timestamp`.
- **Interpretation:** Degree of re-exposure / over-serving. Not a fatigue claim.

#### `event_distribution`
- **Definition:** Proportion of the customer's events by canonical role.
- **Formula:** `count(role) / event_count` for each role (sums to 1)
- **Source columns:** `customerId`, `event_type`, `label`
- **Required events:** none
- **Assumptions:** Unmapped events grouped under `other`.
- **Interpretation:** Composition of the activity stream.

### 1.2 Behavioural-profile metrics (`FeatureExtractor`, contract §6)

Funnel-scoped per-customer metrics. Several mirror §1.1 (noted as aliases). Times derive from `event_timestamp` deltas (epoch-ms ÷ 1000).

#### Counts & reach
- **`total_impressions` / `total_clicks` / `total_skips`** — funnel counts; equivalent to `impression_count` / `click_count` / `skip_count`. Source: `event_type`, `label`. Required: respective roles. `total_skips` is `null` if no skip marker exists.
- **`repeat_impressions`** — impressions of a `(customer, campaign)` after the first. Source: `click_action`, `event_type`, `event_timestamp`. Required: `impression`.
- **`unique_campaigns_seen`** — distinct campaigns with ≥1 impression. **`unique_campaigns_clicked`** — distinct campaigns with ≥1 click. Source: `click_action`, `event_type`.

#### Rates
- **`ctr`** — see §1.1. **`skip_rate`** = `total_skips / total_impressions × 100` (label-derived). **`repeat_impression_rate`** = `repeat_impressions / total_impressions × 100`.

#### Engagement & exploration
- **`positive_reaction_ratio`** — **a reaction ratio, NOT a measure of attention, focus, gaze, reading, or dwell time.** Formula: `clicks / (clicks + skips)` (0–1). Required: `click`, `skip`. Interpretation: of the customer's recorded reactions (clicks + skips), the share that were clicks — i.e. the "positive" reaction. *Implementation note:* the profile field is presently emitted as `attention_score`; its analytic meaning is exactly this reaction ratio and the field name is being aligned to `positive_reaction_ratio`.
- **`exploration_score`** = `unique_campaigns_clicked / unique_campaigns_seen` (0–1). **`campaign_diversity_score`** — alias of `exploration_score` (business-friendly name).
- **`first_impression_success`** (bool) — the customer clicked at least one campaign on its first exposure. **`first_impression_success_rate`** — % of the customer's clicked campaigns clicked on first exposure.
- **`avg_impressions_before_click`** — mean number of impressions of a campaign before its first click (over clicked campaigns). Source: `click_action`, `event_type`, `event_timestamp`. Interpretation: factual count of exposures preceding the first click — not an intent or fatigue claim.

#### Timing (event-gap latency — NOT dwell/read/attention time)
- **`avg_time_to_click_sec`** = mean(`click_timestamp − parent_impression_timestamp`) ÷ 1000. **`avg_time_to_skip_sec`** = same for skips. Required: `impression` + `click`/`skip` with `event_timestamp`. **Assumption/limitation:** this is the elapsed time between two logged events; it may include time the app was backgrounded and is **not** a measure of how long content was viewed. *Configurable:* disabled via `FeatureExtractorConfig.compute_latency_metrics = False`.
- **`avg_session_depth`** = funnel events ÷ distinct sessions.

#### Lineage
- **`first_seen`** (first impression time) / **`last_seen`** (latest event time), UTC ISO-8601; **`profile_updated_at`** (compute time).

#### Behavioural flag (configurable, OFF by default)
- **`delayed_responder_flag`** — emitted as `False` unless `FeatureExtractorConfig.compute_behavioural_flags = True`. When enabled: `avg_impressions_before_click > 3` OR `avg_time_to_click_sec > 60`. **Off by default** because it is a threshold-derived label; interpretation belongs in the scores/insights layer, not as a profile fact.

### 1.3 Not available (gated — required telemetry absent)

These appear in the contract but are **not computed** from the current data; they are emitted as `null`/`None`, never as a value:

| Metric | Why unavailable |
|--------|-----------------|
| `total_conversions`, `conversion_rate` | No conversion events (`recharge_success`/`ott_subscription_success` absent; `Recharge initiated` is intent, not a completed outcome). |
| `fatigue_score` | Requires population min-max normalisation **and** period-over-period CTR decline; not derivable from a single snapshot. |
| `campaign_fatigue_index`, `engagement_momentum`, `skip_velocity` | Require campaign-grain population and/or multi-period history. |
| `segments`, `primary_segment` | Require a multi-customer population for quartile-based rules. |
| `loyalty_score` | No formula is defined in the contract. |

### 1.4 Metric Confidence Levels

Metrics are graded by how directly and robustly the telemetry supports them. This guides how much weight to place on each figure (independent of sample size, which §4 addresses).

**High-confidence metrics** — direct counts and simple ratios from unambiguous event roles; deterministic, no thresholds or interpretation.
- `click_count`, `impression_count`, `skip_count`, `ctr`, `session_count`, `campaign_diversity`, `exposure_frequency`, `interaction_frequency`
- *Why:* each is a count, or a ratio of counts, taken straight from classified events — reproducible and not tuning-dependent. (`skip_count` and any skip-based ratio additionally rely on the documented `label` skip-marker convention; they stay high-confidence as long as that convention holds.)

**Medium-confidence metrics** — correct, but dependent on event ordering, timestamp quality, or impression↔reaction pairing.
- `repeat_interaction_rate`, `avg_impressions_before_click`, `avg_time_to_click_sec`, `avg_time_to_skip_sec`
- *Why:* these require chronological ordering and a backward as-of join to pair a reaction with its parent impression. The timing metrics are event-gap latencies that can include app-backgrounding or session gaps, so they are directionally informative rather than precise.

**Low-confidence / configurable metrics** — interpretive labels driven by tunable cutoffs.
- `delayed_responder_flag`, and any other threshold-derived label.
- *Why:* their value depends on configurable thresholds (e.g. impressions-before-click or time-to-click cutoffs) rather than the data alone. `delayed_responder_flag` is OFF by default; such interpretation is better expressed through the scores/insights layer than as a profile fact.

---

## 2. Scores (`score_calculator.py` + `configs/analytics_thresholds.yaml`)

Scores are weighted blends of **normalised** metrics (0–1). Normalisation is config-driven (`reference` method: `min(value / reference, 1)`); weights are renormalised over whichever inputs are available, so a missing metric degrades a score gracefully rather than breaking it. **All weights and reference scales are editable in `analytics_thresholds.yaml` — none are hardcoded.**

Normalisation references (current config): `ctr/100`, `click_rate/1`, `interaction_frequency/5`, `exposure_frequency/5`, `unique_campaign_count/10`, `campaign_diversity/1`, `repeat_interaction_rate/100`, `average_events_per_session/20`.

### `engagement_score`
- **Purpose:** How actively the customer interacts, relative to exposure.
- **Formula:** weighted mean of normalised inputs, clipped 0–1.
- **Input metrics:** `ctr`, `interaction_frequency`, `click_rate`.
- **Configurable weights:** `ctr 0.50`, `interaction_frequency 0.30`, `click_rate 0.20`.
- **Interpretation:** Higher = more engagement per exposure. Relative score, not a probability.

### `exploration_score`
- **Purpose:** Breadth of distinct campaigns the customer engages with.
- **Formula:** weighted mean of normalised inputs, clipped 0–1.
- **Input metrics:** `unique_campaign_count`, `campaign_diversity`.
- **Configurable weights:** `unique_campaign_count 0.50`, `campaign_diversity 0.50`.
- **Interpretation:** Higher = broader campaign exposure/engagement; low = concentrated on few campaigns.

### `campaign_receptiveness_score`
- **Purpose:** Responsiveness to campaign exposure.
- **Formula:** weighted mean of normalised inputs, clipped 0–1.
- **Input metrics:** `ctr`, `interaction_frequency`.
- **Configurable weights:** `ctr 0.60`, `interaction_frequency 0.40`.
- **Interpretation:** Higher = more responsive to floaters shown. Descriptive, not predictive.

> **Note:** scores are comparative/relative indicators built from measured metrics. They do **not** estimate intent, propensity, or future behaviour.

---

## 3. Insights

The platform produces insights at two layers. Both are descriptive and evidence-backed; neither infers intent, accidental clicks, fatigue, attention, emotion, or journeys.

### 3.1 Comparative & structural observations (`analysis_engine.py`)

Each observation carries `supporting_metrics`, `evidence`, and a `confidence` (0–1). **Confidence is discounted by population size**, so on a single-customer dataset comparative observations score near zero and are suppressed (`min_confidence_to_report = 0.05`). Thresholds live in `analytics_thresholds.yaml → observations`.

#### Comparative — "interacts more/less than the dataset average" (`cmp_<metric>`)
- **Generated for metrics:** `interaction_frequency`, `ctr`, `exposure_frequency`, `unique_campaign_count`.
- **Evidence used:** `customer_value`, `dataset_average`, `ratio_to_average`, `population_size`.
- **Supporting metrics:** the metric being compared.
- **Trigger:** `ratio ≥ high_ratio (1.20)` → "more than"; `ratio ≤ low_ratio (0.80)` → "less than".
- **Limitations:** Requires a meaningful population. With one customer the customer *is* the average (ratio ≈ 1) and confidence ≈ 0 → not reported. No causal or intent claim.

#### Structural — "exposure is high while interaction is relatively low" (`struct_exposure_gt_interaction`)
- **Evidence used:** `exposure_to_interaction_ratio`, `threshold (1.30)`.
- **Supporting metrics:** `exposure_frequency`, `interaction_frequency`.
- **Trigger:** `exposure_frequency ≥ interaction_frequency × 1.30`.
- **Limitations:** Population-free (valid at N=1), but only a factual relation between two rates — not a fatigue or disengagement claim.

#### Diversity — "engages with a diverse set of campaigns" (`div_diverse_campaigns`)
- **Evidence used:** `campaign_diversity`, `diversity_floor (0.40)`, `unique_campaign_count`.
- **Supporting metrics:** `campaign_diversity`, `unique_campaign_count`.
- **Trigger:** `campaign_diversity ≥ 0.40` AND `unique_campaign_count ≥ 2`.
- **Limitations:** Not fired when only one campaign is present (e.g. the current sample).

### 3.2 Consolidated business insights (`insight_generator.py` — surfaced by the service/runner)

> The production output (`analytics_runner` / `analytics_service`) currently surfaces these consolidated, manager-readable insights (single-customer-safe). Each is `{title, insight, evidence}`; `evidence` is retained for auditability. They merge related metrics rather than restating each one.

| Insight | Evidence used | Supporting metrics | Limitations |
|---------|---------------|--------------------|-------------|
| **Engagement with floaters** | `ctr_pct`, `skip_rate_pct`, `clicks`, `skips`, `impressions`, `click_share_of_reactions` | `ctr`, `skip_rate`, `click_count`, `skip_count`, `impression_count` | Skip part requires label-derived skips; merges CTR + skip rate + click/skip split. |
| **Campaign reach & diversity** | `campaigns_reached`, `campaign_diversity`, `unique_campaign_count` | `unique_campaign_count`, `campaign_diversity` | Diversity is degenerate when only one campaign is present. |
| **Exposure vs interaction** | `exposure_frequency`, `interaction_frequency`, `session_count`, `impressions` | `exposure_frequency`, `interaction_frequency` | Factual rate relation; no fatigue/intent claim. |
| **Session activity** | `event_count`, `session_count`, `average_events_per_session` | `event_count`, `session_count`, `average_events_per_session` | Counts all event types; not a journey. |
| **Clicks after repeat exposure** | `avg_impressions_before_click` | `avg_impressions_before_click` | Only for clicked campaigns; factual exposure count, not intent/fatigue. |

A per-customer **`dashboard_summary`** (2–4 plain-language lines) is generated from the same evidence for manager dashboards.

---

## 4. Data-sufficiency limitations (current sample)

The validated sample is **one customer, one funnel campaign, 5 sessions, 13 funnel events, a single day, and zero conversions**.

> **This implementation validates platform capability rather than business performance.** The figures demonstrate that the pipeline ingests, classifies, measures, scores, and explains correctly — they are **not** a representative read on campaign or customer performance, and should not be quoted as business results.

What broader telemetry would unlock:

- **Population benchmarking requires multiple customers.** "Above/below average" comparisons (§3.1) and any percentile/quartile view are meaningless with one customer — the customer *is* the population, so every ratio-to-average is ≈ 1.
- **Campaign comparison requires multiple campaigns.** Per-campaign performance ranking and meaningful `campaign_diversity` contrasts need several campaigns in the funnel; the current sample has one.
- **Segmentation requires a larger population.** Rule-based segments depend on cross-customer thresholds/quartiles and cannot be assigned from a single profile.
- **Conversion analytics require conversion events.** `conversion_rate`, persistence, and outcome attribution stay unavailable until completion events (`recharge_success` / `ott_subscription_success`) are emitted (`event_schema.md` §10).
- **Behavioural benchmarking requires broader telemetry coverage.** Stable averages, fatigue scoring, and trend/momentum metrics need multi-customer, multi-period data.

Crucially, until that data arrives the platform **correctly reports unavailable metrics as `null` (and omits unsupported insights) rather than fabricating values**. Every figure shown is backed by observed telemetry, and every gap is explicit. This gating is the intended, auditable behaviour — not a defect.
</content>

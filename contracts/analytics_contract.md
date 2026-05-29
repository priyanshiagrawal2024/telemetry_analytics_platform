# MyJio Floater Analytics — Analytics Contract

**Document type:** Canonical analytics specification (single source of truth)
**Owner:** MyJio Floater Analytics Platform
**Status:** Production
**Scope:** Telemetry-driven behavioural, campaign, engagement, fatigue, segmentation and trend analytics.

> This is an **analytics platform**, not a recommendation, personalization, targeting, or campaign-delivery system. Every artdefact defined here exists to explain **what worked, for whom, when, and why** — never to decide *what to show next*.

This contract is binding across all layers: ingestion, preprocessing, feature extraction, analytics engine, insight generation, and dashboard. Any metric, field, or segment used anywhere in the platform MUST be defined here first.

---

## Table of Contents

1. [Event Taxonomy](#1-event-taxonomy)
2. [Event Mappings](#2-event-mappings)
3. [Telemetry Schema](#3-telemetry-schema)
4. [Conversion Definitions](#4-conversion-definitions)
5. [Customer Profile Schema](#5-customer-profile-schema)
6. [Global Profile Schema](#6-global-profile-schema)
7. [Core Metrics](#7-core-metrics)
8. [Advanced Metrics](#8-advanced-metrics)
9. [Customer Segmentation Rules](#9-customer-segmentation-rules)
10. [Dashboard KPI Definitions](#10-dashboard-kpi-definitions)
11. [Analytics Output Schemas](#11-analytics-output-schemas)
12. [Business Questions Each Metric Answers](#12-business-questions-each-metric-answers)
13. [Appendix: Conventions & Guardrails](#13-appendix-conventions--guardrails)

---

## 1. Event Taxonomy

A **floater** is an in-app promotional surface (banner, popup, card, interstitial) rendered inside the MyJio application. The platform recognises exactly **four canonical event types**. All raw telemetry must be normalised into one of these.

| Canonical Event | Definition | Funnel Stage | Counts Toward |
|-----------------|------------|--------------|---------------|
| `impression` | A floater was rendered and visible to the customer. | Exposure | Reach, denominators |
| `click` | The customer actively engaged with the floater (tap / CTA). | Engagement | CTR numerator |
| `skip` | The customer dismissed, closed, or ignored-to-dismiss the floater. | Rejection | Skip rate numerator |
| `conversion` | The customer completed a business-valuable outcome attributable to the floater. | Outcome | Conversion / revenue |

**Funnel ordering:** `impression → (click | skip) → conversion`

**Rules**
- A `click` and a `skip` are mutually exclusive for the same floater render instance.
- Every `click`, `skip`, and `conversion` must be traceable back to a parent `impression` within the same `sessionId` (or campaign attribution window — see §4).
- Unknown / unmapped raw events are routed to a `quarantine` bucket and **never** counted in metrics.

---

## 2. Event Mappings

Raw client-side event names are mapped to canonical event types during the **Preprocessing Layer**. This mapping table is authoritative.

| Raw Event (client) | Canonical Event |
|--------------------|-----------------|
| `floater_impression` | `impression` |
| `floater_click` | `click` |
| `floater_skip` | `skip` |
| `dismiss_popup` | `skip` |
| `recharge_success` | `conversion` |
| `ott_subscription_success` | `conversion` |

**Mapping rules**
- Mapping is **many-to-one** (multiple raw events may collapse to one canonical event), never one-to-many.
- Conversion raw events (`recharge_success`, `ott_subscription_success`) carry a `conversion_type` derived from the raw event name (see §4).
- New raw events MUST be added to this table before they can flow into analytics; until then they are quarantined.
- Mapping is versioned. The active version is recorded on every processed event via `mapping_version`.

---

## 3. Telemetry Schema

The normalised event record produced by the Ingestion + Preprocessing layers. This is the contract between data producers (MyJio app) and the analytics engine.

### 3.1 Core fields (required)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | string (UUID) | ✅ | Globally unique id for the normalised event. |
| `customerId` | string | ✅ | Stable, pseudonymised customer identifier. |
| `sessionId` | string | ✅ | App session identifier; resets per session. |
| `campaign` | string | ✅ | Campaign identifier the floater belongs to. |
| `event_type` | enum | ✅ | One of `impression`, `click`, `skip`, `conversion`. |
| `timestamp` | datetime (UTC, ISO-8601) | ✅ | Event occurrence time. |
| `screen_name` | string | ✅ | App screen where the floater rendered. |
| `click_action` | string | ⬜ | CTA / deep-link action (present for `click`). |

### 3.2 Derived / enrichment fields (populated by preprocessing)

| Field | Type | Description |
|-------|------|-------------|
| `raw_event` | string | Original client event name before mapping. |
| `conversion_type` | enum \| null | `recharge`, `ott_subscription`, or null. |
| `impression_seq` | int | 1-based count of times this customer has seen this campaign. |
| `is_repeat_impression` | bool | `true` when `impression_seq > 1`. |
| `time_since_impression_ms` | int \| null | For click/skip: ms elapsed since the parent impression. |
| `event_date` | date | Partition key derived from `timestamp`. |
| `mapping_version` | string | Version of the event-mapping table applied. |
| `ingested_at` | datetime | Platform receipt time (for latency monitoring). |

### 3.3 Data quality constraints

- `timestamp` must not be in the future (clock-skew tolerance: 5 minutes).
- `event_type` must be in the canonical enum; otherwise quarantine.
- Duplicate `event_id` values are idempotently de-duplicated.
- `click`/`skip` without a resolvable parent `impression` are flagged `orphan = true` and excluded from ratio metrics.

---

## 4. Conversion Definitions

A **conversion** is a business-valuable outcome attributed to a floater exposure.

### 4.1 Conversion types

| `conversion_type` | Source raw event | Business meaning |
|-------------------|------------------|------------------|
| `recharge` | `recharge_success` | Customer completed a mobile/data recharge. |
| `ott_subscription` | `ott_subscription_success` | Customer activated/renewed an OTT subscription. |

### 4.2 Attribution rules

- **Attribution window:** A conversion is attributed to the **most recent `click`** on the same `campaign` by the same `customerId` within **24 hours** of the conversion `timestamp`.
- **Fallback (view-through):** If no click exists in window, attribute to the most recent `impression` of that campaign within **6 hours**, flagged `attribution_mode = view_through`.
- **No qualifying exposure:** Conversion is recorded as `attribution_mode = unattributed` and excluded from campaign conversion-rate numerators (but retained for revenue totals).
- **Single-attribution:** A conversion is attributed to exactly one campaign (last-touch). No double counting.

### 4.3 Attribution output fields

| Field | Type | Description |
|-------|------|-------------|
| `attributed_campaign` | string \| null | Campaign credited for the conversion. |
| `attribution_mode` | enum | `click`, `view_through`, `unattributed`. |
| `attribution_latency_ms` | int \| null | Time from attributed exposure to conversion. |

---

## 5. Customer Profile Schema

The per-customer behavioural profile, produced by the Feature Extraction layer. One row per `customerId` (optionally per `customerId × campaign` for campaign-level profiles). This is the primary input to segmentation.

| Field | Type | Description |
|-------|------|-------------|
| `customerId` | string | Customer identifier. |
| `first_seen` | datetime | First impression timestamp on record. |
| `last_seen` | datetime | Most recent event timestamp. |
| `total_impressions` | int | All impressions served to the customer. |
| `total_clicks` | int | All clicks. |
| `total_skips` | int | All skips. |
| `total_conversions` | int | All attributed conversions. |
| `repeat_impressions` | int | Impressions where `is_repeat_impression = true`. |
| `unique_campaigns_seen` | int | Distinct campaigns with ≥1 impression. |
| `unique_campaigns_clicked` | int | Distinct campaigns with ≥1 click. |
| `ctr` | float | Personal CTR (%). |
| `skip_rate` | float | Personal skip rate (%). |
| `conversion_rate` | float | Personal conversion rate (%). |
| `repeat_impression_rate` | float | Repeat impressions as % of total. |
| `avg_time_to_click_sec` | float | Mean seconds from impression to click. |
| `avg_time_to_skip_sec` | float | Mean seconds from impression to skip. |
| `avg_session_depth` | float | Mean events per session. |
| `fatigue_score` | float | Composite fatigue score (0–100). |
| `attention_score` | float | clicks / (clicks + skips). |
| `exploration_score` | float | unique_campaigns_clicked / unique_campaigns_seen. |
| `first_impression_success` | bool | Clicked a campaign on first exposure at least once. |
| `segments` | array<string> | All segments the customer currently qualifies for. |
| `primary_segment` | string | Highest-priority segment (see §9 priority). |
| `profile_updated_at` | datetime | Last recomputation timestamp. |

---

## 6. Global Profile Schema

Platform-wide and campaign-level aggregate profile, produced by the Analytics Engine. Supports trend analytics and dashboard KPIs. Computed per grain: **global**, **per `campaign`**, **per `screen_name`**, and time-bucketed (daily/weekly).

| Field | Type | Description |
|-------|------|-------------|
| `grain` | enum | `global`, `campaign`, `screen`, `campaign_day`, etc. |
| `grain_key` | string | Identifier for the grain (e.g. campaign id) or `ALL`. |
| `period_start` | date | Start of the aggregation window. |
| `period_end` | date | End of the aggregation window. |
| `total_customers` | int | Distinct customers in window. |
| `total_impressions` | int | Impressions in window. |
| `total_clicks` | int | Clicks in window. |
| `total_skips` | int | Skips in window. |
| `total_conversions` | int | Attributed conversions in window. |
| `ctr` | float | Aggregate CTR (%). |
| `skip_rate` | float | Aggregate skip rate (%). |
| `conversion_rate` | float | Aggregate conversion rate (%). |
| `repeat_impression_rate` | float | Aggregate repeat impression rate (%). |
| `avg_time_to_click_sec` | float | Mean time-to-click. |
| `avg_time_to_skip_sec` | float | Mean time-to-skip. |
| `campaign_fatigue_index` | float | repeat_impression_rate × skip_rate. |
| `campaign_persistence_score` | float | conversions / impressions. |
| `campaign_saturation_level` | float | impressions / total_customers. |
| `engagement_momentum` | float | current_ctr − previous_period_ctr. |
| `skip_velocity` | float | Δ skip_rate vs previous period. |
| `first_impression_success_rate` | float | % of users clicking on first exposure. |
| `delayed_engagement_rate` | float | % of users clicking only after repeats. |
| `segment_distribution` | map<string,int> | Customer count per segment. |
| `computed_at` | datetime | Aggregation run timestamp. |

---

## 7. Core Metrics

All percentage metrics are expressed 0–100. Division guards: a metric with a zero denominator returns `null` (not 0) and is excluded from averages.

### 7.1 Click-Through Rate (CTR)
```
CTR = (clicks / impressions) * 100
```
Engagement efficiency of a floater/campaign.

### 7.2 Skip Rate
```
skip_rate = (skips / impressions) * 100
```
Rejection intensity.

### 7.3 Conversion Rate
```
conversion_rate = (conversions / clicks) * 100
```
How effectively clicks turn into business outcomes.

### 7.4 Repeat Impression Rate
```
repeat_impression_rate = (repeat_impressions / total_impressions) * 100
```
Degree of re-exposure / over-serving.

### 7.5 Average Time To Click
```
avg_time_to_click = mean(click_timestamp - impression_timestamp)
```
Decisiveness of engagement (lower = faster intent).

### 7.6 Average Time To Skip
```
avg_time_to_skip = mean(skip_timestamp - impression_timestamp)
```
Speed of rejection (lower = stronger disinterest).

### 7.7 Session Depth
```
session_depth = total_events_in_session
```
Breadth of customer activity within a session.

### 7.8 Fatigue Score
A composite (0–100) over a customer×campaign, blending three normalised signals:
```
fatigue_score = w1 * norm(repeat_impression_rate)
              + w2 * norm(skip_rate)
              + w3 * norm(ctr_decline)

where  ctr_decline   = max(0, previous_ctr - current_ctr)
       w1, w2, w3    = 0.4, 0.4, 0.2   (default weights, configurable)
       norm(x)       = min-max scaled to 0..1 over the population
```
Higher = more fatigued (over-exposed, increasingly skipping, declining CTR).

---

## 8. Advanced Metrics

### 8.1 Attention Score
```
attention_score = clicks / (clicks + skips)
```
Of customers who reacted, what share engaged vs rejected (0–1).

### 8.2 Campaign Fatigue Index
```
campaign_fatigue_index = repeat_impression_rate * skip_rate
```
Campaign-level over-exposure damage signal.

### 8.3 Click Efficiency Score
```
click_efficiency_score = CTR / avg_time_to_click
```
Engagement strength per unit of decision time.

### 8.4 Engagement Momentum
```
engagement_momentum = current_ctr - previous_ctr
```
Directional trend of engagement (positive = improving).

### 8.5 First Impression Success Rate
```
first_impression_success_rate =
    (users_clicking_on_first_exposure / total_users) * 100
```
Immediate appeal / hook strength.

### 8.6 Delayed Engagement Rate
```
delayed_engagement_rate =
    (users_clicking_after_multiple_exposures / total_users) * 100
```
Value of repetition (slow-burn campaigns).

### 8.7 Campaign Persistence Score
```
campaign_persistence_score = conversions / impressions
```
End-to-end exposure-to-outcome efficiency.

### 8.8 User Exploration Score
```
user_exploration_score = unique_campaigns_clicked / total_campaigns_seen
```
Breadth of customer interest (0–1).

### 8.9 Campaign Saturation Level
```
campaign_saturation_level = impressions_per_user
                          = total_impressions / unique_users
```
Average exposure load per customer.

### 8.10 Skip Velocity
```
skip_velocity = change_in_skip_rate_over_time
              = current_skip_rate - previous_skip_rate
```
Acceleration of rejection (early fatigue warning).

---

## 9. Customer Segmentation Rules

Segments are **rule-based and explainable**. A customer may match multiple segments; `primary_segment` is chosen by the priority order below (lower number = higher priority). All thresholds are configurable but defaults are fixed by this contract.

| Priority | Segment | Rule | Business meaning |
|----------|---------|------|------------------|
| 1 | **Fatigued** | `repeat_impression_rate > 50` AND `skip_rate > 30` | Over-served and rejecting — reduce frequency. |
| 2 | **High Skip** | `skip_rate > 50` | Strongly rejects floaters. |
| 3 | **Resistant Users** | high `total_impressions` (top quartile) AND low `ctr` (`< 5`) | Heavily exposed, won't engage. |
| 4 | **Highly Engaged** | `ctr > 15` AND `skip_rate < 10` | Best responders. |
| 5 | **Fast Click Users** | `avg_time_to_click < 5 sec` | Decisive, high-intent engagers. |
| 6 | **Selective Users** | high `ctr` AND low campaign diversity (`exploration_score` low) | Engage deeply but narrowly. |
| 7 | **Explorers** | high campaign diversity (`exploration_score` high) | Engage broadly across campaigns. |
| 8 | **Passive** | `ctr < 5` | Largely unresponsive (catch-all low engagement). |

**Notes**
- "High/low" quartile and diversity cutoffs are computed against the active population window and stored alongside the profile for auditability.
- Every segment assignment carries the **rule snapshot** that produced it (explainability requirement).

---

## 10. Dashboard KPI Definitions

KPIs surfaced in the Streamlit/Plotly dashboard. Each KPI maps to a defined metric and a default visualization.

| KPI | Source Metric | Grain | Default Visual | Target / Direction |
|-----|---------------|-------|----------------|--------------------|
| Overall CTR | §7.1 | global / campaign | Big number + trend line | ↑ higher better |
| Skip Rate | §7.2 | global / campaign | Big number + trend line | ↓ lower better |
| Conversion Rate | §7.3 | campaign | Big number + funnel | ↑ higher better |
| Total Conversions | §6 | campaign | Big number | ↑ |
| Repeat Impression Rate | §7.4 | campaign | Gauge | ↓ (watch over-serving) |
| Campaign Fatigue Index | §8.2 | campaign | Heatmap (campaign × week) | ↓ |
| Engagement Momentum | §8.4 | campaign | Delta indicator | ↑ |
| Skip Velocity | §8.10 | campaign | Sparkline + delta | ↓ |
| First Impression Success Rate | §8.5 | campaign | Bar | ↑ |
| Delayed Engagement Rate | §8.6 | campaign | Bar | context |
| Campaign Saturation Level | §8.9 | campaign | Bar / distribution | watch |
| Avg Time To Click | §7.5 | campaign | Big number | ↓ |
| Segment Distribution | §9 | global | Stacked bar / pie | context |
| Funnel (Impr→Click→Conv) | §1 funnel | campaign | Funnel chart | context |
| Top Screens by CTR | §7.1 by `screen_name` | screen | Ranked bar | context |

**Dashboard sections**
1. **Executive Summary** — headline CTR, skip rate, conversions, momentum.
2. **Campaign Analytics** — per-campaign performance & funnel.
3. **Engagement Analytics** — attention, time-to-click, click efficiency.
4. **Fatigue Analytics** — fatigue index, repeat rate, skip velocity, saturation.
5. **Segmentation** — segment distribution and segment-level metrics.
6. **Trends** — time-series of all core metrics with period-over-period deltas.

---

## 11. Analytics Output Schemas

Stable JSON contracts emitted by the Analytics Engine / Insight Generation layer (consumed by dashboard and downstream stores).

### 11.1 Metric result
```json
{
  "metric": "ctr",
  "grain": "campaign",
  "grain_key": "DIWALI_RECHARGE_2026",
  "period_start": "2026-05-01",
  "period_end": "2026-05-28",
  "value": 12.4,
  "unit": "percent",
  "numerator": 6200,
  "denominator": 50000,
  "previous_value": 11.1,
  "delta": 1.3,
  "direction": "up",
  "computed_at": "2026-05-29T03:00:00Z"
}
```

### 11.2 Campaign analytics summary
```json
{
  "campaign": "DIWALI_RECHARGE_2026",
  "period": {"start": "2026-05-01", "end": "2026-05-28"},
  "totals": {
    "customers": 50000,
    "impressions": 50000,
    "clicks": 6200,
    "skips": 18000,
    "conversions": 2100
  },
  "core_metrics": {
    "ctr": 12.4,
    "skip_rate": 36.0,
    "conversion_rate": 33.9,
    "repeat_impression_rate": 22.0
  },
  "advanced_metrics": {
    "attention_score": 0.256,
    "campaign_fatigue_index": 792.0,
    "campaign_persistence_score": 0.042,
    "engagement_momentum": 1.3,
    "skip_velocity": -0.8,
    "campaign_saturation_level": 1.0
  }
}
```

### 11.3 Segment output
```json
{
  "grain": "global",
  "period": {"start": "2026-05-01", "end": "2026-05-28"},
  "segments": {
    "Highly Engaged": 8200,
    "Passive": 21000,
    "High Skip": 6400,
    "Fatigued": 3100,
    "Fast Click Users": 4500,
    "Explorers": 2700,
    "Selective Users": 1900,
    "Resistant Users": 2200
  }
}
```

### 11.4 Insight object (Insight Generation layer)
```json
{
  "insight_id": "ins_20260529_001",
  "category": "fatigue",
  "severity": "high",
  "campaign": "DIWALI_RECHARGE_2026",
  "headline": "Diwali Recharge floater is fatiguing repeat viewers",
  "explanation": "Repeat impression rate (22%) combined with a 36% skip rate has pushed the Campaign Fatigue Index to 792, while CTR momentum turned negative two weeks running.",
  "evidence": {
    "campaign_fatigue_index": 792.0,
    "skip_velocity": 2.1,
    "engagement_momentum": -1.4
  },
  "affected_segments": ["Fatigued", "High Skip"],
  "generated_at": "2026-05-29T03:05:00Z"
}
```
> Insight objects are **descriptive/diagnostic only** — they explain observed behaviour. They MUST NOT contain prescriptive "show campaign X to user Y" content.

---

## 12. Business Questions Each Metric Answers

| Metric | Business Question | Stakeholder |
|--------|-------------------|-------------|
| CTR (§7.1) | Are floaters compelling enough to earn engagement? | Campaign / Growth |
| Skip Rate (§7.2) | How often are floaters actively rejected? | Campaign / UX |
| Conversion Rate (§7.3) | Do engaged customers complete valuable outcomes? | Revenue |
| Repeat Impression Rate (§7.4) | Are we over-serving the same floater? | Campaign Ops |
| Avg Time To Click (§7.5) | How decisive is customer intent? | UX / Product |
| Avg Time To Skip (§7.6) | How quickly do customers reject? | UX |
| Session Depth (§7.7) | How engaged are customers within a session? | Product |
| Fatigue Score (§7.8) | Which customers are exhausted by exposure? | Retention |
| Attention Score (§8.1) | Among reactors, do they engage or reject? | Campaign |
| Campaign Fatigue Index (§8.2) | Which campaigns are damaging from over-exposure? | Campaign Ops |
| Click Efficiency Score (§8.3) | How strong is engagement per second of attention? | Product |
| Engagement Momentum (§8.4) | Is a campaign improving or declining? | Growth |
| First Impression Success Rate (§8.5) | Does the campaign hook on first sight? | Creative |
| Delayed Engagement Rate (§8.6) | Does repetition eventually pay off? | Campaign Strategy |
| Campaign Persistence Score (§8.7) | How efficiently does exposure convert end-to-end? | Revenue |
| User Exploration Score (§8.8) | How broad is a customer's interest? | Segmentation |
| Campaign Saturation Level (§8.9) | How much exposure load per customer? | Campaign Ops |
| Skip Velocity (§8.10) | Is rejection accelerating (early fatigue)? | Retention |
| Segment Distribution (§9) | Who are our customers behaviourally? | Strategy |

---

## 13. Appendix: Conventions & Guardrails

**Calculation conventions**
- Time fields are UTC; dashboard renders in IST (Asia/Kolkata).
- Percentages are 0–100; ratios/scores noted explicitly as 0–1 where applicable.
- Zero-denominator metrics return `null` and are excluded from rollups.
- Period-over-period deltas compare equal-length adjacent windows.
- All thresholds/weights are configuration-driven; defaults in this document are the contract baseline.

**Explainability requirements**
- Every metric value exposes its numerator and denominator.
- Every segment assignment stores the rule snapshot that produced it.
- Every insight cites the evidence metrics that triggered it.

**Platform guardrails (non-negotiable)**
- ❌ No recommendation systems.
- ❌ No personalization engines.
- ❌ No campaign delivery / marketing automation.
- ❌ No ML-based campaign targeting.
- ✅ Analytics-first, rule-based, explainable, business-readable.

**Change management**
- This contract is versioned. Any change to taxonomy, mappings, schemas, metric formulas, or segment rules requires a version bump and is reflected in `mapping_version` / metric `computed_at` lineage.

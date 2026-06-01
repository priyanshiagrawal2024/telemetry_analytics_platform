# MyJio Floater Analytics — Analytics Contract

**Document type:** Canonical analytics specification (single source of truth)
**Owner:** MyJio Floater Analytics Platform
**Status:** FROZEN — authoritative for all layers
**Version:** 2.0
**Frozen on:** 2026-06-01
**Supersedes:** v1.x (pre-validation assumptions)
**Companion:** `contracts/event_schema.md` (telemetry schema & event derivation), `docs/telemetry_data_findings.md` (validation evidence).

> This is an **analytics platform**, not a recommendation, personalization, targeting, or campaign-delivery system. Every artefact here exists to explain **what worked, for whom, when, and why** — never to decide *what to show next*.
>
> **Reality-first principle:** this version defines only metrics that the **actual** telemetry can support today, plus a clearly fenced **future** section for metrics that require telemetry or population we do not yet have. No metric is claimed as available unless the data backs it. All previously assumed-but-unsupported metrics have been demoted to placeholders or future.

---

## Table of Contents

1. [Canonical Definitions (Derivation Rules)](#1-canonical-definitions-derivation-rules)
2. [Capability Gating Principle](#2-capability-gating-principle)
3. [Supported Metrics](#3-supported-metrics)
4. [Unsupported / Placeholder Metrics](#4-unsupported--placeholder-metrics)
5. [Future Metrics](#5-future-metrics)
6. [Customer Profile Schema](#6-customer-profile-schema)
7. [Single-Customer Limitations](#7-single-customer-limitations)
8. [Calculation Conventions & Guardrails](#8-calculation-conventions--guardrails)
9. [Change Management](#9-change-management)

---

## 1. Canonical Definitions (Derivation Rules)

These definitions are binding and mirror `event_schema.md` §5/§6. All metrics below are computed **after** this derivation.

### 1.1 Campaign
```
campaign := click_action            (e.g. "PLANEXPIRY01")
```
The campaign identifier is **`click_action`**, never `label`. On a click row, `label` is the chosen action, not the campaign. (Evidence: all 13 funnel rows share `click_action = PLANEXPIRY01`.)

### 1.2 Impression
```
event_type == "Recharge floater impression"   →  impression
```

### 1.3 Skip (label-derived; NOT a native event)
```
event_type == "Recharge floater clicks"
AND  lower(label) contains any of { "skip", "dismiss" }   →  skip
```
There is no native skip event. A dismissal is logged under the click event type and is identified **only** by the `label` marker. Skip availability therefore depends on this naming convention; reclassification counts are logged as a data-quality signal.

### 1.4 Click (clicks excluding skips)
```
event_type == "Recharge floater clicks"
AND  NOT (skip rule 1.3)   →  click
```
A `Recharge floater clicks` row whose `label` is a genuine action (`Recharge-Recharge`, `Recharge-Explore all plans`). In the sample: 6 raw rows → **3 clicks + 3 skips**.

### 1.5 Funnel & parent impression
The funnel is `impression → (click | skip)`, keyed on `(customerId, campaign)`. Each click/skip is paired to its **most recent preceding impression of the same campaign** (backward as-of join). Orphans (no preceding impression) are excluded from latency and first-exposure metrics.

---

## 2. Capability Gating Principle

A metric is computed **only when its source events are present** in the input. When the required event is absent:

- the metric is emitted as an explicit **placeholder** (`NaN` / `<NA>` / `None`), **never a misleading `0`**;
- a **warning is logged** naming the missing capability;
- the **same code computes the metric unchanged** once the telemetry arrives.

This cleanly separates "not tracked yet" from "genuinely zero", and is why `skip`/`conversion`-dependent metrics degrade gracefully rather than reporting false values.

---

## 3. Supported Metrics

All percentages are 0–100; scores noted as 0–1 where applicable. Zero-denominator → `null` (excluded from rollups). Times are seconds, derived from `event_timestamp` (epoch-ms) deltas ÷ 1000.

> **Skip-dependent metrics** (`total_skips`, `skip_rate`, `avg_time_to_skip_sec`, `attention_score`) are **supported but label-derived** (§1.3). They are correct for data that follows the `*-skip` convention and degrade to placeholders when no skip markers are found.

| # | Metric | Formula | Unit | Notes |
|---|--------|---------|------|-------|
| 1 | `total_impressions` | `count(event == impression)` | int | Per customer (and per campaign). Sample: 7. |
| 2 | `total_clicks` | `count(event == click)` after skip exclusion | int | Sample: 3 (not 6). |
| 3 | `total_skips` | `count(event == skip)` (label-derived) | int | Sample: 3. Placeholder if no skip markers. |
| 4 | `ctr` | `total_clicks / total_impressions * 100` | % | Sample: 3/7 = 42.9%. |
| 5 | `skip_rate` | `total_skips / total_impressions * 100` | % | Label-derived. Sample: 3/7 = 42.9%. |
| 6 | `repeat_impression_rate` | `repeat_impressions / total_impressions * 100` | % | `repeat = impression_seq > 1` per `(customer, campaign)`. |
| 7 | `avg_time_to_click_sec` | `mean(click_ts − parent_impression_ts) / 1000` | sec | Parent = preceding impression of same campaign. |
| 8 | `avg_time_to_skip_sec` | `mean(skip_ts − parent_impression_ts) / 1000` | sec | Label-derived skips. |
| 9 | `attention_score` | `clicks / (clicks + skips)` | 0–1 | Of reactors, share that engaged. Label-derived. Sample: 3/6 = 0.5. |
| 10 | `exploration_score` | `unique_campaigns_clicked / unique_campaigns_seen` | 0–1 | Breadth of interest. Degenerate at 1 campaign (§7). |
| 11 | `campaign_diversity_score` | `unique_campaigns_clicked / unique_campaigns_seen` | 0–1 | Business-friendly **alias of `exploration_score`** (same formula). |
| 12 | `avg_impressions_before_click` | `mean(impression_seq at first click)` over clicked campaigns | float | Exposures needed before first click. `NaN` for non-clickers. |
| 13 | `first_impression_success_rate` | `% of a customer's clicked campaigns clicked on exposure #1` | % | **Per-customer** rate. `NaN` for non-clickers. (Population version is future — §5.) |
| 14 | `delayed_responder_flag` | `avg_impressions_before_click > T_imp` OR `avg_time_to_click_sec > T_sec` | bool | Defaults `T_imp = 3`, `T_sec = 60`; config-driven. Non-clickers → False. |

**Supporting fields also produced:** `repeat_impressions`, `unique_campaigns_seen`, `unique_campaigns_clicked`, `avg_session_depth` (events ÷ distinct sessions), `first_seen`, `last_seen`, `first_impression_success` (bool: clicked any campaign on exposure #1).

---

## 4. Unsupported / Placeholder Metrics

These are emitted as **placeholders** (`NaN`/`<NA>`/`None`) per §2 and MUST NOT be reported as real values.

| Metric | Status | Why unavailable |
|--------|--------|-----------------|
| `conversion_rate` | ❌ Placeholder | **No conversion telemetry.** `recharge_success` / `ott_subscription_success` are absent; `Recharge initiated` is intent, not a completed outcome (`event_schema.md` §10). With zero conversions, `conversions / clicks` is undefined. Activates when future conversion events arrive. |
| `loyalty_score` | ❌ Not defined / unsupported | **No formula exists** in this contract — `loyalty_score` was only ever *named* in the old event schema, never defined. It also has no meaning on a single customer. It is removed from the supported set until (a) a formula is ratified here and (b) multi-customer data exists. |
| `fatigue_score` | ❌ Placeholder | The §"future" composite needs **(i)** `skip_rate` (label-derived, ok), **(ii)** a **temporal `ctr_decline`** (previous-period CTR − current, owned by the Analytics Engine, not a single snapshot), and **(iii)** **population min-max normalisation**. With one customer and one snapshot, (ii) and (iii) cannot be formed. |
| Segmentation outputs (`segments`, `primary_segment`, segment distribution) | ❌ Placeholder | Rule-based segments depend on **population quartiles** (e.g. "Resistant Users" = top-quartile impressions) and on skip/CTR thresholds applied across customers. A **single customer** provides no population to rank against, so quartile-based segments and the segment distribution are not computable. Emitted as empty/`None`. |

> These placeholders are intentional and explainable: they signal *missing capability*, not failure. Removing them would either fabricate values (a `0` that looks like a real zero) or hide the gap.

---

## 5. Future Metrics

Defined for forward-compatibility; **not computed today**. They activate automatically when their precondition (conversion telemetry, multi-period history, or multi-customer population) is met — no formula change required.

| Metric | Precondition | Formula |
|--------|--------------|---------|
| `conversion_rate` | Conversion events | `conversions / clicks * 100` |
| `campaign_persistence_score` | Conversion events | `conversions / impressions` |
| `total_conversions` | Conversion events | `count(event == conversion)` |
| `fatigue_score` | Skip + multi-period + population | `w1·norm(repeat_impression_rate) + w2·norm(skip_rate) + w3·norm(ctr_decline)`; defaults `w1,w2,w3 = 0.4,0.4,0.2` |
| `campaign_fatigue_index` | Multi-campaign / population | `repeat_impression_rate × skip_rate` |
| `engagement_momentum` | Multi-period | `current_ctr − previous_ctr` |
| `skip_velocity` | Multi-period | `current_skip_rate − previous_skip_rate` |
| `campaign_saturation_level` | Multi-customer | `total_impressions / unique_customers` |
| `first_impression_success_rate` (population) | Multi-customer | `users_clicking_on_first_exposure / total_users * 100` |
| `delayed_engagement_rate` (population) | Multi-customer | `users_clicking_after_multiple_exposures / total_users * 100` |
| Rule-based segments | Multi-customer population | quartile + threshold rules (Highly Engaged, Passive, High Skip, Fatigued, Fast Click, Explorers, Selective, Resistant) |

---

## 6. Customer Profile Schema

One row per `customerId`, produced by Feature Extraction (matches `analytics/feature_extractor.py`). Column order is binding. **S** = supported today (§3), **P** = placeholder (§4), **F** = filled by a later layer / future (§5).

| Field | Type | Class | Notes |
|-------|------|-------|-------|
| `customerId` | string | S | Identity. |
| `first_seen` | datetime (UTC) | S | First impression timestamp. |
| `last_seen` | datetime (UTC) | S | Latest event timestamp. |
| `total_impressions` | int | S | |
| `total_clicks` | int | S | Post skip-exclusion. |
| `total_skips` | int \| `<NA>` | S* | Label-derived; placeholder if no markers. |
| `total_conversions` | int \| `<NA>` | P | No conversion telemetry. |
| `repeat_impressions` | int | S | |
| `unique_campaigns_seen` | int | S | |
| `unique_campaigns_clicked` | int | S | |
| `ctr` | float (%) | S | |
| `skip_rate` | float (%) | S* | Label-derived. |
| `conversion_rate` | float (%) | P | |
| `repeat_impression_rate` | float (%) | S | |
| `avg_time_to_click_sec` | float | S | |
| `avg_time_to_skip_sec` | float | S* | Label-derived. |
| `avg_session_depth` | float | S | Events ÷ distinct sessions. |
| `fatigue_score` | float | P | Needs population + temporal CTR decline. |
| `attention_score` | float (0–1) | S* | Label-derived. |
| `first_impression_success` | bool | S | Clicked any campaign on exposure #1. |
| `exploration_score` | float (0–1) | S | |
| `segments` | array<string> | F | Segmentation layer (needs population). |
| `primary_segment` | string \| null | F | Segmentation layer. |
| `profile_updated_at` | datetime (UTC) | S | Lineage. |
| `avg_impressions_before_click` | float | S | `NaN` for non-clickers. |
| `campaign_diversity_score` | float (0–1) | S | Alias of `exploration_score`. |
| `first_impression_success_rate` | float (%) | S | Per-customer rate. |
| `delayed_responder_flag` | bool | S | Config-driven thresholds. |

`*` = supported but label-derived (degrades to placeholder when skip markers are absent).

---

## 7. Single-Customer Limitations

The validated sample is **one customer** (`1015289504`), **one campaign** (`PLANEXPIRY01`), **5 sessions**, **13 usable funnel events**, over a **single ~4-hour window**, with **zero conversions**. This bounds what the platform can truthfully report from this data:

1. **No population → no segmentation.** Quartile-based segments (e.g. Resistant Users) and the segment distribution require many customers. Not computable.
2. **No population → no fatigue normalisation.** `fatigue_score`'s min-max normalisation and `campaign_fatigue_index`'s cross-campaign comparison have no population to scale against.
3. **No cross-user rates.** Population `first_impression_success_rate` and `delayed_engagement_rate` (users-clicking ÷ total-users) are trivially degenerate with N=1.
4. **Single campaign → degenerate diversity.** `exploration_score` / `campaign_diversity_score` collapse to 0 or 1 and are not meaningful.
5. **Single snapshot → no trends.** `engagement_momentum`, `skip_velocity`, and any period-over-period delta need ≥2 comparable windows.
6. **Tiny N → illustrative only.** With 13 funnel events, every rate (CTR 42.9%, skip 42.9%, attention 0.5) is a demonstration of the pipeline, **not a statistically reliable measurement**.
7. **No conversions → outcome metrics blank.** `conversion_rate`, `campaign_persistence_score`, `total_conversions` stay placeholders.

**Required to lift these limits:** a **multi-customer, multi-campaign, multi-period extract** with (eventually) real conversion events.

---

## 8. Calculation Conventions & Guardrails

**Conventions**
- `event_timestamp` is epoch **milliseconds**, treated as UTC; dashboard renders IST (Asia/Kolkata).
- Percentages 0–100; ratios/scores 0–1 where noted.
- Zero-denominator metrics return `null` and are excluded from rollups.
- Latency metrics use the backward as-of parent-impression join (§1.5); orphans excluded.
- All thresholds/weights are configuration-driven; the defaults here are the contract baseline.

**Explainability**
- Every metric exposes its numerator and denominator.
- Every placeholder logs the missing capability that blocks it.
- Skip reclassification counts are logged each run (data-quality signal).

**Platform guardrails (non-negotiable)**
- ❌ No recommendation systems · ❌ No personalization engines · ❌ No campaign delivery / marketing automation · ❌ No ML-based targeting.
- ✅ Analytics-first, rule-based, explainable, business-readable.

**Assumptions explicitly removed in v2.0** (were in v1.x, contradicted by data)
- ❌ `skip` / `dismiss_popup` as first-class events → skip is **label-derived**.
- ❌ `campaign` as a dedicated clean field → campaign is **`click_action`**.
- ❌ `floater_impression` / `floater_click` raw names → actual names are `Recharge floater impression` / `Recharge floater clicks`.
- ❌ ISO-8601 UTC `timestamp` → epoch-ms `event_timestamp`.
- ❌ Conversion events present → **absent** (future).
- ❌ `loyalty_score` as a supported metric → **undefined**, removed.
- ❌ Population segments / fatigue normalisation on this sample → require multi-customer data.

---

## 9. Change Management

This contract is **frozen** as the source of truth for all development. Any change to derivation rules, metric formulas, the profile schema, supported/future classification, or guardrails requires a **version bump** and a matching update to `event_schema.md`. New telemetry capabilities (conversions, multi-customer, multi-period) **promote** metrics from §4/§5 into §3 without altering existing formulas.
</content>

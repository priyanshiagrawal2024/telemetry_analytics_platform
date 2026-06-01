# Event Schema Contract

**Document type:** Canonical telemetry event schema (single source of truth)
**Owner:** MyJio Floater Analytics Platform
**Status:** FROZEN — authoritative for all layers
**Version:** 2.0
**Frozen on:** 2026-06-01
**Supersedes:** v1.x (pre-validation assumptions)
**Validated against:** `sample_data/telemetry_sample.csv` (the real MyJio export) — see `docs/telemetry_data_findings.md`.

> This schema describes the **actual** MyJio floater telemetry as it exists today, not an idealised contract. Where the real data contradicts earlier assumptions, the real data wins. Every downstream layer (Ingestion, Preprocessing, Feature Extraction, Analytics Engine, Insight Agent, Dashboard) MUST conform to this document.

---

## Table of Contents

1. [File Format & Loading](#1-file-format--loading)
2. [Core Event Fields](#2-core-event-fields)
3. [Floater / Campaign Fields](#3-floater--campaign-fields)
4. [Observed Raw Event Types](#4-observed-raw-event-types)
5. [Canonical Event Derivation](#5-canonical-event-derivation)
6. [Campaign Identifier](#6-campaign-identifier)
7. [Quarantine Event Families](#7-quarantine-event-families)
8. [Customer Journey Definition](#8-customer-journey-definition)
9. [Timestamps](#9-timestamps)
10. [Future Conversion Events](#10-future-conversion-events)
11. [Data Quality Constraints](#11-data-quality-constraints)
12. [Sample Dataset Reality](#12-sample-dataset-reality)

---

## 1. File Format & Loading

**The shipped export is an XLSX workbook disguised with a `.csv` extension.**

- The file begins with the ZIP magic bytes `PK\x03\x04` (OOXML/XLSX containers are ZIP archives). A genuine CSV would begin with the header text (`customerId,...`).
- The `.csv` extension is **not trustworthy** and MUST NOT be used to choose a reader.

**Mandatory loading strategy (implemented in `ingestion/loader.py`):**

1. Read the first 2 bytes of the file.
2. If they equal `b"PK"` → load with `pandas.read_excel(path, engine="openpyxl")`.
3. Otherwise → load with `pandas.read_csv(path)`.

`openpyxl` is a **hard dependency**. Only sheet 0 is read. Producers may change the real format at any time; the magic-byte sniff is the contract, not the extension.

---

## 2. Core Event Fields

The export contains 23 columns. The fields below are the ones the platform relies on. Columns not listed here (`event_category`, `source`, `version`, `txId`, `clientId`, `androidId`, `swipe`, `type`, `api_request`, `additional_info`, `extra_info`) are retained on the raw record for lineage but are **not** consumed by analytics.

| Field             | Type             | Required (analytics) | Description |
| ----------------- | ---------------- | -------------------- | ----------- |
| `customerId`      | int / string     | ✅ Required           | Stable customer identifier. |
| `event_type`      | string           | ✅ Required           | Raw client event name (see §4). Drives canonical derivation. |
| `event_timestamp` | int64 (epoch ms) | ✅ Required           | Event time as **epoch milliseconds** (see §9). |
| `sessionId`       | string (UUID)    | ⬜ Funnel-required     | App session id. Present on floater funnel rows; **NULL on many lifecycle/API rows**. |
| `newscreen_name`  | string           | ⬜ Optional            | Screen/surface that generated the event. |
| `platform`        | string           | ⬜ Optional            | `Android` / `iOS`. |
| `os`              | string           | ⬜ Optional            | Device OS. |

**Required-for-feature-extraction set:** `customerId`, `event_type`, `event_timestamp`. The floater funnel additionally relies on `click_action`, `label`, and `sessionId` (§3, §8).

---

## 3. Floater / Campaign Fields

| Field          | Type   | Required | Description |
| -------------- | ------ | -------- | ----------- |
| `click_action` | string | ⬜        | **The campaign identifier** for floater funnel rows (e.g. `PLANEXPIRY01`). See §6. |
| `label`        | string | ⬜        | The **action chosen** on a floater event — NOT the campaign. Encodes the skip signal (e.g. `Recharge-skip`). See §5. |

> ⚠️ **`label` is not the campaign.** On a click row, `label` holds the chosen CTA (`Recharge-Recharge`, `Recharge-Explore all plans`, `Recharge-skip`). The campaign lives in `click_action`. Using `label` as the campaign key is incorrect.

---

## 4. Observed Raw Event Types

30 distinct `event_type` values appear in the validated sample. Only **two** form the floater behavioural funnel:

| Raw `event_type`              | Count | Role |
| ----------------------------- | ----- | ---- |
| `Recharge floater impression` | 7     | **Impression** (canonical). |
| `Recharge floater clicks`     | 6     | **Click OR Skip** — disambiguated by `label` (§5). |

All other event types are **quarantined** (§7) or reserved as **future** signals (`Recharge initiated`, §10).

---

## 5. Canonical Event Derivation

The platform recognises three canonical behavioural events derivable today: **`impression`**, **`click`**, **`skip`**. (`conversion` is future — §10.) Derivation is performed in the Preprocessing layer and is **authoritative**:

### 5.1 Impression
```
event_type == "Recharge floater impression"   →  impression
```
Clean and unambiguous. In the sample all 7 impressions carry `label = "Recharge"` and `click_action = "PLANEXPIRY01"`.

### 5.2 Skip (label-derived — there is NO native skip event)
```
event_type == "Recharge floater clicks"
AND  lower(label) contains any of { "skip", "dismiss" }   →  skip
```
A dismissal is logged under the **same** `event_type` as a click; the only signal is the `label` marker (e.g. `Recharge-skip`). Skip is therefore a **derived** event, dependent on the label naming convention.

### 5.3 Click (clicks minus skips)
```
event_type == "Recharge floater clicks"
AND  NOT (skip rule above)   →  click
```
i.e. a `Recharge floater clicks` row whose `label` is a genuine action (`Recharge-Recharge`, `Recharge-Explore all plans`, …). In the sample, 6 raw click rows resolve to **3 clicks + 3 skips**.

### 5.4 Derivation rules (binding)
- Matching is **case-insensitive** on a trimmed, lower-cased value.
- The skip rule is evaluated **before** counting clicks; a row is never both.
- The skip markers (`skip`, `dismiss`) are **configuration-driven** but default to this set.
- The number of click→skip reclassifications MUST be **logged as a data-quality signal** so a future change to label naming is detected, not silently mis-counted.
- If no skip markers are found in a dataset, `skip` is treated as **absent** (not zero) — see analytics contract capability gating.

---

## 6. Campaign Identifier

```
campaign := click_action
```

- The stable campaign key is **`click_action`** (e.g. `PLANEXPIRY01`).
- Confirmed in data: all 13 recharge-floater funnel rows (7 impressions + 6 clicks/skips) share `click_action = "PLANEXPIRY01"`, while their `label` varies by action.
- A missing `click_action` is replaced with a defined sentinel (`__unknown_campaign__`) so campaign-scoped logic stays well-defined; such rows are excluded from per-campaign reporting.

---

## 7. Quarantine Event Families

These event types are **legitimate telemetry but carry no floater click/skip funnel**. They are dropped from behavioural analytics (debug-logged), never counted in metrics, and never treated as "unknown/error".

| Family | Event types (observed) |
| ------ | ---------------------- |
| **Floater API plumbing** | `FloaterResponse`, `Floater API called`, `Floater API response received`, `campaign_response_received`, `campaign_response_received_empty`, `campaign_saved_in_db` |
| **Home / API status** | `HomeAPI Request Body`, `HOME_API_STATUS-*` (all variants), `ENTERTAINMENT_API_STATUS-*`, `BurgerMenu API called`, `BurgerMenu API called-success--{body_notEmpty}` |
| **App lifecycle** | `App open`, `App background`, `App closed` |
| **Other surfaces / navigation** | `Navigation_superapp`, `Home_superapp`, `Home`, `JioCloud_login`, `JioCloud_onboarding`, `Cloud_registered`, `Cloud_not_registered`, `JioTune activated no` |

**Other-surface impressions (explicitly out of scope).** Some `Navigation_superapp` / `Home_superapp` rows carry `newscreen_name = "impression"` (labels `home`, `JioTunes_27042026`; `click_action` = `impression` / `entertainment impression`). These are impressions of **other surfaces** (home nav, JioTunes) with **no click/skip funnel**. They are **excluded** from floater impression metrics (they would inflate denominators with no possible reaction). They MAY seed a separate "surface reach" metric in the future — a distinct, future scope item, not part of this funnel.

Any `event_type` not mapped (§5) and not listed above is **quarantined as unknown** and logged with a sample of offending values.

---

## 8. Customer Journey Definition

A customer journey is the time-ordered sequence of a single customer's events, scoped by session and joined into a funnel by campaign:

```
customerId
   └── sessionId (chronological)
          └── floater funnel, keyed on (customerId, click_action):
                 impression  →  (click | skip)
```

- The funnel parent is the **most recent preceding impression of the same `click_action`** (backward as-of join on `(customerId, campaign)`).
- A click/skip with no preceding impression is an **orphan** and is excluded from latency/first-exposure metrics.
- `impression_seq` numbers impressions 1..N per `(customerId, click_action)` chronologically; `is_repeat_impression = impression_seq > 1`.
- **`Recharge initiated` is a downstream intent signal, NOT part of the click/skip funnel and NOT a conversion** (§10).

---

## 9. Timestamps

- **`event_timestamp` is epoch milliseconds** (`int64`, e.g. `1779255760042`). Convert with `unit="ms"`, treat as UTC.
- The string `timestamp`, `timestamp_ist`, and `IST` columns are **unreliable in the sample** (`timestamp_ist` is empty/NaN). Do **not** depend on them; derive IST for display from `event_timestamp` (Asia/Kolkata) at the dashboard layer only.
- Rows with a non-numeric `event_timestamp` are dropped (logged).

---

## 10. Future Conversion Events

No conversion telemetry exists in the current export.

- `recharge_success`, `ott_subscription_success` (and Fiber / UPI activations) are **absent**.
- `Recharge initiated` (2 rows) is **intent**, not a completed outcome, and is **not** mapped to `conversion`.

When production telemetry begins emitting true completion events, they map to canonical `conversion`:

| Future raw event           | Canonical    | `conversion_type`  |
| -------------------------- | ------------ | ------------------ |
| `recharge_success`         | `conversion` | `recharge`         |
| `ott_subscription_success` | `conversion` | `ott_subscription` |
| `fiber_activation_success` | `conversion` | `fiber`            |
| `upi_success`              | `conversion` | `upi`              |

Until then, all conversion-dependent metrics are **placeholders** (see analytics contract).

---

## 11. Data Quality Constraints

- `event_timestamp` must be numeric and not in the future (5-minute clock-skew tolerance).
- Floater funnel rows with NULL `sessionId` are excluded from session-scoped metrics; the large NULL-`sessionId` bucket is dominated by lifecycle/API rows that are quarantined anyway.
- Skip-reclassification counts are logged each run as a data-quality signal (§5.4).
- Unknown event types are quarantined and reported, never counted.
- Enrichment fields not present in the source (`event_id`, `mapping_version`, `ingested_at`, `conversion_type`, attribution fields) are **synthesised downstream**, not expected from the producer.

---

## 12. Sample Dataset Reality

The validated sample is small and **single-customer** — this constrains what can be claimed:

| Measure | Value |
| ------- | ----- |
| Rows | 194 |
| Distinct `event_type` | 30 |
| Unique `customerId` | **1** (`1015289504`) |
| Unique `sessionId` | 5 (+ a NULL bucket of 32 rows) |
| Time span | 2026-05-20, ~4h20m (single day) |
| Floater funnel events | 13 (7 impressions, 3 clicks, 3 skips) |
| Campaigns in funnel | 1 (`PLANEXPIRY01`) |
| Conversions | 0 |

Any population-relative or cross-customer analytics (segmentation quartiles, min-max-normalised fatigue, cross-user rates) require a **multi-customer extract** before they are meaningful. See the analytics contract §"Single-Customer Limitations".
</content>

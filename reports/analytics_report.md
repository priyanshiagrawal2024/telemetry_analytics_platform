# MyJio Floater Analytics — Report

**Dataset:** myjio_floater  
**Source:** C:\Users\Priyanshi.Agrawal\telemetry_analytics_platform\sample_data\telemetry_sample.csv  
**Generated at:** 2026-06-02T10:39:25.978555+00:00

> Descriptive analytics report. All figures are produced by the analytics pipeline and restated here as-is; unavailable metrics are shown as `N/A`. No intent, attention, dwell-time, emotion, accidental-click, or user-journey inference is made.

---

## 1. Dataset Summary

| Measure | Value |
|---------|-------|
| Customers | 1 |
| Events | 194 |
| Campaigns | 4 |
| Sessions | 5 |

## 2. Customer Analytics Summary

### Customer `1015289504`

**Key metrics**

| Metric | Value |
|--------|-------|
| Impressions | 7 |
| Clicks | 3 |
| Skips | 3 |
| CTR | 42.9% |
| Sessions | 5 |
| Campaigns reached | 4 |
| Exposure frequency (per session) | 1.40 |
| Interaction frequency (per session) | 1.20 |
| Campaign diversity (0-1) | 0.12 |

**Key scores**

| Score | Value |
|-------|-------|
| Engagement score (0-1) | 0.289 |
| Exploration score (0-1) | 0.261 |
| Campaign receptiveness score (0-1) | 0.353 |

## 3. Campaign Analytics Summary

**Campaigns:** 4

**Campaign reach** (distinct customers reached per campaign)

| Campaign | Customers reached |
|----------|-------------------|
| 200PlanFloater_forGeminien_US | 1 |
| 5000GB_GetJioHomeen_US | 1 |
| PLANEXPIRY01 | 1 |
| iActivate_Fiber usersen_US | 1 |

**Campaign interaction:** per-campaign interaction is not produced at campaign grain in the current pipeline. Customer-grain interaction frequency averages **1.20** interaction(s) per session across the dataset (see §4).

## 4. Top Metrics

_Dataset averages (equal to the single customer's values when the population is one)._

| Metric | Value |
|--------|-------|
| Click-through rate (CTR) | 42.9% |
| Campaign diversity (0-1) | 0.12 |
| Exposure frequency (impressions/session) | 1.40 |
| Interaction frequency (per session) | 1.20 |

## 5. Top Insights

**Customer `1015289504`**

- **Engagement with floaters** — Of 7 floaters shown, the customer clicked 3 (43% CTR) and skipped 3 (43% skip rate) — an even click/skip split.
- **Campaign reach & diversity** — Reached by 4 distinct campaign(s); exposure is concentrated on a few campaigns (diversity 0.12).
- **Exposure vs interaction** — Across 5 session(s), the customer saw ~1.4 floater(s) per session and acted on ~1.2; more floaters were shown than were acted on.
- **Session activity** — Logged 194 events across 5 session(s), averaging 39 per session.
- **Clicks after repeat exposure** — On average a campaign was shown ~2.0 time(s) before its first click.

## 6. Analytics Observations

**Customer `1015289504` — at a glance**

- 43% CTR with an even click/skip split across 7 floaters.
- Reached 4 campaign(s); exposure concentrated on a few campaigns.
- ~1.4 floaters/session over 5 session(s); more floaters were shown than were acted on.
- First click came after ~2.0 exposure(s) of a campaign.

**Data sufficiency:** this run validates platform capability rather than business performance. Population benchmarking needs multiple customers, campaign comparison needs multiple campaigns, segmentation needs a larger population, and conversion analytics need conversion events. Figures here are illustrative of the pipeline, not a representative business read.

# MyJio Floater Analytics — Project Context

**Document type:** Project context & foundational reference
**Project:** MyJio Floater Analytics Platform
**Status:** Active development
**Audience:** Business stakeholders, product owners, data & platform engineers
**Companion document:** [analytics_contract.md](analytics_contract.md) (canonical metric/schema definitions)

> This document explains **why** the platform exists and **how** it is structured. It is business-oriented but written to be directly actionable for engineering implementation. The binding technical definitions (metrics, schemas, segment rules) live in the analytics contract; this document provides the surrounding context.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Problem Statement](#2-problem-statement)
3. [Objectives](#3-objectives)
4. [Scope](#4-scope)
5. [Out Of Scope](#5-out-of-scope)
6. [Business Questions](#6-business-questions)
7. [System Architecture](#7-system-architecture)
8. [Data Flow](#8-data-flow)
9. [Customer Profile Concept](#9-customer-profile-concept)
10. [Global Profile Concept](#10-global-profile-concept)
11. [Dashboard Overview](#11-dashboard-overview)
12. [AI Insight Agent Overview](#12-ai-insight-agent-overview)
13. [Technology Stack](#13-technology-stack)
14. [Team Responsibilities](#14-team-responsibilities)

---

## 1. Project Overview

The **MyJio Floater Analytics Platform** is an AI-powered telemetry analytics system that explains how customers interact with **floaters** — in-app promotional surfaces (banners, popups, cards, interstitials) shown inside the MyJio application.

The platform ingests floater telemetry events (`impression`, `click`, `skip`, `conversion`), transforms them into behavioural features, computes campaign, engagement, fatigue, segmentation, and trend analytics, and surfaces **business-readable insights** through a dashboard and an AI insight agent.

The platform answers four core questions about every campaign and floater:

- **What** worked?
- **For whom** did it work?
- **When** did it work?
- **Why** did it work?

It is an **analytics and insight-generation platform** — not a recommendation engine, personalization system, or campaign-delivery tool.

---

## 2. Problem Statement

MyJio runs a high volume of floater campaigns across many app screens and customer segments. Today, teams can see *that* campaigns ran, but struggle to understand:

- Whether floaters are genuinely engaging customers or merely being tolerated / dismissed.
- Which customers are becoming **fatigued** from repeated, ignored exposures.
- Why some campaigns convert and others don't, and what distinguishes the customers who respond.
- How engagement is **trending** — improving, declining, or saturating over time.

Without a unified analytics layer, campaign decisions rely on raw counts and intuition. Over-serving fatigued customers erodes engagement and trust, while high-potential campaigns go unrecognised. The business needs an **explainable, behaviour-first analytics platform** that turns floater telemetry into clear, defensible insights.

---

## 3. Objectives

**Primary objective:** Generate actionable, explainable business insights from floater telemetry.

| # | Objective | Outcome |
|---|-----------|---------|
| 1 | Reliable telemetry ingestion & normalisation | Trustworthy, canonical event data. |
| 2 | Behavioural feature extraction | Per-customer and per-campaign profiles. |
| 3 | Campaign effectiveness analytics | Clear view of what worked and how well. |
| 4 | Fatigue analytics | Early detection of over-exposure / disengagement. |
| 5 | Engagement & attention analytics | Understanding depth and quality of interaction. |
| 6 | Rule-based segmentation | Explainable behavioural customer groups. |
| 7 | Trend analytics | Period-over-period direction of every metric. |
| 8 | AI-generated insights | Plain-language, evidence-backed findings. |
| 9 | Business dashboards | Self-serve visibility for stakeholders. |

**Guiding principles:** analytics-first, rule-based over ML where possible, fully explainable, production-ready and modular.

---

## 4. Scope

In scope for this platform:

- **Telemetry ingestion** of floater events from the MyJio app.
- **Preprocessing & normalisation** (event mapping, deduplication, enrichment).
- **Feature extraction** into customer and global profiles.
- **Behavioural analytics** — engagement, attention, session behaviour.
- **Campaign analytics** — CTR, conversion, persistence, momentum.
- **Fatigue analytics** — repeat exposure, skip velocity, fatigue scoring.
- **Segmentation** — rule-based behavioural segments.
- **Trend analytics** — time-series and deltas across metrics.
- **Dashboard visualizations** — executive, campaign, engagement, fatigue, segmentation, trend views.
- **Insight generation** — AI agent producing descriptive/diagnostic insights.

---

## 5. Out Of Scope

The following are explicitly **not** part of this platform:

- ❌ **Recommendation systems** — deciding what floater/campaign to show.
- ❌ **Personalization engines** — tailoring content per user.
- ❌ **Campaign delivery systems** — serving or scheduling floaters.
- ❌ **Marketing automation** — triggered journeys/messaging.
- ❌ **ML-based campaign targeting** — predictive audience selection.

The platform **describes and explains** behaviour. It never **prescribes** what to serve next. Insights may diagnose ("this campaign is fatiguing repeat viewers") but must not recommend actions of the form "show campaign X to user Y."

---

## 6. Business Questions

The platform exists to answer questions like these (each maps to defined metrics in the analytics contract):

**Campaign effectiveness**
- Which campaigns earn the most engagement (CTR) and the least rejection (skip rate)?
- Which campaigns convert clicks into recharges / OTT subscriptions most efficiently?
- Is a campaign's engagement improving or declining over time (momentum)?

**Customer behaviour**
- Who are our most engaged, most resistant, and most fatigued customers?
- How decisive is customer intent (time-to-click)?
- How broadly do customers engage across campaigns (exploration)?

**Fatigue & over-exposure**
- Which campaigns are damaging engagement through over-serving (fatigue index)?
- Is rejection accelerating (skip velocity) — an early fatigue warning?
- How much exposure load are customers carrying (saturation)?

**Timing & repetition**
- Do campaigns hook on first sight (first-impression success) or pay off only after repetition (delayed engagement)?
- When (which periods/screens) does engagement peak?

**Strategy**
- How is our customer base distributed across behavioural segments?
- Which screens drive the strongest engagement?

---

## 7. System Architecture

The platform follows a layered, modular pipeline. Each layer is independently deployable and testable.

```
            ┌─────────────────────────────┐
            │      MyJio Application       │
            │   (emits floater telemetry)  │
            └──────────────┬──────────────┘
                           │ raw events
                           ▼
            ┌─────────────────────────────┐
            │       Ingestion Layer        │  FastAPI
            │  validate · receive · queue  │
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │     Preprocessing Layer      │  event mapping,
            │ normalise · dedupe · enrich  │  quarantine
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │     Feature Extraction       │  Pandas
            │ customer & campaign features │
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │       Analytics Engine       │  core + advanced
            │ metrics · segmentation·trend │  metrics
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │      Insight Generation      │  AI Insight Agent
            │  diagnostic, explainable     │  (Google ADK)
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │          Dashboard           │  Streamlit + Plotly
            │     KPIs · trends · insights │
            └─────────────────────────────┘

   Storage: PostgreSQL (events, profiles, aggregates, insights)
   Packaging: Docker (per-service containers)
```

**Layer responsibilities**

| Layer | Responsibility |
|-------|----------------|
| Ingestion | Receive, validate, and persist raw telemetry reliably. |
| Preprocessing | Map raw → canonical events, dedupe, enrich (sequence, repeat flags, timing). |
| Feature Extraction | Build customer profiles and campaign/global feature sets. |
| Analytics Engine | Compute core/advanced metrics, segments, and trends. |
| Insight Generation | Produce plain-language, evidence-backed insights. |
| Dashboard | Visualize KPIs, trends, segments, and insights. |

---

## 8. Data Flow

End-to-end flow of a telemetry event through the platform:

1. **Emit** — MyJio app fires a raw event (e.g. `floater_impression`, `dismiss_popup`, `recharge_success`).
2. **Ingest** — Ingestion API validates the payload and persists the raw event.
3. **Normalise** — Preprocessing maps the raw event to a canonical type (`impression`, `click`, `skip`, `conversion`), deduplicates by `event_id`, and quarantines unknown events.
4. **Enrich** — Adds derived fields: impression sequence, repeat-impression flag, time-since-impression, event date, mapping version.
5. **Attribute** — Conversions are linked to the responsible click/impression using last-touch attribution windows.
6. **Extract features** — Events are aggregated into **customer profiles** and **campaign/global feature sets**.
7. **Compute analytics** — The analytics engine calculates core and advanced metrics, assigns rule-based segments, and computes trends.
8. **Generate insights** — The AI insight agent reviews metrics/trends and emits descriptive insight objects with cited evidence.
9. **Serve** — Profiles, aggregates, and insights are stored in PostgreSQL and rendered in the dashboard.

```
raw event → ingest → normalise → enrich → attribute
          → feature extraction → analytics → insights → dashboard
```

All percentage metrics are 0–100; zero-denominator metrics return `null`; timestamps are UTC (rendered IST in the dashboard). See the analytics contract for exact rules.

---

## 9. Customer Profile Concept

A **Customer Profile** is the behavioural fingerprint of a single customer (`customerId`), built by the Feature Extraction layer and refreshed on each analytics run.

It answers: *How does this customer behave toward floaters?*

It summarises:
- **Exposure** — total impressions, repeat impressions, unique campaigns seen.
- **Engagement** — clicks, CTR, attention score, time-to-click, exploration score.
- **Rejection** — skips, skip rate, time-to-skip.
- **Outcomes** — conversions and personal conversion rate.
- **Fatigue** — a composite fatigue score from repeat exposure, skipping, and CTR decline.
- **Classification** — the behavioural segments the customer qualifies for, with a primary segment.

The profile is the **primary input to segmentation** and the unit of "for whom did it work?" analysis. Every classification carries the rule snapshot that produced it, keeping the profile fully explainable. (Field-level schema: analytics contract §5.)

---

## 10. Global Profile Concept

A **Global Profile** is the aggregate behavioural picture across many customers, computed at multiple grains: **platform-wide**, **per campaign**, **per screen**, and **time-bucketed** (daily/weekly).

It answers: *How is a campaign / the platform performing overall, and how is it trending?*

It summarises:
- **Volume** — customers, impressions, clicks, skips, conversions in the window.
- **Effectiveness** — aggregate CTR, skip rate, conversion rate, persistence.
- **Fatigue** — campaign fatigue index, saturation, skip velocity.
- **Trend** — engagement momentum and period-over-period deltas.
- **Population** — segment distribution.

The global profile powers the dashboard KPIs and trend analytics, and is the unit of "what worked and when" analysis. (Field-level schema: analytics contract §6.)

---

## 11. Dashboard Overview

The dashboard (Streamlit + Plotly) is the self-serve window into the platform for business and product stakeholders. It is organised into six sections:

| Section | Purpose | Key Visuals |
|---------|---------|-------------|
| **Executive Summary** | Headline health at a glance | CTR, skip rate, conversions, momentum |
| **Campaign Analytics** | Per-campaign performance | Funnel, CTR/conversion bars, ranking |
| **Engagement Analytics** | Quality & depth of interaction | Attention score, time-to-click, click efficiency |
| **Fatigue Analytics** | Over-exposure & disengagement | Fatigue index heatmap, skip velocity, saturation |
| **Segmentation** | Who our customers are | Segment distribution, per-segment metrics |
| **Trends** | Direction over time | Time-series + period-over-period deltas |

Every KPI on the dashboard maps to a defined metric in the analytics contract, exposes its numerator/denominator on drill-down, and is accompanied by relevant AI-generated insights.

---

## 12. AI Insight Agent Overview

The **AI Insight Agent** (built on Google ADK) sits in the Insight Generation layer. It converts computed metrics, trends, and segments into **plain-language, evidence-backed findings** that a business reader can act on.

**What it does**
- Scans metrics and trends for notable patterns (e.g. rising fatigue index, negative momentum, strong first-impression success).
- Produces **insight objects**: a headline, an explanation, the **evidence metrics** that triggered it, affected segments, and a severity.
- Prioritises insights by business impact and surfaces them in the dashboard.

**What it must not do (guardrails)**
- It is **descriptive and diagnostic only**. It explains *what happened and why*.
- It must **never** produce prescriptive recommendations of the form "show campaign X to user Y," or any targeting/personalization output.
- Every insight must **cite the evidence metrics** it is based on — no unsupported claims.

This keeps the platform aligned with its analytics-first mandate while making findings accessible to non-technical stakeholders. (Insight object schema: analytics contract §11.4.)

---

## 13. Technology Stack

| Layer / Concern | Technology |
|-----------------|------------|
| Language | Python |
| API / Ingestion | FastAPI |
| Data processing | Pandas |
| Storage | PostgreSQL |
| Visualization | Plotly |
| Dashboard | Streamlit |
| AI insight agent | Google ADK |
| Packaging / deployment | Docker |

**Conventions**
- Each pipeline layer is a modular, independently testable component.
- Services are containerised (Docker) for reproducible deployment.
- Timestamps stored in UTC; rendered in IST (Asia/Kolkata) at the dashboard.
- Metric formulas, schemas, and segment rules are governed exclusively by the analytics contract.

---

## 14. Team Responsibilities

| Role | Responsibilities |
|------|------------------|
| **Product Owner** | Owns business questions, prioritisation, and success criteria; signs off on the analytics contract. |
| **Data / Ingestion Engineer** | Builds and operates ingestion + preprocessing; guarantees data quality, event mapping, and deduplication. |
| **Analytics Engineer** | Implements feature extraction, core/advanced metrics, segmentation, and trend analytics per the contract. |
| **AI / Insight Engineer** | Builds the AI Insight Agent (Google ADK); ensures insights are explainable and within guardrails. |
| **Dashboard / Frontend Engineer** | Builds Streamlit/Plotly dashboards; maps every KPI to a defined metric. |
| **Platform / DevOps Engineer** | Owns Docker packaging, deployment, PostgreSQL operations, and monitoring. |
| **Business / Campaign Analyst** | Consumes dashboards and insights; translates findings into business action (outside the platform). |

**Shared responsibilities**
- Keep the **analytics contract** authoritative — no metric, field, or segment exists outside it.
- Uphold the **guardrails**: analytics-first, rule-based, explainable; no recommendation, personalization, delivery, automation, or ML-targeting.
- Maintain modularity and production-readiness across all layers.

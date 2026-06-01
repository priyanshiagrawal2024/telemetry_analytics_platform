# Event Schema Contract

## Purpose

This document defines the canonical telemetry event schema used throughout the Telemetry Analytics Platform.

All modules (Analytics Engine, API Layer, Database Layer, Dashboard, and Agent) must conform to this schema.

---

# 1. Core Event Fields

| Field           | Type     | Required | Description                     |
| --------------- | -------- | -------- | ------------------------------- |
| customerId      | string   | ✅        | Unique customer identifier      |
| sessionId       | string   | ✅        | Application session identifier  |
| event_type      | string   | ✅        | Raw telemetry event name        |
| event_timestamp | long     | ✅        | Epoch timestamp in milliseconds |
| timestamp_ist   | datetime | ✅        | Human-readable IST timestamp    |
| platform        | string   | ✅        | Android / iOS                   |
| os              | string   | ✅        | Device operating system         |
| newscreen_name  | string   | ⬜        | Screen generating event         |

---

# 2. Floater Fields

| Field           | Type   | Required | Description                   |
| --------------- | ------ | -------- | ----------------------------- |
| label           | string | ⬜        | Campaign / floater label      |
| click_action    | string | ⬜        | Deep link / CTA action        |
| api_request     | string | ⬜        | Associated API request        |
| additional_info | json   | ⬜        | Campaign metadata             |
| extra_info      | json   | ⬜        | Additional telemetry metadata |

---

# 3. Raw Event Types

Observed event types include:

* Recharge floater impression
* Recharge floater clicks
* FloaterResponse
* campaign_response_received
* campaign_saved_in_db
* Floater API called
* Floater API response received

---

# 4. Analytics Mapping

| Analytics Event   | Raw Event Types             |
| ----------------- | --------------------------- |
| impression        | Recharge floater impression |
| click             | Recharge floater clicks     |
| campaign_served   | FloaterResponse             |
| campaign_received | campaign_response_received  |
| campaign_saved    | campaign_saved_in_db        |

---

# 5. Metrics Supported

This schema supports calculation of:

* Impression Count
* Click Count
* CTR
* Skip Rate
* Repeat Impression Rate
* Average Time To Click
* Attention Score
* Exploration Score
* Loyalty Score
* Campaign Fatigue Index
* Average Impressions Before Click

---

# 6. Future Extension

Future production telemetry may include:

* Recharge Success
* OTT Activation
* Fiber Activation
* UPI Success

These events will enable true conversion analytics.

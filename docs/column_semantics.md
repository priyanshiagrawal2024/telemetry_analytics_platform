# Column Semantics & the Configurable Analytics Framework

**Status:** Design note
**Companions:** [`contracts/column_registry.yaml`](../contracts/column_registry.yaml),
[`contracts/event_schema.md`](../contracts/event_schema.md)

This note explains *why* the platform reasons about **semantic column roles**
instead of hardcoded column names, how that makes the same analytics framework
reusable across domains (telecom, finance, healthcare, ecommerce), and how the
**column registry** delivers that configurability.

---

## 1. Why column semantics are useful

Telemetry from different products uses different column **names** for the same
underlying **concepts**:

| Concept (semantic role) | Telecom (MyJio) | Finance | Healthcare | Ecommerce |
|-------------------------|-----------------|---------|------------|-----------|
| `entity_id`  | `customerId`   | `account_id`   | `patient_id`     | `user_id`        |
| `session_id` | `sessionId`    | `session_token`| `encounter_id`   | `visit_id`       |
| `event_time` | `event_timestamp` | `posted_at` | `recorded_at`  | `event_ts`       |
| `event_name` | `event_type`   | `txn_type`     | `observation`    | `action`         |
| `group_id`   | `click_action` | `product_code` | `treatment_id`   | `campaign`       |
| `action_label` | `label`      | `txn_status`   | `result_flag`    | `cta`            |

If analytics code references `customerId` or `click_action` directly, it is
permanently coupled to one domain. Every new dataset would mean editing the
core logic — fragile and unscalable.

Naming the **role** each column plays decouples *meaning* from *spelling*. The
framework asks "which column is the `entity_id`?" and the registry answers. The
business logic ("count distinct entities", "group events by `group_id`",
"derive behaviour from `event_name`") is written **once**, against roles.

A second benefit is **explainability and governance**: the registry is a single,
reviewable source of truth for what every column means, its type, and whether it
is required — independent of any code.

---

## 2. How future domains reuse the same framework

Onboarding a new domain is a **configuration** task, not a coding task:

1. Take the new dataset's columns.
2. Write a `column_registry.yaml` that maps each raw column to a role from the
   shared vocabulary (`entity_id`, `event_time`, `event_name`, `group_id`, …).
3. Point the framework at that registry.

No framework code changes. The pipeline resolves the right columns by role:

```
raw columns ──(domain registry: column → semantic_role)──► semantic roles
                                                                  │
                                       framework logic keyed on roles only
                                                                  │
                                          metrics / features / analytics
```

Because every domain maps onto the **same** role vocabulary, the same
ingestion, normalization, feature-extraction, and analytics components serve
all of them. A finance dataset and a healthcare dataset run through identical
code; only their registries differ.

> The role vocabulary is intentionally domain-neutral. `group_id` means "the
> campaign / treatment / product an event belongs to" — it carries no
> telecom-specific assumption. This keeps the framework honest to its goal of
> understanding *semantic meaning*, not baked-in business meaning.

---

## 3. How the registry supports configurability

The registry (`contracts/column_registry.yaml`) defines, for **every** column:

```yaml
customerId:
  semantic_role: entity_id        # what role this column plays
  description: Unique entity being analyzed (the customer in this domain).
  data_type: string               # expected type
  required: true                  # needed for the system to operate
```

This unlocks configurability in several ways:

- **Role-based lookup.** Code finds columns by role
  (`columns_for_role("event_time")`) rather than by hardcoded names.
- **Per-domain swappability.** Each domain ships its own registry file; the
  framework binary is unchanged. Switching domains = pointing at a different
  registry.
- **Validation & required-field policy.** `required` flags are declared as data,
  so completeness checks are driven by config, not scattered through code.
- **Discoverability via API.** `GET /column-registry` returns the active registry
  as JSON, so downstream services and operators can introspect the semantic
  schema at runtime.

### Where it plugs in today

The registry is **additive metadata**. The current ingestion path — request
validation, event normalization (impression/click/skip derivation), quarantine,
and batch processing — is **unchanged**. The normalizer simply *gains the
ability* to load the registry and resolve roles
(`EventNormalizer.with_registry(...)`, `semantic_role()`, `columns_for_role()`),
which is read-only and does not affect normalization output.

This is deliberate groundwork: the **future** feature-extraction and analytics
layers will consume these roles to become fully config-driven, while today's
working, tested infrastructure keeps behaving exactly as before.

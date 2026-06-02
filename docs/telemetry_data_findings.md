MyJio Floater Telemetry — Data Validation & Design Review
Analyst date: 2026-06-01 · File examined: sample_data/telemetry_sample.csv · Contracts: analytics_contract.md, event_schema.md

Verdict up front: the file is not a CSV, skip is not an event type, click_action (not label) is the campaign key, there are no conversions, and the sample is a single customer. The analytics_contract.md schema is largely aspirational against this data; event_schema.md is much closer but over-claims a few metrics. Details below.

SECTION 1 — FILE VALIDATION
1.1 Is it CSV or XLSX?
It is an XLSX workbook, despite the .csv extension.

Evidence:

Magic bytes = PK\x03\x04\x14\x00\x06\x00. The leading PK is the ZIP local-file-header signature; OOXML/XLSX files are ZIP containers. A real CSV would start with text (customerId,...).
pd.read_excel(engine="openpyxl") parses it cleanly into 194 rows × 23 columns. pd.read_csv would treat the binary ZIP as text and produce garbage or raise.
1.2 Recommended loading strategy
Sniff magic bytes, do not trust the extension. If first 2 bytes == b"PK" → pd.read_excel(..., engine="openpyxl"); else pd.read_csv. This is exactly what ingestion/loader.py already does — that approach is correct and should be the canonical entrypoint.
Pin openpyxl as a hard dependency.
1.3 Parsing concerns
Concern	Detail
Misleading extension	.csv name → XLSX content. Any future producer change (real CSV, gzip, multi-sheet) must be re-sniffed.
event_timestamp is epoch milliseconds (int64), not ISO-8601	e.g. 1779255760042. Must convert with unit="ms". Contract §3 assumes ISO-8601.
sessionId has 32 NULL rows	5 real sessions + a large null bucket (mostly App-lifecycle/API rows). Session-scoped metrics must tolerate/exclude nulls.
swipe, timestamp_ist parsed as float64 (all-NaN), IST as object	Empty/dirty columns; ignore.
Single sheet assumed	read_excel reads sheet 0 only; fine here, but worth asserting.
SECTION 2 — DATA PROFILING
2.1 Columns (23)
customerId, event_type, event_category, event_timestamp, timestamp, sessionId, platform, os, source, newscreen_name, api_request, click_action, version, txId, clientId, androidId, label, swipe, type, additional_info, extra_info, timestamp_ist, IST

2.2 Data types
int64: customerId, event_timestamp, version
str/object: event_type, event_category, timestamp, sessionId, platform, os, source, newscreen_name, api_request, click_action, txId, clientId, androidId, label, type, additional_info, extra_info, IST
float64 (effectively empty): swipe, timestamp_ist
2.3 Volumes
Measure	Value
Row count	194
Unique customerId	1 → 1015289504
Unique sessionId	5 (+ 32 rows with NULL sessionId)
Time span	2026-05-20 05:36 → 09:57 UTC (~4h20m, single day)
Distinct event_type	30
2.4 event_type counts (top + floater-relevant)

FloaterResponse                     20     Recharge floater impression   7
App open                            17     Recharge floater clicks       6
Navigation_superapp                 16     campaign_response_received    3
App background                      16     campaign_saved_in_db          3
campaign_response_received_empty    14     Recharge initiated            2
HomeAPI Request Body                12     Home_superapp                 5
HOME_API_STATUS-REQUEST_INITIATED   11     (… ~15 more API/lifecycle types)
Floater API called                  10
Floater API response received       10
Only Recharge floater impression (7) and Recharge floater clicks (6) are a true behavioural funnel. Everything else is API plumbing, app lifecycle, or other surfaces.

2.5 label counts

NaN 127 | R1 11 | 200PlanFloater_forGemini 10 | home 10 | iActivate_Fiber users 9
Recharge 9 | JioTunes_27042026 5 | Recharge-skip 3 | Recharge-Explore all plans 2
D000 2 | Recharge-Recharge 1 | 200 1 | 2025 1 | pro user 1 | 5000GB_GetJioHome 1 | B1 1
2.6 click_action counts

NaN 135 | PLANEXPIRY01 13 | 200PlanFloater_forGeminien_US 10 | impression 10
iActivate_Fiber usersen_US 9 | entertainment impression 5 | Cloud 2 | Footer_Click_superapp 2
Finance 2 | 1032160 1 | 1019769 1 | Mobile 1 | Saavn miniapp 1 | Entertainment 1 | 5000GB_GetJioHomeen_US 1
Note: all 7 impressions + all 6 clicks of the recharge floater carry click_action = PLANEXPIRY01 (13 rows). This is the stable campaign key.

SECTION 3 — TELEMETRY SEMANTICS (with evidence)
The recharge floater funnel, ordered by session/time, is the decisive evidence:


event_type                   sessionId   label                        click_action
Recharge floater impression  2de9a433…   Recharge                     PLANEXPIRY01
Recharge floater clicks      2de9a433…   Recharge-Explore all plans   PLANEXPIRY01   ← real click
Recharge initiated           2de9a433…   200                          1032160        ← intent, NOT conversion
Recharge floater impression  2de9a433…   Recharge                     PLANEXPIRY01
Recharge floater clicks      2de9a433…   Recharge-skip                PLANEXPIRY01   ← SKIP (encoded in label)
…
Recharge floater clicks      8342dac8…   Recharge-Recharge            PLANEXPIRY01   ← real click
Recharge floater clicks      8342dac8…   Recharge-skip                PLANEXPIRY01   ← SKIP
Recharge floater clicks      fd9eb194…   Recharge-skip                PLANEXPIRY01   ← SKIP
#	Question	Answer	Evidence
1	What is an impression?	event_type == "Recharge floater impression". 7 rows, all label="Recharge", click_action="PLANEXPIRY01".	Clean, unambiguous; each precedes a click/skip in the same session.
2	What is a click?	A "Recharge floater clicks" row whose label is a real action (Recharge-Recharge, Recharge-Explore all plans). 3 real clicks.	label holds the chosen CTA.
3	What is a skip?	A "Recharge floater clicks" row whose label == "Recharge-skip". 3 skips. Skip is NOT a distinct event_type.	3 rows labelled Recharge-skip under the same event_type as clicks.
4	What is a campaign?	click_action (here PLANEXPIRY01). NOT label — on a click, label is the action, not the campaign.	All 13 funnel rows share click_action=PLANEXPIRY01; their label varies (Recharge / Recharge-skip / Recharge-Explore…).
5	What is a customer journey?	(customerId → sessionId → time-ordered events); funnel = impression → (click | skip) keyed on (customerId, click_action). Recharge initiated is a downstream intent signal, not part of the click/skip funnel.	5 sessions, impressions consistently followed by a click or skip seconds later.
6	What should be ignored?	API/plumbing & lifecycle: FloaterResponse, campaign_response_received[_empty], campaign_saved_in_db, Floater API called/response received, HOME_API_*, ENTERTAINMENT_API_*, App open/closed/background, HomeAPI*, JioCloud*, BurgerMenu*.	These have no click/skip funnel; they are infra telemetry.
Open item (needs your decision): Navigation_superapp / Home_superapp rows with newscreen_name="impression" (labels home, JioTunes_27042026; click_action = impression / entertainment impression) — 15 rows — are other surfaces' impressions (home nav / JioTunes), with no corresponding click or skip funnel. They are genuine impressions but not part of the recharge-floater funnel. Recommendation: do NOT fold them into floater impression metrics (they would inflate denominators and have no reactions). They could later seed a separate "surface reach" metric if desired.

SECTION 4 — CONTRACT VALIDATION
4.1 analytics_contract.md
✅ Correct assumptions

Four-stage funnel concept (impression → click/skip → conversion) is the right model.
Core entities exist: customerId, sessionId, click_action, event_type, a timestamp, a screen field.
impression and click events genuinely exist.
Repeat-impression logic (§3.2 impression_seq) is computable — PLANEXPIRY01 is shown 7× to the one user.
Time-to-click is computable (impression→click ms deltas within session/campaign).
❌ Incorrect assumptions

Skip is a first-class event (floater_skip, dismiss_popup → skip): false. Skip is encoded in label="Recharge-skip" on a click event.
Conversion events present (recharge_success, ott_subscription_success): absent. Only Recharge initiated (intent) exists.
campaign is a clean dedicated field: false. Real campaign id lives in click_action; label ≠ campaign.
Raw event names floater_impression / floater_click (§2 mapping): wrong literals. Actual names are Recharge floater impression / Recharge floater clicks.
timestamp is ISO-8601 UTC (§3): false. It is epoch ms (event_timestamp); IST columns are empty.
event_id UUID, mapping_version, ingested_at, conversion_type, attribution fields (§3.2/§4): none present — all enrichment to be synthesised downstream.
🕳 Missing assumptions

File is XLSX, not CSV.
Label-encoded skip convention (*-skip marker).
click_action as the campaign key.
Multiple non-floater impression surfaces (home/entertainment).
Sample is single-customer / single-day → no population for quartiles, min-max normalisation, or cross-user rates.
Large volume of API/lifecycle noise to quarantine; sessionId nulls.
🔧 Required contract updates

§1/§2: add a derivation rule — skip = Recharge floater clicks AND label matches *-skip/dismiss markers; clicks must exclude those.
§3: rename mapping literals to the real event names; declare event_timestamp (epoch ms) + IST handling; mark campaign := click_action; mark event_id, mapping_version, attribution fields as synthesised (not source).
§1/§3: define a quarantine enumeration for the API/lifecycle event families above.
§4: explicitly state conversion telemetry is future (Recharge initiated ≠ conversion); keep conversion metrics as placeholders.
§9 / §7.8: add a population-sufficiency precondition — quartile segments and min-max-normalised fatigue require a multi-customer extract; degrade to null on single-customer data.
Add an "other-surface impression" decision (home/entertainment) — out of floater scope unless promoted.
4.2 event_schema.md
✅ Correct — this contract matches the data well:

§1/§2 fields (customerId, sessionId, event_type, event_timestamp epoch ms, timestamp_ist, platform, os, newscreen_name, label, click_action, additional_info, extra_info) all present.
§3 raw event types (Recharge floater impression/clicks, FloaterResponse, campaign_response_received, campaign_saved_in_db, Floater API called/response) all observed.
§6 correctly defers Recharge Success/OTT/Fiber/UPI as future conversion telemetry — accurate.
❌ Incorrect / over-claimed

§4 maps click ← Recharge floater clicks with no skip handling — but ~half of those rows (Recharge-skip) are dismissals, so this mapping silently mislabels skips as clicks.
§5 lists Skip Rate, Attention Score, Campaign Fatigue Index, Loyalty Score as "supported" — overstated: skip is not a native event (needs label derivation), and Loyalty Score is never defined in analytics_contract.md.
🕳 Missing

The label-encoded skip convention.
click_action = campaign identifier.
The newscreen_name="impression" non-floater surfaces.
SECTION 5 — METRIC FEASIBILITY
Counts available: 7 impressions, 3 real clicks, 3 skips (label-derived), 0 conversions, 1 campaign (PLANEXPIRY01), 1 customer.

Metric	Status	Why
Impression Count	✅ Fully Supported	7 clean Recharge floater impression rows.
Click Count	✅ Fully Supported*	6 raw click rows → 3 true clicks after removing *-skip. Requires the skip-reclassification step first.
Skip Count	🟡 Partially Supported	Derivable (3) only via label marker scan — depends on the "Recharge-skip" naming convention, not a native event_type. Fragile if labels change.
CTR	✅ Fully Supported*	clicks/impressions = 3/7. Valid once clicks are disambiguated from skips.
Skip Rate	🟡 Partially Supported	skips/impressions = 3/7, but inherits the label-derivation fragility of Skip Count.
Repeat Impression Rate	✅ Fully Supported	PLANEXPIRY01 shown 7× to one user; impression_seq keyed on (customerId, click_action) yields repeats cleanly.
Average Time To Click	✅ Fully Supported	impression→click ms deltas exist (e.g. ~8s, ~11s) within session/campaign.
Average Time To Skip	🟡 Partially Supported	Computable (impression→skip deltas, e.g. ~4s, ~5s) but only because skips are label-derived.
Attention Score	🟡 Partially Supported	clicks/(clicks+skips) = 3/6 = 0.5 — computable, but depends on label-derived skips.
Exploration Score	🟡 Partially Supported	Formula computable, but degenerate: only 1 campaign seen → score is trivially 1.0 or 0. Not meaningful without multi-campaign data.
Loyalty Score	❌ Not Supported	No formula defined in analytics_contract.md (only named in event_schema.md §5). Undefined + single-customer. Define it in the contract first.
Average Impressions Before Click	✅ Fully Supported	Backward as-of join of first click to its impression_seq is computable per the funnel ordering.
Campaign Fatigue Index	🟡 Partially Supported	repeat_impression_rate × skip_rate computable for PLANEXPIRY01, but inherits label-derived skip fragility and is not population-meaningful (1 customer/1 campaign).
Conversion Rate	❌ Not Supported	No conversion events. Recharge initiated is intent, not a completed outcome (recharge_success/OTT absent — event_schema.md §6 future).
Cross-cutting limiters (apply to all):

Single customer ⇒ fatigue_score (min-max norm over population), §9 quartile segments (Resistant Users), and population rates (first_impression_success_rate, delayed_engagement_rate across users) are Not Supported / not meaningful until a multi-customer extract exists.
Tiny N (13 funnel events) ⇒ all rates are illustrative, not statistically reliable.
SECTION 6 — REVISED FEATURE-EXTRACTION DESIGN (no code)
6.1 Proposed architecture

XLSX export (.csv-named)
   │  ingestion/loader.py  — magic-byte sniff → read_excel(openpyxl)
   ▼
Preprocessing / Normalisation
   ├─ map raw event_type → canonical {impression, click} (case-insensitive)
   ├─ SKIP REFINEMENT: click rows whose `label` matches skip markers → skip
   ├─ campaign := click_action  (NOT label)
   ├─ event_timestamp parsed as epoch-ms; quarantine non-numeric
   ├─ quarantine API/lifecycle families; drop sessionId-null funnel orphans
   ▼
Feature Extraction (customer × profile)   ← this layer
   ├─ counts, impression_seq/repeats, diversity, session depth
   ├─ time-to-click / time-to-skip (backward as-of join on (customer,campaign))
   ├─ first-exposure analysis (one as-of join → 3 metrics)
   └─ CAPABILITY GATING: absent events (conversion; skip if no markers) → NA placeholders, not 0
   ▼
Analytics Engine  — period-over-period, population norms, segments (needs multi-customer)
   ▼
Insight Generation → Dashboard
This is essentially what the existing for_myjio_sample() preset already encodes — the design below ratifies it.

6.2 Correct campaign identifier
campaign := click_action (e.g. PLANEXPIRY01). Never label. On a click, label carries the action, not the campaign. Confirmed: all 13 funnel rows share click_action=PLANEXPIRY01.

6.3 Correct skip detection logic
Map Recharge floater clicks → click.
Reclassify any such row whose label (normalised, lower) contains a marker in {skip, dismiss} → skip.
Result here: 6 click rows → 3 click + 3 skip. Note the fragility: this is a string-convention dependency — log the reclassification count and surface it as a data-quality signal so a label-schema change is caught.
6.4 Correct impression logic
event_type == "Recharge floater impression" → impression. Sequence per (customerId, click_action) chronologically → impression_seq; is_repeat_impression = seq > 1. Exclude the newscreen_name="impression" home/entertainment rows from floater impression counts (pending your decision in §3).

6.5 Correct click logic
A "Recharge floater clicks" row that is not reclassified as skip (i.e. label ∈ {Recharge-Recharge, Recharge-Explore all plans, …}). True clicks = 3. CTR numerator uses this post-refinement count.

6.6 Exact customer-profile fields to generate
Aligned to analytics_contract.md §5, with capability gating:

Identity / window: customerId, first_seen, last_seen, profile_updated_at

Computable now (✅):
total_impressions, total_clicks, repeat_impressions, unique_campaigns_seen, unique_campaigns_clicked, ctr, repeat_impression_rate, avg_time_to_click_sec, avg_session_depth, exploration_score, first_impression_success, first_impression_success_rate (per-customer), avg_impressions_before_click, campaign_diversity_score, delayed_responder_flag

Computable but label-derived / caveated (🟡):
total_skips, skip_rate, avg_time_to_skip_sec, attention_score — emit values only when skip markers are found, else placeholder.

Placeholders until §6-future telemetry / multi-customer (❌ → NA):
total_conversions, conversion_rate (no conversion events); fatigue_score (needs population norm + temporal CTR decline); segments, primary_segment (segmentation layer; needs population quartiles).

Design principle (keep): absent-capability metrics must be explicit <NA>/NaN placeholders with a logged warning, never a misleading 0. The same code computes them unchanged when richer telemetry arrives.

Recommended next decisions (before any code)
Confirm click_action as campaign key and the *-skip label-skip convention as contract-level rules.
Decide the home/entertainment newscreen_name="impression" rows: out-of-scope vs. separate surface-reach metric (I recommend out-of-scope for the floater funnel).
Define Loyalty Score formally in analytics_contract.md, or drop it from event_schema.md §5.
Request a multi-customer extract before relying on segmentation, fatigue normalisation, or population rates.
I have not written or modified any code. analytics/feature_extractor.py already reflects much of §6 (its for_myjio_sample() preset matches these findings) — but it is currently a working-tree modification; I'd recommend reconciling it against this review and the contract updates before committing.

Want me to draft the concrete contract patches (the §4.1 "Required contract updates" list) as edits to analytics_contract.md / event_schema.md next?
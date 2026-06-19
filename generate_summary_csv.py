"""
Generate manager-friendly analytics CSV from raw telemetry.
Usage:
python generate_summary_csv.py
OR
python generate_summary_csv.py input_file.xlsx output_prefix
"""
from pathlib import Path
import sys
import pandas as pd
from analytics.feature_extractor import (
    TelemetryLoader,
    EventClassifier,
    FeatureExtractor,
    MetricCalculator,
    load_semantic_schema,
)
def main():
    if len(sys.argv) >= 2:
        input_file = sys.argv[1]
    else:
        input_file = "sample_data/telemetry_sample.csv"
    if len(sys.argv) >= 3:
        prefix = sys.argv[2]
    else:
        prefix = "analytics_output"
    print("=" * 70)
    print("TELEMETRY ANALYTICS CSV GENERATOR")
    print("=" * 70)
    loader = TelemetryLoader()
    print(f"\nLoading file: {input_file}")
    raw = loader.load(input_file)
    import json

    if len(raw.columns) == 1 and raw.columns[0] == "value":

        print("Detected JSON telemetry format. Flattening records...")

        rows = []

        for item in raw["value"]:
            try:
                rows.append(json.loads(item))
            except Exception:
                pass

        raw = pd.json_normalize(rows)

        print(f"Flattened to {len(raw.columns)} columns")
    print(f"Loaded {len(raw)} rows")
    schema = load_semantic_schema()
    # --------------------------------------------
    # GENERIC METRICS
    # --------------------------------------------
    metric_result = MetricCalculator(schema).compute(raw)
    customer_metrics = metric_result.customer_metrics.copy()
    # --------------------------------------------
    # FEATURE EXTRACTION
    # --------------------------------------------
    classified = EventClassifier().classify(raw)
    profiles = FeatureExtractor().extract(classified)
    # --------------------------------------------
    # DETAILED CUSTOMER METRICS
    # --------------------------------------------
    detailed_file = f"{prefix}_customer_metrics_detailed.csv"
    profiles.to_csv(
        detailed_file,
        index=False,
    )
    print(f"Generated: {detailed_file}")
    # --------------------------------------------
    # BUSINESS SUMMARY
    # --------------------------------------------
    business_columns = [
        "customerId",
        "total_impressions",
        "total_clicks",
        "total_skips",
        "ctr",
        "skip_rate",
        "dropoff_rate",
        "repeat_impressions",
        "repeat_impression_rate",
        "peak_click_hour",
        "peak_impression_hour",
        "weekday_ctr",
        "weekend_ctr",
        "weekend_activity_jump",
        "avg_time_to_click_sec",
        "avg_time_to_skip_sec",
        "avg_session_depth",
        "attention_score",
        "exploration_score",
        "campaign_diversity_score",
        "avg_impressions_before_click",
        "first_impression_success_rate",
    ]
    available_columns = [
        c for c in business_columns
        if c in profiles.columns
    ]
    business_summary = profiles[available_columns].copy()
    business_file = f"{prefix}_business_summary.csv"
    business_summary.to_csv(
        business_file,
        index=False,
    )
    print(f"Generated: {business_file}")
    # --------------------------------------------
    # GLOBAL SUMMARY
    # --------------------------------------------
    summary = metric_result.dataset_summary
    global_metrics = {
        "n_customers": summary.get("n_customers"),
        "n_events": summary.get("n_events"),
        "n_sessions": summary.get("n_sessions"),
        "n_campaigns": summary.get("n_campaigns"),
    }
    global_df = pd.DataFrame([global_metrics])
    global_file = f"{prefix}_global_summary.csv"
    global_df.to_csv(
        global_file,
        index=False,
    )
    print(f"Generated: {global_file}")
    # --------------------------------------------
    # HUMAN READABLE SUMMARY
    # --------------------------------------------
    manager_file = f"{prefix}_manager_view.csv"
    summary_rows = []

    for _, row in profiles.iterrows():

        customer = row["customerId"]

        impressions = row.get("total_impressions", 0) or 0
        clicks = row.get("total_clicks", 0) or 0
        skips = row.get("total_skips", 0) or 0

        ignored = max(
            0,
            impressions - clicks - skips
        )

        metrics = [

        (
            "Total Impressions",
            impressions,
            "Count(Impression Events)",
            f"{impressions}"
        ),

        (
            "Total Clicks",
            clicks,
            "Count(Click Events)",
            f"{clicks}"
        ),

        (
            "Total Skips",
            skips,
            "Count(Skip Events)",
            f"{skips}"
        ),

        (
            "Ignored Impressions",
            ignored,
            "Impressions - Clicks - Skips",
            f"{impressions} - {clicks} - {skips} = {ignored}"
        ),

        (
            "CTR (%)",
            round(row.get("ctr", 0), 2),
            "(Clicks / Impressions) × 100",
            f"({clicks}/{impressions}) × 100"
            if impressions else "N/A"
        ),

        (
            "Skip Rate (%)",
            round(row.get("skip_rate", 0), 2),
            "(Skips / Impressions) × 100",
            f"({skips}/{impressions}) × 100"
            if impressions else "N/A"
        ),

        (
            "Drop Off Rate (%)",
            round(row.get("dropoff_rate", 0), 2),
            "(Ignored Impressions / Impressions) × 100",
            f"({ignored}/{impressions}) × 100"
            if impressions else "N/A"
        ),

        (
            "Repeat Impressions",
            row.get("repeat_impressions"),
            "Impressions after first exposure",
            f"{row.get('repeat_impressions')}"
        ),

        (
            "Repeat Impression Rate (%)",
            round(row.get("repeat_impression_rate", 0), 2),
            "(Repeat Impressions / Impressions) × 100",
            f"({row.get('repeat_impressions')}/{impressions}) × 100"
            if impressions else "N/A"
        ),

        (
            "Peak Click Hour",
            row.get("peak_click_hour"),
            "Hour having maximum clicks",
            str(row.get("peak_click_hour"))
        ),

        (
            "Peak Impression Hour",
            row.get("peak_impression_hour"),
            "Hour having maximum impressions",
            str(row.get("peak_impression_hour"))
        ),

        (
            "Weekday CTR (%)",
            row.get("weekday_ctr"),
            "(Weekday Clicks / Weekday Impressions) × 100",
            str(row.get("weekday_ctr"))
        ),

        (
            "Weekend CTR (%)",
            row.get("weekend_ctr"),
            "(Weekend Clicks / Weekend Impressions) × 100",
            "N/A (No Weekend Data)"
            if pd.isna(row.get("weekend_ctr"))
            else str(row.get("weekend_ctr"))
        ),

        (
            "Weekend Activity Jump (%)",
            row.get("weekend_activity_jump"),
            "Weekend CTR - Weekday CTR",
            "N/A (No Weekend Data)"
            if pd.isna(row.get("weekend_activity_jump"))
            else str(row.get("weekend_activity_jump"))
        ),

        (
            "Attention Score",
            round(row.get("attention_score", 0), 4),
            "Clicks / (Clicks + Skips)",
            f"{clicks} / ({clicks} + {skips})"
            if (clicks + skips) else "N/A"
        ),

        (
            "Exploration Score",
            round(row.get("exploration_score", 0), 4),
            "Unique Campaigns Clicked / Unique Campaigns Seen",
            f"{row.get('unique_campaigns_clicked')} / {row.get('unique_campaigns_seen')}"
        ),

        (
            "Campaign Diversity Score",
            round(row.get("campaign_diversity_score", 0), 4),
            "Unique Campaigns Clicked / Unique Campaigns Seen",
            f"{row.get('unique_campaigns_clicked')} / {row.get('unique_campaigns_seen')}"
        ),

        (
            "Average Impressions Before Click",
            row.get("avg_impressions_before_click"),
            "Average exposure count before first click",
            str(row.get("avg_impressions_before_click"))
        ),

        (
            "First Impression Success Rate (%)",
            row.get("first_impression_success_rate"),
            "(Campaigns Clicked On First Exposure / Clicked Campaigns) × 100",
            str(row.get("first_impression_success_rate"))
        ),

        (
            "Average Time To Click (sec)",
            row.get("avg_time_to_click_sec"),
            "Avg(Click Timestamp - Impression Timestamp)",
            str(row.get("avg_time_to_click_sec"))
        ),

        (
            "Average Time To Skip (sec)",
            row.get("avg_time_to_skip_sec"),
            "Avg(Skip Timestamp - Impression Timestamp)",
            str(row.get("avg_time_to_skip_sec"))
        ),

        (
            "Average Session Depth",
            row.get("avg_session_depth"),
            "Events / Sessions",
            str(row.get("avg_session_depth"))
        ),
    ]

    for metric_name, value, formula, calc in metrics:

        summary_rows.append({
            "customerId": customer,
            "metric": metric_name,
            "value": value,
            "formula": formula,
            "calculation": calc,
        })

    manager_df = pd.DataFrame(summary_rows)

    manager_df.to_csv(
    manager_file,
    index=False
    )

    print(f"Generated: {manager_file}")

    print("\nSUCCESS")
    print(f"Customer Profiles : {len(profiles)}")
    print(f"Events Processed  : {len(raw)}")


if __name__ == "__main__":
    main()
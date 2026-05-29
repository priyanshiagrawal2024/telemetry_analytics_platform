# Project Overview

Project Name:
MyJio Floater Analytics Platform

Objective:
Build an AI-powered telemetry analytics system for understanding customer behaviour and floater engagement inside the MyJio application.

Primary Goal:
Generate actionable business insights from telemetry events.

This project focuses on:

- Customer Analytics
- Campaign Analytics
- Engagement Analytics
- Fatigue Analytics
- Segmentation Analytics
- Trend Analytics

This is NOT a recommendation system.

The system should analyze:
- what worked
- for whom it worked
- when it worked
- why it worked

and identify meaningful behavioural patterns.

# Scope

## In Scope

- Telemetry ingestion
- Feature extraction
- Behavioural analytics
- Campaign analytics
- Fatigue analytics
- Segmentation
- Dashboard visualizations
- Insight generation

## Out Of Scope

- Recommendation systems
- Campaign delivery systems
- Personalization engines
- Marketing automation
- ML-based campaign targeting

# Event Taxonomy

Standard Events:

- impression
- click
- skip
- conversion

# Event Mapping

floater_impression → impression

floater_click → click

floater_skip → skip

dismiss_popup → skip

recharge_success → conversion

ott_subscription_success → conversion

# Core Telemetry Fields

customerId

sessionId

campaign

event_type

timestamp

screen_name

click_action

# Metrics

CTR

CTR = clicks / impressions * 100

Skip Rate

skip_rate = skips / impressions * 100

Conversion Rate

conversion_rate = conversions / clicks * 100

Repeat Impression Rate

repeat_impressions / total_impressions * 100

Average Time To Click

click_timestamp - impression_timestamp

Average Time To Skip

skip_timestamp - impression_timestamp

Session Depth

total_events_in_session

Fatigue Score

Composite score based on:
- repeat impressions
- skip rate
- declining CTR


# Advanced Analytics Metrics

Attention Score

clicks / (clicks + skips)

Campaign Fatigue Index

repeat_impression_rate × skip_rate

Click Efficiency Score

CTR / avg_time_to_click

Engagement Momentum

current_ctr - previous_ctr

First Impression Success Rate

users_clicking_on_first_exposure / total_users

Delayed Engagement Rate

users_clicking_after_multiple_exposures / total_users

Campaign Persistence Score

conversions / impressions

User Exploration Score

unique_campaigns_clicked / total_campaigns_seen

Campaign Saturation Level

impressions_per_user

Skip Velocity

change_in_skip_rate_over_time


# Customer Segments

Highly Engaged

CTR > 15
Skip Rate < 10

Passive

CTR < 5

High Skip

Skip Rate > 50

Fatigued

Repeat Impression Rate > 50
AND
Skip Rate > 30

Fast Click Users

Average Time To Click < 5 sec

Explorers

High campaign diversity

Selective Users

High CTR
Low campaign diversity

Resistant Users

High impressions
Low clicks

# Architecture

Telemetry Events

↓

Ingestion Layer

↓

Preprocessing Layer

↓

Feature Extraction

↓

Analytics Engine

↓

Insight Generation

↓

Dashboard

# Technology Stack

Python

FastAPI

PostgreSQL

Pandas

Plotly

Streamlit

Google ADK

Docker

# Development Rules

Always build analytics-first.

Do not implement recommendation systems.

Do not implement personalization systems.

All analytics should be explainable.

Prefer rule-based analytics over ML.

Generate business-readable insights.

Focus on:
- customer behaviour
- campaign effectiveness
- fatigue analysis
- engagement analysis

All modules must be modular and production-ready.
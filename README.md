# DT Ops Tools

Monorepo for Digital Turbine internal automation tools — CLI/agent entry points only.
Streamlit UIs are in the separate `dt-ops-streamlit` repo.

| Tool | Folder | Trigger | Description |
|------|--------|---------|-------------|
| AdOps Optimizer | /adops_optimizer | "New Optimization" | Campaign optimization, KPI segmentation, bid adjustments, ROAS |
| App QA Agent | /app_qa | "New App QA" | APK screening: Play Integrity, wake locks, legal/compliance |
| Tracking Link Builder | /tracking_link_builder | "New Link" | Attribution and tracking link generation |

Slack bot routes via `.cursorrules`. Explicit triggers + natural language fallback.

# AdOps Optimizer

**Sources:** `AdOps/campaign-optimizer/` (all Python **except** `app_streamlit.py`, which is Streamlit-only).

**ROAS / AI-Stuff:** The path `AI-Stuff/ROAS Optimization/` was **not present** in the workspace used for this migration. ROI and ROAS handling live in `optimizer.py` (`kpi_mode`, column specs). If you add standalone ROAS-only scripts later, place them here and note them below.

## Contents

| File | Role |
|------|------|
| `optimizer.py` | Core pipeline (Excel + CSV → optimized workbook) |
| `main.py` | CLI entry (`python main.py --help`) |
| `slack_runner.py` | Slack `/optimize` (Socket Mode) |
| `app_web.py` | Flask local UI |
| `app.py` | PyQt desktop UI |
| `templates/` | Flask HTML |

Streamlit UI → **`dt-ops-streamlit/adops_optimizer/app.py`**.

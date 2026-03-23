# AdOps Optimizer — Agent Instructions

This file tells an AI agent (e.g. Cursor bot in Slack) how to run the optimizer.
All logic lives in **`optimizer.py`**. Call it directly — do not use the desktop or web UI.

---

## Quick Start

```python
from optimizer import run_optimization, run_scale_optimization, xlsx_to_csv
```

There are **two optimization modes**. Decide which to run based on the user's request:

| Keyword / intent | Mode |
|---|---|
| "performance", "KPI", "ROI", "ROAS", "bid decrease", "segment" | **Performance** |
| "scale", "fill rate", "fillrate", "bid increase", "volume", "capacity" | **Scale** |

---

## Mode 1: Performance Optimization

Requires **both** an internal file (.xlsx) and an advertiser file (.csv).

```python
buf, summary = run_optimization(
    internal_file="path/to/internal.xlsx",   # REQUIRED
    advertiser_file="path/to/advertiser.csv", # REQUIRED
    kpi_col_d7_spec="I",          # Column letter or name pattern for D7 KPI
    kpi_col_d2nd_spec="K",        # Column letter or name pattern for D2nd KPI
    kpi_d7_pct=3.36,              # D7 KPI target (percentage)
    kpi_d2nd_pct=13.36,           # D2nd KPI target (percentage)
    weight_main=0.80,             # D7 weight (0-1)
    weight_secondary=0.20,        # D2nd weight (0-1)
    kpi_mode="roi",               # "roi" or "roas"
)
```

**What it does**: Segments sites by KPI performance (green/yellow/orange/red), adjusts bids up or down based on segment and progression, suggests daily caps for high-spend sites at bid floor.

---

## Mode 2: Scale Optimization

Requires **only** the internal file. Advertiser file is optional (adds ROI columns to report).

```python
buf, summary = run_scale_optimization(
    internal_file="path/to/internal.xlsx",    # REQUIRED
    advertiser_file="path/to/advertiser.csv", # OPTIONAL (or None)
)
```

**What it does**: Increases bids based on FillRate bands to scale volume:
- FillRate >= 85%: no change (at capacity)
- 70–85%: +10%, 50–70%: +15%, 35–50%: +20%, 20–35%: +25%, 0–20%: +30%
- CVR > 20%: adds +5% bonus
- Capped at highTier × 1.20
- Skips sites with spend < $100, maxPreloads < 100, or existing dailyCap

---

## Getting CSV Output

Both functions return `(buf, summary)` where `buf` is an Excel BytesIO.
To convert to CSV:

```python
csv_buf = xlsx_to_csv(buf)

# Write to file
with open("output.csv", "wb") as f:
    f.write(csv_buf.getvalue())
```

Or provide a **CSV download link** via the web app:
- Excel: `GET /download/<download_id>`
- CSV:   `GET /download/<download_id>/csv`

---

## CLI Usage

```bash
# Performance mode (default)
python main.py --mode performance \
  --internal internal.xlsx \
  --advertiser advertiser.csv \
  --d7-spec I --d2nd-spec K \
  --d7-target 3.36 --d2nd-target 13.36 \
  --format csv -o output.csv

# Scale mode
python main.py --mode scale \
  --internal internal.xlsx \
  --format csv -o output.csv

# Scale mode with optional advertiser for ROI display
python main.py --mode scale \
  --internal internal.xlsx \
  --advertiser advertiser.csv \
  --format csv -o output.csv
```

---

## Summary Dict

Both functions return a `summary` dict:

```python
{
    "total_rows": int,        # Total sites in output
    "rows_actioned": int,     # Sites with a bid recommendation
    "rows_disregarded": int,  # Sites excluded (Performance only)
    "rows_with_cap": int,     # Daily cap suggestions (Performance only)
    "action_breakdown": dict, # e.g. {"Increase bid 15%": 42, "Decrease bid 10%": 18}
    "segment_breakdown": dict,# e.g. {"green": 50, "red": 12} (Performance only)
    "optimization_mode": str, # "scale" (only present in Scale summary)
}
```

---

## File Requirements

**Internal file (.xlsx)** must contain columns (case-insensitive):
`campaignName`, `siteId`, `siteName`, `maxPreloads`, `fillRate`, `bidRate`, `highTier`, `spend`, `cvr`, `dailyCap`

**Advertiser file (.csv)** must contain:
`campaignName`, `siteId`, plus KPI columns referenced by `--d7-spec` / `--d2nd-spec`

---

## Important Notes

- OM Push and Notifications sites are **always excluded** from both modes
- Output is sorted by FillRate (high → low) in Scale mode
- The agent should run `optimizer.py` directly — never the UI apps
- If the user asks for a report or optimization without specifying mode, ask which mode they want
- Always output CSV (use `xlsx_to_csv()` or `--format csv`) unless the user specifically asks for Excel

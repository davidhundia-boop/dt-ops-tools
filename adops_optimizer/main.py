#!/usr/bin/env python3
"""
CLI entry point for AdOps campaign optimization.
Runs the same pipeline as the Streamlit UI: internal .xlsx + advertiser .csv → Excel report.
"""
from __future__ import annotations

import argparse
import os
import sys

from optimizer import run_optimization


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run Digital Turbine campaign optimization (outputs .xlsx).",
    )
    p.add_argument("--internal", required=True, help="Path to internal campaign data (.xlsx)")
    p.add_argument("--advertiser", required=True, help="Path to advertiser performance report (.csv)")
    p.add_argument(
        "--d7-spec",
        required=True,
        help="D7 KPI column: Excel letter (e.g. I) or CSV column name / pattern (e.g. ROAS D7)",
    )
    p.add_argument(
        "--d2nd-spec",
        required=True,
        help="Secondary KPI column: letter or name pattern (e.g. K or ROI D30)",
    )
    p.add_argument("--d7-target", type=float, required=True, help="D7 KPI target (percentage or ratio per KPI mode)")
    p.add_argument("--d2nd-target", type=float, required=True, help="Secondary KPI target")
    p.add_argument("--weight-main", type=float, default=80.0, help="Main KPI weight %% (default 80)")
    p.add_argument("--weight-secondary", type=float, default=20.0, help="Secondary KPI weight %% (default 20)")
    p.add_argument(
        "--kpi-mode",
        choices=("roi", "roas"),
        default="roi",
        help="KPI interpretation: roi (%%) or roas (ratio)",
    )
    p.add_argument(
        "-o",
        "--output",
        default="optimization_output.xlsx",
        help="Output Excel path (default: optimization_output.xlsx)",
    )
    args = p.parse_args(argv)

    if not os.path.isfile(args.internal):
        print(f"Error: internal file not found: {args.internal}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.advertiser):
        print(f"Error: advertiser file not found: {args.advertiser}", file=sys.stderr)
        return 1

    try:
        buf, summary = run_optimization(
            internal_file=args.internal,
            advertiser_file=args.advertiser,
            kpi_d7_pct=args.d7_target,
            kpi_d2nd_pct=args.d2nd_target,
            weight_main=args.weight_main / 100.0,
            weight_secondary=args.weight_secondary / 100.0,
            kpi_col_d7_spec=args.d7_spec,
            kpi_col_d2nd_spec=args.d2nd_spec,
            kpi_mode=args.kpi_mode,
        )
    except Exception as e:
        print(f"Optimization failed: {e}", file=sys.stderr)
        return 1

    out_path = os.path.abspath(args.output)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())

    print(f"Wrote {out_path}")
    print(
        f"Rows: {summary.get('total_rows', 0)} | "
        f"Actioned: {summary.get('rows_actioned', 0)} | "
        f"Mode: {summary.get('kpi_mode', '')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

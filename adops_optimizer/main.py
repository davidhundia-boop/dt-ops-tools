#!/usr/bin/env python3
"""
CLI entry point for AdOps campaign optimization.
Supports both Performance and Scale optimization modes, with xlsx or csv output.
"""
from __future__ import annotations

import argparse
import os
import sys

from optimizer import run_optimization, run_scale_optimization, xlsx_to_csv


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run Digital Turbine campaign optimization.",
    )
    p.add_argument(
        "--mode",
        choices=("performance", "scale"),
        default="performance",
        help="Optimization mode: performance (KPI-based) or scale (FillRate-based). Default: performance",
    )
    p.add_argument("--internal", required=True, help="Path to internal campaign data (.xlsx)")
    p.add_argument("--advertiser", default=None, help="Path to advertiser performance report (.csv). Required for performance mode, optional for scale mode.")
    p.add_argument(
        "--d7-spec",
        default=None,
        help="D7 KPI column: Excel letter (e.g. I) or CSV column name / pattern (e.g. ROAS D7). Required for performance mode.",
    )
    p.add_argument(
        "--d2nd-spec",
        default=None,
        help="Secondary KPI column: letter or name pattern (e.g. K or ROI D30). Required for performance mode.",
    )
    p.add_argument("--d7-target", type=float, default=None, help="D7 KPI target (percentage or ratio per KPI mode). Required for performance mode.")
    p.add_argument("--d2nd-target", type=float, default=None, help="Secondary KPI target. Required for performance mode.")
    p.add_argument("--weight-main", type=float, default=80.0, help="Main KPI weight %% (default 80)")
    p.add_argument("--weight-secondary", type=float, default=20.0, help="Secondary KPI weight %% (default 20)")
    p.add_argument(
        "--kpi-mode",
        choices=("roi", "roas"),
        default="roi",
        help="KPI interpretation: roi (%%) or roas (ratio)",
    )
    p.add_argument(
        "--format",
        choices=("xlsx", "csv"),
        default="xlsx",
        help="Output format: xlsx (formatted Excel) or csv (plain CSV). Default: xlsx",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output file path. Default: optimization_output.xlsx or .csv based on --format",
    )
    args = p.parse_args(argv)

    if not os.path.isfile(args.internal):
        print(f"Error: internal file not found: {args.internal}", file=sys.stderr)
        return 1

    # Default output filename
    if args.output is None:
        ext = "csv" if args.format == "csv" else "xlsx"
        args.output = f"optimization_output.{ext}"

    if args.mode == "performance":
        # Validate required performance args
        if not args.advertiser:
            print("Error: --advertiser is required for performance mode.", file=sys.stderr)
            return 1
        if not os.path.isfile(args.advertiser):
            print(f"Error: advertiser file not found: {args.advertiser}", file=sys.stderr)
            return 1
        if not args.d7_spec or not args.d2nd_spec:
            print("Error: --d7-spec and --d2nd-spec are required for performance mode.", file=sys.stderr)
            return 1
        if args.d7_target is None or args.d2nd_target is None:
            print("Error: --d7-target and --d2nd-target are required for performance mode.", file=sys.stderr)
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
            print(f"Performance optimization failed: {e}", file=sys.stderr)
            return 1

    else:  # scale
        if args.advertiser and not os.path.isfile(args.advertiser):
            print(f"Error: advertiser file not found: {args.advertiser}", file=sys.stderr)
            return 1

        try:
            buf, summary = run_scale_optimization(
                internal_file=args.internal,
                advertiser_file=args.advertiser if args.advertiser else None,
                kpi_col_d7_spec=args.d7_spec,
                kpi_col_d2nd_spec=args.d2nd_spec,
                kpi_mode=args.kpi_mode,
            )
        except Exception as e:
            print(f"Scale optimization failed: {e}", file=sys.stderr)
            return 1

    # Convert to CSV if requested
    if args.format == "csv":
        buf = xlsx_to_csv(buf)

    out_path = os.path.abspath(args.output)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())

    mode_label = "Scale" if args.mode == "scale" else "Performance"
    print(f"[{mode_label}] Wrote {out_path}")
    print(
        f"Rows: {summary.get('total_rows', 0)} | "
        f"Actioned: {summary.get('rows_actioned', 0)} | "
        f"Format: {args.format}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

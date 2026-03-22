#!/usr/bin/env python3
"""
CLI entry point for App QA — runs wake lock, Play Integrity, and legal checks on a local APK.
For Slack integration, run `python qa_bot.py` (Socket Mode).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# qa_bot.py expects Slack tokens at import time; CLI-only runs use placeholders.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-cli-placeholder-not-for-slack")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-cli-placeholder-not-for-slack")

import qa_bot as qa


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run bundled QA analyzers on an APK file.")
    p.add_argument("apk", help="Path to .apk file")
    p.add_argument(
        "--json",
        action="store_true",
        help="Print combined JSON to stdout instead of a short text summary",
    )
    args = p.parse_args(argv)

    try:
        wl = qa.run_wake_lock(args.apk)
        pi = qa.run_play_integrity(args.apk)
        legal = qa.run_legal(args.apk)
    except Exception as e:
        print(f"QA run failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"wake_lock": wl, "play_integrity": pi, "legal": legal}, indent=2))
        return 0

    print("--- Wake lock ---")
    print(json.dumps(wl, indent=2)[:2000])
    print("\n--- Play Integrity ---")
    print(json.dumps(pi, indent=2)[:2000])
    print("\n--- Legal ---")
    print(json.dumps(legal, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

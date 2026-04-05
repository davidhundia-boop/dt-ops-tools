#!/usr/bin/env python3
"""
CLI entry point for App QA — runs wake lock, Play Integrity, legal checks,
and optional in-app UI verification on a local APK.
For Slack integration, run `python qa_bot.py` (Socket Mode).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-cli-placeholder-not-for-slack")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-cli-placeholder-not-for-slack")

import qa_bot as qa
from in_app_legal_verifier import verify_in_app_legal, compute_verdict


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run bundled QA analyzers on an APK file.")
    p.add_argument("apk", help="Path to .apk file")
    p.add_argument(
        "--json",
        action="store_true",
        help="Print combined JSON to stdout instead of a short text summary",
    )
    p.add_argument(
        "--verify-ui",
        action="store_true",
        help="Run in-app UI verification on a connected emulator/device",
    )
    p.add_argument(
        "--package",
        default=None,
        help="Package name (auto-detected from legal check if omitted)",
    )
    p.add_argument(
        "--screenshots",
        default=None,
        help="Directory to save verification screenshots",
    )
    args = p.parse_args(argv)

    try:
        wl = qa.run_wake_lock(args.apk)
        pi = qa.run_play_integrity(args.apk)
        legal = qa.run_legal(args.apk)
        classification = qa.run_classification(args.apk, legal)
    except Exception as e:
        print(f"QA run failed: {e}", file=sys.stderr)
        return 1

    result = {
        "wake_lock": wl,
        "play_integrity": pi,
        "legal": legal,
        "classification": classification,
    }

    if args.verify_ui:
        package = args.package or legal.get("package_name")
        if not package:
            print("ERROR: --package required for UI verification (could not auto-detect)", file=sys.stderr)
            return 1

        ui_result = verify_in_app_legal(args.apk, package, args.screenshots)

        static_legal = legal.get("in_app_legal", {})
        static_pp = bool(static_legal.get("in_app_pp_urls"))
        static_tc = bool(static_legal.get("in_app_tc_urls"))

        nav_info = ui_result.get("navigation_info", {})
        blocker = None
        if nav_info.get("login_wall"):
            blocker = "LOGIN_WALL"
        elif nav_info.get("game_tutorial_blocked"):
            blocker = "TUTORIAL_BLOCKED"

        ui_pp = ui_result["privacy_policy"]
        ui_tc = ui_result["terms_and_conditions"]

        pp_blocker = blocker
        if not ui_pp["ui_found"] and ui_pp.get("ui_path"):
            pp_blocker = "UNVERIFIED"
        pp_verdict = compute_verdict(static_pp, ui_pp["ui_found"], pp_blocker or blocker)

        tc_blocker = blocker
        if not ui_tc["ui_found"] and ui_tc.get("ui_path"):
            tc_blocker = "UNVERIFIED"
        tc_verdict = compute_verdict(static_tc, ui_tc["ui_found"], tc_blocker or blocker)

        result["in_app_verification"] = {
            "privacy_policy": {**pp_verdict, **ui_pp, "static_found": static_pp},
            "terms_and_conditions": {**tc_verdict, **ui_tc, "static_found": static_tc},
            "navigation_info": nav_info,
        }

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    print("--- Classification ---")
    cls = classification
    if cls.get("error"):
        print(f"  ERROR: {cls['error']}")
    else:
        print(f"  Main category : {cls.get('main_category', 'Unknown')}")
        sub = cls.get("sub_category")
        conf = cls.get("confidence", "genre_only")
        if sub:
            label = f"{sub}"
            if conf == "weak":
                label += "  (possibly — low confidence)"
            print(f"  Sub-category  : {label}")
        print(f"  Confidence    : {conf}")
        if cls.get("signals"):
            print(f"  Signals       : {', '.join(cls['signals'][:6])}")

    print("\n--- Wake lock ---")
    print(json.dumps(wl, indent=2)[:2000])
    print("\n--- Play Integrity ---")
    print(json.dumps(pi, indent=2)[:2000])
    print("\n--- Legal ---")
    print(json.dumps(legal, indent=2)[:2000])

    if "in_app_verification" in result:
        print("\n--- In-App Legal Verification ---")
        v = result["in_app_verification"]
        for check_key, label in [("privacy_policy", "Privacy Policy"), ("terms_and_conditions", "T&C")]:
            c = v[check_key]
            icon = {"PASS": "PASS", "FAIL": "FAIL", "INCONCLUSIVE": "????"}.get(c["verdict"], c["verdict"])
            path_str = " > ".join(c.get("ui_path", [])) or "N/A"
            print(f"  {label}: [{icon}] confidence={c['confidence']}  path={path_str}")
            for note in c.get("notes", []):
                print(f"    Note: {note}")

        nav = v.get("navigation_info", {})
        print(f"  Navigation: {nav.get('navigation_time_seconds', 0)}s, "
              f"login_wall={nav.get('login_wall')}, "
              f"tutorial_blocked={nav.get('game_tutorial_blocked')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

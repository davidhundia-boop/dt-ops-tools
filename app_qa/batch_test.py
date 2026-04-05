#!/usr/bin/env python3
"""Batch test multiple APKs from a folder."""
import json
import os
import sys
import time

from in_app_legal_verifier import verify_in_app_legal, _detect_package_name, check_device_connected

SKIP: set[str] = set()

def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\david\Downloads\App QA"
    ss_base = r"D:\AI Stuff\dt-ops-tools\app_qa\screenshots\batch"

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        print("VisionAgent mode (GEMINI_API_KEY set)")
    else:
        print("Legacy heuristic mode (set GEMINI_API_KEY for LLM navigation)")

    if not check_device_connected():
        print("ERROR: No emulator/device connected")
        return

    entries = []
    for item in os.listdir(folder):
        if item in SKIP:
            print(f"SKIP: {item} (known issue)")
            continue
        full = os.path.join(folder, item)
        ext = os.path.splitext(item)[1].lower()
        if os.path.isdir(full):
            has_apks = any(f.endswith(".apk") for f in os.listdir(full))
            if has_apks:
                entries.append((item, full))
        elif ext in (".apk", ".apks", ".zip"):
            entries.append((item, full))

    if not entries:
        print("No APKs found")
        return

    print(f"\nFound {len(entries)} apps to test:\n")
    for name, path in entries:
        print(f"  - {name}")

    results = {}
    for name, path in entries:
        print(f"\n{'='*60}")
        print(f"TESTING: {name}")
        print(f"{'='*60}")

        pkg = _detect_package_name(path)
        if not pkg:
            print(f"  ERROR: Could not detect package name for {name}")
            results[name] = {"error": "package detection failed"}
            continue

        print(f"  Package: {pkg}")
        ss_dir = os.path.join(ss_base, pkg)
        os.makedirs(ss_dir, exist_ok=True)

        start = time.time()
        try:
            result = verify_in_app_legal(path, pkg, ss_dir, gemini_api_key=gemini_key)
            elapsed = time.time() - start

            pp = result.get("privacy_policy", {})
            tc = result.get("terms_and_conditions", {})
            nav = result.get("navigation_info", {})

            pp_status = "FOUND" if pp.get("ui_found") else "NOT FOUND"
            tc_status = "FOUND" if tc.get("ui_found") else "NOT FOUND"

            print(f"\n  Privacy Policy:      {pp_status}")
            if pp.get("ui_path"):
                print(f"    Path: {' > '.join(pp['ui_path'])}")
            for note in pp.get("notes", []):
                print(f"    Note: {note}")

            print(f"  Terms & Conditions:  {tc_status}")
            if tc.get("ui_path"):
                print(f"    Path: {' > '.join(tc['ui_path'])}")
            for note in tc.get("notes", []):
                print(f"    Note: {note}")

            if nav.get("login_wall"):
                print(f"  BLOCKED: Login wall")
            if nav.get("game_tutorial_blocked"):
                print(f"  BLOCKED: Game tutorial")

            print(f"  Time: {elapsed:.0f}s")

            results[name] = result

        except Exception as e:
            elapsed = time.time() - start
            print(f"  EXCEPTION after {elapsed:.0f}s: {e}")
            results[name] = {"error": str(e)}

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}\n")
    for name, result in results.items():
        if "error" in result and isinstance(result["error"], str):
            print(f"  {name:30s}  ERROR: {result['error']}")
            continue
        pp = result.get("privacy_policy", {})
        tc = result.get("terms_and_conditions", {})
        pp_ok = pp.get("ui_found", False)
        tc_ok = tc.get("ui_found", False)
        if pp_ok and tc_ok:
            verdict = "PASS"
        elif pp_ok or tc_ok:
            verdict = "PARTIAL"
        else:
            notes = pp.get("notes", []) + tc.get("notes", [])
            nav = result.get("navigation_info", {})
            if nav.get("login_wall") or any("login" in n.lower() for n in notes):
                verdict = "INCONCLUSIVE (login wall)"
            elif any("GAME_ENGINE" in n for n in notes):
                verdict = "INCONCLUSIVE (game engine)"
            else:
                verdict = "FAIL"
        print(f"  {name:30s}  {verdict}")

    # Save full results
    out_path = os.path.join(ss_base, "batch_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results: {out_path}")


if __name__ == "__main__":
    main()

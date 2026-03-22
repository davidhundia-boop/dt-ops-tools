#!/usr/bin/env python3
"""
Play Integrity & App Protection Analyzer v4
=============================================
Detects whether an APK will show the "Get this app from Play" redirect
screen (or otherwise break) when preinstalled via Digital Turbine.

FAIL (hard blocks, detectable statically):
  - Auto Protect (pairip) — Google injects at Play Console upload time
  - Legacy Play Licensing (LVL) — hard licensed/not-licensed check

WARNING (needs one-time manual verification):
  - Play Integrity API — verdict handling is server-side, cannot
    determine statically whether DT installs are permitted
  - Firebase AppCheck, Forced In-App Updates, Play Feature Delivery,
    Play Asset Delivery

INCONCLUSIVE:
  - DEX extraction failed

Usage:
    python play_integrity_analyzer.py <path_to_apk>
    python play_integrity_analyzer.py <directory_of_apks>
"""

import sys
import os
import zipfile
import re
import json
import struct
import io
from datetime import datetime

if sys.stdout.encoding and sys.stdout.encoding.lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower().replace('-', '') != 'utf8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import logging
    logging.getLogger("androguard").setLevel(logging.CRITICAL)
    try:
        from loguru import logger as loguru_logger
        loguru_logger.disable("androguard")
    except ImportError:
        pass
    from androguard.core.apk import APK
    HAS_ANDROGUARD = True
except ImportError:
    HAS_ANDROGUARD = False


# ============================================================================
# SDK EXCLUSION PATTERNS
# ============================================================================

SDK_EXCLUSION_PATTERNS = [
    "com/adjust/sdk", "com.adjust.sdk",
    "com/appsflyer", "com.appsflyer",
    "com/facebook/appevents", "com.facebook.appevents",
    "google/android/gms/ads", "PackageInfoSignalSource",
    "gads:",
    "com/singular", "com.singular",
    "io/branch", "io.branch",
    "com/kochava", "com.kochava",
    "okhttp3/CertificatePinner", "okhttp3.CertificatePinner",
    "org/bouncycastle", "org.bouncycastle",
    "google/android/material",
    "PackageInfoCompat",
    "vending.billing", "vending/billing", "IInAppBilling",
    "approved_installers_for_package_name_override",
    "install_source_info_signal",
]

PLAY_INTEGRITY_FALSE_POSITIVE_CONTEXTS = [
    "com.facebook.appevents.integrity",
    "com/facebook/appevents/integrity",
]


# ============================================================================
# PAIRIP INDICATORS  (Auto Protect)
# ============================================================================

PAIRIP_DEX_STRINGS = [
    "com.pairip",
    "com/pairip",
    "pairipcore",
    "licensecheck2",
]


# ============================================================================
# DEX STRING EXTRACTION
# ============================================================================

def extract_dex_strings_raw(dex_bytes):
    strings = set()
    try:
        if len(dex_bytes) < 112 or dex_bytes[:4] != b'dex\n':
            return strings
        string_ids_size = struct.unpack_from('<I', dex_bytes, 56)[0]
        string_ids_off = struct.unpack_from('<I', dex_bytes, 60)[0]
        for i in range(string_ids_size):
            str_data_off = struct.unpack_from('<I', dex_bytes, string_ids_off + i * 4)[0]
            if str_data_off >= len(dex_bytes):
                continue
            pos = str_data_off
            size = 0
            shift = 0
            while pos < len(dex_bytes):
                byte = dex_bytes[pos]
                size |= (byte & 0x7F) << shift
                pos += 1
                if byte & 0x80 == 0:
                    break
                shift += 7
            end = min(pos + size, len(dex_bytes))
            try:
                s = dex_bytes[pos:end].decode('utf-8', errors='ignore')
                if len(s) > 3:
                    strings.add(s)
            except Exception:
                pass
    except Exception:
        pass
    return strings


# ============================================================================
# SDK CONTEXT FILTER
# ============================================================================

def _is_sdk_noise(matched_dex_string):
    lowered = matched_dex_string.lower()
    for pattern in SDK_EXCLUSION_PATTERNS:
        if pattern.lower() in lowered:
            return True
    return False


# ============================================================================
# ANALYZER
# ============================================================================

class PlayIntegrityAnalyzer:
    def __init__(self, apk_path):
        self.apk_path = apk_path
        self.apk_name = os.path.basename(apk_path)
        self.package_name = "unknown"
        self.app_name = "unknown"
        self.dex_strings = set()
        self.zip_entries = []
        self.manifest_content = ""
        self.extraction_errors = []
        self.dex_file_count = 0

        self.pairip_evidence = []
        self.play_integrity_evidence = []
        self.play_integrity_detected = False
        self.lvl_evidence = []

        self.results = {"fail": [], "warning": [], "pass": [], "info": []}

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def analyze(self):
        print(f"\n{'='*70}")
        print(f"  Play Integrity & App Protection Analyzer v4")
        print(f"  APK: {self.apk_name}")
        print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*70}\n")

        print("[1/5] Extracting APK data...")
        self._extract_apk_data()
        print(f"      Package: {self.package_name}")
        print(f"      App Name: {self.app_name}")
        print(f"      DEX files: {self.dex_file_count}")
        print(f"      Extracted {len(self.dex_strings)} unique strings")

        print("\n[2/5] Checking for pairip (Auto Protect)...")
        self._check_pairip()

        print("\n[3/5] Checking for Play Integrity API...")
        self._check_play_integrity()

        print("\n[4/5] Checking Legacy Play Licensing...")
        self._check_legacy_licensing()

        print("\n[5/5] Generating report...\n")
        verdict = self._determine_verdict()
        self._print_report(verdict)
        return self.results

    def _extract_apk_data(self):
        if HAS_ANDROGUARD:
            try:
                apk = APK(self.apk_path)
                self.package_name = apk.get_package() or "unknown"
                self.app_name = apk.get_app_name() or "unknown"
                manifest_xml = apk.get_android_manifest_xml()
                if manifest_xml is not None:
                    try:
                        from lxml import etree
                        self.manifest_content = etree.tostring(manifest_xml, encoding='unicode')
                    except Exception:
                        self.manifest_content = str(manifest_xml)
            except Exception as e:
                self.extraction_errors.append(f"Androguard manifest parse failed: {e}")
                print(f"      [!] Androguard manifest parse failed: {e}")
                self._fallback_manifest()
        else:
            self._fallback_manifest()

        try:
            with zipfile.ZipFile(self.apk_path, 'r') as zf:
                all_names = zf.namelist()
                self.zip_entries = all_names
                dex_files = [f for f in all_names if f.endswith('.dex')]

                if not dex_files:
                    inner_apks = [f for f in all_names if f.lower().endswith('.apk')]
                    if inner_apks:
                        print(f"      Detected split APK bundle with {len(inner_apks)} inner APK(s)")
                        self._extract_from_split_bundle(zf, inner_apks)
                        return

                self.dex_file_count = len(dex_files)
                for dex_file in dex_files:
                    strings = extract_dex_strings_raw(zf.read(dex_file))
                    self.dex_strings.update(strings)
                    print(f"      Parsed {dex_file}: {len(strings)} strings")
        except Exception as e:
            self.extraction_errors.append(f"DEX extraction error: {e}")
            print(f"      [!] DEX extraction error: {e}")

    def _extract_from_split_bundle(self, outer_zf, inner_apk_names):
        import tempfile
        all_inner_entries = []
        for inner_name in sorted(inner_apk_names):
            try:
                inner_bytes = outer_zf.read(inner_name)
                with zipfile.ZipFile(io.BytesIO(inner_bytes), 'r') as inner_zf:
                    inner_names = inner_zf.namelist()
                    for entry in inner_names:
                        all_inner_entries.append(f"{inner_name}/{entry}")
                    dex_files = [f for f in inner_names if f.endswith('.dex')]
                    self.dex_file_count += len(dex_files)
                    for dex_file in dex_files:
                        strings = extract_dex_strings_raw(inner_zf.read(dex_file))
                        self.dex_strings.update(strings)
                        print(f"      Parsed {inner_name}/{dex_file}: {len(strings)} strings")

                    is_base = 'base' in inner_name.lower()
                    if is_base and self.package_name == "unknown":
                        if HAS_ANDROGUARD:
                            tmp = None
                            try:
                                tmp = tempfile.NamedTemporaryFile(suffix='.apk', delete=False)
                                tmp.write(inner_bytes); tmp.close()
                                apk = APK(tmp.name)
                                self.package_name = apk.get_package() or "unknown"
                                self.app_name = apk.get_app_name() or "unknown"
                                mx = apk.get_android_manifest_xml()
                                if mx is not None:
                                    try:
                                        from lxml import etree
                                        self.manifest_content = etree.tostring(mx, encoding='unicode')
                                    except Exception:
                                        self.manifest_content = str(mx)
                            except Exception as e:
                                print(f"      [!] Androguard failed on {inner_name}: {e}")
                            finally:
                                if tmp:
                                    try: os.unlink(tmp.name)
                                    except Exception: pass
                        if self.package_name == "unknown" and 'AndroidManifest.xml' in inner_names:
                            self.manifest_content = inner_zf.read('AndroidManifest.xml').decode('utf-8', errors='ignore')
            except Exception as e:
                self.extraction_errors.append(f"Error reading {inner_name}: {e}")
                print(f"      [!] Error reading {inner_name}: {e}")

        self.zip_entries.extend(all_inner_entries)

    def _fallback_manifest(self):
        try:
            with zipfile.ZipFile(self.apk_path, 'r') as zf:
                if 'AndroidManifest.xml' in zf.namelist():
                    self.manifest_content = zf.read('AndroidManifest.xml').decode('utf-8', errors='ignore')
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 1: pairip (Auto Protect)
    # ------------------------------------------------------------------

    def _check_pairip(self):
        evidence = []

        for entry in self.zip_entries:
            if 'libpairipcore.so' in entry:
                evidence.append(f"FILE: {entry}")
            elif entry.startswith('lib/') and 'pairip' in entry.lower():
                evidence.append(f"FILE: {entry}")

        for dex_str in self.dex_strings:
            for indicator in PAIRIP_DEX_STRINGS:
                if indicator.lower() in dex_str.lower():
                    evidence.append(f"DEX: '{indicator}' in '{dex_str[:120]}'")
                    break

        for entry in self.zip_entries:
            if entry.startswith('assets/') and 'pairip' in entry.lower():
                evidence.append(f"ASSET: {entry}")

        self.pairip_evidence = evidence
        if evidence:
            print(f"      [FAIL] pairip detected -- {len(evidence)} indicators")
            for ev in evidence[:5]:
                print(f"        - {ev}")
            if len(evidence) > 5:
                print(f"        ... and {len(evidence)-5} more")
        else:
            print(f"      [OK] No pairip / Auto Protect found")

    # ------------------------------------------------------------------
    # Step 2: Play Integrity API  (WARNING)
    # ------------------------------------------------------------------

    def _check_play_integrity(self):
        strong_strings = [
            "com.google.android.play.core.integrity",
            "com/google/android/play/core/integrity",
        ]
        supporting_strings = [
            "IntegrityTokenRequest",
            "IntegrityTokenResponse",
            "StandardIntegrityManager",
            "IntegrityServiceException",
        ]

        evidence = []
        strong_hits = 0

        for search in strong_strings:
            for dex_str in self.dex_strings:
                if search.lower() in dex_str.lower():
                    if _is_sdk_noise(dex_str):
                        continue
                    if any(ctx in dex_str for ctx in PLAY_INTEGRITY_FALSE_POSITIVE_CONTEXTS):
                        continue
                    evidence.append(f"DEX: '{search}' in '{dex_str[:120]}'")
                    strong_hits += 1
                    break

        for search in supporting_strings:
            for dex_str in self.dex_strings:
                if search.lower() in dex_str.lower():
                    if _is_sdk_noise(dex_str):
                        continue
                    if any(ctx in dex_str for ctx in PLAY_INTEGRITY_FALSE_POSITIVE_CONTEXTS):
                        continue
                    evidence.append(f"DEX: '{search}' in '{dex_str[:120]}'")
                    break

        self.play_integrity_detected = strong_hits >= 1
        self.play_integrity_evidence = evidence

        if self.play_integrity_detected:
            print(f"      [WARNING] Play Integrity API detected -- {len(evidence)} indicators")
            for ev in evidence[:5]:
                print(f"        - {ev}")
        else:
            print(f"      [OK] No Play Integrity API found")

    # ------------------------------------------------------------------
    # Step 3: Legacy Play Licensing
    # ------------------------------------------------------------------

    def _check_legacy_licensing(self):
        strong_dex_strings = [
            "com.google.android.vending.licensing",
            "com/google/android/vending/licensing",
            "ServerManagedPolicy",
            "StrictPolicy",
        ]
        evidence = []
        dex_strong_hits = 0

        for search in strong_dex_strings:
            for dex_str in self.dex_strings:
                if search.lower() in dex_str.lower():
                    if _is_sdk_noise(dex_str):
                        continue
                    evidence.append(f"DEX: '{search}' in '{dex_str[:120]}'")
                    dex_strong_hits += 1
                    break

        has_manifest_permission = "com.android.vending.CHECK_LICENSE" in self.manifest_content
        if has_manifest_permission:
            evidence.append("MANIFEST: 'com.android.vending.CHECK_LICENSE' declared")

        self.lvl_evidence = evidence if dex_strong_hits >= 1 else []

        if self.lvl_evidence:
            print(f"      [FAIL] Legacy Play Licensing -- {len(evidence)} indicators")
            for ev in evidence[:5]:
                print(f"        - {ev}")
        elif has_manifest_permission:
            print(f"      [OK] Legacy Play Licensing (CHECK_LICENSE in manifest but no DEX evidence)")
        else:
            print(f"      [OK] No Legacy Play Licensing (LVL)")

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    def _determine_verdict(self):
        extraction_failed = (
            len(self.dex_strings) == 0
            or (self.package_name == "unknown" and self.dex_file_count == 0)
        )

        if self.pairip_evidence:
            self.results["fail"].append({
                "id": "pairip_auto_protect",
                "name": "Auto Protect (pairip)",
                "description": (
                    "Google injects the pairip library at Play Console upload time. "
                    "It blocks ALL non-Play installs including Digital Turbine. "
                    "The developer must disable Auto Protection in Play Console "
                    "and upload a new build."
                ),
                "evidence": self.pairip_evidence,
            })

        if self.play_integrity_detected:
            self.results["warning"].append({
                "id": "play_integrity_api",
                "name": "Play Integrity API",
                "description": (
                    "App uses Play Integrity API. Cannot determine statically "
                    "whether DT installs are permitted -- verdict handling is "
                    "server-side. Requires one-time manual verification: confirm "
                    "with the developer that KNOWN_ installers (including Digital "
                    "Turbine) are allowed through."
                ),
                "evidence": self.play_integrity_evidence,
            })

        if self.lvl_evidence:
            self.results["fail"].append({
                "id": "legacy_play_licensing",
                "name": "Legacy Play Licensing (LVL)",
                "description": (
                    "App uses Legacy Play Licensing which blocks all non-Play "
                    "installs. No KNOWN_ installer exemption exists in LVL."
                ),
                "evidence": self.lvl_evidence,
            })

        if extraction_failed and not self.results["fail"]:
            self.results["info"].append({
                "id": "extraction_failed",
                "name": "Extraction Failure",
                "description": "Could not fully analyze this APK. Manual testing required.",
            })

        if self.results["fail"]:
            return "FAIL"
        if extraction_failed:
            return "INCONCLUSIVE"
        if self.results["warning"]:
            return "WARNING"
        return "PASS"

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _print_report(self, verdict):
        icons = {"FAIL": "[FAIL]", "WARNING": "[WARN]", "PASS": "[PASS]", "INCONCLUSIVE": "[????]"}
        icon = icons.get(verdict, "[????]")

        messages = {
            "FAIL": "This APK will break when preinstalled via Digital Turbine.",
            "WARNING": "This APK may have issues when preinstalled. Manual verification needed.",
            "PASS": "No blocking protections detected. APK is likely safe for DT preloads.",
            "INCONCLUSIVE": "Could not fully analyze this APK. DO NOT treat as PASS. Manual testing required.",
        }

        print(f"{'='*70}")
        print(f"  VERDICT: {icon} {verdict}")
        print(f"{'='*70}")
        print(f"  Package:  {self.package_name}")
        print(f"  App Name: {self.app_name}")
        print(f"  {messages[verdict]}")
        print()

        if self.extraction_errors:
            print(f"  {'-'*66}")
            print(f"  [!] Extraction issues:")
            for err in self.extraction_errors:
                print(f"    - {err}")
            print()

        if self.results["fail"]:
            print(f"  {'-'*66}")
            print(f"  [FAIL] Will block DT preloads:")
            print(f"  {'-'*66}")
            for item in self.results["fail"]:
                print(f"\n    * {item['name']}")
                print(f"      {item['description']}")
                if item.get("evidence"):
                    print(f"      Evidence ({len(item['evidence'])} indicators):")
                    for ev in item['evidence'][:8]:
                        print(f"        - {ev}")
                    if len(item['evidence']) > 8:
                        print(f"        ... and {len(item['evidence'])-8} more")

        if self.results["warning"]:
            print(f"\n  {'-'*66}")
            print(f"  [WARNING] Needs manual verification:")
            print(f"  {'-'*66}")
            for item in self.results["warning"]:
                print(f"\n    * {item['name']}")
                msg = item.get('description') or item.get('message', '')
                print(f"      {msg}")
                if item.get("evidence"):
                    print(f"      Evidence ({len(item['evidence'])} indicators):")
                    for ev in item['evidence'][:5]:
                        print(f"        - {ev}")

        if self.results["info"]:
            print(f"\n  {'-'*66}")
            for item in self.results["info"]:
                print(f"  [!] {item['name']}: {item['description']}")

        print(f"\n{'='*70}")
        print(f"  Summary: {len(self.results['fail'])} FAIL | {len(self.results['warning'])} WARNING")
        print(f"{'='*70}\n")

        return verdict

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    def to_json(self):
        verdict = self._determine_verdict_silent()
        return {
            "apk": self.apk_name,
            "package": self.package_name,
            "app_name": self.app_name,
            "verdict": verdict,
            "fail_count": len(self.results["fail"]),
            "warning_count": len(self.results["warning"]),
            "dex_string_count": len(self.dex_strings),
            "extraction_errors": self.extraction_errors,
            "play_integrity_detected": self.play_integrity_detected,
            "details": {
                "fail": [self._item_json(r) for r in self.results["fail"]],
                "warning": [self._item_json(r) for r in self.results["warning"]],
            },
            "analyzed_at": datetime.now().isoformat(),
        }

    def _determine_verdict_silent(self):
        if self.results["fail"]:
            return "FAIL"
        extraction_failed = (
            len(self.dex_strings) == 0
            or (self.package_name == "unknown" and self.dex_file_count == 0)
        )
        if extraction_failed:
            return "INCONCLUSIVE"
        if self.results["warning"]:
            return "WARNING"
        return "PASS"

    @staticmethod
    def _item_json(r):
        entry = {"id": r.get("id", ""), "name": r.get("name", "")}
        if r.get("evidence"):
            entry["evidence_count"] = len(r["evidence"])
            entry["evidence"] = r["evidence"][:10]
        if r.get("message"):
            entry["message"] = r["message"]
        if r.get("description"):
            entry["description"] = r["description"]
        return entry


# ============================================================================
# BATCH PROCESSING
# ============================================================================

def analyze_directory(directory):
    apk_files = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith('.apk'):
                apk_files.append(os.path.join(root, f))

    if not apk_files:
        print(f"No APK files found in {directory}")
        return

    print(f"\nFound {len(apk_files)} APK(s) to analyze\n")

    all_results = []
    for apk_path in sorted(apk_files):
        analyzer = PlayIntegrityAnalyzer(apk_path)
        analyzer.analyze()
        all_results.append(analyzer.to_json())

    counts = {v: sum(1 for r in all_results if r["verdict"] == v)
              for v in ("FAIL", "WARNING", "PASS", "INCONCLUSIVE")}

    print(f"\n{'='*70}")
    print(f"  BATCH SUMMARY")
    print(f"{'='*70}")
    print(f"  Total APKs:  {len(all_results)}")
    for label in ("FAIL", "WARNING", "PASS", "INCONCLUSIVE"):
        if counts[label]:
            print(f"  [{label:13s}] {counts[label]}")
    print(f"{'='*70}")

    for label in ("FAIL", "WARNING"):
        matches = [r for r in all_results if r["verdict"] == label]
        if matches:
            print(f"\n  {label} APKs:")
            for r in matches:
                key = "fail" if label == "FAIL" else "warning"
                reasons = ", ".join(d["name"] for d in r["details"].get(key, []))
                print(f"    [{label[:4]}] {r['apk']} -- {reasons or 'see report'}")

    inconclusive = [r for r in all_results if r["verdict"] == "INCONCLUSIVE"]
    if inconclusive:
        print(f"\n  INCONCLUSIVE APKs:")
        for r in inconclusive:
            print(f"    [????] {r['apk']} -- manual testing required")

    report_path = os.path.join(directory, "play_integrity_report.json")
    try:
        with open(report_path, 'w') as f:
            json.dump(all_results, f, indent=2)
        print(f"\n  Full report saved to: {report_path}")
    except Exception:
        report_path = os.path.expanduser("~/play_integrity_report.json")
        with open(report_path, 'w') as f:
            json.dump(all_results, f, indent=2)
        print(f"\n  Full report saved to: {report_path}")
    print()
    return all_results


# ============================================================================
# MAIN
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python play_integrity_analyzer.py <path_to_apk>")
        print("  python play_integrity_analyzer.py <directory_of_apks>")
        sys.exit(1)

    target = sys.argv[1]
    if os.path.isdir(target):
        analyze_directory(target)
    elif os.path.isfile(target) and target.lower().endswith('.apk'):
        analyzer = PlayIntegrityAnalyzer(target)
        analyzer.analyze()
        json_path = target.rsplit('.', 1)[0] + '_integrity_report.json'
        try:
            with open(json_path, 'w') as f:
                json.dump(analyzer.to_json(), f, indent=2)
            print(f"  JSON report saved to: {json_path}")
        except Exception:
            json_path = os.path.expanduser("~/play_integrity_report.json")
            with open(json_path, 'w') as f:
                json.dump(analyzer.to_json(), f, indent=2)
            print(f"  JSON report saved to: {json_path}")
    else:
        print(f"Error: '{target}' is not a valid APK file or directory.")
        sys.exit(1)


if __name__ == "__main__":
    main()

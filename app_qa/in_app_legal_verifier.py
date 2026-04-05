#!/usr/bin/env python3
"""
In-App Legal Verifier — runtime UI verification of Privacy Policy and T&C
accessibility inside Android apps using a local emulator and adb.

Prerequisites:
  - Android SDK Platform Tools (adb on PATH)
  - Running Android emulator or connected device
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


ADB = "adb"
HIERARCHY_PATH = "/sdcard/window_dump.xml"


def _adb(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ADB] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_device_connected() -> bool:
    result = _adb(["devices"])
    lines = result.stdout.strip().splitlines()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return True
    return False


def install_apk(apk_path: str) -> bool:
    """Install an APK, .apks bundle, or directory of split APKs.

    Supports:
      - Regular .apk files
      - Directories containing base.apk + split_*.apk files
      - .apks bundles (ZIP of split APKs, from bundletool)
      - .zip files containing APKs
      - Auto-patches INSTALL_FAILED_MISSING_SPLIT by removing requiredSplitTypes
    """
    if os.path.isdir(apk_path):
        return _install_split_dir(apk_path)

    ext = os.path.splitext(apk_path)[1].lower()

    if ext in (".apks", ".zip"):
        return _install_apk_bundle(apk_path)

    result = _adb(["install", "-r", apk_path], timeout=120)
    combined = result.stdout + result.stderr
    if result.returncode == 0 and "Success" in combined:
        return True

    if "INSTALL_FAILED_MISSING_SPLIT" in combined:
        try:
            from patch_apk import needs_split_patch, patch_apk as do_patch
            if needs_split_patch(apk_path):
                patched = do_patch(apk_path)
                r2 = _adb(["install", "-r", "-t", patched], timeout=120)
                return r2.returncode == 0 and "Success" in (r2.stdout + r2.stderr)
        except Exception:
            pass

    return False


def _install_split_dir(dir_path: str) -> bool:
    """Install split APKs from a directory using adb install-multiple.

    Picks base.apk + the best ABI split for the connected device.
    """
    apks = [os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith(".apk")]
    if not apks:
        return False

    device_abi = _adb(["shell", "getprop", "ro.product.cpu.abilist"]).stdout.strip()
    preferred_abis = [a.strip().replace("-", "_") for a in device_abi.split(",")]

    base = [a for a in apks if os.path.basename(a) == "base.apk"]
    splits = [a for a in apks if os.path.basename(a) != "base.apk"]

    selected = list(base)
    for abi in preferred_abis:
        abi_splits = [s for s in splits if abi in os.path.basename(s)]
        if abi_splits:
            selected.extend(abi_splits)
            break

    non_abi_splits = [s for s in splits
                      if not any(a in os.path.basename(s) for a in ("arm", "x86", "mips"))]
    selected.extend(non_abi_splits)

    if not selected:
        selected = apks

    result = _adb(["install-multiple", "-r"] + selected, timeout=180)
    combined = result.stdout + result.stderr
    return result.returncode == 0 and "Success" in combined


def _install_apk_bundle(bundle_path: str) -> bool:
    """Install an .apks/.zip bundle by extracting to a folder and using _install_split_dir.

    Follows the manual testing approach: treat .apks as a ZIP, extract all
    APKs into a flat directory, then let _install_split_dir handle ABI selection.

    Also handles the case where a .zip is actually a renamed single APK
    (contains AndroidManifest.xml + classes.dex but no .apk files inside).
    """
    import zipfile

    try:
        with zipfile.ZipFile(bundle_path, "r") as z:
            names = z.namelist()
            apk_names = [n for n in names if n.endswith(".apk")]

            if not apk_names:
                # No .apk files inside — check if the ZIP itself IS a renamed APK
                if "AndroidManifest.xml" in names:
                    import shutil
                    tmp_apk = bundle_path + ".tmp.apk"
                    try:
                        shutil.copy2(bundle_path, tmp_apk)
                        result = _adb(["install", "-r", tmp_apk], timeout=120)
                        combined = result.stdout + result.stderr
                        if result.returncode == 0 and "Success" in combined:
                            return True

                        if "INSTALL_FAILED_MISSING_SPLIT" in combined:
                            try:
                                from patch_apk import needs_split_patch, patch_apk as do_patch
                                if needs_split_patch(tmp_apk):
                                    patched = do_patch(tmp_apk)
                                    r2 = _adb(["install", "-r", "-t", patched], timeout=120)
                                    c2 = r2.stdout + r2.stderr
                                    return r2.returncode == 0 and "Success" in c2
                            except Exception:
                                pass
                        return False
                    finally:
                        if os.path.exists(tmp_apk):
                            os.unlink(tmp_apk)
                return False

            with tempfile.TemporaryDirectory() as tmpdir:
                for name in apk_names:
                    dst = os.path.join(tmpdir, os.path.basename(name))
                    with open(dst, "wb") as f:
                        f.write(z.read(name))

                return _install_split_dir(tmpdir)
    except Exception:
        return False


def launch_app(package: str, wait_timeout: int = 15) -> bool:
    """Launch app and wait until it's in the foreground.

    Tries monkey first, falls back to am start if the app doesn't foreground.
    Returns True if the app reached the foreground within wait_timeout seconds.
    """
    _adb([
        "shell", "monkey", "-p", package,
        "-c", "android.intent.category.LAUNCHER", "1",
    ])
    deadline = time.time() + wait_timeout
    tried_am = False
    while time.time() < deadline:
        time.sleep(2)
        fg = get_foreground_package()
        if fg and fg == package:
            time.sleep(2)
            fg2 = get_foreground_package()
            if fg2 and fg2 == package:
                return True
            # App appeared but immediately crashed
            return False

        if _detect_app_crash(package):
            return False

        if not tried_am and time.time() - (deadline - wait_timeout) > 5:
            tried_am = True
            _adb([
                "shell", "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "--activity-single-top", package,
            ])
            time.sleep(3)
            fg = get_foreground_package()
            if fg and fg == package:
                time.sleep(2)
                fg2 = get_foreground_package()
                if fg2 and fg2 == package:
                    return True
                return False
    return False


def _detect_app_crash(package: str) -> bool:
    """Check logcat for recent crash of the given package (last 50 lines)."""
    result = _adb(["logcat", "-d", "-t", "50", "--pid",
                    _get_app_pid(package) or "0"], timeout=5)
    output = result.stdout + result.stderr
    crash_signals = ["FATAL EXCEPTION", "Process: " + package + ", PID:",
                     "has died", "Force finishing activity"]
    return any(s in output for s in crash_signals)


def _get_app_pid(package: str) -> str | None:
    result = _adb(["shell", "pidof", package], timeout=5)
    pid = result.stdout.strip()
    return pid if pid.isdigit() else None


def get_foreground_package() -> str | None:
    result = _adb(["shell", "dumpsys", "activity", "activities"])
    for pattern in ("mResumedActivity", "topResumedActivity", "ResumedActivity"):
        for line in result.stdout.splitlines():
            if pattern in line:
                match = re.search(r"u0 ([a-zA-Z0-9_.]+)/", line)
                if match:
                    return match.group(1)
    return None


def uninstall_app(package: str) -> None:
    _adb(["uninstall", package], timeout=30)


def take_screenshot(output_path: str) -> bool:
    _adb(["shell", "screencap", "-p", "/sdcard/screenshot.png"])
    result = _adb(["pull", "/sdcard/screenshot.png", output_path])
    _adb(["shell", "rm", "/sdcard/screenshot.png"])
    return result.returncode == 0


def tap(x: int, y: int) -> None:
    _adb(["shell", "input", "tap", str(x), str(y)])
    time.sleep(1)


def press_back() -> None:
    _adb(["shell", "input", "keyevent", "4"])
    time.sleep(0.5)


def swipe_left() -> None:
    _adb(["shell", "input", "swipe", "800", "500", "200", "500", "300"])
    time.sleep(1)


def dump_ui_hierarchy() -> str:
    _adb(["shell", "uiautomator", "dump", HIERARCHY_PATH])
    result = _adb(["shell", "cat", HIERARCHY_PATH])
    xml = result.stdout
    xml = re.sub(r"<\?xml[^?]*\?>", "", xml).strip()
    return xml


def hierarchy_hash(xml: str) -> str:
    return hashlib.md5(xml.encode("utf-8", errors="ignore")).hexdigest()


@dataclass
class UiElement:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    clickable: bool
    bounds_raw: str
    package: str = ""
    center_x: int = 0
    center_y: int = 0

    def __post_init__(self):
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", self.bounds_raw)
        if match:
            x1, y1, x2, y2 = (int(g) for g in match.groups())
            self.center_x = (x1 + x2) // 2
            self.center_y = (y1 + y2) // 2

    @property
    def searchable_text(self) -> str:
        return f"{self.text} {self.content_desc}".strip().lower()


PRIORITY_1_KEYWORDS = [
    "privacy policy", "privacy", "terms of service",
    "terms and conditions", "terms of use", "terms & conditions",
    "legal", "eula", "end user license agreement",
]
PRIORITY_2_KEYWORDS = [
    "settings", "setting", "preferences", "gear",
]
PRIORITY_3_KEYWORDS = [
    "about", "info", "app info", "about us",
]
PRIORITY_4_KEYWORDS = [
    "open navigation drawer", "menu", "navigation", "drawer",
    "more options", "profile", "account", "me", "more",
]

_PRIORITY_MAP = {
    1: PRIORITY_1_KEYWORDS,
    2: PRIORITY_2_KEYWORDS,
    3: PRIORITY_3_KEYWORDS,
    4: PRIORITY_4_KEYWORDS,
}


def parse_ui_elements(xml_str: str, clickable_only: bool = True) -> list[UiElement]:
    elements: list[UiElement] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return elements
    for node in root.iter("node"):
        clickable = node.get("clickable", "false") == "true"
        if clickable_only and not clickable:
            continue
        elements.append(UiElement(
            text=node.get("text", ""),
            content_desc=node.get("content-desc", ""),
            resource_id=node.get("resource-id", ""),
            class_name=node.get("class", ""),
            clickable=clickable,
            bounds_raw=node.get("bounds", "[0,0][0,0]"),
            package=node.get("package", ""),
        ))
    return elements


def _keyword_in_text(search: str, kw: str) -> bool:
    """Match keyword as a whole word/phrase (avoids e.g. 'me' in 'game')."""
    return re.search(r"\b" + re.escape(kw) + r"\b", search) is not None


def find_elements_by_keywords(
    elements: list[UiElement], priority: int
) -> list[UiElement]:
    keywords = _PRIORITY_MAP.get(priority, [])
    matches: list[UiElement] = []
    for el in elements:
        search = el.searchable_text
        if any(_keyword_in_text(search, kw) for kw in keywords):
            matches.append(el)
    return matches


DISMISS_PATTERNS: dict[str, list[str]] = {
    "skip": ["skip", "skip intro", "skip tutorial", "skip for now"],
    "advance": ["next", "continue", "get started", "let's go", "start"],
    "permission": ["allow", "while using the app", "while using", "only this time"],
    "consent": ["accept", "accept all", "i agree", "ok", "got it", "agree", "consent"],
    "defer": ["not now", "later", "no thanks", "maybe later", "remind me later"],
    "close": ["close", "dismiss"],
}
LOGIN_KEYWORDS = ["sign in", "log in", "create account", "register"]


def classify_dismiss_action(el: UiElement) -> str | None:
    search = el.searchable_text
    if any(kw in search for kw in LOGIN_KEYWORDS):
        return "login_wall"
    for action, keywords in DISMISS_PATTERNS.items():
        if any(kw in search for kw in keywords):
            return action
    if el.text.strip() in ("X", "x", "×") or "close" in el.content_desc.lower():
        return "close"
    return None


SYSTEM_DIALOG_PACKAGES = {"com.google.android.permissioncontroller",
                          "com.android.permissioncontroller",
                          "com.android.packageinstaller",
                          "com.google.android.packageinstaller"}

SYSTEM_DIALOG_ACTIONS = {"allow", "don't allow", "deny", "while using the app",
                         "only this time", "ok", "cancel", "close"}


def dismiss_system_dialogs() -> bool:
    """Handle system-level dialogs (permission requests, crash dialogs, etc.).

    Returns True if a dialog was dismissed.
    """
    xml = dump_ui_hierarchy()
    elements = parse_ui_elements(xml)
    if not elements:
        return False

    any_system = any(el.package in SYSTEM_DIALOG_PACKAGES for el in elements)
    if not any_system:
        return False

    for el in elements:
        if not el.clickable:
            continue
        text = el.text.lower().strip()
        resource = el.resource_id.lower()
        if text in SYSTEM_DIALOG_ACTIONS or "permission_allow" in resource or \
                "permission_deny" in resource:
            tap(el.center_x, el.center_y)
            time.sleep(1)
            return True

    return False


def _rank_clickable(el: UiElement) -> int:
    """Score a clickable element for how likely it advances onboarding.

    Lower score = tap first.  Pattern-matched dismiss buttons beat generic
    clickable elements, which beat login/sign-in elements.
    """
    search = el.searchable_text
    # Best: known dismiss keywords
    for keywords in DISMISS_PATTERNS.values():
        if any(kw in search for kw in keywords):
            return 0
    # Good: looks like a button (Button, ImageButton class)
    cls = el.class_name.lower()
    if "button" in cls:
        return 1
    # OK: any clickable with text
    if el.text.strip():
        return 2
    # Fallback: clickable with no text (icon buttons, etc.)
    return 3


EXTERNAL_LOGIN_PACKAGES = {
    "com.android.vending", "com.google.android.gms",
    "com.google.android.gsf.login", "com.google.android.play.games",
    "com.facebook.katana", "com.facebook.orca",
}


def run_dismiss_loop(max_seconds: int = 60) -> str:
    """Aggressively push through onboarding by tapping clickable elements.

    Strategy: on every screen, dismiss system dialogs first, then tap the
    most promising clickable element (known dismiss patterns first, then
    any button, then any clickable).  Keeps going until the screen
    stabilises or we time out.

    Returns:
        "ok"             — reached a stable screen
        "login_wall"     — only login elements remain with zero alternatives
        "timeout"        — exhausted all attempts
    """
    start = time.time()
    prev_hash = ""
    stable_count = 0
    tapped_coords: set[tuple[int, int]] = set()

    time.sleep(3)

    while time.time() - start < max_seconds:
        # --- System dialogs (permission prompts, etc.) ---
        if dismiss_system_dialogs():
            stable_count = 0
            prev_hash = ""
            tapped_coords.clear()
            time.sleep(1)
            continue

        xml = dump_ui_hierarchy()
        h = hierarchy_hash(xml)
        if h == prev_hash:
            stable_count += 1
            if stable_count >= 3:
                return "ok"
            time.sleep(2)
            continue
        else:
            stable_count = 0
            tapped_coords.clear()
        prev_hash = h

        # --- Gather all clickable + non-clickable elements ---
        clickable = parse_ui_elements(xml, clickable_only=True)
        all_els = parse_ui_elements(xml, clickable_only=False)

        # --- External login screens: press back ---
        fg = get_foreground_package()
        if fg and fg in EXTERNAL_LOGIN_PACKAGES:
            press_back()
            time.sleep(2)
            stable_count = 0
            prev_hash = ""
            continue

        # --- Rank clickable elements and tap the best one ---
        candidates = [el for el in clickable
                      if (el.center_x, el.center_y) not in tapped_coords
                      and el.center_x > 0 and el.center_y > 0]

        # Also pick up non-clickable elements with dismiss text
        for el in all_els:
            if el.clickable:
                continue
            action = classify_dismiss_action(el)
            if action and action != "login_wall":
                if (el.center_x, el.center_y) not in tapped_coords:
                    candidates.append(el)

        if not candidates:
            # Nothing left to tap — check if it's a pure login wall
            has_login = any(
                any(kw in el.searchable_text for kw in LOGIN_KEYWORDS)
                for el in all_els
            )
            if has_login:
                # Last resort: try pressing back
                press_back()
                time.sleep(2)
                prev_hash = ""
                # If we keep landing on login with zero options, give up
                stable_count += 1
                if stable_count >= 3:
                    return "login_wall"
                continue
            stable_count += 1
            if stable_count >= 3:
                return "ok"
            time.sleep(2)
            continue

        # Sort: dismiss-pattern matches first, then buttons, then rest
        candidates.sort(key=_rank_clickable)
        best = candidates[0]
        tapped_coords.add((best.center_x, best.center_y))
        tap(best.center_x, best.center_y)
        time.sleep(1.5)
        stable_count = 0

    return "timeout"


GAME_VIEW_CLASSES = {"android.view.SurfaceView", "android.opengl.GLSurfaceView"}


def is_game_canvas(xml_str: str) -> bool:
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    all_nodes = list(root.iter("node"))
    if not all_nodes:
        return False
    clickable_with_text = [
        n for n in all_nodes
        if n.get("clickable") == "true"
        and (n.get("text", "").strip() or n.get("content-desc", "").strip())
    ]
    has_game_view = any(n.get("class") in GAME_VIEW_CLASSES for n in all_nodes)
    return has_game_view and len(clickable_with_text) == 0


def _get_screen_size() -> tuple[int, int]:
    result = _adb(["shell", "wm", "size"])
    match = re.search(r"(\d+)x(\d+)", result.stdout)
    if match:
        return int(match.group(1)), int(match.group(2))
    return 1080, 1920


def _count_ui_nodes(xml_str: str) -> int:
    try:
        root = ET.fromstring(xml_str)
        return sum(1 for _ in root.iter("node"))
    except ET.ParseError:
        return 0


def _has_native_overlay(xml_str: str) -> bool:
    """Check if native Android elements are layered on top of a game canvas."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    nodes = list(root.iter("node"))
    has_surface = any(n.get("class") in GAME_VIEW_CLASSES for n in nodes)
    has_text = any(
        n.get("text", "").strip() or n.get("content-desc", "").strip()
        for n in nodes if n.get("class") not in GAME_VIEW_CLASSES
    )
    return has_surface and has_text


def run_game_tutorial_bypass() -> str:
    """Attempt to navigate past game onboarding using center/corner taps.

    Falls back to blind tapping when OCR/visual detection isn't needed yet.

    Returns:
        "ok"                    — native UI elements appeared
        "game_tutorial_blocked" — all attempts failed
    """
    w, h = _get_screen_size()
    cx, cy = w // 2, h // 2

    xml = dump_ui_hierarchy()
    elements = parse_ui_elements(xml)
    for el in elements:
        search = el.searchable_text
        if any(kw in search for kw in ["skip", "close", "x", "×"]):
            tap(el.center_x, el.center_y)
            time.sleep(1)
            xml = dump_ui_hierarchy()
            if not is_game_canvas(xml):
                return "ok"

    for _ in range(3):
        tap(cx, cy)
        time.sleep(2)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml) or _has_native_overlay(xml):
            return "ok"
        dismiss_system_dialogs()

    for _ in range(3):
        swipe_left()
        time.sleep(1.5)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml) or _has_native_overlay(xml):
            return "ok"

    for _ in range(2):
        time.sleep(3)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml) or _has_native_overlay(xml):
            return "ok"
        dismiss_system_dialogs()

    return "game_tutorial_blocked"


def _navigate_game_with_screen_analysis(
    package: str,
    screenshot_dir: str | None,
    max_depth: int = 4,
    timeout: int = 120,
) -> dict:
    """Navigate a game-engine app using OCR + visual element detection.

    Takes screenshots, runs OCR + OpenCV contour detection to find
    interactive elements (icons, buttons, text labels), then taps them
    to drill into settings/support menus looking for PP and T&C.

    Returns dict matching the navigate_to_legal() return schema.
    """
    from screen_analyzer import (
        analyze_emulator_screen, find_settings_icon, find_navigation_targets,
        find_by_keywords, find_close_or_dismiss, tap_element, ScreenElement,
    )

    start = time.time()
    pp_found = False
    tc_found = False
    pp_path: list[str] = []
    tc_path: list[str] = []
    visited_screens: set[str] = set()

    w, h = _get_screen_size()

    def _screen_signature(elements: list) -> str:
        labels = sorted(e.label.lower() for e in elements if hasattr(e, "label"))
        return "|".join(labels[:10])

    def _check_legal(elements: list, path: list[str]) -> bool:
        nonlocal pp_found, tc_found, pp_path, tc_path
        found_any = False

        pp_matches = find_by_keywords(elements, "privacy policy", "privacy")
        if pp_matches and not pp_found:
            pp_found = True
            pp_path = path + [pp_matches[0].label]
            found_any = True

        tc_matches = find_by_keywords(
            elements,
            "terms of use", "terms of service", "terms and conditions",
            "terms & conditions", "eula",
        )
        if tc_matches and not tc_found:
            tc_found = True
            tc_path = path + [tc_matches[0].label]
            found_any = True

        return found_any

    def _wait_for_load(seconds: int = 5):
        time.sleep(seconds)

    path: list[str] = []

    # Step 1: Analyze the initial game screen
    elements, _ = analyze_emulator_screen(run_ocr=True)
    _check_legal(elements, path)
    if pp_found and tc_found:
        return _legal_result(pp_found, tc_found, pp_path, tc_path)

    sig = _screen_signature(elements)
    visited_screens.add(sig)

    # Step 2: Find and tap the settings icon
    settings = find_settings_icon(elements, w, h)
    if settings:
        tap_element(settings)
        path.append("settings_icon")
        _wait_for_load(3)
        dismiss_system_dialogs()

        elements, _ = analyze_emulator_screen(run_ocr=True)
        _check_legal(elements, path)
        if pp_found and tc_found:
            return _legal_result(pp_found, tc_found, pp_path, tc_path)

        sig = _screen_signature(elements)
        visited_screens.add(sig)

        # Step 3: Look for navigation targets (support, about, help, etc.)
        nav_targets = find_navigation_targets(elements)
        for target in nav_targets:
            if time.time() - start > timeout:
                break

            tap_element(target)
            sub_path = path + [target.label]
            _wait_for_load(8)

            sub_elements, _ = analyze_emulator_screen(run_ocr=True)
            sub_sig = _screen_signature(sub_elements)

            if sub_sig in visited_screens:
                press_back()
                time.sleep(1)
                continue
            visited_screens.add(sub_sig)

            _check_legal(sub_elements, sub_path)
            if pp_found and tc_found:
                return _legal_result(pp_found, tc_found, pp_path, tc_path)

            # If this opened a webview/page, scroll down and check again
            _adb(["shell", "input", "swipe", "540", "1800", "540", "600", "500"])
            time.sleep(2)
            scroll_elements, _ = analyze_emulator_screen(run_ocr=True)
            _check_legal(scroll_elements, sub_path)
            if pp_found and tc_found:
                return _legal_result(pp_found, tc_found, pp_path, tc_path)

            press_back()
            time.sleep(2)

    # Step 4: If settings icon wasn't found, try direct navigation text
    if not settings and not (pp_found and tc_found):
        nav_targets = find_navigation_targets(elements)
        for target in nav_targets:
            if time.time() - start > timeout:
                break
            tap_element(target)
            _wait_for_load(8)

            sub_elements, _ = analyze_emulator_screen(run_ocr=True)
            _check_legal(sub_elements, [target.label])
            if pp_found and tc_found:
                return _legal_result(pp_found, tc_found, pp_path, tc_path)
            press_back()
            time.sleep(1)

    return _legal_result(pp_found, tc_found, pp_path, tc_path)


def _check_linked_page_for_legal(nav: dict, check_type: str,
                                 base_path: list[str]) -> None:
    """Check the currently visible page (after tapping a legal link) for
    the missing legal item.  Scrolls down and checks both UI hierarchy
    and OCR to find the other legal document on the same linked page."""
    pp_kws = ["privacy policy", "privacy notice", "privacy"]
    tc_kws = ["terms of use", "terms of service", "terms and conditions",
              "terms & conditions", "eula"]
    target_kws = pp_kws if check_type == "pp" else tc_kws
    key_found = "pp_found" if check_type == "pp" else "tc_found"
    key_path = "pp_path" if check_type == "pp" else "tc_path"

    def _scan_xml() -> bool:
        xml = dump_ui_hierarchy()
        all_els = parse_ui_elements(xml, clickable_only=False)
        for el in all_els:
            if any(kw in el.searchable_text for kw in target_kws):
                nav[key_found] = True
                nav[key_path] = base_path + [el.text or el.content_desc]
                return True
        return False

    # Check the initial view
    if _scan_xml():
        return

    # Scroll down and check again (legal links are often below the fold)
    for _ in range(3):
        _adb(["shell", "input", "swipe", "540", "1800", "540", "600", "500"])
        time.sleep(2)
        if _scan_xml():
            return

    # Also try OCR in case the page is a WebView that UI Automator can't read
    try:
        from screen_analyzer import analyze_emulator_screen, find_by_keywords
        elements, _ = analyze_emulator_screen(run_ocr=True)
        matches = find_by_keywords(elements, *target_kws)
        if matches:
            nav[key_found] = True
            nav[key_path] = base_path + [matches[0].label]
    except Exception:
        pass


def _legal_result(pp_found: bool, tc_found: bool,
                  pp_path: list[str], tc_path: list[str]) -> dict:
    return {
        "pp_found": pp_found,
        "tc_found": tc_found,
        "pp_path": pp_path,
        "tc_path": tc_path,
        "pp_element": None,
        "tc_element": None,
        "blocker": None,
    }


PP_KEYWORDS = ["privacy policy", "privacy"]
TC_KEYWORDS = [
    "terms of service", "terms and conditions", "terms of use",
    "terms & conditions", "terms", "eula", "end user license agreement",
]
LEGAL_GENERIC_KEYWORDS = ["legal"]


@dataclass
class NavigationResult:
    pp_element: UiElement | None = None
    tc_element: UiElement | None = None
    entry_point: UiElement | None = None


def _match_legal(el: UiElement) -> tuple[bool, bool]:
    search = el.searchable_text
    is_pp = any(kw in search for kw in PP_KEYWORDS)
    is_tc = any(kw in search for kw in TC_KEYWORDS)
    if not is_pp and not is_tc and any(kw in search for kw in LEGAL_GENERIC_KEYWORDS):
        is_pp = True
        is_tc = True
    return is_pp, is_tc


def find_legal_screens_from_elements(
    elements: list[UiElement],
) -> NavigationResult:
    result = NavigationResult()
    for el in elements:
        is_pp, is_tc = _match_legal(el)
        if is_pp and result.pp_element is None:
            result.pp_element = el
        if is_tc and result.tc_element is None:
            result.tc_element = el

    if result.pp_element or result.tc_element:
        return result

    for priority in (2, 3, 4):
        matches = find_elements_by_keywords(elements, priority)
        if matches:
            result.entry_point = matches[0]
            return result

    return result


def navigate_to_legal(max_depth: int = 3, timeout: int = 45) -> dict:
    """Navigate the app UI to find Privacy Policy and T&C screens.

    Returns dict with:
        pp_found, tc_found, pp_path, tc_path, pp_element, tc_element, blocker
    """
    start = time.time()
    pp_found = False
    tc_found = False
    pp_path: list[str] = []
    tc_path: list[str] = []
    pp_element: UiElement | None = None
    tc_element: UiElement | None = None
    visited_hashes: set[str] = set()

    def _search_current_screen(depth: int, path: list[str]) -> None:
        nonlocal pp_found, tc_found, pp_path, tc_path, pp_element, tc_element

        if time.time() - start > timeout:
            return
        if depth > max_depth:
            return

        xml = dump_ui_hierarchy()
        h = hierarchy_hash(xml)
        if h in visited_hashes:
            return
        visited_hashes.add(h)

        elements = parse_ui_elements(xml)
        nav = find_legal_screens_from_elements(elements)

        if nav.pp_element and not pp_found:
            pp_element = nav.pp_element
            pp_path = path + [nav.pp_element.text or nav.pp_element.content_desc]
            pp_found = True

        if nav.tc_element and not tc_found:
            tc_element = nav.tc_element
            tc_path = path + [nav.tc_element.text or nav.tc_element.content_desc]
            tc_found = True

        if pp_found and tc_found:
            return

        if nav.entry_point and depth < max_depth:
            label = nav.entry_point.text or nav.entry_point.content_desc
            tap(nav.entry_point.center_x, nav.entry_point.center_y)
            _search_current_screen(depth + 1, path + [label])

            if not (pp_found and tc_found):
                press_back()
                time.sleep(0.5)

                xml2 = dump_ui_hierarchy()
                elements2 = parse_ui_elements(xml2)
                for priority in (2, 3, 4):
                    candidates = find_elements_by_keywords(elements2, priority)
                    for candidate in candidates:
                        c_label = candidate.text or candidate.content_desc
                        if c_label == label:
                            continue
                        tap(candidate.center_x, candidate.center_y)
                        _search_current_screen(depth + 1, path + [c_label])
                        if pp_found and tc_found:
                            return
                        press_back()
                        time.sleep(0.5)

    _search_current_screen(0, [])

    return {
        "pp_found": pp_found,
        "tc_found": tc_found,
        "pp_path": pp_path,
        "tc_path": tc_path,
        "pp_element": pp_element,
        "tc_element": tc_element,
        "blocker": None,
    }


PP_TITLE_MARKERS = [
    "privacy policy", "privacy notice", "data privacy",
]
TC_TITLE_MARKERS = [
    "terms of service", "terms and conditions", "terms of use",
    "end user license agreement", "legal notice", "terms & conditions",
]
LEGAL_PHRASE_MARKERS = [
    "we collect", "personal information", "personal data",
    "third parties", "data processing", "your rights",
    "cookies", "you agree to", "by using", "we may share",
    "data controller", "opt out",
]


def verify_legal_content(xml_str: str, check_type: str = "pp") -> dict:
    """Verify that the current screen shows real legal content.

    Args:
        xml_str: UI hierarchy XML dump
        check_type: "pp" for privacy policy, "tc" for terms & conditions

    Returns:
        {"verified": bool, "method": str|None, "url": str|None}
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return {"verified": False, "method": None, "url": None}

    for node in root.iter("node"):
        if "WebView" in node.get("class", ""):
            url = node.get("content-desc", "") or node.get("text", "")
            return {"verified": True, "method": "webview", "url": url or None}

    all_text = ""
    for node in root.iter("node"):
        text = node.get("text", "")
        if text:
            all_text += " " + text
    all_text_lower = all_text.lower()

    title_markers = PP_TITLE_MARKERS if check_type == "pp" else TC_TITLE_MARKERS
    has_title = any(marker in all_text_lower for marker in title_markers)
    phrase_hits = sum(1 for marker in LEGAL_PHRASE_MARKERS if marker in all_text_lower)

    if has_title and phrase_hits >= 2:
        return {"verified": True, "method": "text_content", "url": None}

    return {"verified": False, "method": None, "url": None}


def verify_in_app_legal(
    apk_path: str,
    package: str,
    screenshot_dir: str | None = None,
    gemini_api_key: str | None = None,
    device_serial: str | None = None,
) -> dict:
    """Full pipeline: install → launch → navigate → verify → cleanup.

    When *gemini_api_key* is provided the new VisionAgent drives navigation
    (LLM navigator + XML sensor).  Without it the legacy heuristic path runs.
    """
    if not check_device_connected():
        return {
            "error": "No Android device/emulator connected. Run 'adb devices' to check.",
            "privacy_policy": _fail_result("No device"),
            "terms_and_conditions": _fail_result("No device"),
            "navigation_info": _empty_nav_info(),
        }

    if screenshot_dir:
        os.makedirs(screenshot_dir, exist_ok=True)

    if not install_apk(apk_path):
        return {
            "error": f"Failed to install {apk_path}",
            "privacy_policy": _fail_result("Install failed"),
            "terms_and_conditions": _fail_result("Install failed"),
            "navigation_info": _empty_nav_info(),
        }

    try:
        if not launch_app(package):
            note = f"App {package} did not reach foreground after launch"
            if _detect_app_crash(package) or not _get_app_pid(package):
                note += (
                    ". The app crashed on start — if this is a base-only App Bundle APK, "
                    "it may be missing required split APKs (native libraries)."
                )
            return {
                "error": note,
                "privacy_policy": _fail_result("Launch failed — app crashed"),
                "terms_and_conditions": _fail_result("Launch failed — app crashed"),
                "navigation_info": _empty_nav_info(),
            }

        if gemini_api_key:
            return _run_vision_path(
                package, screenshot_dir or ".", gemini_api_key, device_serial,
            )

        return _run_legacy_path(package, screenshot_dir)

    finally:
        uninstall_app(package)


def _run_vision_path(
    package: str, screenshot_dir: str,
    api_key: str, device_serial: str | None,
) -> dict:
    """LLM-driven navigation via VisionAgent."""
    from vision_agent import VisionAgent

    agent = VisionAgent(
        package=package,
        screenshot_dir=screenshot_dir,
        api_key=api_key,
        device_serial=device_serial,
    )
    return agent.run()


def _run_legacy_path(package: str, screenshot_dir: str | None) -> dict:
    """Original heuristic-based navigation (fallback when no API key)."""
    nav_info = {
        "onboarding_dismissed": False,
        "login_wall": False,
        "game_tutorial_blocked": False,
        "screens_visited": 0,
        "navigation_time_seconds": 0.0,
    }

    start_time = time.time()

    dismiss_result = run_dismiss_loop(max_seconds=60)
    nav_info["onboarding_dismissed"] = dismiss_result != "login_wall"

    for _ in range(3):
        dismiss_system_dialogs()
        time.sleep(1)
        fg = get_foreground_package()
        if fg and fg == package:
            break
        if fg and fg != package:
            _adb([
                "shell", "monkey", "-p", package,
                "-c", "android.intent.category.LAUNCHER", "1",
            ])
            time.sleep(3)

    if dismiss_result == "login_wall":
        nav_info["login_wall"] = True
        pp_ss = _take_ss(screenshot_dir, package, "login_wall")
        return {
            "privacy_policy": _inconclusive_result("LOGIN_WALL", pp_ss),
            "terms_and_conditions": _inconclusive_result("LOGIN_WALL", pp_ss),
            "navigation_info": nav_info,
        }

    xml = dump_ui_hierarchy()
    use_game_mode = is_game_canvas(xml)

    if use_game_mode:
        run_game_tutorial_bypass()
        dismiss_system_dialogs()
        time.sleep(2)

        nav = _navigate_game_with_screen_analysis(
            package, screenshot_dir, max_depth=4, timeout=120,
        )
    else:
        nav = navigate_to_legal(max_depth=3, timeout=45)

    nav_info["screens_visited"] = len(nav.get("pp_path", [])) + len(nav.get("tc_path", []))

    if nav["pp_found"] and not nav["tc_found"] and nav.get("pp_element"):
        el = nav["pp_element"]
        tap(el.center_x, el.center_y)
        time.sleep(4)
        _check_linked_page_for_legal(nav, "tc", [el.text or el.content_desc])
        press_back()
        time.sleep(1)
    elif nav["tc_found"] and not nav["pp_found"] and nav.get("tc_element"):
        el = nav["tc_element"]
        tap(el.center_x, el.center_y)
        time.sleep(4)
        _check_linked_page_for_legal(nav, "pp", [el.text or el.content_desc])
        press_back()
        time.sleep(1)

    pp_result = _build_check_result(nav, "pp", package, screenshot_dir)
    tc_result = _build_check_result(nav, "tc", package, screenshot_dir)

    nav_info["navigation_time_seconds"] = round(time.time() - start_time, 1)

    return {
        "privacy_policy": pp_result,
        "terms_and_conditions": tc_result,
        "navigation_info": nav_info,
    }


def _build_check_result(
    nav: dict, check_type: str, package: str, screenshot_dir: str | None
) -> dict:
    key_found = "pp_found" if check_type == "pp" else "tc_found"
    key_path = "pp_path" if check_type == "pp" else "tc_path"
    key_element = "pp_element" if check_type == "pp" else "tc_element"

    if not nav[key_found]:
        return {
            "ui_found": False,
            "ui_path": [],
            "ui_method": None,
            "ui_url": None,
            "screenshot": None,
            "notes": [],
        }

    element = nav[key_element]

    # If element is None but found is True, it was detected via OCR/screen analysis
    if element is None:
        ss_path = _take_ss(screenshot_dir, package, check_type)
        return {
            "ui_found": True,
            "ui_path": nav[key_path],
            "ui_method": "ocr_screen_analysis",
            "ui_url": None,
            "screenshot": ss_path,
            "notes": [],
        }

    # If the element text is a strong direct match (e.g. "Privacy Policy",
    # "Terms of Use"), the presence of the labeled link is sufficient proof —
    # no need to tap and verify the destination page.
    strong_pp = ["privacy policy", "privacy notice", "data privacy"]
    strong_tc = ["terms of service", "terms of use", "terms and conditions",
                 "terms & conditions", "eula", "end user license agreement"]
    strong_kws = strong_pp if check_type == "pp" else strong_tc
    el_text = element.searchable_text
    if any(kw in el_text for kw in strong_kws):
        ss_path = _take_ss(screenshot_dir, package, check_type)
        return {
            "ui_found": True,
            "ui_path": nav[key_path],
            "ui_method": "direct_label",
            "ui_url": None,
            "screenshot": ss_path,
            "notes": [],
        }

    tap(element.center_x, element.center_y)
    time.sleep(2)

    xml = dump_ui_hierarchy()
    verification = verify_legal_content(xml, check_type)

    ss_path = _take_ss(screenshot_dir, package, check_type)

    result = {
        "ui_found": verification["verified"],
        "ui_path": nav[key_path],
        "ui_method": verification["method"],
        "ui_url": verification.get("url"),
        "screenshot": ss_path,
        "notes": [] if verification["verified"] else [
            f"Found a '{nav[key_path][-1] if nav[key_path] else '?'}' element "
            "but could not confirm legal content on the destination screen.",
        ],
    }

    press_back()
    time.sleep(0.5)

    return result


def _take_ss(screenshot_dir: str | None, package: str, label: str) -> str | None:
    if not screenshot_dir:
        return None
    path = os.path.join(screenshot_dir, f"{package}_{label}.png")
    if take_screenshot(path):
        return path
    return None


def _fail_result(note: str) -> dict:
    return {
        "ui_found": False, "ui_path": [], "ui_method": None,
        "ui_url": None, "screenshot": None, "notes": [note],
    }


def _inconclusive_result(confidence: str, screenshot: str | None,
                         note: str | None = None) -> dict:
    return {
        "ui_found": False, "ui_path": [], "ui_method": None,
        "ui_url": None, "screenshot": screenshot,
        "notes": [note or f"Navigation blocked: {confidence}"],
    }


def _empty_nav_info() -> dict:
    return {
        "onboarding_dismissed": False, "login_wall": False,
        "game_tutorial_blocked": False, "screens_visited": 0,
        "navigation_time_seconds": 0.0,
    }


def compute_verdict(
    static_found: bool,
    ui_found: bool,
    blocker: str | None,
) -> dict:
    """Compute verdict and confidence from static + UI results.

    Args:
        static_found: True if static analysis found legal links/activities
        ui_found: True if UI verification confirmed accessible legal content
        blocker: None, "LOGIN_WALL", "TUTORIAL_BLOCKED", or "UNVERIFIED"
    """
    if blocker in ("LOGIN_WALL", "TUTORIAL_BLOCKED"):
        return {"verdict": "INCONCLUSIVE", "confidence": blocker}

    if ui_found and static_found:
        return {"verdict": "PASS", "confidence": "STRONG"}

    if ui_found and not static_found:
        return {"verdict": "PASS", "confidence": "CONFIRMED"}

    if not ui_found and static_found and blocker != "UNVERIFIED":
        return {"verdict": "FAIL", "confidence": "STATIC_ONLY"}

    if blocker == "UNVERIFIED":
        return {"verdict": "FAIL", "confidence": "UNVERIFIED"}

    return {"verdict": "FAIL", "confidence": "NOT_FOUND"}


def _detect_package_name(apk_path: str) -> str | None:
    """Extract package name from an APK file, directory of splits, or .apks bundle."""
    target = apk_path

    if os.path.isdir(apk_path):
        base = os.path.join(apk_path, "base.apk")
        if os.path.isfile(base):
            target = base
        else:
            apks = [os.path.join(apk_path, f) for f in os.listdir(apk_path)
                    if f.endswith(".apk")]
            target = apks[0] if apks else None

    elif os.path.splitext(apk_path)[1].lower() in (".apks", ".zip"):
        import zipfile
        try:
            with zipfile.ZipFile(apk_path, "r") as z:
                apk_names = [n for n in z.namelist() if n.endswith(".apk")]
                base_names = [n for n in apk_names if os.path.basename(n) == "base.apk"]
                pick = base_names[0] if base_names else (apk_names[0] if apk_names else None)
                if pick:
                    tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
                    tmp.write(z.read(pick))
                    tmp.close()
                    target = tmp.name
        except Exception:
            pass

    if not target or not os.path.isfile(target):
        return None

    pkg = _detect_package_from_apk(target)

    if target != apk_path and target.startswith(tempfile.gettempdir()):
        try:
            os.unlink(target)
        except OSError:
            pass

    return pkg


def _detect_package_from_apk(apk_file: str) -> str | None:
    """Extract package name from a single APK file."""
    try:
        r = subprocess.run(
            ["aapt2", "dump", "badging", apk_file],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if line.startswith("package:"):
                for part in line.split():
                    if part.startswith("name="):
                        return part.split("=")[1].strip("'\"")
    except Exception:
        pass

    try:
        from androguard.core.apk import APK
        return APK(apk_file).get_package()
    except Exception:
        pass

    return None


def main():
    import argparse
    p = argparse.ArgumentParser(description="Verify in-app legal content accessibility")
    p.add_argument("apk", help="Path to APK file or directory of split APKs")
    p.add_argument("--package", "-p", default=None,
                   help="Package name (auto-detected from APK if omitted)")
    p.add_argument("--screenshots", "-s", default=None, help="Directory to save screenshots")
    p.add_argument("--json", action="store_true", help="Output JSON")
    args = p.parse_args()

    package = args.package
    if not package:
        package = _detect_package_name(args.apk)
        if not package:
            p.error("Could not auto-detect package name. Use --package to specify it.")

    result = verify_in_app_legal(args.apk, package, args.screenshots)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for check in ("privacy_policy", "terms_and_conditions"):
            r = result[check]
            label = "Privacy Policy" if check == "privacy_policy" else "Terms & Conditions"
            found = r["ui_found"]
            icon = "FOUND" if found else "NOT FOUND"
            path = " > ".join(r["ui_path"]) if r["ui_path"] else "N/A"
            print(f"  {label}: {icon}  (path: {path})")
            for note in r.get("notes", []):
                print(f"    Note: {note}")

        nav = result.get("navigation_info", {})
        if nav.get("login_wall"):
            print("  BLOCKED: Login wall detected")
        if nav.get("game_tutorial_blocked"):
            print("  BLOCKED: Game tutorial could not be bypassed")


if __name__ == "__main__":
    main()

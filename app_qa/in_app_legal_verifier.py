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
    result = _adb(["install", "-r", apk_path], timeout=120)
    return result.returncode == 0 and "Success" in result.stdout


def launch_app(package: str) -> None:
    _adb([
        "shell", "monkey", "-p", package,
        "-c", "android.intent.category.LAUNCHER", "1",
    ])
    time.sleep(3)


def get_foreground_package() -> str | None:
    result = _adb(["shell", "dumpsys", "activity", "activities"])
    for line in result.stdout.splitlines():
        if "mResumedActivity" in line:
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
    return result.stdout


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


def parse_ui_elements(xml_str: str) -> list[UiElement]:
    elements: list[UiElement] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return elements
    for node in root.iter("node"):
        clickable = node.get("clickable", "false") == "true"
        if not clickable:
            continue
        elements.append(UiElement(
            text=node.get("text", ""),
            content_desc=node.get("content-desc", ""),
            resource_id=node.get("resource-id", ""),
            class_name=node.get("class", ""),
            clickable=clickable,
            bounds_raw=node.get("bounds", "[0,0][0,0]"),
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
    "skip": ["skip", "skip intro", "skip tutorial"],
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


def run_dismiss_loop(max_seconds: int = 30) -> str:
    """Dismiss onboarding popups for up to max_seconds.

    Returns:
        "ok"             — reached a stable screen (main screen)
        "login_wall"     — detected a login requirement with no skip option
        "timeout"        — loop exhausted without reaching stable screen
    """
    start = time.time()
    prev_hash = ""

    while time.time() - start < max_seconds:
        xml = dump_ui_hierarchy()
        h = hierarchy_hash(xml)
        if h == prev_hash:
            return "ok"
        prev_hash = h

        elements = parse_ui_elements(xml)
        login_elements = []
        dismiss_elements = []

        for el in elements:
            action = classify_dismiss_action(el)
            if action == "login_wall":
                login_elements.append(el)
            elif action is not None:
                dismiss_elements.append(el)

        if login_elements and not dismiss_elements:
            return "login_wall"

        if dismiss_elements:
            tap(dismiss_elements[0].center_x, dismiss_elements[0].center_y)
            time.sleep(1.5)
            continue

        return "ok"

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


def run_game_tutorial_bypass() -> str:
    """Attempt to get past a game tutorial screen.

    Returns:
        "ok"                    — UI elements appeared, tutorial bypassed
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
        time.sleep(1.5)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml):
            return "ok"

    for tx, ty in [(w - 100, cy), (w - 100, h - 200)]:
        tap(tx, ty)
        time.sleep(1.5)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml):
            return "ok"

    for _ in range(4):
        swipe_left()
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml):
            return "ok"

    for _ in range(3):
        time.sleep(3)
        xml = dump_ui_hierarchy()
        if not is_game_canvas(xml):
            return "ok"

    return "game_tutorial_blocked"


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

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

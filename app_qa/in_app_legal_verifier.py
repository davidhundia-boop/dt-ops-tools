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

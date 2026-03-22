"""
APK fetcher helper for Play Integrity Screener.
Handles Play Store URL parsing and APK download via apkeep CLI.
"""

import glob
import os
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse


def extract_package_name(input_str: str) -> str:
    """
    Accepts:
    - Full Play Store URL: https://play.google.com/store/apps/details?id=com.example.app
    - URL with extra params: ...?id=com.example.app&hl=en
    - Bare package name: com.example.app

    Returns the package name. Raises ValueError if unrecognized.
    """
    input_str = input_str.strip()

    if "play.google.com" in input_str:
        parsed = urlparse(input_str)
        params = parse_qs(parsed.query)
        if "id" in params:
            return params["id"][0]
        raise ValueError(f"Could not find package id in Play Store URL: {input_str}")

    # Bare package name: at least two dot-separated segments, valid Java identifier chars
    if re.match(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$", input_str):
        return input_str

    raise ValueError(
        f"Unrecognized input: {input_str!r}\n"
        "Expected a Google Play Store URL or a package name like com.example.app"
    )


def fetch_apk(package_name: str, output_dir: str) -> str:
    """
    Downloads APK using apkeep CLI:
      apkeep -a <package_name> <output_dir>

    Returns the path to the downloaded .apk file.

    Raises RuntimeError if apkeep is not installed, times out, or no APK is found.
    Cleans up output_dir on failure.
    """
    if shutil.which("apkeep") is None:
        raise RuntimeError(
            "apkeep is not installed or not in PATH.\n"
            "Install via:  cargo install apkeep\n"
            "Or download a binary from: https://github.com/EFForg/apkeep/releases"
        )

    try:
        proc = subprocess.run(
            ["apkeep", "-a", package_name, output_dir],
            timeout=60,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        _cleanup(output_dir)
        raise RuntimeError(
            f"apkeep timed out after 60 seconds downloading {package_name}"
        )
    except Exception as exc:
        _cleanup(output_dir)
        raise RuntimeError(f"apkeep failed: {exc}") from exc

    # Search recursively first, then flat
    apk_files = glob.glob(os.path.join(output_dir, "**", "*.apk"), recursive=True)
    if not apk_files:
        apk_files = glob.glob(os.path.join(output_dir, "*.apk"))

    if not apk_files:
        stderr_hint = (proc.stderr or proc.stdout or "").strip()
        _cleanup(output_dir)
        raise RuntimeError(
            f"No APK found after running apkeep for {package_name}.\n"
            + (f"apkeep output: {stderr_hint}" if stderr_hint else "")
        )

    return apk_files[0]


def _cleanup(directory: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)

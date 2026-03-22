#!/usr/bin/env python3
"""
QA Bot — Slack bot that runs all 3 QA scripts on an APK and posts a report.

Trigger: @mention the bot in any channel with "New App QA" + APK attachment or URL.
Scripts are kept up to date by pulling from the QA-Agent repo on startup.
The report is posted as a single threaded reply containing summary + full details.

Setup:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in your tokens
    python qa_bot.py
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

QA_AGENT_REPO = "https://github.com/davidhundia-boop/QA-Agent.git"
SCRIPTS_DIR = Path(__file__).parent / "scripts"
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

app = App(token=BOT_TOKEN)


def _sync_scripts() -> None:
    """Pull the latest scripts from the QA-Agent repo into scripts/."""
    repo_dir = SCRIPTS_DIR / ".qa-agent-repo"
    if repo_dir.exists():
        print("Updating QA-Agent repo...")
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], check=True)
    else:
        print("Cloning QA-Agent repo...")
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", QA_AGENT_REPO, str(repo_dir)], check=True)

    # Copy the three analyser scripts into scripts/ so existing import paths work
    for script in ("wake_lock_analyzer.py", "play_integrity_analyzer.py", "check_app_legal.py"):
        src = repo_dir / script
        if src.exists():
            shutil.copy2(src, SCRIPTS_DIR / script)

    print("Scripts synced from QA-Agent repo.")


_sync_scripts()


# ── APK Download ───────────────────────────────────────────────────────────────

def download_apk_slack(file_info: dict) -> tuple[str, str]:
    """Download an APK from a Slack file attachment. Returns (tmp_path, filename)."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise ValueError("No download URL found in Slack file info")
    headers = {"Authorization": f"Bearer {BOT_TOKEN}"}
    resp = requests.get(url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
    for chunk in resp.iter_content(chunk_size=65536):
        tmp.write(chunk)
    tmp.close()
    return tmp.name, file_info.get("name", "unknown.apk")


def download_apk_url(url: str) -> tuple[str, str]:
    """Download an APK from a direct URL. Returns (tmp_path, filename)."""
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    filename = url.split("/")[-1].split("?")[0] or "download.apk"
    if not filename.lower().endswith(".apk"):
        filename += ".apk"
    tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
    for chunk in resp.iter_content(chunk_size=65536):
        tmp.write(chunk)
    tmp.close()
    return tmp.name, filename


# ── Script Runners ─────────────────────────────────────────────────────────────

def run_wake_lock(apk_path: str) -> dict:
    """Run wake_lock_analyzer.py as a subprocess; parse its JSON stdout."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "wake_lock_analyzer.py"), apk_path],
            capture_output=True, text=True, timeout=300,
        )
        output = result.stdout.strip()
        if output:
            return json.loads(output)
        return {"error": (result.stderr or "No output from script")[:500]}
    except subprocess.TimeoutExpired:
        return {"error": "Wake lock analysis timed out (300s)"}
    except json.JSONDecodeError as exc:
        return {"error": f"Failed to parse wake lock output: {exc}"}
    except Exception as exc:
        return {"error": str(exc)}


def run_play_integrity(apk_path: str) -> dict:
    """Run play_integrity_analyzer.py; reads the JSON report file it creates."""
    # The script writes JSON next to the APK or falls back to ~/play_integrity_report.json
    json_path = apk_path.rsplit(".", 1)[0] + "_integrity_report.json"
    fallback_path = os.path.expanduser("~/play_integrity_report.json")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "play_integrity_analyzer.py"), apk_path],
            capture_output=True, text=True, timeout=300,
        )
        for path in [json_path, fallback_path]:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return data
        error_msg = result.stderr or result.stdout or "No JSON report file created"
        return {"error": error_msg[:500]}
    except subprocess.TimeoutExpired:
        return {"error": "Play Integrity analysis timed out (300s)"}
    except Exception as exc:
        return {"error": str(exc)}


def run_legal(apk_path: str) -> dict:
    """Import check_app_legal as a module and call check_app() for structured data."""
    spec = importlib.util.spec_from_file_location(
        "check_app_legal", SCRIPTS_DIR / "check_app_legal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit as exc:
        if exc.code != 0:
            return {"error": "Missing dependency: google-play-scraper. Run: pip install google-play-scraper"}
    except Exception as exc:
        return {"error": f"Import error in check_app_legal: {exc}"}

    try:
        pkg, apk_src = mod.resolve_input(apk_path)
        if not pkg:
            return {"error": "Could not extract package name from APK (is aapt/aapt2 installed?)"}

        session = mod.create_session()
        r = mod.check_app(pkg, session, apk_source=apk_src, verbose=False)
        ds = r.data_safety
        return {
            "package_name": r.package_name,
            "app_name": r.app_name,
            "developer": r.developer,
            "play_store_found": r.play_store_found,
            "privacy_policy_url": r.privacy_policy_url,
            "developer_website": r.developer_website,
            "developer_email": r.developer_email,
            "privacy_policy_verdict": r.privacy_policy_verdict,
            "tc_verdict": r.tc_verdict,
            "confidence": r.confidence,
            "notes": r.notes,
            "tc_links": [{"text": lnk.text, "url": lnk.url} for lnk in r.tc_links],
            "data_safety": {
                "status": ds.status,
                "no_data_collected": ds.no_data_collected,
                "no_data_shared": ds.no_data_shared,
                "collected": [
                    {"category": c.category, "data_types": c.data_types, "purposes": c.purposes}
                    for c in ds.collected
                ],
                "shared": [
                    {"category": c.category, "data_types": c.data_types}
                    for c in ds.shared
                ],
                "security_practices": ds.security_practices,
                "plausibility": ds.plausibility,
                "suspect_permissions": ds.suspect_permissions,
            } if ds else None,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── Slack Event Handler ────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, client, say):
    text = event.get("text", "")
    channel = event.get("channel")
    ts = event.get("ts")

    if "new app qa" not in text.lower():
        return

    # Acknowledge immediately in thread so the user knows it's working
    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=":hourglass_flowing_sand: Running QA analysis (Wake Lock + Play Integrity + Legal)... ~1–2 min.",
    )

    # ── Resolve APK source ──
    apk_path = None
    apk_filename = None

    # 1. Slack file attachment
    for f in event.get("files", []):
        if f.get("name", "").lower().endswith(".apk") or f.get("filetype") == "apk":
            try:
                apk_path, apk_filename = download_apk_slack(f)
                break
            except Exception as exc:
                client.chat_postMessage(
                    channel=channel, thread_ts=ts,
                    text=f":x: Failed to download attached APK: {exc}",
                )
                return

    # 2. Direct URL in message text
    if not apk_path:
        urls = re.findall(r"https?://[^\s>|]+\.apk[^\s>|]*", text)
        if urls:
            try:
                apk_path, apk_filename = download_apk_url(urls[0])
            except Exception as exc:
                client.chat_postMessage(
                    channel=channel, thread_ts=ts,
                    text=f":x: Failed to download APK from URL: {exc}",
                )
                return

    if not apk_path:
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=(
                ":question: No APK found. Please either:\n"
                "• Attach the `.apk` file directly to your message, or\n"
                "• Include a direct download URL ending in `.apk`\n\n"
                "_Example: `@QA Bot New App QA` with an APK file attached_"
            ),
        )
        return

    # ── Run all 3 analyses ──
    try:
        wl = run_wake_lock(apk_path)
        pi = run_play_integrity(apk_path)
        legal = run_legal(apk_path)

        from report_formatter import build_report_blocks
        blocks = build_report_blocks(wl, pi, legal, apk_filename or "unknown.apk")

        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            blocks=blocks,
            text="QA Report complete",
        )
    except Exception as exc:
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f":x: Unexpected error during analysis: {exc}",
        )
    finally:
        if apk_path and os.path.isfile(apk_path):
            try:
                os.unlink(apk_path)
            except Exception:
                pass


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("QA Bot is running. Waiting for @mentions with 'New App QA'...")
    handler.start()

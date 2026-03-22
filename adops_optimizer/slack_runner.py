"""
Slack-based Campaign Optimizer runner.
Listens for /optimize slash command, grabs the two most recent file uploads
from the channel (site_performance .xlsx + DT_DX .csv), runs the optimizer,
and posts the output Excel + summary back to Slack.

Setup:
  1. Create a Slack App at https://api.slack.com/apps
  2. Enable Socket Mode (Settings > Socket Mode > toggle on, create app-level token)
  3. Add these Bot Token Scopes (OAuth & Permissions):
       - channels:history
       - channels:read
       - chat:write
       - commands
       - files:read
       - files:write
  4. Create Slash Command: /optimize
       - Request URL: leave blank (socket mode handles it)
       - Description: "Run campaign optimization on uploaded files"
  5. Install the app to your workspace
  6. Invite the bot to your channel: /invite @YourBotName
  7. Set env vars and run:
       export SLACK_BOT_TOKEN=xoxb-...
       export SLACK_APP_TOKEN=xapp-...
       python slack_runner.py

Usage:
  1. Upload your two files to the channel (site_performance.xlsx + DT_DX.csv)
  2. Type /optimize (uses default Domino Dreams preset)
     or /optimize preset_name (for other presets you define below)
"""

import os
import sys
import tempfile
import traceback
from io import BytesIO

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Presets — add new clients here
# ---------------------------------------------------------------------------
PRESETS = {
    "domino_dreams": {
        "label": "Domino Dreams – ROAS D7",
        "kpi_mode": "roas",
        "kpi_col_d7_spec": "Domino Dreams Marketing Campaigns Daily Metrics Full ROAS D7",
        "kpi_col_d2nd_spec": None,          # None = reuse D7 column
        "kpi_d7_pct": 2.18,
        "kpi_d2nd_pct": None,               # None = same as D7
        "weight_main": 1.0,
        "weight_secondary": 0.0,
    },
    # Example: add another client
    # "another_client": {
    #     "label": "Another Client – ROI D7/D30",
    #     "kpi_mode": "roi",
    #     "kpi_col_d7_spec": "I",            # column letter works too
    #     "kpi_col_d2nd_spec": "J",
    #     "kpi_d7_pct": 10.0,
    #     "kpi_d2nd_pct": 8.0,
    #     "weight_main": 0.80,
    #     "weight_secondary": 0.20,
    # },
}

DEFAULT_PRESET = "domino_dreams"

# ---------------------------------------------------------------------------
# Slack app init
# ---------------------------------------------------------------------------
app = App(token=os.environ["SLACK_BOT_TOKEN"])


def download_slack_file(url: str, token: str) -> bytes:
    """Download a file from Slack's private URL."""
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
    resp.raise_for_status()
    return resp.content


def find_recent_files(client, channel_id: str, lookback: int = 30):
    """
    Scan the last `lookback` messages in the channel for file uploads.
    Returns (xlsx_url, csv_url) or raises if not found.
    """
    result = client.conversations_history(channel=channel_id, limit=lookback)
    xlsx_file = None
    csv_file = None

    for msg in result["messages"]:
        for f in msg.get("files", []):
            name = (f.get("name") or "").lower()
            if name.endswith(".xlsx") and not xlsx_file:
                xlsx_file = f.get("url_private_download") or f.get("url_private")
            elif name.endswith(".csv") and not csv_file:
                csv_file = f.get("url_private_download") or f.get("url_private")
        if xlsx_file and csv_file:
            break

    if not xlsx_file:
        raise FileNotFoundError("No .xlsx file found in recent channel messages. Upload site_performance.xlsx first.")
    if not csv_file:
        raise FileNotFoundError("No .csv file found in recent channel messages. Upload the DT_DX CSV first.")
    return xlsx_file, csv_file


def format_summary(summary: dict, preset_label: str) -> str:
    """Build a Slack-friendly summary block."""
    seg = summary.get("segment_breakdown", {})
    actions = summary.get("action_breakdown", {})

    seg_line = "  ".join(f":{k}_circle: {k.title()}: {v}" for k, v in seg.items())
    action_lines = "\n".join(f"  • {k}: {v}" for k, v in actions.items()) or "  (none)"

    return (
        f"*Optimization complete* — _{preset_label}_\n"
        f"```\n"
        f"Total rows:      {summary['total_rows']}\n"
        f"Rows actioned:   {summary['rows_actioned']}\n"
        f"Rows disregarded:{summary['rows_disregarded']}\n"
        f"Daily cap flags: {summary['rows_with_cap']}\n"
        f"KPI mode:        {summary['kpi_mode'].upper()}\n"
        f"D7 target:       {summary['kpi_d7_target']:.4f}\n"
        f"D7 column:       {summary['kpi_d7_col']}\n"
        f"```\n"
        f"*Segments:* {seg_line}\n"
        f"*Actions:*\n{action_lines}"
    )


# ---------------------------------------------------------------------------
# /optimize handler
# ---------------------------------------------------------------------------
@app.command("/optimize")
def handle_optimize(ack, command, client, respond):
    ack("Running optimization… :hourglass_flowing_sand:")

    channel_id = command["channel_id"]
    preset_key = (command.get("text") or "").strip().lower() or DEFAULT_PRESET
    token = os.environ["SLACK_BOT_TOKEN"]

    # Resolve preset
    if preset_key not in PRESETS:
        available = ", ".join(f"`{k}`" for k in PRESETS)
        respond(f"Unknown preset `{preset_key}`. Available: {available}")
        return

    preset = PRESETS[preset_key]

    xlsx_path = None
    csv_path = None
    try:
        # 1. Find files in channel history
        xlsx_url, csv_url = find_recent_files(client, channel_id)

        # 2. Download files
        xlsx_bytes = download_slack_file(xlsx_url, token)
        csv_bytes = download_slack_file(csv_url, token)

        # 3. Write to temp files (optimizer expects file paths)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f_xlsx:
            f_xlsx.write(xlsx_bytes)
            xlsx_path = f_xlsx.name
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f_csv:
            f_csv.write(csv_bytes)
            csv_path = f_csv.name

        # 4. Import and run optimizer
        from optimizer import run_optimization

        # Resolve secondary column: fall back to D7 if not set
        d2nd_spec = preset["kpi_col_d2nd_spec"] or preset["kpi_col_d7_spec"]
        d2nd_pct = preset["kpi_d2nd_pct"] if preset["kpi_d2nd_pct"] is not None else preset["kpi_d7_pct"]

        buf, summary = run_optimization(
            internal_file=xlsx_path,
            advertiser_file=csv_path,
            kpi_col_d7_spec=preset["kpi_col_d7_spec"],
            kpi_col_d2nd_spec=d2nd_spec,
            kpi_d7_pct=preset["kpi_d7_pct"],
            kpi_d2nd_pct=d2nd_pct,
            weight_main=preset["weight_main"],
            weight_secondary=preset["weight_secondary"],
            kpi_mode=preset["kpi_mode"],
        )

        # 5. Upload output Excel to Slack
        client.files_upload_v2(
            channel=channel_id,
            file=buf.getvalue(),
            filename="optimization_output.xlsx",
            title="Campaign Optimization Output",
            initial_comment=format_summary(summary, preset["label"]),
        )

    except FileNotFoundError as e:
        respond(f":warning: {e}")
    except Exception as e:
        tb = traceback.format_exc()
        respond(f":x: Optimization failed:\n```{tb[-1500:]}```")
    finally:
        for p in [xlsx_path, csv_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Bot starting… listening for /optimize commands")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()

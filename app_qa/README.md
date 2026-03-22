# App QA Agent

**Sources:**

- **QA-Agent:** `apk_fetcher.py`, `check_app_legal.py`, `play_integrity_analyzer.py`, `wake_lock_analyzer.py` (not `app.py` — that is Streamlit-only in `dt-ops-streamlit`).
- **AI-Stuff / Wake Lock / QA Bot:** `qa_bot.py`, `report_formatter.py` (Slack bot). Scripts under `QA Bot/scripts/` matched QA-Agent copies; **QA-Agent** versions were used as canonical.

## Commands

```bash
python main.py /path/to/app.apk --json
python qa_bot.py    # Slack — set SLACK_* tokens in .env
```

Streamlit screener → **`dt-ops-streamlit/app_qa/app.py`**.

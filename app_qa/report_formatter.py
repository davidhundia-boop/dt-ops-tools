#!/usr/bin/env python3
"""
Slack Block Kit report formatter for the QA Bot.

Converts output from all 3 analysis scripts into a single structured Slack message:
  - Tier 1: colour-coded summary (3 rows + overall verdict)
  - Tier 2: full details per check (wake lock, play integrity, legal/privacy)
"""

from datetime import datetime


# ── Helpers ────────────────────────────────────────────────────────────────────

def _trunc(text: str, max_len: int = 400) -> str:
    text = str(text) if text else ""
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _trunc(text, 3000)}}


def _divider() -> dict:
    return {"type": "divider"}


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _trunc(text, 3000)}]}


# ── Verdict helpers ────────────────────────────────────────────────────────────

def _wl_verdict(wl: dict) -> tuple[str, str]:
    """Returns (circle_emoji, status_text) for Wake Lock."""
    if wl.get("error"):
        return ":warning:", f"ERROR — {wl['error'][:80]}"
    if wl.get("wake_lock_detected"):
        conf = wl.get("confidence", "unknown")
        tiers = [f["tier"] for f in wl.get("flag_reasons", []) if "tier" in f]
        tier_str = f"Tier {min(tiers)}" if tiers else ""
        label = f"{tier_str} — {conf} confidence" if tier_str else f"{conf} confidence"
        circle = ":red_circle:" if conf == "high" else ":large_yellow_circle:"
        return circle, f"FLAGGED ({label})"
    if wl.get("needs_manual_review") == "Yes":
        return ":large_yellow_circle:", "NEEDS MANUAL REVIEW"
    return ":large_green_circle:", "NOT FLAGGED"


def _pi_verdict(pi: dict) -> tuple[str, str]:
    """Returns (circle_emoji, status_text) for Play Integrity."""
    if pi.get("error"):
        return ":warning:", f"ERROR — {pi['error'][:80]}"
    v = pi.get("verdict", "UNKNOWN")
    mapping = {
        "FAIL":         (":red_circle:",          "FAIL"),
        "WARNING":      (":large_yellow_circle:", "WARNING"),
        "PASS":         (":large_green_circle:",  "PASS"),
        "INCONCLUSIVE": (":warning:",             "INCONCLUSIVE"),
    }
    return mapping.get(v, (":warning:", v))


def _legal_verdict(legal: dict) -> tuple[str, str]:
    """Returns (circle_emoji, status_text) for Legal/Privacy."""
    if legal.get("error"):
        return ":warning:", f"ERROR — {legal['error'][:80]}"
    c = legal.get("confidence", "FAIL")
    mapping = {
        "PASS":    (":large_green_circle:",  "PASS"),
        "WARNING": (":large_yellow_circle:", "WARNING"),
        "FAIL":    (":red_circle:",          "FAIL"),
    }
    return mapping.get(c, (":warning:", c))


_SEVERITY = {":red_circle:": 2, ":large_yellow_circle:": 1, ":warning:": 1, ":large_green_circle:": 0}


def _overall(wl: dict, pi: dict, legal: dict) -> str:
    """Derive overall verdict text from the three individual check results."""
    worst = max(
        _SEVERITY.get(_wl_verdict(wl)[0], 0),
        _SEVERITY.get(_pi_verdict(pi)[0], 0),
        _SEVERITY.get(_legal_verdict(legal)[0], 0),
    )
    if worst >= 2:
        return ":x: *Overall: ACTION REQUIRED*"
    if worst == 1:
        return ":warning: *Overall: REVIEW NEEDED*"
    return ":white_check_mark: *Overall: PASS*"


# ── Detail section builders ────────────────────────────────────────────────────

def _wake_lock_details(wl: dict) -> list[dict]:
    blocks = [_section("*:lock: Wake Lock — Full Details*")]

    if wl.get("error"):
        blocks.append(_section(f":x: Error running wake lock analysis:\n>{wl['error']}"))
        return blocks

    circle, status = _wl_verdict(wl)
    conf = wl.get("confidence", "n/a")
    scanned = wl.get("classes_scanned", "?")
    total = wl.get("total_classes_in_apk", "?")
    elapsed = wl.get("time_taken_seconds", "?")

    meta = (
        f"Result: {circle} *{status}*\n"
        f"Confidence: `{conf}`   |   Classes scanned: `{scanned} / {total}`   |   Time: `{elapsed}s`"
    )
    blocks.append(_section(meta))

    for fr in wl.get("flag_reasons", []):
        tier = fr.get("tier", "?")
        vector = fr.get("vector", "?")
        cls = fr.get("found_in_class", "?")
        method = fr.get("found_in_method", "?")
        evidence = _trunc(fr.get("evidence", ""), 350)
        note = fr.get("note")

        detail = (
            f">*Vector:* `{vector}`   _(Tier {tier})_\n"
            f">*Class:* `{cls}`\n"
            f">*Method:* `{method}`\n"
            f">*Evidence:* {evidence}"
        )
        if note:
            detail += f"\n>:information_source: _{_trunc(note, 200)}_"
        blocks.append(_section(detail))

    if wl.get("needs_manual_review") == "Yes" and wl.get("manual_review_instructions"):
        blocks.append(_section(
            f":clipboard: *Manual Review Required:*\n>{_trunc(wl['manual_review_instructions'], 400)}"
        ))

    for dr in wl.get("doubt_reasons", []):
        blocks.append(_context(f":information_source: {_trunc(dr, 250)}"))

    if not wl.get("wake_lock_detected") and not wl.get("error"):
        comment = wl.get("comment", "No wake-lock patterns detected.")
        blocks.append(_context(_trunc(comment, 300)))

    return blocks


def _play_integrity_details(pi: dict) -> list[dict]:
    blocks = [_section("*:shield: Play Integrity — Full Details*")]

    if pi.get("error"):
        blocks.append(_section(f":x: Error running Play Integrity analysis:\n>{pi['error']}"))
        return blocks

    circle, status = _pi_verdict(pi)
    verdict = pi.get("verdict", "UNKNOWN")
    fail_count = pi.get("fail_count", 0)
    warn_count = pi.get("warning_count", 0)
    app_name = pi.get("app_name") or pi.get("package", "?")

    meta = (
        f"Verdict: {circle} *{verdict}*\n"
        f"App: `{app_name}`   |   Failures: `{fail_count}`   |   Warnings: `{warn_count}`"
    )
    blocks.append(_section(meta))

    details = pi.get("details", {})

    for item in details.get("fail", []):
        name = item.get("name") or item.get("id", "Unknown")
        desc = _trunc(item.get("description", ""), 300)
        msg = _trunc(item.get("message", ""), 200)
        evidence = item.get("evidence", [])

        detail = f":x: *FAIL — {name}*\n"
        if desc:
            detail += f">{desc}\n"
        if msg:
            detail += f">{msg}\n"
        if evidence:
            ev_lines = "\n".join(f">• `{_trunc(str(e), 120)}`" for e in evidence[:5])
            detail += ev_lines
            if len(evidence) > 5:
                detail += f"\n>_...and {len(evidence) - 5} more matches_"
        blocks.append(_section(detail.strip()))

    for item in details.get("warning", []):
        name = item.get("name") or item.get("id", "Unknown")
        desc = _trunc(item.get("description", ""), 300)
        msg = _trunc(item.get("message", ""), 200)

        detail = f":warning: *WARNING — {name}*\n"
        if desc:
            detail += f">{desc}\n"
        if msg:
            detail += f">{msg}"
        blocks.append(_section(detail.strip()))

    if verdict == "PASS":
        blocks.append(_context(
            ":white_check_mark: No Play Integrity issues detected. "
            "App should install cleanly via Digital Turbine."
        ))

    if verdict == "INCONCLUSIVE":
        blocks.append(_context(
            ":warning: DEX extraction failed or no strings found. "
            "Manual APK inspection is recommended."
        ))

    return blocks


def _legal_details(legal: dict) -> list[dict]:
    blocks = [_section("*:scroll: Privacy & Legal — Full Details*")]

    if legal.get("error"):
        blocks.append(_section(f":x: Error running legal analysis:\n>{legal['error']}"))
        return blocks

    circle, status = _legal_verdict(legal)
    conf = legal.get("confidence", "?")
    app_name = legal.get("app_name") or legal.get("package_name", "?")
    developer = legal.get("developer", "")

    meta_parts = [f"Rating: {circle} *{conf}*", f"App: `{app_name}`"]
    if developer:
        meta_parts.append(f"Developer: `{developer}`")
    blocks.append(_section("   |   ".join(meta_parts)))

    # Privacy Policy
    pp_verdict = legal.get("privacy_policy_verdict", "NOT FOUND")
    pp_url = legal.get("privacy_policy_url")
    if pp_verdict.startswith("FOUND"):
        pp_line = ":white_check_mark: *Privacy Policy:* FOUND"
        if pp_url:
            pp_line += f"\n><{pp_url}|{pp_url}>"
    else:
        pp_line = ":x: *Privacy Policy:* NOT FOUND"
    blocks.append(_section(pp_line))

    # Terms & Conditions
    tc_verdict = legal.get("tc_verdict", "NOT FOUND")
    tc_links = legal.get("tc_links", [])
    if tc_verdict.startswith("FOUND") and tc_links:
        tc_line = ":white_check_mark: *Terms & Conditions:* FOUND"
        for lnk in tc_links[:2]:
            tc_line += f"\n><{lnk['url']}|{lnk.get('text', lnk['url'])}>"
    else:
        tc_line = ":x: *Terms & Conditions:* NOT FOUND"
    blocks.append(_section(tc_line))

    # Data Safety
    ds = legal.get("data_safety")
    if ds:
        ds_status_map = {
            "COMPLETE":    ":white_check_mark: COMPLETE",
            "NO_DATA":     ":large_yellow_circle: NO DATA DECLARED",
            "MISSING":     ":x: MISSING",
            "PARSE_ERROR": ":warning: PARSE ERROR",
        }
        ds_line = f"*Data Safety:* {ds_status_map.get(ds.get('status', ''), ':warning: UNKNOWN')}"

        collected = ds.get("collected", [])
        shared = ds.get("shared", [])
        security = ds.get("security_practices", [])

        if collected:
            items = []
            for c in collected[:5]:
                if c.get("data_types"):
                    items.append(f"{c['category']} ({', '.join(c['data_types'][:3])})")
                else:
                    items.append(c["category"])
            ds_line += f"\n>*Collects:* {', '.join(items)}"
            if len(collected) > 5:
                ds_line += f" _+{len(collected) - 5} more_"
        elif ds.get("no_data_collected"):
            ds_line += "\n>*Collects:* _Nothing declared_"

        if shared:
            items = []
            for c in shared[:4]:
                if c.get("data_types"):
                    items.append(f"{c['category']} ({', '.join(c['data_types'][:2])})")
                else:
                    items.append(c["category"])
            ds_line += f"\n>*Shares:* {', '.join(items)}"
            if len(shared) > 4:
                ds_line += f" _+{len(shared) - 4} more_"
        elif ds.get("no_data_shared"):
            ds_line += "\n>*Shares:* _Nothing declared_"

        if security:
            ds_line += f"\n>*Security:* {' · '.join(security[:3])}"
            if len(security) > 3:
                ds_line += f" _+{len(security) - 3} more_"

        blocks.append(_section(ds_line))

        # Plausibility suspect warning
        if ds.get("plausibility") == "SUSPECT":
            perms = ", ".join(f"`{p}`" for p in ds.get("suspect_permissions", [])[:5])
            blocks.append(_section(
                f":warning: *Plausibility SUSPECT* — app declares no data collected "
                f"but requests sensitive permissions: {perms}"
            ))

    # Developer contact info
    dev_website = legal.get("developer_website")
    dev_email = legal.get("developer_email")
    if dev_website or dev_email:
        parts = []
        if dev_website:
            parts.append(f"<{dev_website}|{dev_website}>")
        if dev_email:
            parts.append(dev_email)
        blocks.append(_context(f":globe_with_meridians: Developer contact: {' | '.join(parts)}"))

    # Notes
    notes = legal.get("notes", [])
    if notes:
        note_lines = "\n".join(f"• {_trunc(n, 150)}" for n in notes[:6])
        blocks.append(_section(f":notepad_spiral: *Notes:*\n{note_lines}"))

    return blocks


# ── Main Builder ───────────────────────────────────────────────────────────────

def build_report_blocks(wl: dict, pi: dict, legal: dict, filename: str) -> list[dict]:
    """
    Build the complete Slack Block Kit payload for the QA report.

    Structure:
      1. Header + metadata
      2. Colour-coded summary (3 rows + overall verdict)
      3. Wake Lock full details
      4. Play Integrity full details
      5. Legal / Privacy full details
    """
    # Derive app identity from whichever script succeeded first
    app_name = (
        (wl.get("apk_name") or "").replace(".apk", "").replace("_", " ").strip()
        or pi.get("app_name")
        or legal.get("app_name")
        or filename.replace(".apk", "").replace("_", " ").strip()
        or "Unknown App"
    )
    package = (
        wl.get("package")
        or pi.get("package")
        or legal.get("package_name")
        or "unknown.package"
    )
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    blocks: list[dict] = []

    # ── Header ──
    blocks.append(_header(f"QA Report — {app_name}"))
    blocks.append(_context(f"`{package}`  |  _{filename}_  |  {timestamp}"))
    blocks.append(_divider())

    # ── Tier 1: Summary ──
    wl_circle,    wl_status    = _wl_verdict(wl)
    pi_circle,    pi_status    = _pi_verdict(pi)
    legal_circle, legal_status = _legal_verdict(legal)

    summary = (
        f"{wl_circle}  *Wake Lock*              {wl_status}\n"
        f"{pi_circle}  *Play Integrity*         {pi_status}\n"
        f"{legal_circle}  *Privacy & Legal*     {legal_status}"
    )
    blocks.append(_section(summary))
    blocks.append(_divider())
    blocks.append(_section(_overall(wl, pi, legal)))
    blocks.append(_divider())

    # ── Tier 2: Full details per check ──
    blocks.extend(_wake_lock_details(wl))
    blocks.append(_divider())
    blocks.extend(_play_integrity_details(pi))
    blocks.append(_divider())
    blocks.extend(_legal_details(legal))

    # Slack hard limit: 50 blocks per message
    if len(blocks) > 50:
        blocks = blocks[:49]
        blocks.append(_context(
            "_Report truncated — too many findings to display in a single message. "
            "Check the bot logs for the complete output._"
        ))

    return blocks

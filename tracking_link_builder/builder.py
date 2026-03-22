#!/usr/bin/env python3
"""
Tracking Link Builder
Applies test values (click ID, device ID) to a raw tracking link
based on integration type and parameter conventions.
"""

import re
import hashlib
import argparse
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote

# ── Constants ─────────────────────────────────────────────────────────────────

ODT_PID = "onedigitalturbine_int"
DEFAULT_CLICK_ID = "David1"

# Plain (non-SHA1) advertising ID param names to recognise
PLAIN_AD_ID_PARAMS = {
    "advertising_id", "android_id", "device_id",
    "idfa", "gaid", "aaid", "af_idfa", "af_android_id",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

UUID_RE  = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
SHA1_RE  = re.compile(r'^[0-9a-f]{40}$', re.I)


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value.strip()))


def is_sha1_hash(value: str) -> bool:
    return bool(SHA1_RE.match(value.strip()))


def sha1_hash(value: str) -> str:
    """Return lowercase hex SHA-1 digest of value (lowercased before hashing)."""
    return hashlib.sha1(value.strip().lower().encode()).hexdigest()


def find_click_id_key(params: list[tuple[str, str]]) -> str | None:
    """
    Return the param name that represents the click ID.
    Matches 'clickid' and 'click_id' (case-insensitive).
    """
    for key, _ in params:
        if key.lower().replace("_", "") == "clickid":
            return key
    return None


def resolve_device_id(device_id: str, sha1_required: bool) -> tuple[str, str | None]:
    """
    Return (resolved_device_id, info_message).
    If sha1_required:
      - already a SHA1 hash  → use as-is
      - UUID                 → hash it, return info message
      - anything else        → raise ValueError
    If not sha1_required:
      - use raw value as-is (warn if format looks unexpected)
    """
    device_id = device_id.strip()
    info = None

    if sha1_required:
        if is_sha1_hash(device_id):
            return device_id.lower(), info
        elif is_uuid(device_id):
            hashed = sha1_hash(device_id)
            info = f"Device ID auto-hashed (SHA-1): {hashed}"
            return hashed, info
        else:
            raise ValueError(
                "The tracking link requires a SHA-1 hashed Device ID, but the value "
                "provided doesn't look like a UUID or a 40-char hex hash.\n"
                "  UUID example:  278d8c12-bdfc-4843-a4cd-043631edab0a\n"
                "  SHA-1 example: e9b0c0da16e7daca61515124da91f9f9b9ed2b80"
            )
    else:
        if not is_uuid(device_id) and not is_sha1_hash(device_id):
            info = "WARNING: Device ID format looks unexpected — verify the output."
        return device_id, info

# ── Core builder ──────────────────────────────────────────────────────────────

def build_link(
    raw_link: str,
    device_id: str,
    click_id_val: str = DEFAULT_CLICK_ID,
) -> dict:
    """
    Process a raw tracking link and return a result dict:
      {
        "output_url":        str,
        "integration_type":  "Legacy" | "OneDigitalTurbine",
        "pid":               str,
        "sha1_required":     bool,
        "changes":           list[dict],   # {param, old, new, desc}
        "messages":          list[str],    # info / warnings
      }
    """
    parsed = urlparse(raw_link.strip())
    params = parse_qsl(parsed.query, keep_blank_values=True)

    pid = next((v for k, v in params if k == "pid"), "")
    is_odt = pid == ODT_PID
    integration_type = "OneDigitalTurbine" if is_odt else "Legacy"

    sha1_keys = [k for k, _ in params if "sha1" in k.lower()]
    sha1_required = len(sha1_keys) > 0

    messages: list[str] = []
    changes:  list[dict] = []

    resolved_id, id_msg = resolve_device_id(device_id, sha1_required)
    if id_msg:
        messages.append(id_msg)

    click_key = find_click_id_key(params)

    # Both Legacy and ODT share the same substitution rules for now
    new_params = []
    for key, value in params:
        new_value = value

        # Replace click ID
        if click_key and key == click_key:
            new_value = click_id_val
            changes.append({"param": key, "old": value, "new": new_value, "desc": "Test click ID"})

        # Replace SHA-1 advertising ID params
        elif "sha1" in key.lower():
            new_value = resolved_id
            changes.append({"param": key, "old": value, "new": new_value, "desc": "Hashed Device ID (SHA-1)"})

        # Replace plain advertising ID params (only when no sha1 params exist)
        elif not sha1_required and key.lower() in PLAIN_AD_ID_PARAMS:
            new_value = resolved_id
            changes.append({"param": key, "old": value, "new": new_value, "desc": "Raw Device ID"})

        new_params.append((key, new_value))

    # Preserve [ ] characters in placeholder values (e.g. [CAMPAIGN_ID])
    output_url = urlunparse(parsed._replace(
        query=urlencode(new_params, quote_via=lambda s, safe, enc, err: quote(s, safe="[]"))
    ))

    return {
        "output_url":       output_url,
        "integration_type": integration_type,
        "pid":              pid,
        "sha1_required":    sha1_required,
        "changes":          changes,
        "messages":         messages,
    }

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build a test-ready tracking link by injecting click ID and device ID.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python builder.py \\
    --link "https://app.appsflyer.com/com.example?pid=appia_int&clickid=[ClickID]&sha1_advertising_id=[AAID_SHA1]" \\
    --device-id "278d8c12-bdfc-4843-a4cd-043631edab0a"

  python builder.py \\
    --link "https://app.appsflyer.com/..." \\
    --device-id "e9b0c0da16e7daca61515124da91f9f9b9ed2b80" \\
    --click-id "TestClick99"
        """,
    )
    parser.add_argument("--link",      required=True, help="Raw tracking link")
    parser.add_argument("--device-id", required=True, help="Your GAID/AAID (UUID or SHA-1 hash)")
    parser.add_argument("--click-id",  default=DEFAULT_CLICK_ID, help=f"Test click ID value (default: {DEFAULT_CLICK_ID})")

    args = parser.parse_args()

    try:
        result = build_link(
            raw_link=args.link,
            device_id=args.device_id,
            click_id_val=args.click_id,
        )
    except ValueError as e:
        print(f"\n[ERROR] {e}\n")
        raise SystemExit(1)

    # ── Print summary ──────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print(f"  Integration : {result['integration_type']}")
    print(f"  PID         : {result['pid'] or '(none)'}")
    print(f"  SHA-1 mode  : {'yes' if result['sha1_required'] else 'no'}")
    print("=" * 70)

    for msg in result["messages"]:
        prefix = "[WARN]" if msg.startswith("WARNING") else "[INFO]"
        print(f"  {prefix} {msg}")

    if result["changes"]:
        print()
        print("  Changes applied:")
        for c in result["changes"]:
            print(f"    [+] {c['param']}  ->  {c['new']}   ({c['desc']})")
    else:
        print()
        print("  No parameters were modified.")

    print()
    print("  Output URL:")
    print(f"  {result['output_url']}")
    print()


if __name__ == "__main__":
    main()

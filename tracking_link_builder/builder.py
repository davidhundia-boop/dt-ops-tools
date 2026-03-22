#!/usr/bin/env python3
"""
DT Test Link Builder v2
========================
Takes a raw tracking URL + device ID → produces a ready-to-fire test link.

Key behaviors:
  1. Detect MMP from the host
  2. Detect Unified integration (id2 present → hardcode id2=dV9XX0xY)
  3. Replace click ID using MMP-specific param name
  4. Replace device ID, auto-hashing to SHA1 when the param requires it
  5. Replace [ClickID] placeholders embedded inside other param values (e.g. Adjust callbacks)
"""

from __future__ import annotations

import argparse
import hashlib
import re
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

# ── Constants ─────────────────────────────────────────────────────────────────

UNIFIED_ID2_VALUE = "dV9XX0xY"
DEFAULT_CLICK_ID = "David1"

# ── MMP detection ─────────────────────────────────────────────────────────────
# Checked in order; first match wins.

MMP_HOST_RULES = [
    ("appsflyer.com", "appsflyer"),
    ("adjust.com", "adjust"),
    ("sng.link", "singular"),
    ("kochava.com", "kochava"),
    (".app.link", "branch"),  # Branch universal links
]


def detect_mmp(host: str) -> str:
    """Return MMP slug or 'unknown'."""
    host = host.lower()
    for pattern, mmp in MMP_HOST_RULES:
        if pattern in host:
            return mmp
    return "unknown"


# ── Per-MMP configuration ────────────────────────────────────────────────────
# Each MMP declares:
#   click_id_params   – param names that carry the click ID
#   device_id_plain   – param names for raw (unhashed) device ID
#   device_id_hashed  – param names for SHA1-hashed device ID
#
# Order matters: the first matching param in each list is used.

MMP_CONFIG = {
    "appsflyer": {
        "click_id_params": ["clickid"],
        "device_id_plain": ["advertising_id"],
        "device_id_hashed": ["sha1_advertising_id"],
    },
    "adjust": {
        "click_id_params": ["digital_turbine_referrer"],
        "device_id_plain": ["gps_adid"],
        "device_id_hashed": ["gps_adid_lower_sha1"],
    },
    "singular": {
        "click_id_params": ["cl"],
        "device_id_plain": ["aifa"],
        "device_id_hashed": ["aif1"],  # ← no "sha1" in name
    },
    "kochava": {
        "click_id_params": ["click_id"],
        "device_id_plain": ["device_id"],  # when device_id_type=adid
        "device_id_hashed": [],  # same param, value changes
    },
    "branch": {
        "click_id_params": ["~click_id"],
        "device_id_plain": ["$aaid"],  # URL-encoded as %24aaid
        "device_id_hashed": [],
    },
}

# Fallback for unknown MMPs — broad matching like the original script
MMP_CONFIG["unknown"] = {
    "click_id_params": ["clickid", "click_id"],
    "device_id_plain": [
        "advertising_id",
        "device_id",
        "aaid",
        "gaid",
        "idfa",
        "af_idfa",
        "af_android_id",
    ],
    "device_id_hashed": ["sha1_advertising_id", "gps_adid_lower_sha1", "aif1"],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
SHA1_RE = re.compile(r"^[0-9a-f]{40}$", re.I)

# Placeholders in param values that should be replaced with the click ID
CLICK_PLACEHOLDERS = ("[ClickID]", "[CLICK_ID]", "{click_id}", "{ClickID}")


def is_uuid(v: str) -> bool:
    return bool(UUID_RE.match(v.strip()))


def is_sha1(v: str) -> bool:
    return bool(SHA1_RE.match(v.strip()))


def sha1_hash(v: str) -> str:
    """SHA-1 hex digest (input lowercased before hashing)."""
    return hashlib.sha1(v.strip().lower().encode()).hexdigest()


def resolve_device_id(raw: str, need_hash: bool) -> tuple[str, str | None]:
    """
    Returns (resolved_value, optional_info_message).
    Raises ValueError if the input can't satisfy the hash requirement.
    """
    raw = raw.strip()
    if need_hash:
        if is_sha1(raw):
            return raw.lower(), None
        if is_uuid(raw):
            h = sha1_hash(raw)
            return h, f"Device ID auto-hashed (SHA-1): {h}"
        raise ValueError(
            f"Link requires SHA-1 hashed device ID but got: {raw}\n"
            "  Provide a UUID (e.g. 278d8c12-bdfc-4843-a4cd-043631edab0a)\n"
            "  or a 40-char hex SHA-1 hash."
        )
    msg = None
    if not is_uuid(raw) and not is_sha1(raw):
        msg = "WARNING: Device ID format looks unexpected - verify the output."
    return raw, msg


def has_param_name(params: list[tuple[str, str]], name: str) -> bool:
    """True if any query key matches `name` case-insensitively."""
    n = name.lower()
    return any(k.lower() == n for k, _ in params)


def substitute_embedded_click_ids(value: str, click_id_val: str) -> tuple[str, bool]:
    """Replace all known click-ID placeholders in a param value. Returns (new_value, changed)."""
    new_value = value
    for ph in CLICK_PLACEHOLDERS:
        if ph in new_value:
            new_value = new_value.replace(ph, click_id_val)
    return new_value, new_value != value


# ── Core builder ──────────────────────────────────────────────────────────────


def build_link(
    raw_link: str,
    device_id: str,
    click_id_val: str = DEFAULT_CLICK_ID,
) -> dict:
    """
    Returns:
      {
        "output_url":        str,
        "mmp":               str,
        "is_unified":        bool,
        "sha1_required":     bool,
        "changes":           list[dict],   # {param, old, new, desc}
        "messages":          list[str],
      }
    """
    parsed = urlparse(raw_link.strip())
    params = parse_qsl(parsed.query, keep_blank_values=True)

    messages: list[str] = []
    changes: list[dict] = []

    # ── 1. Detect MMP ────────────────────────────────────────────────────
    mmp = detect_mmp(parsed.hostname or "")
    conf = MMP_CONFIG[mmp]
    messages.append(f"MMP detected: {mmp}")

    # ── 2. Detect Unified (id2 present?) ─────────────────────────────────
    is_unified = has_param_name(params, "id2")
    if is_unified:
        messages.append("Unified integration detected (id2 found) -> will hardcode id2")

    # ── 3. Determine hashing requirement ─────────────────────────────────
    conf_hashed_lower = {p.lower() for p in conf["device_id_hashed"]}
    hashed_params_in_url: set[str] = set()
    for k, _ in params:
        kl = k.lower()
        if kl in conf_hashed_lower:
            hashed_params_in_url.add(k)
        if "sha1" in kl:
            hashed_params_in_url.add(k)

    sha1_required = len(hashed_params_in_url) > 0
    resolved_id, id_msg = resolve_device_id(device_id, sha1_required)
    if id_msg:
        messages.append(id_msg)

    # ── 4. Build sets for fast matching ──────────────────────────────────
    click_params = {p.lower() for p in conf["click_id_params"]}
    plain_params = {p.lower() for p in conf["device_id_plain"]}
    hashed_params = {p.lower() for p in conf["device_id_hashed"]}

    # ── 5. Walk params and apply substitutions ───────────────────────────
    new_params: list[tuple[str, str]] = []
    for key, value in params:
        kl = key.lower()
        new_value = value

        # 5a. id2 → hardcode for Unified
        if kl == "id2" and is_unified:
            new_value = UNIFIED_ID2_VALUE
            changes.append(
                {
                    "param": key,
                    "old": value,
                    "new": new_value,
                    "desc": "Unified id2 hardcoded",
                }
            )

        # 5b. Click ID param
        elif kl in click_params:
            new_value = click_id_val
            changes.append(
                {
                    "param": key,
                    "old": value,
                    "new": new_value,
                    "desc": "Test click ID",
                }
            )

        # 5c. Hashed device ID param
        elif kl in hashed_params or key in hashed_params_in_url:
            new_value = resolved_id
            changes.append(
                {
                    "param": key,
                    "old": value,
                    "new": new_value,
                    "desc": "Hashed device ID (SHA-1)",
                }
            )

        # 5d. Plain device ID param (only when link doesn't expect hash)
        elif not sha1_required and kl in plain_params:
            new_value = resolved_id
            changes.append(
                {
                    "param": key,
                    "old": value,
                    "new": new_value,
                    "desc": "Raw device ID",
                }
            )

        # 5e. Replace [ClickID] placeholders embedded in other values
        else:
            embedded, did_change = substitute_embedded_click_ids(new_value, click_id_val)
            if did_change:
                new_value = embedded
                changes.append(
                    {
                        "param": key,
                        "old": value,
                        "new": new_value,
                        "desc": "Embedded click ID placeholder(s) replaced",
                    }
                )

        new_params.append((key, new_value))

    # ── 6. Reassemble URL ────────────────────────────────────────────────
    output_url = urlunparse(
        parsed._replace(
            query=urlencode(
                new_params,
                quote_via=lambda s, safe, enc, err: quote(s, safe="[]{}"),
            )
        )
    )

    return {
        "output_url": output_url,
        "mmp": mmp,
        "is_unified": is_unified,
        "sha1_required": sha1_required,
        "changes": changes,
        "messages": messages,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="DT Test Link Builder — inject click ID + device ID into a raw tracking URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # AppsFlyer Unified (SHA-1, auto-hashed)
  python builder.py \\
    --link "https://app.appsflyer.com/com.example?pid=onedigitalturbine_int&id2=[CHANNEL]&sha1_advertising_id=[AAID_SHA1]&clickid=[ClickID]" \\
    --device-id "65a53a0f-87a1-43aa-9df8-da3ed7f6c954"

  # Kochava Unified (plain device ID)
  python builder.py \\
    --link "https://control.kochava.com/v1/cpi/click?network_id=11693&device_id=[AAID]&id2=[CHANNEL]&click_id=[ClickID]" \\
    --device-id "72d9cbf8-106d-4545-87e8-149c6359bbf7" \\
    --click-id "OOtest0113"

  # Singular Unified (aif1 = SHA-1 hashed)
  python builder.py \\
    --link "https://vybs.sng.link/D7hng/n1o6?cl=[ClickID]&id2=[CHANNEL]&aif1=[AAID_SHA1]" \\
    --device-id "5816f1b1-2544-4b57-86bb-9bc1dd4c1f85"
        """,
    )
    ap.add_argument("--link", required=True, help="Raw tracking link")
    ap.add_argument("--device-id", required=True, help="GAID/AAID (UUID or SHA-1)")
    ap.add_argument(
        "--click-id",
        default=DEFAULT_CLICK_ID,
        help=f"Test click ID (default: {DEFAULT_CLICK_ID})",
    )

    args = ap.parse_args()

    try:
        result = build_link(args.link, args.device_id, args.click_id)
    except ValueError as e:
        print(f"\n[ERROR] {e}\n")
        raise SystemExit(1)

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"  MMP         : {result['mmp']}")
    print(
        f"  Unified     : {'YES -> id2 hardcoded' if result['is_unified'] else 'no'}"
    )
    print(f"  SHA-1 mode  : {'yes' if result['sha1_required'] else 'no'}")
    print("=" * 72)

    for msg in result["messages"]:
        tag = "[WARN]" if "WARNING" in msg else "[INFO]"
        print(f"  {tag} {msg}")

    if result["changes"]:
        print()
        print("  Changes applied:")
        for c in result["changes"]:
            print(f"    - {c['param']:30s} -> {c['new']}")
            print(f"      {c['desc']}")
    else:
        print("\n  [!] No parameters were modified - check the link format.")

    print(f"\n  Output URL:\n  {result['output_url']}\n")


if __name__ == "__main__":
    main()

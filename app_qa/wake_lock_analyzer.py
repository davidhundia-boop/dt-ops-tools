#!/usr/bin/env python3
"""
Manifest-driven APK static analyzer for screen-wake detection.

Uses the AndroidManifest.xml as the map to find relevant classes,
then walks inheritance chains and scans only those classes for
wake-lock patterns. Handles ProGuard/R8 obfuscation and filters
out third-party SDK noise by focusing on the MAIN/LAUNCHER activity.
"""

import json
import logging
import os
import re
import struct
import sys
import time

os.environ["LOGURU_LEVEL"] = "ERROR"

from androguard.core.apk import APK
from androguard.core.axml import AXMLPrinter
from androguard.core.dex import DEX

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("wake_lock_analyzer")

STOP_SUPERCLASSES = frozenset(
    {
        "Landroid/app/Activity;",
        "Landroidx/appcompat/app/AppCompatActivity;",
        "Landroidx/activity/ComponentActivity;",
        "Landroidx/fragment/app/FragmentActivity;",
        "Ljava/lang/Object;",
    }
)

# Game engine runtime packages — treated as functionally first-party.
# The game IS the engine runtime; wake locks here are session-wide.
GAME_ENGINE_PACKAGES = [
    "com.unity3d.player",       # Unity Java runtime (UnityPlayer, UnityPlayerActivity)
    "com.epicgames.ue4",        # Unreal Engine 4
    "com.unrealengine",         # Unreal Engine (alt package)
    "org.cocos2dx",             # Cocos2d-x
    "org.libgdx",               # libGDX
    "io.flutter",               # Flutter engine
    "com.google.androidgamesdk",# Google Android Game SDK
]

# Known advertising SDK packages — wake locks here are scoped to ad video playback.
# IMPORTANT: com.unity3d.ads (ad SDK) is separate from com.unity3d.player (game engine).
AD_SDK_PACKAGES = [
    "com.applovin",
    "com.fyber",
    "com.mintegral",
    "com.ironsource",
    "com.unity3d.ads",          # Unity Ads SDK — NOT the same as com.unity3d.player
    "com.google.android.gms.ads",
    "com.facebook.ads",
    "com.vungle",
    "com.chartboost",
    "com.inmobi",
    "com.startapp",
    "com.tapjoy",
    "io.bidmachine",
    "com.bytedance.sdk",
]

WINDOW_SIZE = 15

# ── 5-tier confidence system ─────────────────────────────────────────────────

TIER_INFO = {
    1: {"confidence": "high", "is_flagged": True, "needs_manual_review": False},
    2: {"confidence": "high", "is_flagged": True, "needs_manual_review": False},
    3: {"confidence": "medium", "is_flagged": True, "needs_manual_review": False},
    4: {"confidence": "low", "is_flagged": True, "needs_manual_review": True},
    5: {"confidence": "none", "is_flagged": False, "needs_manual_review": False},
}

# Maps vector method_sig → tier for Phase A (main activity chain)
_SIG_TIER_MAIN = {
    "Window;->addFlags": 1,
    "View;->setKeepScreenOn": 1,
    "PowerManager;->newWakeLock": 3,
    "MediaPlayer;->setScreenOnWhilePlaying": 4,
    "Settings$System;->put": 3,
}

# Maps vector method_sig → tier for Phase B app-owned activities
_SIG_TIER_APP = {
    "Window;->addFlags": 3,
    "View;->setKeepScreenOn": 3,
    "PowerManager;->newWakeLock": 3,
    "MediaPlayer;->setScreenOnWhilePlaying": 4,
    "Settings$System;->put": 3,
}

# Maps vector ID → tier for raw-DEX Phase A (main chain)
_VID_TIER_MAIN = {
    "addFlags": 1, "setKeepScreenOn": 1, "newWakeLock": 3,
    "setScreenOnWhilePlaying": 4, "screen_off_timeout_i": 3, "screen_off_timeout_s": 3,
}

# Maps vector ID → tier for raw-DEX Phase B app-owned
_VID_TIER_APP = {
    "addFlags": 3, "setKeepScreenOn": 3, "newWakeLock": 3,
    "setScreenOnWhilePlaying": 4, "screen_off_timeout_i": 3, "screen_off_timeout_s": 3,
}

# Maps vector method_sig → tier for Phase A-engine (game engine classes)
_SIG_TIER_ENGINE = {
    "Window;->addFlags": 2,
    "View;->setKeepScreenOn": 2,
    "PowerManager;->newWakeLock": 2,
    "MediaPlayer;->setScreenOnWhilePlaying": 4,
    "Settings$System;->put": 3,
}

# Maps vector ID → tier for raw-DEX Phase A-engine
_VID_TIER_ENGINE = {
    "addFlags": 2, "setKeepScreenOn": 2, "newWakeLock": 2,
    "setScreenOnWhilePlaying": 4, "screen_off_timeout_i": 3, "screen_off_timeout_s": 3,
}

# Tier map for known ad SDKs (all vectors → Tier 5)
_VID_TIER_ADSDK: dict[str, int] = {vid: 5 for vid in _VID_TIER_APP}


def _tier_note(vec_key: str, tier: int, cls_name: str = "") -> str | None:
    """Return the human-readable note for a given vector+tier, or None."""
    if tier == 1:
        return None
    if tier == 2:
        # cls_name is Java-format here; check against engine package prefixes
        if any(cls_name.startswith(pkg) for pkg in GAME_ENGINE_PACKAGES):
            return (
                "Game engine runtime class — functionally first-party. "
                "Unity/Unreal/etc. keep-screen-on is session-wide, not "
                "scoped to ad playback."
            )
        return None
    if tier == 5:
        return (
            "All detections are from third-party ad SDKs, not the app's own "
            "code. SDKs only keep screen on during video ad playback."
        )
    if tier == 3:
        if "addFlags" in vec_key or "setKeepScreenOn" in vec_key:
            return (
                f"Screen hold found in secondary activity {cls_name} — may "
                "only apply to specific app screens, not the main experience."
            )
        if "newWakeLock" in vec_key or "PowerManager" in vec_key:
            return (
                "Deprecated screen-level wake lock — functional but uncommon "
                "in modern apps."
            )
        if "keepScreenOn" in vec_key.lower() or "AXML" in vec_key:
            return (
                "Layout attribute detected — could belong to app or an "
                "unfiltered SDK layout. Manual verification recommended."
            )
        return None
    if tier == 4:
        if "MediaPlayer" in vec_key or "setScreenOnWhilePlaying" in vec_key:
            return (
                "Screen stays on during active media playback only. This is "
                "not a persistent screen hold — it only applies while "
                "video/audio is playing."
            )
        return None
    return None


# ── helpers ──────────────────────────────────────────────────────────────────


def to_dalvik(java_name: str) -> str:
    return "L" + java_name.replace(".", "/") + ";"


def from_dalvik(dalvik_name: str) -> str:
    return dalvik_name.lstrip("L").rstrip(";").replace("/", ".")


def is_game_engine_class(dalvik_name: str) -> bool:
    """True if the class belongs to a known game engine runtime package."""
    java_name = from_dalvik(dalvik_name)
    return any(
        java_name == pkg or java_name.startswith(pkg + ".")
        for pkg in GAME_ENGINE_PACKAGES
    )


def is_ad_sdk_class(dalvik_name: str) -> bool:
    """True if the class belongs to a known advertising SDK package.

    Uses full prefix matching to avoid conflating e.g. com.unity3d.ads
    (ad SDK) with com.unity3d.player (game engine).
    """
    java_name = from_dalvik(dalvik_name)
    return any(
        java_name == pkg or java_name.startswith(pkg + ".")
        for pkg in AD_SDK_PACKAGES
    )


# ── Low-level DEX format utilities ───────────────────────────────────────────

UP = struct.unpack_from


def _read_uleb128(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def _dex_read_string(blob: bytes, sids_off: int, idx: int, cache: dict) -> str:
    cached = cache.get(idx)
    if cached is not None:
        return cached
    off = UP("<I", blob, sids_off + idx * 4)[0]
    _, pos = _read_uleb128(blob, off)
    end = blob.index(0, pos)
    s = blob[pos:end].decode("utf-8", errors="replace")
    cache[idx] = s
    return s


def _dex_read_type(blob: bytes, tids_off: int, tids_size: int, sids_off: int, idx: int, cache: dict) -> str | None:
    if idx >= tids_size or idx == 0xFFFFFFFF:
        return None
    desc_idx = UP("<I", blob, tids_off + idx * 4)[0]
    return _dex_read_string(blob, sids_off, desc_idx, cache)


# Dalvik instruction sizes in 16-bit code units, indexed by opcode 0x00..0xFF.
_INSN_SIZE = bytes(
    [
        1, 1, 2, 3, 1, 2, 3, 1, 2, 3, 1, 1, 1, 1, 1, 1,  # 0x00
        1, 1, 1, 2, 3, 2, 2, 3, 5, 2, 2, 3, 2, 1, 1, 2,  # 0x10
        2, 1, 2, 2, 3, 3, 3, 1, 1, 2, 3, 3, 3, 2, 2, 2,  # 0x20
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1,  # 0x30
        1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  # 0x40
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  # 0x50
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3,  # 0x60
        3, 3, 3, 1, 3, 3, 3, 3, 3, 1, 1, 1, 1, 1, 1, 1,  # 0x70
        1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,  # 0x80
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  # 0x90
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  # 0xa0
        1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,  # 0xb0
        1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,  # 0xc0
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  # 0xd0
        2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,  # 0xe0
        1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 4, 4, 3, 3, 2, 2,  # 0xf0
    ]
)


# ── Fast DEX header scanner ─────────────────────────────────────────────────


def _extract_hierarchy_fast(blob: bytes) -> dict[str, str | None]:
    """Extract {class_name: superclass_name} from raw DEX bytes using header tables only."""
    if len(blob) < 112 or blob[:3] != b"dex":
        return {}

    sids_off = UP("<I", blob, 60)[0]
    tids_size = UP("<I", blob, 64)[0]
    tids_off = UP("<I", blob, 68)[0]
    cdefs_size = UP("<I", blob, 96)[0]
    cdefs_off = UP("<I", blob, 100)[0]

    cache: dict[int, str] = {}
    rt = lambda idx: _dex_read_type(blob, tids_off, tids_size, sids_off, idx, cache)

    hierarchy: dict[str, str | None] = {}
    for i in range(cdefs_size):
        off = cdefs_off + i * 32
        cls_idx = UP("<I", blob, off)[0]
        super_idx = UP("<I", blob, off + 8)[0]
        cls_name = rt(cls_idx)
        super_name = rt(super_idx) if super_idx != 0xFFFFFFFF else None
        if cls_name:
            hierarchy[cls_name] = super_name

    return hierarchy


def _get_dex_blobs(apk: APK) -> list[tuple[str, bytes]]:
    return [
        (name, apk.get_file(name))
        for name in sorted(apk.get_files())
        if re.match(r"^classes\d*\.dex$", name)
    ]


def build_hierarchy_fast(
    dex_blobs: list[tuple[str, bytes]],
) -> tuple[dict[str, str | None], dict[str, int], int]:
    hierarchy: dict[str, str | None] = {}
    class_to_dex: dict[str, int] = {}
    for idx, (_name, blob) in enumerate(dex_blobs):
        h = _extract_hierarchy_fast(blob)
        for cls_name, super_name in h.items():
            hierarchy[cls_name] = super_name
            class_to_dex[cls_name] = idx
    return hierarchy, class_to_dex, len(hierarchy)


# ── Selective full parse (for Phase A — small set of DEX files) ──────────────


def selective_full_parse(dex_blobs: list[tuple[str, bytes]], needed: set[int]) -> dict:
    class_map: dict = {}
    for idx in sorted(needed):
        _name, blob = dex_blobs[idx]
        try:
            dvm = DEX(blob)
        except Exception as exc:
            log.warning("Failed to parse %s: %s", _name, exc)
            continue
        for cls in dvm.get_classes():
            class_map[cls.get_name()] = cls
    return class_map


# ── Targeted raw-DEX bytecode scanner (for Phase B — avoids full parse) ──────

_WAKE_VECTORS = [
    {
        "id": "addFlags",
        "label": "Window.addFlags(FLAG_KEEP_SCREEN_ON)",
        "class_type": b"Landroid/view/Window;",
        "method_name": b"addFlags",
        "value_type": "int",
        "check": lambda v: isinstance(v, int) and (v & 0x80) != 0,
    },
    {
        "id": "setKeepScreenOn",
        "label": "View.setKeepScreenOn(true)",
        "class_type": b"Landroid/view/View;",
        "method_name": b"setKeepScreenOn",
        "value_type": "int",
        "check": lambda v: v == 1,
    },
    {
        "id": "setScreenOnWhilePlaying",
        "label": "MediaPlayer.setScreenOnWhilePlaying(true)",
        "class_type": b"Landroid/media/MediaPlayer;",
        "method_name": b"setScreenOnWhilePlaying",
        "value_type": "int",
        "check": lambda v: v == 1,
    },
    {
        "id": "newWakeLock",
        "label": "PowerManager.newWakeLock(SCREEN_*)",
        "class_type": b"Landroid/os/PowerManager;",
        "method_name": b"newWakeLock",
        "value_type": "int",
        "check": lambda v: isinstance(v, int) and (v & 0x3F) in (6, 10, 26),
    },
    {
        "id": "screen_off_timeout_i",
        "label": "Settings.System.put*(screen_off_timeout)",
        "class_type": b"Landroid/provider/Settings$System;",
        "method_name": b"putInt",
        "value_type": "string",
        "check": lambda v: v == "screen_off_timeout",
    },
    {
        "id": "screen_off_timeout_s",
        "label": "Settings.System.put*(screen_off_timeout)",
        "class_type": b"Landroid/provider/Settings$System;",
        "method_name": b"putString",
        "value_type": "string",
        "check": lambda v: v == "screen_off_timeout",
    },
]


def _build_target_method_map(blob: bytes) -> dict[int, dict]:
    """Build {method_id_index: vector_info} for target methods in this DEX."""
    sids_size = UP("<I", blob, 56)[0]
    sids_off = UP("<I", blob, 60)[0]
    tids_size = UP("<I", blob, 64)[0]
    tids_off = UP("<I", blob, 68)[0]
    mids_size = UP("<I", blob, 88)[0]
    mids_off = UP("<I", blob, 92)[0]

    def _rsb(idx: int) -> bytes:
        off = UP("<I", blob, sids_off + idx * 4)[0]
        pos = off
        while blob[pos] & 0x80:
            pos += 1
        pos += 1
        end = blob.index(0, pos)
        return blob[pos:end]

    target_names = {v["method_name"] for v in _WAKE_VECTORS}
    target_types = {v["class_type"] for v in _WAKE_VECTORS}

    name_idx_to_bytes: dict[int, bytes] = {}
    for i in range(sids_size):
        s = _rsb(i)
        if s in target_names:
            name_idx_to_bytes[i] = s
    if not name_idx_to_bytes:
        return {}

    type_idx_to_bytes: dict[int, bytes] = {}
    for i in range(tids_size):
        desc_si = UP("<I", blob, tids_off + i * 4)[0]
        s = _rsb(desc_si)
        if s in target_types:
            type_idx_to_bytes[i] = s
    if not type_idx_to_bytes:
        return {}

    vec_lookup: dict[tuple[bytes, bytes], dict] = {
        (v["class_type"], v["method_name"]): v for v in _WAKE_VECTORS
    }

    result: dict[int, dict] = {}
    for i in range(mids_size):
        off = mids_off + i * 8
        ci = UP("<H", blob, off)[0]
        ni = UP("<I", blob, off + 4)[0]
        ct = type_idx_to_bytes.get(ci)
        mn = name_idx_to_bytes.get(ni)
        if ct is not None and mn is not None:
            vec = vec_lookup.get((ct, mn))
            if vec:
                result[i] = vec
    return result


def _scan_code_item(
    blob: bytes,
    code_off: int,
    target_mids: dict[int, dict],
    read_string,
) -> list[tuple[dict, int | str | None]]:
    """Scan one method's bytecode and return [(vector, matched_value), ...]."""
    insns_size = UP("<I", blob, code_off + 12)[0]
    base = code_off + 16
    if insns_size == 0:
        return []

    hits: list[tuple[dict, int | str | None]] = []
    # ring buffer of recent const/string values: (insn_counter, type, value)
    window: list[tuple[int, str, int | str]] = []
    counter = 0

    ip = 0
    while ip < insns_size:
        word = UP("<H", blob, base + ip * 2)[0]
        opcode = word & 0xFF

        # Payload pseudo-instructions
        if opcode == 0x00 and word != 0x0000:
            try:
                if word == 0x0100:
                    sz = UP("<H", blob, base + (ip + 1) * 2)[0]
                    ip += 4 + sz * 2
                elif word == 0x0200:
                    sz = UP("<H", blob, base + (ip + 1) * 2)[0]
                    ip += 2 + sz * 4
                elif word == 0x0300:
                    ew = UP("<H", blob, base + (ip + 1) * 2)[0]
                    es = UP("<I", blob, base + (ip + 2) * 2)[0]
                    ip += 4 + (es * ew + 1) // 2
                else:
                    ip += 1
            except (struct.error, IndexError):
                break
            continue

        size = _INSN_SIZE[opcode]
        if ip + size > insns_size:
            break

        try:
            if opcode == 0x12:  # const/4
                val = (word >> 12) & 0xF
                if val >= 8:
                    val -= 16
                window.append((counter, "i", val))
            elif opcode == 0x13:  # const/16
                val = UP("<h", blob, base + (ip + 1) * 2)[0]
                window.append((counter, "i", val))
            elif opcode == 0x14:  # const
                val = UP("<i", blob, base + (ip + 1) * 2)[0]
                window.append((counter, "i", val))
            elif opcode == 0x15:  # const/high16
                val = UP("<h", blob, base + (ip + 1) * 2)[0] << 16
                window.append((counter, "i", val))
            elif opcode == 0x1A:  # const-string
                si = UP("<H", blob, base + (ip + 1) * 2)[0]
                window.append((counter, "s", si))
            elif opcode == 0x1B:  # const-string/jumbo
                si = UP("<I", blob, base + (ip + 1) * 2)[0]
                window.append((counter, "s", si))
            elif (0x6E <= opcode <= 0x72) or (0x74 <= opcode <= 0x78):
                mid = UP("<H", blob, base + (ip + 1) * 2)[0]
                vec = target_mids.get(mid)
                if vec is not None:
                    cutoff = counter - WINDOW_SIZE
                    if vec["value_type"] == "int":
                        for wc, wt, wv in reversed(window):
                            if wc < cutoff:
                                break
                            if wt == "i" and vec["check"](wv):
                                hits.append((vec, wv))
                                break
                    elif vec["value_type"] == "string":
                        for wc, wt, wv in reversed(window):
                            if wc < cutoff:
                                break
                            if wt == "s":
                                sv = read_string(wv)
                                if vec["check"](sv):
                                    hits.append((vec, sv))
                                    break
        except (struct.error, IndexError):
            pass

        # Prune old window entries
        if len(window) > WINDOW_SIZE * 2:
            cutoff = counter - WINDOW_SIZE
            window = [(c, t, v) for c, t, v in window if c >= cutoff]

        counter += 1
        ip += size

    return hits


def scan_dex_targeted(
    blob: bytes,
    target_class_names: set[str],
    hierarchy: dict[str, str | None],
    vid_tier_map: dict[str, int],
) -> tuple[list[dict], set[str]]:
    """
    Scan specific classes in a raw DEX blob for wake-lock patterns using
    direct binary parsing. Returns (findings, scanned_class_names).
    """
    if len(blob) < 112 or blob[:3] != b"dex":
        return [], set()

    target_mids = _build_target_method_map(blob)
    if not target_mids:
        return [], set()

    sids_off = UP("<I", blob, 60)[0]
    tids_size = UP("<I", blob, 64)[0]
    tids_off = UP("<I", blob, 68)[0]
    mids_off = UP("<I", blob, 88 + 4)[0]
    cdefs_size = UP("<I", blob, 96)[0]
    cdefs_off = UP("<I", blob, 100)[0]

    str_cache: dict[int, str] = {}
    rs = lambda idx: _dex_read_string(blob, sids_off, idx, str_cache)
    rt = lambda idx: _dex_read_type(blob, tids_off, tids_size, sids_off, idx, str_cache)

    def method_name_from_id(mid_idx: int) -> str:
        name_si = UP("<I", blob, mids_off + mid_idx * 8 + 4)[0]
        return rs(name_si)

    findings: list[dict] = []
    scanned: set[str] = set()

    # Collect classes to scan: the target classes + their chain classes present in this DEX
    classes_in_dex: set[str] = set()
    for i in range(cdefs_size):
        cn = rt(UP("<I", blob, cdefs_off + i * 32)[0])
        if cn:
            classes_in_dex.add(cn)

    scan_set: set[str] = set()
    for tc in target_class_names:
        chain = []
        cur = tc
        visited = set()
        while cur and cur not in visited:
            visited.add(cur)
            if cur in classes_in_dex:
                chain.append(cur)
            if cur in STOP_SUPERCLASSES:
                break
            cur = hierarchy.get(cur)
        scan_set.update(chain)

    if not scan_set:
        return [], set()

    blob_len = len(blob)
    mids_size = UP("<I", blob, 88)[0]

    for i in range(cdefs_size):
        off = cdefs_off + i * 32
        cls_name = rt(UP("<I", blob, off)[0])
        if cls_name not in scan_set:
            continue

        scanned.add(cls_name)
        class_data_off = UP("<I", blob, off + 24)[0]
        if class_data_off == 0 or class_data_off >= blob_len:
            continue

        try:
            pos = class_data_off
            sf_size, pos = _read_uleb128(blob, pos)
            if_size, pos = _read_uleb128(blob, pos)
            dm_size, pos = _read_uleb128(blob, pos)
            vm_size, pos = _read_uleb128(blob, pos)

            for _ in range(sf_size + if_size):
                _, pos = _read_uleb128(blob, pos)
                _, pos = _read_uleb128(blob, pos)

            for method_count in (dm_size, vm_size):
                method_idx = 0
                for _ in range(method_count):
                    diff, pos = _read_uleb128(blob, pos)
                    method_idx += diff
                    _, pos = _read_uleb128(blob, pos)  # access_flags
                    code_off, pos = _read_uleb128(blob, pos)
                    if code_off == 0 or code_off + 16 > blob_len:
                        continue
                    if method_idx >= mids_size:
                        continue

                    try:
                        hits = _scan_code_item(blob, code_off, target_mids, rs)
                    except Exception:
                        continue

                    for vec, _val in hits:
                        mname = method_name_from_id(method_idx)
                        java_cls = from_dalvik(cls_name)
                        tier = vid_tier_map.get(vec["id"], 5)
                        note = _tier_note(vec["id"], tier, java_cls)
                        findings.append(
                            {
                                "vector": vec["label"],
                                "found_in_class": java_cls,
                                "found_in_method": mname,
                                "tier": tier,
                                "confidence": TIER_INFO[tier]["confidence"],
                                "note": note,
                                "evidence": (
                                    f"Raw DEX bytecode in {java_cls}.{mname}() "
                                    f"contains invoke to {vec['label'].split('(')[0]} "
                                    f"with matching constant."
                                ),
                            }
                        )
        except Exception:
            continue

    return findings, scanned


# ── Androguard-based bytecode scan (for Phase A — high-fidelity) ─────────────

DETECTION_VECTORS = [
    {
        "method_sig": "Window;->addFlags",
        "label": "Window.addFlags(FLAG_KEEP_SCREEN_ON)",
        "value_type": "int",
        "check": lambda v: isinstance(v, int) and (v & 0x80) != 0,
    },
    {
        "method_sig": "View;->setKeepScreenOn",
        "label": "View.setKeepScreenOn(true)",
        "value_type": "int",
        "check": lambda v: v == 1,
    },
    {
        "method_sig": "MediaPlayer;->setScreenOnWhilePlaying",
        "label": "MediaPlayer.setScreenOnWhilePlaying(true)",
        "value_type": "int",
        "check": lambda v: v == 1,
    },
    {
        "method_sig": "PowerManager;->newWakeLock",
        "label": "PowerManager.newWakeLock(SCREEN_*)",
        "value_type": "int",
        "check": lambda v: isinstance(v, int) and (v & 0x3F) in (6, 10, 26),
    },
    {
        "method_sig": "Settings$System;->put",
        "label": "Settings.System.put*(screen_off_timeout)",
        "value_type": "string",
        "check": lambda v: v == "screen_off_timeout",
    },
]


def _extract_literal(operands) -> int | None:
    for op in operands:
        if isinstance(op, (list, tuple)) and len(op) >= 2:
            kind = op[0]
            if isinstance(kind, int) and kind == 1:
                return int(op[1])
            if hasattr(kind, "value") and kind.value == 1:
                return int(op[1])
    return None


def _extract_string(operands) -> str | None:
    for op in operands:
        if isinstance(op, (list, tuple)) and len(op) >= 3:
            val = op[2]
            if isinstance(val, str):
                return val
    return None


def scan_class_androguard(cls, sig_tier_map: dict[str, int]) -> list[dict]:
    """Scan all methods in *cls* using androguard instruction objects."""
    findings: list[dict] = []
    class_name = cls.get_name()
    java_cls = from_dalvik(class_name)

    for method in cls.get_methods():
        try:
            instructions = list(method.get_instructions())
        except Exception:
            continue

        for i, inst in enumerate(instructions):
            output = inst.get_output()
            if not output:
                continue

            for vec in DETECTION_VECTORS:
                if vec["method_sig"] not in output:
                    continue

                window_start = max(0, i - WINDOW_SIZE)
                window = instructions[window_start:i]

                matched = False
                if vec["value_type"] == "int":
                    for w in window:
                        if w.get_name().startswith("const"):
                            val = _extract_literal(w.get_operands())
                            if val is not None and vec["check"](val):
                                matched = True
                                break
                elif vec["value_type"] == "string":
                    for w in window:
                        if "string" in w.get_name():
                            val = _extract_string(w.get_operands())
                            if val is not None and vec["check"](val):
                                matched = True
                                break

                if matched:
                    tier = sig_tier_map.get(vec["method_sig"], 3)
                    note = _tier_note(vec["method_sig"], tier, java_cls)
                    findings.append(
                        {
                            "vector": vec["label"],
                            "found_in_class": java_cls,
                            "found_in_method": method.get_name(),
                            "tier": tier,
                            "confidence": TIER_INFO[tier]["confidence"],
                            "note": note,
                            "evidence": (
                                f"Dalvik bytecode in {java_cls}.{method.get_name()}() "
                                f"invokes {vec['method_sig'].split(';->')[0].split('/')[-1]}"
                                f".{vec['method_sig'].split('->')[-1]}() with matching "
                                f"constant in preceding {WINDOW_SIZE}-instruction window."
                            ),
                        }
                    )
    return findings


# ── Manifest helpers ─────────────────────────────────────────────────────────


def resolve_activity_name(raw: str, package: str) -> str:
    if raw.startswith("."):
        return package + raw
    if "." not in raw:
        return package + "." + raw
    return raw


# ── Inheritance chain ────────────────────────────────────────────────────────


def walk_inheritance_fast(dalvik_name: str, hierarchy: dict[str, str | None]) -> list[str]:
    chain: list[str] = []
    visited: set[str] = set()
    current = dalvik_name
    while current and current not in visited:
        visited.add(current)
        chain.append(current)
        if current in STOP_SUPERCLASSES:
            break
        parent = hierarchy.get(current)
        if parent is None:
            break
        current = parent
    return chain


# ── AXML layout / theme scan ────────────────────────────────────────────────


_AD_SDK_LAYOUT_MARKERS = re.compile(
    r"mbridge|applovin|fyber|inneractive|admob|ironsource|vungle|chartboost|"
    r"bidmachine|tapjoy|pangle|bytedance|inmobi|mopub|unity_ads|unityads|"
    r"facebook_ad|audience_network|reward_video|interstitial_ad",
    re.IGNORECASE,
)


def scan_axml_resources(apk: APK) -> list[dict]:
    findings: list[dict] = []
    xml_files = [
        f for f in apk.get_files() if f.startswith("res/") and f.endswith(".xml")
    ]

    for fpath in xml_files:
        try:
            raw = apk.get_file(fpath)
        except Exception:
            continue

        if b"keepScreenOn" not in raw:
            continue

        try:
            xml_str = AXMLPrinter(raw).get_xml()
            if isinstance(xml_str, bytes):
                xml_str = xml_str.decode("utf-8", errors="replace")
        except Exception:
            continue

        if 'keepScreenOn="true"' in xml_str:
            is_sdk = bool(_AD_SDK_LAYOUT_MARKERS.search(fpath))
            tier = 5 if is_sdk else 3
            findings.append(
                {
                    "vector": f"AXML keepScreenOn=true in {fpath}",
                    "found_in_class": fpath,
                    "found_in_method": "N/A",
                    "tier": tier,
                    "confidence": TIER_INFO[tier]["confidence"],
                    "note": _tier_note("AXML keepScreenOn", tier),
                    "evidence": (
                        f"Android binary XML layout {fpath} contains "
                        f"android:keepScreenOn=\"true\" attribute"
                        f"{' (ad SDK layout)' if is_sdk else ''}."
                    ),
                }
            )

    return findings


# ── Phase A+: Unity IL2CPP metadata scan ─────────────────────────────────────

KNOWN_ENGINE_ACTIVITIES = {
    "Lcom/unity3d/player/UnityPlayerActivity;": "unity",
    "Lcom/unity3d/player/UnityPlayerGameActivity;": "unity",
}


def _metadata_extract_class_context(blob: bytes, anchor: bytes) -> dict:
    """Extract the owning class name and sibling method/field names around *anchor*."""
    idx = blob.find(anchor)
    if idx < 0:
        return {}

    region = blob[max(0, idx - 800):idx + 400]
    entries = []
    cur: list[int] = []
    for b in region:
        if b == 0:
            if cur:
                entries.append(bytes(cur).decode("utf-8", errors="replace"))
                cur = []
        elif 32 <= b < 127:
            cur.append(b)
        else:
            if cur:
                entries.append(bytes(cur).decode("utf-8", errors="replace"))
                cur = []
    if cur:
        entries.append(bytes(cur).decode("utf-8", errors="replace"))

    class_name = None
    for e in entries:
        if e and e[0].isupper() and len(e) > 3 and not e.startswith("<") and "get_" not in e and "set_" not in e:
            if any(kw in e for kw in ["Manager", "Controller", "Handler", "Service",
                                       "Game", "App", "Scene", "Player", "Screen"]):
                class_name = e
                break

    game_lifecycle = [e for e in entries if any(kw in e for kw in
        ["StartGame", "CreateGame", "ReturnFrom", "ActiveGame", "activeGame",
         "GameLogic", "GameMatch", "OnPause", "OnResume", "Pause", "Resume",
         "Battle", "Match", "Session", "Round", "Level"])]

    has_update = any("Update" in e and "Sleep" in e for e in entries)
    has_set = any("Set" in e and "Sleep" in e for e in entries)

    return {
        "class_name": class_name,
        "game_lifecycle_methods": game_lifecycle[:8],
        "has_dynamic_toggle": has_update and has_set,
        "has_set_only": has_set and not has_update,
    }


def _count_tier2_signals(entries: list[str]) -> int:
    """Count how many Tier 2 qualifying signal patterns appear in *entries*."""
    signals = 0
    has_sleep_update = any(
        "sleep" in e.lower() and "update" in e.lower() for e in entries
    )
    has_sleep_set_custom = any(
        "sleep" in e.lower() and "set" in e.lower()
        and "set_sleepTimeout" != e  # exclude Unity engine's own setter
        for e in entries
    )
    has_game_state = any(
        any(kw in e for kw in [
            "StartGame", "CreateGame", "PauseGame", "ResumeGame",
            "ActiveGame", "activeGame", "GameSession", "EnterBattle",
            "ExitBattle", "GameLogic", "GameMatch",
        ])
        for e in entries
    )
    if has_sleep_update:
        signals += 1
    if has_sleep_set_custom:
        signals += 1
    if has_game_state:
        signals += 1
    return signals


def scan_unity_il2cpp(apk: APK, inheritance_chain: list[str]) -> list[dict]:
    """Detect Screen.sleepTimeout usage in Unity IL2CPP games via metadata."""
    engine = None
    for cls_name in inheritance_chain:
        if cls_name in KNOWN_ENGINE_ACTIVITIES:
            engine = "unity"
            break
    if engine is None:
        return []

    metadata_file = None
    for fn in apk.get_files():
        if "global-metadata.dat" in fn:
            metadata_file = fn
            break
    if metadata_file is None:
        return []

    blob = apk.get_file(metadata_file)
    has_set_sleep = b"set_sleepTimeout" in blob

    if not has_set_sleep:
        return []

    # Extract context around any custom sleep-related methods
    custom_patterns = [b"SetSleepTimeout", b"UpdateSleepTimeout", b"KeepScreenAwake",
                       b"DisableScreenTimeout", b"PreventScreenSleep"]
    custom_hits = [p.decode() for p in custom_patterns if p in blob]

    ctx = {}
    if custom_hits:
        ctx = _metadata_extract_class_context(blob, custom_hits[0].encode())
    elif has_set_sleep:
        ctx = _metadata_extract_class_context(blob, b"set_sleepTimeout")

    owner = ctx.get("class_name") or "unknown class"
    lifecycle = ctx.get("game_lifecycle_methods", [])

    # Collect all nearby entries for Tier 2 signal counting
    all_entries = custom_hits + lifecycle
    if ctx.get("has_dynamic_toggle"):
        all_entries.append("UpdateSleepTimeout")
    if ctx.get("has_set_only"):
        all_entries.append("SetSleepTimeout")
    # Also add the raw entries from context extraction for game-state detection
    all_entries.extend(lifecycle)

    tier2_signals = _count_tier2_signals(all_entries)
    is_tier2 = tier2_signals >= 2

    if is_tier2:
        tier = 2
        dynamic = ctx.get("has_dynamic_toggle", False)
        if dynamic and lifecycle:
            evidence = (
                f"IL2CPP metadata contains game-specific C# methods: "
                f"{', '.join(custom_hits) if custom_hits else 'set_sleepTimeout'}, "
                f"defined in {owner}. "
                f"Sibling methods suggest gameplay state management: "
                f"{', '.join(lifecycle[:5])}. "
                f"The Update+Set pair indicates a DYNAMIC TOGGLE: the game "
                f"likely calls Screen.sleepTimeout = -1 (NeverSleep) during "
                f"active gameplay sessions and resets to -2 (SystemSetting) "
                f"in menus/idle. The exact integer value (-1 NeverSleep vs a "
                f"positive seconds value) is compiled to native ARM code and "
                f"cannot be read statically; however the toggle pattern and "
                f"game-session context strongly indicate NeverSleep (-1) "
                f"during active play."
            )
        else:
            evidence = (
                f"IL2CPP metadata contains game-specific C# methods: "
                f"{', '.join(custom_hits) if custom_hits else 'set_sleepTimeout'}, "
                f"defined in {owner}. "
                f"Developer-written wrapper methods around Unity's sleep API "
                f"prove deliberate intent to control screen sleep. "
                f"The exact value (-1 = NeverSleep / indefinite, "
                f"-2 = SystemSetting, or a positive integer = seconds) is "
                f"compiled to native ARM code and cannot be determined "
                f"statically, but the developer context strongly indicates "
                f"NeverSleep (-1) is used during gameplay."
            )
        return [{
            "vector": "Unity IL2CPP: C# Screen.sleepTimeout API",
            "found_in_class": f"global-metadata.dat — {owner}",
            "found_in_method": ", ".join(custom_hits) if custom_hits else "set_sleepTimeout",
            "tier": tier,
            "confidence": TIER_INFO[tier]["confidence"],
            "note": None,
            "evidence": evidence,
        }]

    # Tier 4: API present but no developer-written context
    tier = 4
    return [{
        "vector": "Unity IL2CPP: C# Screen.sleepTimeout API",
        "found_in_class": "global-metadata.dat (IL2CPP compiled C#)",
        "found_in_method": "set_sleepTimeout",
        "tier": tier,
        "confidence": TIER_INFO[tier]["confidence"],
        "note": (
            "Unity sleepTimeout API reference found but intent is "
            "unverifiable through static analysis. The app MAY keep the "
            "screen on during gameplay. Requires manual testing: install the "
            "app, enter active gameplay, and observe if the screen dims "
            "after 2 minutes of no touch input."
        ),
        "evidence": (
            "IL2CPP metadata contains Unity's Screen.set_sleepTimeout "
            "property setter, but no developer-written wrapper methods or "
            "game-state management context was found nearby. The API "
            "accepts: -1 (NeverSleep = screen stays on indefinitely), "
            "-2 (SystemSetting = OS default timeout), or a positive "
            "integer (seconds until screen dims). Cannot determine which "
            "value is passed without decompiling native ARM code."
        ),
    }]


# ── Phase C: global raw string search ────────────────────────────────────────


def phase_c_global_string_search(raw_dex_blobs: list[bytes]) -> list[dict]:
    findings: list[dict] = []
    targets = [b"keepScreenOn", b"FLAG_KEEP_SCREEN_ON"]
    for idx, data in enumerate(raw_dex_blobs):
        label = f"classes{idx or ''}.dex"
        for needle in targets:
            if needle in data:
                findings.append(
                    {
                        "vector": f"Raw string '{needle.decode()}' in {label}",
                        "found_in_class": label,
                        "found_in_method": "N/A (raw bytes)",
                        "tier": 5,
                        "confidence": TIER_INFO[5]["confidence"],
                        "note": (
                            "Raw string reference only — no confirmed code "
                            "invocation. Could be an unreachable reference or "
                            "SDK artifact."
                        ),
                        "evidence": (
                            f"Raw byte string '{needle.decode()}' found in {label}; "
                            "no confirmed invocation — could be an unreachable "
                            "reference or SDK artifact."
                        ),
                    }
                )
    return findings


# ── Main analysis pipeline ───────────────────────────────────────────────────


def analyze_apk(apk_path: str) -> dict:
    t_start = time.perf_counter()
    apk_name = os.path.basename(apk_path)
    apk = APK(apk_path)
    package = apk.get_package()

    # 1. Manifest-driven class extraction
    main_activity_java = apk.get_main_activity()
    all_activities_java = list(apk.get_activities())

    if main_activity_java:
        main_activity_java = resolve_activity_name(main_activity_java, package)
    else:
        log.warning("No MAIN/LAUNCHER activity found; will scan all activities.")

    all_activities_dalvik = [
        to_dalvik(resolve_activity_name(a, package)) for a in all_activities_java
    ]
    main_dalvik = to_dalvik(main_activity_java) if main_activity_java else None

    # 2. Fast DEX header scan (class hierarchy only)
    dex_blobs = _get_dex_blobs(apk)
    raw_dex_bytes = [blob for _, blob in dex_blobs]
    hierarchy, class_to_dex, total_classes = build_hierarchy_fast(dex_blobs)

    # 3. Inheritance chain (via fast hierarchy map)
    inheritance_chain: list[str] = []
    if main_dalvik and main_dalvik in hierarchy:
        inheritance_chain = walk_inheritance_fast(main_dalvik, hierarchy)
    elif main_dalvik:
        log.warning("Main activity %s not found in DEX; falling back to Phase B+C.", main_dalvik)

    chain_java = [from_dalvik(d) for d in inheritance_chain]

    # 4. Targeted bytecode scan
    all_findings: list[dict] = []
    scanned_classes: set[str] = set()

    # Collect all game-engine classes present in the DEX hierarchy.
    # These are always scanned regardless of manifest declarations because
    # the game runtime IS the app — wake locks here are session-wide.
    engine_class_names: set[str] = {
        cls for cls in hierarchy if is_game_engine_class(cls)
    }

    # Phase A: full androguard parse of DEX files containing the main-activity
    # inheritance chain AND the game-engine classes.
    phase_a_dex = {class_to_dex[d] for d in inheritance_chain if d in class_to_dex}
    phase_a_dex.update(class_to_dex[d] for d in engine_class_names if d in class_to_dex)
    class_map = selective_full_parse(dex_blobs, phase_a_dex)

    for dalvik_name in inheritance_chain:
        cls = class_map.get(dalvik_name)
        if cls is None:
            continue
        scanned_classes.add(dalvik_name)
        all_findings.extend(scan_class_androguard(cls, _SIG_TIER_MAIN))

    # Phase A-engine: scan every game-engine class at Tier 2.
    # Runs unconditionally — engine classes are functionally first-party.
    for dalvik_name in sorted(engine_class_names):
        if dalvik_name in scanned_classes:
            continue
        cls = class_map.get(dalvik_name)
        if cls is None:
            continue
        scanned_classes.add(dalvik_name)
        all_findings.extend(scan_class_androguard(cls, _SIG_TIER_ENGINE))

    # Phase A+: Unity IL2CPP metadata scan (if Phase A + A-engine found nothing)
    if not all_findings:
        all_findings.extend(scan_unity_il2cpp(apk, inheritance_chain))

    # Phase B: targeted raw-DEX scan of manifest activities + keyword classes
    if not all_findings:
        # Classes with wake-lock-relevant keywords in their simple class name
        keyword_names = frozenset(["Player", "Activity", "Application", "Service", "WakeLock"])
        keyword_classes: set[str] = {
            cls for cls in hierarchy
            if any(kw in from_dalvik(cls).split(".")[-1] for kw in keyword_names)
            and cls not in scanned_classes
            and not is_game_engine_class(cls)   # already handled in Phase A-engine
        }

        other_activities = [d for d in all_activities_dalvik if d not in scanned_classes]
        expanded_b: set[str] = set(other_activities) | keyword_classes

        # Three-bucket classification per spec:
        #   App package  → Tier 3 (non-main app code)
        #   Engine       → Tier 2 (should already be empty after Phase A-engine)
        #   Ad SDK       → Tier 5
        #   Unknown      → Tier 3 (needs manual review)
        app_b: list[str] = []
        engine_b: list[str] = []
        ad_sdk_b: list[str] = []
        unknown_b: list[str] = []
        for d in expanded_b:
            java_name = from_dalvik(d)
            if java_name.startswith(package):
                app_b.append(d)
            elif is_game_engine_class(d):
                engine_b.append(d)
            elif is_ad_sdk_class(d):
                ad_sdk_b.append(d)
            else:
                unknown_b.append(d)

        for vid_map, tier_list in [
            (_VID_TIER_APP, app_b),
            (_VID_TIER_ENGINE, engine_b),   # safety fallback; normally empty
            (_VID_TIER_ADSDK, ad_sdk_b),
            (_VID_TIER_APP, unknown_b),     # unknown third-party → Tier 3
        ]:
            dex_to_classes: dict[int, list[str]] = {}
            for d in tier_list:
                idx = class_to_dex.get(d)
                if idx is not None:
                    dex_to_classes.setdefault(idx, []).append(d)

            for dex_idx in sorted(dex_to_classes):
                _, blob = dex_blobs[dex_idx]
                target_names = set(dex_to_classes[dex_idx])
                hits, scanned_b = scan_dex_targeted(
                    blob, target_names, hierarchy, vid_map
                )
                scanned_classes.update(scanned_b)
                all_findings.extend(hits)

    # AXML resource scan
    all_findings.extend(scan_axml_resources(apk))

    # Phase C: global string search (only if Phases A/B found nothing at all)
    if not all_findings:
        all_findings.extend(phase_c_global_string_search(raw_dex_bytes))

    # 5. Determine overall tier and verdict
    # Best (lowest number) tier wins; no findings → use a sentinel
    if all_findings:
        best_tier = min(f["tier"] for f in all_findings)
    else:
        best_tier = 0  # sentinel: nothing found

    if best_tier == 0:
        is_flagged = False
        confidence = "high"
        needs_manual_review = False
        overall_tier = None
    else:
        ti = TIER_INFO[best_tier]
        is_flagged = ti["is_flagged"]
        confidence = ti["confidence"]
        needs_manual_review = ti["needs_manual_review"]
        overall_tier = best_tier

    # Build doubt_reasons list
    doubt_reasons: list[str] = []
    if all_findings and not is_flagged:
        tiers_present = {f["tier"] for f in all_findings}
        if tiers_present == {5}:
            doubt_reasons.append(
                "All detections are from third-party advertising SDKs "
                "(e.g. Fyber, AppLovin, BidMachine, Mintegral), not the app's "
                "own code. Ad SDKs keep the screen on only during video ad playback."
            )
        if 4 in tiers_present:
            doubt_reasons.append(
                "Unity sleepTimeout API reference found but developer intent "
                "is unverifiable — the app MAY or MAY NOT keep the screen on."
            )
            if 5 in tiers_present:
                doubt_reasons.append(
                    "Additional detections are from third-party ad SDKs and "
                    "do not represent the app's own code."
                )

    # Manual review instructions for Tier 4
    manual_review_instructions = None
    if needs_manual_review:
        manual_review_instructions = (
            "Unity sleepTimeout detected but intent unverifiable. To confirm: "
            "1) Install the APK on a device, 2) Enter active gameplay (not "
            "just menus), 3) Wait 2+ minutes without touching the screen, "
            "4) If screen stays on = wake lock confirmed, if screen dims = "
            "no wake lock."
        )

    # Deduplicate
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in all_findings:
        key = (f["vector"], f["found_in_class"], f["found_in_method"])
        if key not in seen:
            seen.add(key)
            unique.append(f)

    # Build the top-level comment summarizing evidence
    comment_parts: list[str] = []
    all_tier5 = unique and all(f["tier"] == 5 for f in unique)
    if all_tier5:
        sdk_classes = [f["found_in_class"] for f in unique]
        comment_parts.append(
            f"All detections are from third-party ad SDKs ({', '.join(sdk_classes)}), "
            "not the app's own code. SDKs only keep screen on during video ad playback."
        )
    elif unique:
        for f in unique:
            comment_parts.append(f["evidence"])
            if f.get("note"):
                comment_parts.append(f"Note: {f['note']}")
    if doubt_reasons and not all_tier5:
        for dr in doubt_reasons:
            comment_parts.append(dr)
    if manual_review_instructions:
        comment_parts.append(manual_review_instructions)
    if not unique and not doubt_reasons:
        comment_parts.append(
            "No wake-lock patterns found in main activity chain, "
            "manifest activities, AXML layouts, or global string search."
        )
    comment = " | ".join(comment_parts)

    elapsed = time.perf_counter() - t_start
    return {
        "apk_name": apk_name,
        "package": package,
        "main_activity": main_activity_java,
        "inheritance_chain": chain_java,
        "wake_lock_detected": is_flagged,
        "confidence": confidence,
        "needs_manual_review": "Yes" if needs_manual_review else "No",
        "flag_reasons": unique,
        "doubt_reasons": doubt_reasons,
        "manual_review_instructions": manual_review_instructions,
        "comment": comment,
        "classes_scanned": len(scanned_classes),
        "total_classes_in_apk": total_classes,
        "time_taken_seconds": round(elapsed, 2),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path_to_apk> [path_to_apk ...]", file=sys.stderr)
        sys.exit(1)

    for apk_path in sys.argv[1:]:
        if not os.path.isfile(apk_path):
            print(json.dumps({"error": f"File not found: {apk_path}"}), file=sys.stderr)
            continue
        try:
            result = analyze_apk(apk_path)
            print(json.dumps(result, indent=2))
        except Exception as exc:
            print(
                json.dumps({"apk_name": os.path.basename(apk_path), "error": str(exc)}),
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()

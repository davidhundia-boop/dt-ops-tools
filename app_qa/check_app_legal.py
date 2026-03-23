#!/usr/bin/env python3
"""
App Legal Compliance Checker (Privacy Policy & Terms & Conditions)

Checks Android apps for legal compliance by:
1. Querying Google Play Store listings
2. Crawling developer websites for legal links
3. Probing common legal subpages as fallback
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import struct
import zipfile

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from androguard.core.apk import APK as AndroguardAPK
    from androguard.core.axml import AXMLPrinter
    HAS_ANDROGUARD = True
except ImportError:
    HAS_ANDROGUARD = False

try:
    from google_play_scraper import app as gp_app
    from google_play_scraper.exceptions import NotFoundError
except ImportError:
    print("Missing dependency: google-play-scraper")
    print("Install with: pip install google-play-scraper")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15

TC_KEYWORDS = [
    "terms & conditions", "terms and conditions", "terms of service",
    "terms of use", "terms", "tos", "eula", "end user license",
    "user agreement", "legal terms",
]
PP_KEYWORDS = [
    "privacy policy", "privacy", "data policy", "data protection",
]
OTHER_LEGAL_KEYWORDS = [
    "cookie policy", "cookie", "disclaimer", "legal notice",
    "acceptable use", "refund policy", "dmca",
]

SUBPAGE_PATHS = [
    "/terms", "/terms-and-conditions", "/terms-of-service", "/tos",
    "/legal", "/legal/terms", "/eula", "/privacy", "/privacy-policy",
]

SKIP_PREFIXES = ("javascript:", "mailto:", "tel:", "#")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SENSITIVE_PERMISSIONS = {
    "INTERNET", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
    "ACCESS_BACKGROUND_LOCATION", "READ_CONTACTS", "WRITE_CONTACTS",
    "CAMERA", "RECORD_AUDIO", "READ_PHONE_STATE",
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE",
    "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO",
    "READ_CALENDAR", "WRITE_CALENDAR",
    "READ_SMS", "RECEIVE_SMS", "SEND_SMS",
    "READ_CALL_LOG", "BODY_SENSORS",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LegalLink:
    text: str
    url: str
    location: str  # "footer", "nav", "sidebar", "body", "subpage_probe"
    verified: Optional[bool] = None


@dataclass
class DataCategory:
    category: str
    data_types: list[str]
    purposes: list[str]
    optional: bool = False


@dataclass
class DataSafetyInfo:
    has_data_safety_section: bool
    collected: list[DataCategory]
    shared: list[DataCategory]
    security_practices: list[str]
    no_data_collected: bool
    no_data_shared: bool
    status: str  # "COMPLETE" / "NO_DATA" / "MISSING" / "PARSE_ERROR"
    plausibility: str = "N/A"  # "YES" / "SUSPECT" / "N/A"
    suspect_permissions: list[str] = field(default_factory=list)


@dataclass
class LegalCheckResult:
    package_name: str
    apk_source: Optional[str] = None
    app_name: Optional[str] = None
    developer: Optional[str] = None
    play_store_found: bool = False
    privacy_policy_url: Optional[str] = None
    developer_website: Optional[str] = None
    developer_email: Optional[str] = None
    website_accessible: Optional[bool] = None
    data_safety: Optional[DataSafetyInfo] = None
    tc_links: list[LegalLink] = field(default_factory=list)
    pp_links_on_site: list[LegalLink] = field(default_factory=list)
    other_legal_links: list[LegalLink] = field(default_factory=list)
    in_app_legal: Optional[dict] = None
    privacy_policy_verdict: str = "NOT FOUND"
    tc_verdict: str = "NOT FOUND"
    confidence: str = "FAIL"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# APK input resolution
# ---------------------------------------------------------------------------

def _is_apk_input(entry: str) -> bool:
    return entry.lower().endswith(".apk") or (
        not entry.startswith("-") and os.path.isfile(entry)
    )


def _extract_package_from_apk(apk_path: str,
                               verbose: bool = False) -> Optional[str]:
    """Extract the package name from an APK using aapt2, falling back to aapt."""
    for tool in ("aapt2", "aapt"):
        try:
            proc = subprocess.run(
                [tool, "dump", "badging", apk_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                match = re.search(r"name='([^']+)'", proc.stdout)
                if match:
                    if verbose:
                        print(f"  [*] Extracted package {match.group(1)} "
                              f"from {apk_path} via {tool}")
                    return match.group(1)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            continue

    return None


def resolve_input(entry: str,
                  verbose: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Return (package_name, apk_source_path).

    Auto-detects APK files vs. plain package names.
    """
    if _is_apk_input(entry):
        if not os.path.isfile(entry):
            print(f"Error: APK file not found: {entry}")
            return None, entry
        pkg = _extract_package_from_apk(entry, verbose)
        if pkg is None:
            print(f"Error: could not extract package name from {entry} "
                  "(aapt2/aapt not found or APK invalid)")
            return None, entry
        return pkg, entry
    return entry, None


def extract_apk_permissions(apk_path: str,
                            verbose: bool = False) -> Optional[list[str]]:
    """Extract Android permissions from an APK using aapt2/aapt."""
    for tool in ("aapt2", "aapt"):
        try:
            proc = subprocess.run(
                [tool, "dump", "permissions", apk_path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                perms = re.findall(
                    r"name='android\.permission\.([^']+)'", proc.stdout,
                )
                if verbose:
                    print(f"  [*] Extracted {len(perms)} permissions from APK")
                return perms
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ---------------------------------------------------------------------------
# In-App Legal Link Detection (static APK analysis)
# ---------------------------------------------------------------------------

_LEGAL_URL_PATTERNS = re.compile(
    r"https?://[^\s\"'<>]+(?:privac|policy|terms|tos(?:/|$)|eula|legal|"
    r"terms.of.service|terms.of.use|terms.and.conditions|"
    r"datenschutz|impressum|gdpr|ccpa)",
    re.IGNORECASE,
)

_PP_URL_KEYWORDS = ("privac", "policy", "datenschutz", "gdpr", "ccpa")
_TC_URL_KEYWORDS = (
    "terms", "tos", "eula", "legal",
    "terms-of-service", "terms-of-use", "terms-and-conditions",
    "user-agreement", "end-user-license",
)

_LEGAL_RES_NAMES = re.compile(
    r"(privac|policy|terms|eula|legal|tos_url|terms_url|pp_url|"
    r"privacy_policy|terms_of_service|terms_of_use|user_agreement)",
    re.IGNORECASE,
)

_LEGAL_ACTIVITY_NAMES = re.compile(
    r"(privac|policy|terms|legal|eula|tos(?=[A-Z_/]))",
    re.IGNORECASE,
)


def _extract_dex_strings(dex_bytes: bytes) -> set[str]:
    """Extract all strings from a DEX file via binary header parsing.

    Replicates the pattern from play_integrity_analyzer.py.
    """
    strings: set[str] = set()
    try:
        if len(dex_bytes) < 112 or dex_bytes[:4] != b"dex\n":
            return strings
        string_ids_size = struct.unpack_from("<I", dex_bytes, 56)[0]
        string_ids_off = struct.unpack_from("<I", dex_bytes, 60)[0]
        for i in range(string_ids_size):
            str_data_off = struct.unpack_from(
                "<I", dex_bytes, string_ids_off + i * 4
            )[0]
            if str_data_off >= len(dex_bytes):
                continue
            pos = str_data_off
            size = 0
            shift = 0
            while pos < len(dex_bytes):
                byte = dex_bytes[pos]
                size |= (byte & 0x7F) << shift
                pos += 1
                if byte & 0x80 == 0:
                    break
                shift += 7
            end = min(pos + size, len(dex_bytes))
            try:
                s = dex_bytes[pos:end].decode("utf-8", errors="ignore")
                if len(s) > 3:
                    strings.add(s)
            except Exception:
                pass
    except Exception:
        pass
    return strings


def _classify_legal_url(url: str) -> str:
    """Return 'pp', 'tc', or '' based on URL keywords."""
    low = url.lower()
    if any(kw in low for kw in _PP_URL_KEYWORDS):
        return "pp"
    if any(kw in low for kw in _TC_URL_KEYWORDS):
        return "tc"
    return ""


def scan_apk_legal_links(
    apk_path: str, verbose: bool = False,
) -> dict:
    """Statically analyse an APK for in-app privacy policy and T&C links.

    Three extraction passes:
      1. String resources (res/values/strings.xml) via androguard or aapt2
      2. URLs from DEX bytecode via binary header parsing
      3. Activity names from AndroidManifest.xml

    Returns dict with keys:
      in_app_pp_urls, in_app_tc_urls, legal_activities,
      legal_strings, verdict, notes
    """
    pp_urls: list[str] = []
    tc_urls: list[str] = []
    legal_activities: list[str] = []
    legal_strings: list[str] = []
    notes: list[str] = []

    # ------------------------------------------------------------------
    # Pass 1 — String resources
    # ------------------------------------------------------------------
    if verbose:
        print("  [*] In-app legal: scanning string resources ...")

    if HAS_ANDROGUARD:
        try:
            apk_obj = AndroguardAPK(apk_path)
            # Check all res/values string XML files
            for fname in apk_obj.get_files():
                if not (fname.startswith("res/") and fname.endswith(".xml")
                        and "values" in fname):
                    continue
                try:
                    raw = apk_obj.get_file(fname)
                    xml_text = AXMLPrinter(raw).get_xml()
                    if isinstance(xml_text, bytes):
                        xml_text = xml_text.decode("utf-8", errors="ignore")
                except Exception:
                    continue

                # Find string entries with legal-sounding names or values
                for m in re.finditer(
                    r'<string\s+name="([^"]*)"[^>]*>([^<]*)</string>',
                    xml_text,
                ):
                    name, value = m.group(1), m.group(2)
                    if _LEGAL_RES_NAMES.search(name):
                        legal_strings.append(name)
                        # If the value is a URL, classify it
                        if value.startswith("http"):
                            cls = _classify_legal_url(value)
                            if cls == "pp":
                                pp_urls.append(value)
                            elif cls == "tc":
                                tc_urls.append(value)
                            else:
                                pp_urls.append(value)  # legal-named resource → assume PP
                        if verbose:
                            print(f"    [+] String resource: {name}={value[:80]}")
                    elif value.startswith("http") and _LEGAL_URL_PATTERNS.search(value):
                        cls = _classify_legal_url(value)
                        if cls == "pp":
                            pp_urls.append(value)
                        elif cls == "tc":
                            tc_urls.append(value)
                        legal_strings.append(name)
                        if verbose:
                            print(f"    [+] URL in resource: {name}={value[:80]}")
        except Exception as exc:
            notes.append(f"String resource scan failed: {exc}")
    else:
        # Fallback: aapt2 dump resources
        try:
            for tool in ("aapt2", "aapt"):
                try:
                    proc = subprocess.run(
                        [tool, "dump", "resources", apk_path],
                        capture_output=True, text=True, timeout=30,
                    )
                    if proc.returncode == 0:
                        for line in proc.stdout.splitlines():
                            if _LEGAL_RES_NAMES.search(line):
                                legal_strings.append(line.strip()[:120])
                                # Try to extract URL from the line
                                url_m = re.search(r"https?://[^\s\"']+", line)
                                if url_m:
                                    url = url_m.group(0)
                                    cls = _classify_legal_url(url)
                                    if cls == "pp":
                                        pp_urls.append(url)
                                    elif cls == "tc":
                                        tc_urls.append(url)
                        break
                except FileNotFoundError:
                    continue
        except Exception as exc:
            notes.append(f"aapt resource scan failed: {exc}")

    # ------------------------------------------------------------------
    # Pass 2 — DEX string extraction (URLs from bytecode)
    # ------------------------------------------------------------------
    if verbose:
        print("  [*] In-app legal: scanning DEX strings for URLs ...")

    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            dex_files = [
                n for n in zf.namelist()
                if re.match(r"^classes\d*\.dex$", n)
            ]
            for dex_name in dex_files:
                dex_bytes = zf.read(dex_name)
                all_strings = _extract_dex_strings(dex_bytes)

                for s in all_strings:
                    if _LEGAL_URL_PATTERNS.search(s):
                        # Extract clean URL (may have trailing junk)
                        url_m = re.search(r"https?://[^\s\"'<>\\]+", s)
                        if url_m:
                            url = url_m.group(0).rstrip(".,;:)")
                            cls = _classify_legal_url(url)
                            if cls == "pp" and url not in pp_urls:
                                pp_urls.append(url)
                                if verbose:
                                    print(f"    [+] PP URL in DEX: {url[:100]}")
                            elif cls == "tc" and url not in tc_urls:
                                tc_urls.append(url)
                                if verbose:
                                    print(f"    [+] T&C URL in DEX: {url[:100]}")
    except Exception as exc:
        notes.append(f"DEX string scan failed: {exc}")

    # ------------------------------------------------------------------
    # Pass 3 — Activity names from AndroidManifest.xml
    # ------------------------------------------------------------------
    if verbose:
        print("  [*] In-app legal: scanning manifest for legal activities ...")

    if HAS_ANDROGUARD:
        try:
            if not locals().get("apk_obj"):
                apk_obj = AndroguardAPK(apk_path)
            activities = apk_obj.get_activities()
            for act in activities:
                if _LEGAL_ACTIVITY_NAMES.search(act):
                    legal_activities.append(act)
                    if verbose:
                        print(f"    [+] Legal activity: {act}")
        except Exception as exc:
            notes.append(f"Manifest activity scan failed: {exc}")
    else:
        # Fallback: parse aapt2 dump badging output for activities
        try:
            for tool in ("aapt2", "aapt"):
                try:
                    proc = subprocess.run(
                        [tool, "dump", "badging", apk_path],
                        capture_output=True, text=True, timeout=30,
                    )
                    if proc.returncode == 0:
                        for m in re.finditer(
                            r"name='([^']+Activity[^']*)'", proc.stdout,
                        ):
                            act = m.group(1)
                            if _LEGAL_ACTIVITY_NAMES.search(act):
                                legal_activities.append(act)
                                if verbose:
                                    print(f"    [+] Legal activity: {act}")
                        break
                except FileNotFoundError:
                    continue
        except Exception as exc:
            notes.append(f"Manifest activity scan (aapt) failed: {exc}")

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------
    has_pp = bool(pp_urls)
    has_tc = bool(tc_urls)
    has_activities = bool(legal_activities)

    if has_pp or has_tc:
        verdict = "FOUND"
    elif has_activities:
        verdict = "FOUND"
        notes.append(
            "Legal activities detected but no explicit URLs found "
            "— links may be loaded dynamically at runtime"
        )
    elif legal_strings:
        verdict = "POSSIBLY DYNAMIC"
        notes.append(
            "Legal-related string resource keys found but no URLs "
            "— app may load legal content from a remote server"
        )
    else:
        verdict = "NOT FOUND"

    return {
        "in_app_pp_urls": pp_urls,
        "in_app_tc_urls": tc_urls,
        "legal_activities": legal_activities,
        "legal_strings": legal_strings,
        "verdict": verdict,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _get_root_url(url: str) -> str:
    """Extract the root domain URL (scheme + netloc) from any URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_links_from_soup(
    soup, base_url: str, location_suffix: str = "",
) -> tuple[list[LegalLink], list[LegalLink], list[LegalLink]]:
    """Extract legal links from parsed HTML. Returns (tc, pp, other)."""
    tc: list[LegalLink] = []
    pp: list[LegalLink] = []
    other: list[LegalLink] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if _should_skip(href):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)

        text = a.get_text(strip=True)
        loc = _classify_location(a)
        if location_suffix:
            loc = f"{loc}, {location_suffix}"

        if _matches(text, href, TC_KEYWORDS):
            tc.append(LegalLink(text=text, url=full, location=loc))
        elif _matches(text, href, PP_KEYWORDS):
            pp.append(LegalLink(text=text, url=full, location=loc))
        elif _matches(text, href, OTHER_LEGAL_KEYWORDS):
            other.append(LegalLink(text=text, url=full, location=loc))

    return tc, pp, other


def _render_page_js(url: str, verbose: bool = False) -> Optional[str]:
    """Render a page with headless Chromium via Playwright. Returns HTML."""
    if not HAS_PLAYWRIGHT:
        return None
    if verbose:
        print(f"  [*] Rendering {url} with headless browser ...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=20000)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        if verbose:
            print(f"  [!] JS rendering failed: {exc}")
        return None


def _matches(text: str, href: str, keywords: list[str]) -> bool:
    t, h = text.lower().strip(), href.lower()
    return any(kw in t or kw in h for kw in keywords)


def _should_skip(href: str) -> bool:
    if not href:
        return True
    s = href.strip()
    return any(s.startswith(p) for p in SKIP_PREFIXES)


def _classify_location(tag) -> str:
    """Walk up the DOM to determine where a link sits on the page."""
    cur = tag.parent
    while cur and cur.name:
        name = cur.name.lower()
        attrs = " ".join(
            [cur.get("id", ""), " ".join(cur.get("class", []))]
        ).lower()

        if name == "footer" or any(k in attrs for k in ("footer", "foot", "bottom")):
            return "footer"
        if name in ("nav", "header") or any(k in attrs for k in ("nav", "header", "menu")):
            return "nav"
        if name == "aside" or any(k in attrs for k in ("sidebar", "aside")):
            return "sidebar"

        cur = cur.parent
    return "body"


# ---------------------------------------------------------------------------
# Layer 1 – Play Store
# ---------------------------------------------------------------------------

def fetch_play_store(package_name: str, country: str = "",
                     verbose: bool = False) -> Optional[dict]:
    if verbose:
        print(f"  [*] Querying Play Store for {package_name} ...")

    # Try the specified country first, then fall back to common regions
    countries = [country] if country else ["us", "gb", "in", "de", "br"]
    for c in countries:
        try:
            return gp_app(package_name, country=c)
        except NotFoundError:
            continue
        except Exception as exc:
            if verbose:
                print(f"  [!] Play Store error ({c}): {exc}")
            continue
    return None


# ---------------------------------------------------------------------------
# Layer 1b – Data Safety (via Playwright)
# ---------------------------------------------------------------------------

def _make_ds_error(status: str) -> DataSafetyInfo:
    return DataSafetyInfo(
        has_data_safety_section=False,
        collected=[], shared=[], security_practices=[],
        no_data_collected=False, no_data_shared=False,
        status=status,
    )


def _parse_category_groups(cat_groups: list) -> list[DataCategory]:
    """Parse category groups from the Google Play ds:3 data blob."""
    categories: list[DataCategory] = []
    for cg in cat_groups:
        if not isinstance(cg, list) or not cg:
            continue
        meta = cg[0] if isinstance(cg[0], list) else None
        if not meta or len(meta) < 2 or not isinstance(meta[1], str):
            continue

        cat_name = meta[1]
        types_desc = ""
        if len(meta) > 2 and isinstance(meta[2], list) and len(meta[2]) > 1:
            types_desc = meta[2][1] or ""

        data_types: list[str] = []
        purposes: set[str] = set()

        details = cg[4] if len(cg) > 4 and isinstance(cg[4], list) else []
        for detail in details:
            if isinstance(detail, list) and len(detail) >= 3:
                dtype = detail[0] if isinstance(detail[0], str) else ""
                purpose_str = detail[2] if isinstance(detail[2], str) else ""
                if dtype:
                    data_types.append(dtype)
                for p in purpose_str.split(", "):
                    p = p.strip()
                    if p:
                        purposes.add(p)

        if not data_types and types_desc:
            data_types = [
                t.strip()
                for t in re.split(r",\s*and\s+|\s*,\s*", types_desc)
                if t.strip()
            ]

        optional = bool(details) and all(
            isinstance(d, list) and len(d) > 1 and d[1] == 1
            for d in details
        )

        categories.append(DataCategory(
            category=cat_name,
            data_types=data_types,
            purposes=sorted(purposes),
            optional=optional,
        ))
    return categories


def _parse_ds_script(soup) -> Optional[DataSafetyInfo]:
    """Extract structured data from the embedded AF_initDataCallback blob."""
    script = soup.find("script", class_="ds:3")
    if not script or not script.string:
        return None

    match = re.search(
        r"AF_initDataCallback\(\{[^}]*data:(\[.+\]),\s*sideChannel",
        script.string, re.DOTALL,
    )
    if not match:
        return None

    try:
        blob = json.loads(match.group(1))
        entries = blob[1][2]
    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
        return None

    ds_obj = None
    for entry in (entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and "138" in entry:
            ds_obj = entry["138"]
            break

    if not isinstance(ds_obj, list) or len(ds_obj) < 5:
        return None

    collected: list[DataCategory] = []
    shared: list[DataCategory] = []
    security: list[str] = []

    for block in ds_obj:
        if not isinstance(block, list):
            continue

        # Shared / collected sections: block is [section_shared, section_collected]
        # where each section is [cat_groups, "Data shared/collected", ...]
        if (isinstance(block, list) and block
                and isinstance(block[0], list) and len(block[0]) >= 2
                and isinstance(block[0][1], str)
                and block[0][1] in ("Data shared", "Data collected")):
            for section in block:
                if not isinstance(section, list) or len(section) < 2:
                    continue
                name = section[1] if isinstance(section[1], str) else ""
                groups = section[0] if isinstance(section[0], list) else []
                cats = _parse_category_groups(groups)
                if "shared" in name.lower():
                    shared = cats
                elif "collected" in name.lower():
                    collected = cats
            continue

        # Security practices: [img_data, "Security practices", [practices]]
        if (len(block) >= 3 and block[1] == "Security practices"
                and isinstance(block[2], list)):
            for practice in block[2]:
                if (isinstance(practice, list) and len(practice) >= 2
                        and isinstance(practice[1], str)):
                    security.append(practice[1])

    has_data = bool(collected or shared or security)
    return DataSafetyInfo(
        has_data_safety_section=True,
        collected=collected, shared=shared,
        security_practices=security,
        no_data_collected=len(collected) == 0,
        no_data_shared=len(shared) == 0,
        status="COMPLETE" if has_data else "NO_DATA",
    )


def fetch_data_safety(package_name: str,
                      verbose: bool = False) -> Optional[DataSafetyInfo]:
    """Fetch and parse the Google Play Data Safety page via Playwright."""
    if not HAS_PLAYWRIGHT:
        if verbose:
            print("  [!] Playwright not installed \u2014 skipping Data Safety")
        return None

    if not re.match(r"^[a-zA-Z0-9._]+$", package_name):
        return _make_ds_error("PARSE_ERROR")

    if verbose:
        print(f"  [*] Fetching Data Safety section for {package_name} ...")

    url = f"https://play.google.com/store/apps/datasafety?id={package_name}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=20000)
            html = page.content()
            browser.close()
    except Exception as exc:
        if verbose:
            print(f"  [!] Data Safety fetch failed: {exc}")
        return _make_ds_error("PARSE_ERROR")

    soup = BeautifulSoup(html, "html.parser")

    # Primary: parse the embedded ds:3 script data blob
    result = _parse_ds_script(soup)
    if result is not None:
        return result

    # Fallback: detect "no data" declarations from page text
    page_text = soup.get_text(separator=" ").lower()

    no_collect = any(phrase in page_text for phrase in (
        "no data collected",
        "doesn\u2019t collect user data",
        "doesn't collect user data",
    ))
    no_share = any(phrase in page_text for phrase in (
        "no data shared",
        "doesn\u2019t share user data",
        "doesn't share user data",
    ))

    if no_collect or no_share:
        return DataSafetyInfo(
            has_data_safety_section=True,
            collected=[], shared=[], security_practices=[],
            no_data_collected=no_collect, no_data_shared=no_share,
            status="NO_DATA",
        )

    if "we don" in page_text and "information" in page_text:
        return _make_ds_error("MISSING")

    return _make_ds_error("PARSE_ERROR")


# ---------------------------------------------------------------------------
# Layer 2 – Developer website crawl
# ---------------------------------------------------------------------------

def crawl_website(session: requests.Session, url: str, verbose: bool = False):
    """Return (tc_links, pp_links, other_links, accessible, notes)."""
    tc, pp, other, notes = [], [], [], []

    if verbose:
        print(f"  [*] Crawling developer website: {url}")

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        notes.append("SSL certificate error on developer website")
        return tc, pp, other, False, notes
    except requests.exceptions.ConnectionError:
        notes.append("Could not connect to developer website")
        return tc, pp, other, False, notes
    except requests.exceptions.Timeout:
        notes.append("Developer website timed out")
        return tc, pp, other, False, notes
    except requests.exceptions.RequestException as exc:
        notes.append(f"Error fetching developer website: {exc}")
        return tc, pp, other, False, notes

    base_url = resp.url
    soup = BeautifulSoup(resp.text, "html.parser")

    # Detect SPA / JS-rendered sites (tiny shell HTML with no real content)
    body_text = soup.get_text(strip=True)
    html_len = len(resp.text)
    is_spa = html_len < 3000 and len(body_text) < 200
    if is_spa:
        notes.append(
            "Developer website appears to be a JavaScript SPA \u2014 "
            "link crawl may be incomplete; relying on subpage probing"
        )

    tc, pp, other = _extract_links_from_soup(soup, base_url)

    page_text = body_text.lower()
    if not tc and any(kw in page_text for kw in
                      ("terms and conditions", "terms of service", "terms of use")):
        notes.append("T&C keywords found in page text but no dedicated link detected")
    if not pp and "privacy policy" in page_text:
        notes.append("Privacy Policy keywords found in page text but no dedicated link detected")

    return tc, pp, other, True, notes


# ---------------------------------------------------------------------------
# Layer 3 – Subpage probing
# ---------------------------------------------------------------------------

LEGAL_CONTENT_MARKERS = [
    "terms and conditions", "terms of service", "terms of use",
    "user agreement", "end user license", "privacy policy",
    "data protection", "we collect", "we may collect",
    "personal information", "by using this", "you agree to",
]


def probe_subpages(session: requests.Session, base_url: str,
                   verbose: bool = False) -> tuple[list[LegalLink], list[str]]:
    """Probe common legal subpaths using GET requests.

    Compares each response body against the homepage to filter out SPA
    catch-all routes that return the same shell HTML for every path.

    Returns (found_links, notes).
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found: list[LegalLink] = []
    notes: list[str] = []

    if verbose:
        print("  [*] Probing common legal subpages ...")

    # Fetch the homepage body as a baseline for SPA detection
    try:
        home_resp = session.get(origin + "/", timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
        home_body = home_resp.text
        home_len = len(home_body)
    except requests.exceptions.RequestException:
        home_body = ""
        home_len = 0

    for path in SUBPAGE_PATHS:
        probe_url = origin + path
        try:
            resp = session.get(probe_url, timeout=REQUEST_TIMEOUT,
                               allow_redirects=True)
            if resp.status_code >= 400:
                continue

            probe_body = resp.text
            probe_len = len(probe_body)

            # Soft-404 / SPA detection: compare content against homepage
            if home_len > 0:
                ratio = SequenceMatcher(
                    None, probe_body[:2000], home_body[:2000],
                ).ratio()
                if ratio > 0.85:
                    if verbose:
                        print(f"    [~] {path} \u2014 {ratio:.0%} similar to "
                              f"homepage (soft 404), skipping")
                    continue

            # Check if the page has real legal content
            page_lower = BeautifulSoup(probe_body, "html.parser").get_text().lower()
            has_legal_content = any(m in page_lower for m in LEGAL_CONTENT_MARKERS)

            if not has_legal_content:
                if verbose:
                    print(f"    [~] {path} — 200 but no legal content, skipping")
                continue

            if any(k in path for k in ("privacy",)):
                label = "Privacy Policy (probed)"
            else:
                label = "Terms (probed)"

            found.append(LegalLink(
                text=label, url=probe_url,
                location="subpage_probe", verified=True,
            ))
            if verbose:
                print(f"    [+] {path} — legal content confirmed")

        except requests.exceptions.RequestException:
            continue

    if not found and home_len > 0 and home_len < 3000:
        notes.append(
            "Subpage probing found no confirmed legal pages "
            "(site may be a JavaScript SPA requiring a browser)"
        )

    return found, notes


# ---------------------------------------------------------------------------
# Link verification
# ---------------------------------------------------------------------------

def verify_links(session: requests.Session, links: list[LegalLink],
                 verbose: bool = False):
    for link in links:
        if link.verified is not None:
            continue
        try:
            r = session.head(link.url, timeout=REQUEST_TIMEOUT,
                             allow_redirects=True)
            if r.status_code < 400:
                link.verified = True
                continue
            # HEAD blocked — retry with GET (stream to avoid downloading body)
            r = session.get(link.url, timeout=REQUEST_TIMEOUT,
                            allow_redirects=True, stream=True)
            link.verified = r.status_code < 400
            r.close()
        except requests.exceptions.RequestException:
            link.verified = False


# ---------------------------------------------------------------------------
# Verdicts & confidence
# ---------------------------------------------------------------------------

def _set_verdicts(result: LegalCheckResult):
    # In-app legal findings
    iap = result.in_app_legal or {}
    iap_pp = bool(iap.get("in_app_pp_urls"))
    iap_tc = bool(iap.get("in_app_tc_urls"))
    iap_activities = bool(iap.get("legal_activities"))

    # Privacy Policy
    if result.privacy_policy_url:
        result.privacy_policy_verdict = "FOUND (Play Store)"
    elif result.pp_links_on_site:
        best = result.pp_links_on_site[0]
        result.privacy_policy_verdict = (
            f"FOUND (Developer Website - {best.location})"
        )
        result.privacy_policy_url = best.url
    elif iap_pp:
        result.privacy_policy_verdict = "FOUND (In-App)"
        result.privacy_policy_url = iap["in_app_pp_urls"][0]
    else:
        result.privacy_policy_verdict = "NOT FOUND"

    # T&C
    if result.tc_links:
        best = result.tc_links[0]
        result.tc_verdict = (
            f"FOUND (Developer Website - {best.location})"
        )
    elif iap_tc:
        result.tc_verdict = "FOUND (In-App)"
    else:
        result.tc_verdict = "NOT FOUND"

    # Rating
    pp_found = result.privacy_policy_verdict.startswith("FOUND")
    tc_found = result.tc_verdict.startswith("FOUND")
    ds = result.data_safety
    ds_checked = ds is not None
    ds_ok = ds_checked and ds.status in ("COMPLETE", "NO_DATA")

    if ds_checked:
        if pp_found and tc_found and ds_ok:
            result.confidence = "PASS"
        elif pp_found or tc_found or ds_ok:
            result.confidence = "WARNING"
        else:
            result.confidence = "FAIL"
    else:
        if pp_found and tc_found:
            result.confidence = "PASS"
        elif pp_found or tc_found:
            result.confidence = "WARNING"
        elif iap_activities:
            # Legal activity exists but no explicit URLs — still a signal
            result.confidence = "WARNING"
        else:
            result.confidence = "FAIL"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def check_app(package_name: str, session: requests.Session, *,
              verify: bool = True, probe: bool = True,
              datasafety: bool = True, country: str = "",
              dev_url: Optional[str] = None,
              apk_source: Optional[str] = None,
              verbose: bool = False) -> LegalCheckResult:

    result = LegalCheckResult(package_name=package_name, apk_source=apk_source)

    # Layer 1 — Play Store
    data = fetch_play_store(package_name, country=country, verbose=verbose)
    if data is None:
        result.notes.append("App not found on Google Play Store")
    else:
        result.play_store_found = True
        result.app_name = data.get("title")
        result.developer = data.get("developer")
        result.privacy_policy_url = data.get("privacyPolicy") or None
        result.developer_website = data.get("developerWebsite") or None
        result.developer_email = data.get("developerEmail") or None

        if not result.privacy_policy_url:
            result.notes.append("No Privacy Policy URL listed on Play Store")

    # --url override / fallback
    if dev_url and not result.developer_website:
        result.developer_website = dev_url
        if not result.play_store_found:
            result.notes.append(f"Using manually provided URL: {dev_url}")

    # Data Safety (only if app was found on Play Store)
    if datasafety and result.play_store_found:
        result.data_safety = fetch_data_safety(package_name, verbose)
        if result.data_safety is None:
            result.notes.append(
                "Data Safety check skipped (Playwright not installed)"
            )
        elif result.data_safety.status == "MISSING":
            result.notes.append("Data Safety section not found or not completed")
        elif result.data_safety.status == "PARSE_ERROR":
            result.notes.append("Could not parse Data Safety section")
        elif result.data_safety.status == "NO_DATA":
            result.notes.append("App declares no data collected or shared")

    # Plausibility cross-check (APK permissions vs. data safety declaration)
    if result.data_safety and result.apk_source and os.path.isfile(result.apk_source):
        perms = extract_apk_permissions(result.apk_source, verbose)
        if perms is not None:
            if result.data_safety.no_data_collected:
                sensitive = [p for p in perms if p in SENSITIVE_PERMISSIONS]
                if sensitive:
                    result.data_safety.plausibility = "SUSPECT"
                    result.data_safety.suspect_permissions = sensitive
                else:
                    result.data_safety.plausibility = "YES"
            else:
                result.data_safety.plausibility = "YES"

    # Layer 1c — In-app legal link detection (static APK analysis)
    if result.apk_source and os.path.isfile(result.apk_source):
        if verbose:
            print("  [*] Running in-app legal link scan ...")
        result.in_app_legal = scan_apk_legal_links(
            result.apk_source, verbose=verbose,
        )
        if result.in_app_legal["verdict"] == "NOT FOUND":
            result.notes.append(
                "No privacy policy or T&C links detected inside the APK "
                "— legal content may only be accessible via the Play Store listing"
            )
        for note in result.in_app_legal.get("notes", []):
            result.notes.append(f"In-app scan: {note}")

    # Layer 2 — Developer website crawl
    if result.developer_website:
        dev_url = result.developer_website
        root_url = _get_root_url(dev_url)
        urls_to_scan = [dev_url]
        if root_url.rstrip("/") != dev_url.rstrip("/"):
            urls_to_scan.append(root_url)

        # 2a — Requests-based crawl (fast)
        for scan_url in urls_to_scan:
            tc, pp, other, accessible, crawl_notes = crawl_website(
                session, scan_url, verbose,
            )
            if scan_url == dev_url:
                result.website_accessible = accessible
            # Merge without duplicates
            existing = {l.url for l in result.tc_links}
            result.tc_links.extend(l for l in tc if l.url not in existing)
            existing = {l.url for l in result.pp_links_on_site}
            result.pp_links_on_site.extend(l for l in pp if l.url not in existing)
            existing = {l.url for l in result.other_legal_links}
            result.other_legal_links.extend(l for l in other if l.url not in existing)
            result.notes.extend(crawl_notes)

        # 2b — Playwright JS fallback (if no legal links found)
        if not result.tc_links and not result.pp_links_on_site:
            if HAS_PLAYWRIGHT:
                for scan_url in urls_to_scan:
                    rendered = _render_page_js(scan_url, verbose)
                    if rendered:
                        soup = BeautifulSoup(rendered, "html.parser")
                        tc, pp, other = _extract_links_from_soup(
                            soup, scan_url, "JS rendered",
                        )
                        existing = {l.url for l in result.tc_links}
                        result.tc_links.extend(
                            l for l in tc if l.url not in existing)
                        existing = {l.url for l in result.pp_links_on_site}
                        result.pp_links_on_site.extend(
                            l for l in pp if l.url not in existing)
                        existing = {l.url for l in result.other_legal_links}
                        result.other_legal_links.extend(
                            l for l in other if l.url not in existing)
                        if result.tc_links or result.pp_links_on_site:
                            break
            else:
                spa_detected = any("JavaScript SPA" in n for n in result.notes)
                if spa_detected:
                    result.notes.append(
                        "Install playwright for JS rendering: "
                        "pip install playwright && playwright install chromium"
                    )

        # Layer 3 — Subpage probing (fallback when no T&C found)
        if probe and not result.tc_links and result.website_accessible:
            probed, probe_notes = probe_subpages(
                session, result.developer_website, verbose,
            )
            result.notes.extend(probe_notes)
            existing_tc = {l.url for l in result.tc_links}
            existing_pp = {l.url for l in result.pp_links_on_site}
            for link in probed:
                low = link.url.lower()
                if any(k in low for k in ("term", "tos", "eula", "legal")):
                    if link.url not in existing_tc:
                        result.tc_links.append(link)
                elif any(k in low for k in ("privacy",)):
                    if link.url not in existing_pp:
                        result.pp_links_on_site.append(link)
    elif not result.developer_website:
        result.notes.append(
            "No developer website available (use --url to provide one)"
        )

    # Verify links
    if verify:
        if verbose:
            print("  [*] Verifying discovered links ...")
        all_links = result.tc_links + result.pp_links_on_site + result.other_legal_links
        verify_links(session, all_links, verbose)

        broken = [l for l in result.tc_links if l.verified is False]
        if broken:
            result.notes.append(
                f"{len(broken)} T&C link(s) failed verification (may be broken)"
            )

    _set_verdicts(result)
    return result


# ---------------------------------------------------------------------------
# Output — display-width helpers for table alignment with emoji
# ---------------------------------------------------------------------------

_WIDE_CODEPOINTS = {0x2705, 0x274C, 0x26A0, 0x2714, 0x2718}


def _vw(s: str) -> int:
    """Estimate terminal display width of *s*."""
    w = 0
    for ch in s:
        cp = ord(ch)
        if cp == 0xFE0F:
            continue
        if cp >= 0x1F300 or cp in _WIDE_CODEPOINTS:
            w += 2
        else:
            w += 1
    return w


def _vpad(text: str, width: int, align: str = "left") -> str:
    """Pad *text* to a fixed display *width*, accounting for wide chars."""
    pad = max(0, width - _vw(text))
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    if align == "right":
        return " " * pad + text
    return text + " " * pad


# ---------------------------------------------------------------------------
# Output — per-app report
# ---------------------------------------------------------------------------

def print_result(result: LegalCheckResult):
    # ── Header ────────────────────────────────────────────────────────────
    pkg = result.package_name
    if result.apk_source:
        pkg += f"  (from: {os.path.basename(result.apk_source)})"
    app_line = ""
    if result.app_name and result.developer:
        app_line = f"{result.app_name} by {result.developer}"
    elif result.app_name:
        app_line = result.app_name

    print()
    print("  " + "\u2550" * 66)
    print(f"  {pkg}")
    if app_line:
        print(f"  {app_line}")
    print("  " + "\u2550" * 66)
    print()

    # ── Rating (most important info — always at the top) ──────────────────
    r_icon = {"PASS": "\u2705", "WARNING": "\u26a0\ufe0f", "FAIL": "\u274c"
              }.get(result.confidence, "")
    print(f"  RATING: {r_icon} {result.confidence}")
    print()

    # ── Build check-table rows: (label, icon, detail) ─────────────────────
    rows: list[tuple[str, str, str]] = []

    pp_found = result.privacy_policy_verdict.startswith("FOUND")
    if pp_found:
        m = re.search(r"\((.+)\)", result.privacy_policy_verdict)
        pp_detail = m.group(1) if m else "Found"
    else:
        pp_detail = "Not found"
    rows.append(("Privacy Pol.", "\u2705" if pp_found else "\u274c", pp_detail))

    tc_found = result.tc_verdict.startswith("FOUND")
    if tc_found:
        m = re.search(r"\((.+)\)", result.tc_verdict)
        tc_detail = m.group(1) if m else "Found"
    else:
        tc_detail = "Not found"
    rows.append(("Terms & Con.", "\u2705" if tc_found else "\u274c", tc_detail))

    ds = result.data_safety
    if ds is not None:
        ds_map = {
            "COMPLETE":    ("\u2705",       "Complete"),
            "NO_DATA":     ("\u26a0\ufe0f", "No data declared"),
            "MISSING":     ("\u274c",       "Missing"),
            "PARSE_ERROR": ("\u274c",       "Parse error"),
        }
        ds_icon, ds_detail = ds_map.get(ds.status, ("\u274c", "Error"))
    else:
        ds_icon, ds_detail = ("\u2014", "Skipped")
    rows.append(("Data Safety", ds_icon, ds_detail))

    iap = result.in_app_legal
    if iap is not None:
        v = iap.get("verdict", "NOT FOUND")
        if v == "FOUND":
            iap_icon = "\u2705"
            parts = []
            if iap.get("in_app_pp_urls"):
                parts.append("PP")
            if iap.get("in_app_tc_urls"):
                parts.append("T&C")
            if iap.get("legal_activities") and not parts:
                parts.append("Activity detected")
            iap_detail = f"Found ({', '.join(parts)})" if parts else "Found"
        elif v == "POSSIBLY DYNAMIC":
            iap_icon = "\u26a0\ufe0f"
            iap_detail = "Possibly dynamic"
        else:
            iap_icon = "\u274c"
            iap_detail = "Not detected"
    else:
        iap_icon, iap_detail = ("\u2014", "Skipped (no APK)")
    rows.append(("In-App Legal", iap_icon, iap_detail))

    # Column content-widths (padding spaces added by _row)
    W1, W2 = 13, 6
    W3 = max(20, max(len(d) for _, _, d in rows))

    def _sep(l, m, r):
        return (f"  {l}{'─' * (W1 + 2)}{m}"
                f"{'─' * (W2 + 2)}{m}{'─' * (W3 + 2)}{r}")

    def _row(c1, c2, c3):
        return (f"  │ {_vpad(c1, W1)} │ "
                f"{_vpad(c2, W2, 'center')} │ {_vpad(c3, W3)} │")

    print(_sep("\u250c", "\u252c", "\u2510"))
    print(_row("Check", "Status", "Detail"))
    print(_sep("\u251c", "\u253c", "\u2524"))
    for label, icon, detail in rows:
        print(_row(label, icon, detail))
    print(_sep("\u2514", "\u2534", "\u2518"))

    # ── URLs (compact) ────────────────────────────────────────────────────
    urls: list[tuple[str, str]] = []
    if result.privacy_policy_url:
        urls.append(("PP ", result.privacy_policy_url))
    for link in result.tc_links:
        urls.append(("T&C", link.url))
    # In-app URLs
    iap = result.in_app_legal or {}
    for u in iap.get("in_app_pp_urls", []):
        if not any(u == existing for _, existing in urls):
            urls.append(("PP (in-app)", u))
    for u in iap.get("in_app_tc_urls", []):
        if not any(u == existing for _, existing in urls):
            urls.append(("T&C (in-app)", u))
    if urls:
        print()
        print("  URLs:")
        for label, url in urls:
            print(f"    {label} \u2192 {url}")

    # In-app legal activities
    if iap.get("legal_activities"):
        print()
        print("  In-App Legal Activities:")
        for act in iap["legal_activities"]:
            print(f"    \u2192 {act}")

    # ── Developer info (one line) ─────────────────────────────────────────
    dev_parts: list[str] = []
    if result.developer_website:
        dev_parts.append(
            urlparse(result.developer_website).netloc
            or result.developer_website
        )
    if result.developer_email:
        dev_parts.append(result.developer_email)
    if dev_parts:
        print(f"\n  Developer: {' | '.join(dev_parts)}")

    # ── Data Safety details (only when there is meaningful data) ──────────
    if ds and ds.status == "COMPLETE" and (
        ds.collected or ds.shared or ds.security_practices
    ):
        print()
        print("  Data Safety:")
        if ds.collected:
            items = []
            for dc in ds.collected:
                t = f" ({', '.join(dc.data_types)})" if dc.data_types else ""
                items.append(f"{dc.category}{t}")
            print(f"    Collects: {', '.join(items)}")
        if ds.shared:
            items = []
            for dc in ds.shared:
                t = f" ({', '.join(dc.data_types)})" if dc.data_types else ""
                items.append(f"{dc.category}{t}")
            print(f"    Shares:   {', '.join(items)}")
        if ds.security_practices:
            print(f"    Security: {', '.join(ds.security_practices)}")
    elif ds and ds.status == "NO_DATA":
        print('\n  Data Safety: "This app doesn\'t collect user data"')
        print("    Verify this is plausible given the app's permissions")

    if ds and ds.plausibility == "SUSPECT" and ds.suspect_permissions:
        perms = ", ".join(ds.suspect_permissions)
        print(f"\n  \u26a0 Plausibility: SUSPECT \u2014 requests {perms}")

    # ── Notes (only shown when something is wrong) ────────────────────────
    if result.notes:
        print()
        print("  Notes:")
        for note in result.notes:
            print(f"    \u26a0 {note}")

    print()


# ---------------------------------------------------------------------------
# Output — batch summary table
# ---------------------------------------------------------------------------

def print_summary_table(results: list[LegalCheckResult]):
    """Compact summary table printed BEFORE detailed per-app reports."""
    table_rows: list[tuple[str, str, str, str, str, str]] = []
    for i, r in enumerate(results, 1):
        name = r.app_name or r.package_name
        if r.developer:
            label = f"{name} ({r.developer.split()[0].rstrip(',')})"
        else:
            label = name

        pp_ok = r.privacy_policy_verdict.startswith("FOUND")
        tc_ok = r.tc_verdict.startswith("FOUND")
        d = r.data_safety
        if d is None:
            ds_icon = "\u2014"
        elif d.status in ("COMPLETE", "NO_DATA"):
            ds_icon = "\u2705"
        else:
            ds_icon = "\u274c"

        rating_short = {"PASS": "PASS", "WARNING": "WARN", "FAIL": "FAIL"
                        }.get(r.confidence, r.confidence)

        table_rows.append((
            str(i),
            label,
            "\u2705" if pp_ok else "\u274c",
            "\u2705" if tc_ok else "\u274c",
            ds_icon,
            rating_short,
        ))

    # Dynamic app-name column width
    NW = max(3, len(str(len(results))))
    AW = min(34, max(20, max(len(r[1]) for r in table_rows)))
    table_rows = [
        (n, (a[: AW - 3] + "..." if len(a) > AW else a), *rest)
        for n, a, *rest in table_rows
    ]
    PW, TW, DW, RW = 4, 4, 12, 8
    widths = [NW, AW, PW, TW, DW, RW]
    headers = ["#", "App", "PP", "T&C", "Data Safety", "Rating"]

    def _sep(l, m, r):
        return "  " + l + m.join("\u2500" * (w + 2) for w in widths) + r

    def _row(cells):
        parts = [f" {_vpad(c, w)} " for c, w in zip(cells, widths)]
        return "  \u2502" + "\u2502".join(parts) + "\u2502"

    print()
    print("  RESULTS SUMMARY")
    print(_sep("\u250c", "\u252c", "\u2510"))
    print(_row(headers))
    print(_sep("\u251c", "\u253c", "\u2524"))
    for row in table_rows:
        print(_row(row))
    print(_sep("\u2514", "\u2534", "\u2518"))

    total = len(results)
    n_pass = sum(1 for r in results if r.confidence == "PASS")
    n_warn = sum(1 for r in results if r.confidence == "WARNING")
    n_fail = sum(1 for r in results if r.confidence == "FAIL")
    print(f"  PASS: {n_pass}/{total} | WARN: {n_warn}/{total} | FAIL: {n_fail}/{total}")
    print()


def export_csv(results: list[LegalCheckResult], path: str):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "Package Name", "APK Source", "App Name", "Developer",
            "Privacy Policy Verdict", "Privacy Policy URL",
            "T&C Verdict", "T&C URLs",
            "Data Safety Status", "Data Collected", "Data Shared",
            "Security Practices", "Data Collection Plausible",
            "Developer Website", "Developer Email",
            "Rating", "Notes",
        ])
        for r in results:
            ds = r.data_safety
            if ds and ds.collected:
                collected_str = " | ".join(
                    f"{dc.category} ({', '.join(dc.data_types)})"
                    if dc.data_types else dc.category
                    for dc in ds.collected
                )
            else:
                collected_str = ""
            if ds and ds.shared:
                shared_str = " | ".join(
                    f"{dc.category} ({', '.join(dc.data_types)})"
                    if dc.data_types else dc.category
                    for dc in ds.shared
                )
            else:
                shared_str = ""
            writer.writerow([
                r.package_name,
                r.apk_source or "",
                r.app_name or "",
                r.developer or "",
                r.privacy_policy_verdict,
                r.privacy_policy_url or "",
                r.tc_verdict,
                " | ".join(l.url for l in r.tc_links),
                ds.status if ds else "SKIPPED",
                collected_str,
                shared_str,
                " | ".join(ds.security_practices) if ds else "",
                ds.plausibility if ds else "N/A",
                r.developer_website or "",
                r.developer_email or "",
                r.confidence,
                " | ".join(r.notes),
            ])
    print(f"  Results exported to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "App Legal Compliance Checker \u2014 "
            "checks Privacy Policy & T&C for Android apps"
        ),
    )
    p.add_argument(
        "packages", nargs="*",
        help="Package names or APK file paths (auto-detected)",
    )
    p.add_argument(
        "--file", "-f", metavar="FILE",
        help="File with package names / APK paths (one per line, # = comment)",
    )
    p.add_argument(
        "--csv", metavar="FILE", dest="csv_file",
        help="Export results to a CSV file",
    )
    p.add_argument(
        "--no-verify", action="store_true",
        help="Skip link verification (faster)",
    )
    p.add_argument(
        "--no-probe", action="store_true",
        help="Skip subpage probing",
    )
    p.add_argument(
        "--no-datasafety", action="store_true",
        help="Skip Data Safety section check",
    )
    p.add_argument(
        "--country", metavar="CC", default="",
        help="Play Store country code (e.g. us, gb, in). "
             "Default: tries us, gb, in, de, br",
    )
    p.add_argument(
        "--url", metavar="URL", dest="dev_url",
        help="Developer website URL (fallback when Play Store has no listing)",
    )
    p.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay in seconds between apps (default: 1)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show progress details",
    )
    return p


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args()

    packages: list[str] = list(args.packages) if args.packages else []

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        packages.append(line)
        except FileNotFoundError:
            print(f"Error: file not found: {args.file}")
            sys.exit(1)

    if not packages:
        print("Error: no package names provided. Use positional args or --file.")
        sys.exit(1)

    session = create_session()
    results: list[LegalCheckResult] = []

    for idx, entry in enumerate(packages):
        if idx > 0:
            time.sleep(args.delay)

        pkg, apk_src = resolve_input(entry, args.verbose)
        if pkg is None:
            continue

        if args.verbose:
            print(f"\n[{idx + 1}/{len(packages)}] Checking {pkg} ...")

        result = check_app(
            pkg, session,
            verify=not args.no_verify,
            probe=not args.no_probe,
            datasafety=not args.no_datasafety,
            country=args.country,
            dev_url=args.dev_url,
            apk_source=apk_src,
            verbose=args.verbose,
        )
        results.append(result)

    # Summary table first (batch mode) — find failures at a glance
    if len(results) > 1:
        print_summary_table(results)

    # Detailed per-app reports
    for result in results:
        print_result(result)

    if args.csv_file:
        export_csv(results, args.csv_file)


if __name__ == "__main__":
    main()

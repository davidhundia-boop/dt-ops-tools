#!/usr/bin/env python3
"""
App Category Classifier

Classifies an Android app into a Play Store main category and a specific
sub-category, prioritising 14 high-priority DT labels.

Confidence levels returned:
  strong     — keyword in app name, genre exact match, or 2+ desc keyword hits
  weak       — single keyword in description only (reported as "Potentially X")
  genre_only — no rule matched; sub-category derived from Play Store genre ID
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

_NAME_KW     = 5   # keyword found in app name
_DESC_KW     = 2   # keyword found in description (max 3 counted per rule)
_GENRE_EXACT = 5   # genreId is in the rule's genre_exact list
_GENRE_HINT  = 2   # genreId matches a genre_hint
_PERM        = 1   # Android permission associated with category

STRONG_THRESHOLD = 5   # >= 5  → strong
WEAK_THRESHOLD   = 2   # 2–4  → weak / "Potentially X"


# ---------------------------------------------------------------------------
# Sub-category rules
# ---------------------------------------------------------------------------
# genre_exact  : genreId substrings that alone are a strong signal
# genre_hints  : genreId substrings that add weight but are not definitive
# name_keywords: regex patterns matched against the app NAME
# desc_keywords: regex patterns matched against the app DESCRIPTION
# permissions  : Android permission names that add minor signal

RULES: list[dict] = [
    {
        "name": "AI App",
        "genre_exact": [],
        "genre_hints": ["productivity", "tools"],
        "name_keywords": [
            r"\bai\b", r"\bgpt\b", r"\bllm\b",
            r"\bchatbot\b", r"\bartificial intelligence\b",
        ],
        "desc_keywords": [
            r"\bai\b", r"\bartificial intelligence\b", r"\bchatbot\b",
            r"\bgpt\b", r"\bllm\b", r"\bgenerative ai\b",
            r"\bai assistant\b", r"\bai chat\b", r"\bai.?powered\b",
            r"\bchat with ai\b", r"\bask ai\b", r"\bai writing\b",
            r"\bai image\b", r"\bai model\b",
        ],
        "permissions": [],
    },
    {
        "name": "Gambling - Sport Betting",
        "genre_exact": [],
        "genre_hints": ["sports"],
        "name_keywords": [
            r"\bbet(365|way|fair|mgm|fred)?\b",
            r"\bsportsbook\b", r"\bwager\b", r"\bodds\b",
        ],
        "desc_keywords": [
            r"\bbet(ting|s)?\b", r"\bsportsbook\b", r"\bodds\b",
            r"\bwager(ing)?\b", r"\bin.?play bet\b",
            r"\blive bet(ting)?\b", r"\bsports? bet(ting)?\b",
            r"\bfootball bet\b",
        ],
        "permissions": [],
    },
    {
        "name": "Gambling - Casino",
        "genre_exact": ["casino"],
        "genre_hints": ["entertainment"],
        "name_keywords": [
            r"\bcasino\b", r"\bslots?\b", r"\bpoker\b",
            r"\bblackjack\b", r"\broulette\b",
        ],
        "desc_keywords": [
            r"\bcasino\b", r"\bslot machine\b", r"\bpoker\b",
            r"\bblackjack\b", r"\broulette\b", r"\bspin to win\b",
            r"\bjackpot\b", r"\breal money (game|casino|win)\b",
        ],
        "permissions": [],
    },
    {
        "name": "Caller ID App",
        "genre_exact": [],
        "genre_hints": ["communication", "tools"],
        "name_keywords": [
            r"\bcaller\b", r"\bcall (block|screen|filter)\b",
            r"\bwho called\b", r"\bphone lookup\b",
        ],
        "desc_keywords": [
            r"\bcaller\s*id\b", r"\bspam (call|caller)\b",
            r"\bcall (block|screen|filter|detect)\b",
            r"\bwho called\b", r"\breverse (phone|number)\b",
            r"\bunknown caller\b", r"\bphone lookup\b",
        ],
        "permissions": ["READ_PHONE_STATE", "READ_CALL_LOG"],
    },
    {
        "name": "Launcher",
        "genre_exact": [],
        "genre_hints": ["personalization"],
        "name_keywords": [r"\blauncher\b"],
        "desc_keywords": [
            r"\blauncher\b", r"\bhome\s*screen launcher\b",
            r"\bapp drawer\b", r"\breplace (your )?home screen\b",
            r"\bcustomize (your )?home screen\b",
        ],
        "permissions": [],
    },
    {
        "name": "Dating App",
        "genre_exact": ["dating"],
        "genre_hints": ["lifestyle", "social"],
        "name_keywords": [
            r"\bdating\b", r"\bmatch\b", r"\btinder\b",
            r"\bbumble\b", r"\bhinge\b",
        ],
        "desc_keywords": [
            r"\bdating\b", r"\bswipe (right|left|to match)\b",
            r"\bmeet singles\b", r"\bfind (love|a partner|a date)\b",
            r"\bhookup\b", r"\bdate (online|app)\b",
            r"\bmatch(making)?\b",
        ],
        "permissions": [],
    },
    {
        "name": "Religion App",
        "genre_exact": [],
        "genre_hints": ["lifestyle", "books_and_reference"],
        "name_keywords": [
            r"\bbible\b", r"\bquran\b", r"\bkoran\b",
            r"\bprayer\b", r"\bchurch\b", r"\bchristian\b",
        ],
        "desc_keywords": [
            r"\bbible\b", r"\bquran\b", r"\bkoran\b",
            r"\bprayer(s)?\b", r"\bchurch\b", r"\bchristian\b",
            r"\bisla(m|mic)\b", r"\bmuslim\b", r"\bjewish\b",
            r"\btorah\b", r"\bdevotional\b", r"\bworship\b",
            r"\bsermon\b", r"\bholy (scripture|bible|book)\b",
            r"\bhindu\b", r"\bbuddh(a|ist|ism)\b",
        ],
        "permissions": [],
    },
    {
        "name": "Lockscreen App",
        "genre_exact": [],
        "genre_hints": ["personalization", "tools"],
        "name_keywords": [r"\block\s*screen\b", r"\blockscreen\b"],
        "desc_keywords": [
            r"\block\s*screen\b", r"\bscreen lock(er)?\b",
            r"\blockscreen\b",
            r"\bwallpaper (on|for) (your )?lock screen\b",
        ],
        "permissions": [],
    },
    {
        "name": "Browser",
        "genre_exact": [],
        "genre_hints": ["communication", "tools"],
        "name_keywords": [r"\bbrowser\b"],
        "desc_keywords": [
            r"\bweb browser\b", r"\bbrowse the (internet|web)\b",
            r"\binternet browser\b", r"\btabbed browsing\b",
        ],
        "permissions": [],
    },
    {
        "name": "Rewarded Play App",
        "genre_exact": [],
        "genre_hints": ["entertainment", "game"],
        "name_keywords": [
            r"\bearn\b", r"\breward(s)?\b", r"\bcashback\b",
        ],
        "desc_keywords": [
            r"\bearn (rewards?|money|cash|points|gift cards?)\b",
            r"\bget paid\b", r"\breward(ed)? (play|games?|app)\b",
            r"\bcashback\b", r"\bplay (and|to) earn\b",
            r"\bwin (prize|cash|money|rewards?)\b",
            r"\bpoints? (for|to) redeem\b",
            r"\bgift card(s)? reward\b",
        ],
        "permissions": [],
    },
    {
        "name": "Financial - Loan App",
        "genre_exact": [],
        "genre_hints": ["finance"],
        "name_keywords": [
            r"\bloan\b", r"\bcash advance\b",
            r"\binstant (cash|money)\b",
        ],
        "desc_keywords": [
            r"\bloan(s)?\b", r"\bborrow(ing)?\b",
            r"\blend(ing|er)?\b", r"\bpayday\b",
            r"\bcash advance\b", r"\binstallment loan\b",
            r"\binstant (cash|money)\b", r"\bpersonal loan\b",
            r"\bapply for (a )?(loan|credit)\b",
        ],
        "permissions": [],
    },
    {
        "name": "VPN",
        "genre_exact": [],
        "genre_hints": ["tools", "communication"],
        "name_keywords": [r"\bvpn\b", r"\bproxy\b"],
        "desc_keywords": [
            r"\bvpn\b", r"\bvirtual private network\b",
            r"\bproxy\b", r"\bencrypt (your |connection|traffic)\b",
            r"\bhide (your )?(ip|location|identity)\b",
            r"\banonymous(ly)? (browse|browsing|surf)\b",
        ],
        "permissions": [],
    },
    {
        "name": "Security - Antivirus",
        "genre_exact": [],
        "genre_hints": ["tools"],
        "name_keywords": [
            r"\bantivirus\b", r"\banti.?virus\b", r"\bmalware\b",
        ],
        "desc_keywords": [
            r"\bantivirus\b", r"\banti.?virus\b",
            r"\bmalware\b", r"\bvirus (scan|protect|detect|remov)\b",
            r"\bsecurity scan\b", r"\bthreat (detect|protect)\b",
            r"\bphone (guard|protect)\b",
        ],
        "permissions": [],
    },
    {
        "name": "Health App",
        "genre_exact": ["health_and_fitness", "medical"],
        "genre_hints": [],
        "name_keywords": [
            r"\bhealth\b", r"\bfitness\b", r"\bworkout\b",
            r"\bmedical\b", r"\bdiet\b", r"\bwellness\b",
        ],
        "desc_keywords": [
            r"\bhealth\b", r"\bfitness\b", r"\bworkout\b",
            r"\bmedical\b", r"\bdoctor\b", r"\bsymptom(s)?\b",
            r"\bbmi\b", r"\bcalorie(s)?\b",
            r"\bdiet\b", r"\bnutrition\b",
            r"\bmental health\b", r"\btherapy\b",
            r"\bmeditation\b", r"\byoga\b", r"\bwellness\b",
        ],
        "permissions": ["BODY_SENSORS"],
    },
]


# ---------------------------------------------------------------------------
# Play Store genreId → (main_category, fallback_sub_category)
# ---------------------------------------------------------------------------

_GENRE_MAP: dict[str, tuple[str, str]] = {
    # Games
    "GAME_ACTION":       ("Games", "Game - Action"),
    "GAME_ADVENTURE":    ("Games", "Game - Adventure"),
    "GAME_ARCADE":       ("Games", "Game - Arcade"),
    "GAME_BOARD":        ("Games", "Game - Board"),
    "GAME_CARD":         ("Games", "Game - Card"),
    "GAME_CASINO":       ("Games", "Game - Casino"),
    "GAME_CASUAL":       ("Games", "Game - Casual"),
    "GAME_EDUCATIONAL":  ("Games", "Game - Educational"),
    "GAME_MUSIC":        ("Games", "Game - Music"),
    "GAME_PUZZLE":       ("Games", "Game - Puzzle"),
    "GAME_RACING":       ("Games", "Game - Racing"),
    "GAME_ROLE_PLAYING": ("Games", "Game - Role Playing"),
    "GAME_SIMULATION":   ("Games", "Game - Simulation"),
    "GAME_SPORTS":       ("Games", "Game - Sports"),
    "GAME_STRATEGY":     ("Games", "Game - Strategy"),
    "GAME_TRIVIA":       ("Games", "Game - Trivia"),
    "GAME_WORD":         ("Games", "Game - Word"),
    # Apps
    "ART_AND_DESIGN":       ("Art & Design",       "Art & Design"),
    "AUTO_AND_VEHICLES":    ("Auto & Vehicles",    "Auto & Vehicles"),
    "BEAUTY":               ("Beauty",             "Beauty"),
    "BOOKS_AND_REFERENCE":  ("Books & Reference",  "Books & Reference"),
    "BUSINESS":             ("Business",           "Business"),
    "COMICS":               ("Comics",             "Comics"),
    "COMMUNICATION":        ("Communication",      "Communication"),
    "DATING":               ("Dating",             "Dating App"),
    "EDUCATION":            ("Education",          "Education"),
    "ENTERTAINMENT":        ("Entertainment",      "Entertainment"),
    "EVENTS":               ("Events",             "Events"),
    "FINANCE":              ("Finance",            "Finance"),
    "FOOD_AND_DRINK":       ("Food & Drink",       "Food & Drink"),
    "HEALTH_AND_FITNESS":   ("Health & Fitness",   "Health & Fitness"),
    "HOUSE_AND_HOME":       ("House & Home",       "House & Home"),
    "LIBRARIES_AND_DEMO":   ("Libraries & Demo",   "Libraries & Demo"),
    "LIFESTYLE":            ("Lifestyle",          "Lifestyle"),
    "MAPS_AND_NAVIGATION":  ("Maps & Navigation",  "Maps & Navigation"),
    "MEDICAL":              ("Medical",            "Medical"),
    "MUSIC_AND_AUDIO":      ("Music & Audio",      "Music & Audio"),
    "NEWS_AND_MAGAZINES":   ("News & Magazines",   "News & Magazines"),
    "PARENTING":            ("Parenting",          "Parenting"),
    "PERSONALIZATION":      ("Personalization",    "Personalization"),
    "PHOTOGRAPHY":          ("Photography",        "Photography"),
    "PRODUCTIVITY":         ("Productivity",       "Productivity"),
    "SHOPPING":             ("Shopping",           "Shopping"),
    "SOCIAL":               ("Social",             "Social"),
    "SPORTS":               ("Sports",             "Sports"),
    "TOOLS":                ("Tools",              "Tools"),
    "TRAVEL_AND_LOCAL":     ("Travel & Local",     "Travel & Local"),
    "VIDEO_PLAYERS":        ("Video Players",      "Video Player"),
    "WEATHER":              ("Weather",            "Weather"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_app(
    package_name: str = "",
    app_name: str = "",
    description: str = "",
    genre: str = "",
    genre_id: str = "",
    permissions: list[str] | None = None,
) -> dict:
    """
    Classify an app into main_category + sub_category.

    Returns:
        {
            "main_category": str,           # e.g. "Sports"
            "sub_category": str | None,     # e.g. "Gambling - Sport Betting"
            "confidence": str,              # "strong" | "weak" | "genre_only"
            "signals": list[str],           # what triggered the match
        }
    """
    if permissions is None:
        permissions = []

    genre_id_upper = (genre_id or "").upper()
    genre_lower    = (genre_id or genre or "").lower()

    mapped = _GENRE_MAP.get(genre_id_upper)
    main_category    = mapped[0] if mapped else (genre or _fmt_genre_id(genre_id) or "Unknown")
    genre_fallback   = mapped[1] if mapped else (genre or _fmt_genre_id(genre_id) or None)

    name_lower = (app_name or "").lower()
    desc_lower = (description or "").lower()[:4000]   # cap for performance

    results: list[tuple[int, str, list[str]]] = []

    for rule in RULES:
        score   = 0
        signals: list[str] = []

        # Genre exact
        for exact in rule["genre_exact"]:
            if exact.lower() in genre_lower:
                score += _GENRE_EXACT
                signals.append(f"genre:{exact}")
                break

        # Genre hint
        for hint in rule["genre_hints"]:
            if hint.lower() in genre_lower:
                score += _GENRE_HINT
                signals.append(f"genre_hint:{hint}")
                break

        # Name keywords (first match wins, but score counts once)
        for pattern in rule["name_keywords"]:
            m = re.search(pattern, name_lower)
            if m:
                score += _NAME_KW
                signals.append(f"name:{m.group(0)}")
                break

        # Description keywords (up to 3 counted)
        desc_hits = 0
        for pattern in rule["desc_keywords"]:
            if desc_hits >= 3:
                break
            m = re.search(pattern, desc_lower)
            if m:
                score += _DESC_KW
                signals.append(f"desc:{m.group(0)}")
                desc_hits += 1

        # Permission hints
        for perm in rule["permissions"]:
            if perm in permissions:
                score += _PERM
                signals.append(f"perm:{perm}")

        if score >= WEAK_THRESHOLD:
            results.append((score, rule["name"], signals))

    if results:
        results.sort(key=lambda x: -x[0])
        best_score, best_name, best_signals = results[0]
        confidence = "strong" if best_score >= STRONG_THRESHOLD else "weak"
        return {
            "main_category": main_category,
            "sub_category":  best_name,
            "confidence":    confidence,
            "signals":       best_signals,
        }

    # No rule matched — use Play Store genre as sub-category
    return {
        "main_category": main_category,
        "sub_category":  genre_fallback,
        "confidence":    "genre_only",
        "signals":       [f"genre_id:{genre_id}"] if genre_id else [],
    }


def _fmt_genre_id(genre_id: str) -> str:
    """'HEALTH_AND_FITNESS' → 'Health & Fitness'."""
    if not genre_id:
        return ""
    return genre_id.replace("_AND_", " & ").replace("_", " ").title()

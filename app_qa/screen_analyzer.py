#!/usr/bin/env python3
"""
Screen Analyzer — combines OCR text detection with OpenCV visual element
detection to find interactive UI elements in Android screenshots.

Designed for game-engine UIs where UI Automator cannot see elements.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum

import cv2
import numpy as np

logger = logging.getLogger(__name__)

ADB = "adb"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class ElementType(Enum):
    TEXT = "text"
    ICON = "icon"
    BUTTON = "button"


@dataclass
class ScreenElement:
    """A detected visual or text element on the screen."""
    element_type: ElementType
    label: str
    confidence: float
    x1: int
    y1: int
    x2: int
    y2: int
    center_x: int = field(init=False)
    center_y: int = field(init=False)

    def __post_init__(self):
        self.center_x = (self.x1 + self.x2) // 2
        self.center_y = (self.y1 + self.y2) // 2

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        return self.width * self.height

    def __repr__(self) -> str:
        return (f'{self.element_type.value}("{self.label}" '
                f'@ ({self.center_x},{self.center_y}) '
                f'{self.width}x{self.height} conf={self.confidence:.2f})')


# ---------------------------------------------------------------------------
# Screen position helpers
# ---------------------------------------------------------------------------

class ScreenRegion(Enum):
    TOP_LEFT = "top_left"
    TOP_CENTER = "top_center"
    TOP_RIGHT = "top_right"
    CENTER = "center"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_CENTER = "bottom_center"
    BOTTOM_RIGHT = "bottom_right"


def classify_region(el: ScreenElement, screen_w: int, screen_h: int) -> ScreenRegion:
    cx_frac = el.center_x / screen_w
    cy_frac = el.center_y / screen_h

    if cy_frac < 0.2:
        if cx_frac < 0.33:
            return ScreenRegion.TOP_LEFT
        elif cx_frac > 0.66:
            return ScreenRegion.TOP_RIGHT
        return ScreenRegion.TOP_CENTER
    elif cy_frac > 0.8:
        if cx_frac < 0.33:
            return ScreenRegion.BOTTOM_LEFT
        elif cx_frac > 0.66:
            return ScreenRegion.BOTTOM_RIGHT
        return ScreenRegion.BOTTOM_CENTER
    return ScreenRegion.CENTER


# ---------------------------------------------------------------------------
# Visual element detection (OpenCV)
# ---------------------------------------------------------------------------

def _detect_visual_elements(image_path: str,
                            min_area: int = 1500,
                            max_area: int = 80000) -> list[ScreenElement]:
    """Detect icon-like and button-like visual elements using contour analysis.

    Finds distinct shapes (buttons, icons, badges) that stand out from
    the background by their edges and enclosed area.
    """
    img = cv2.imread(image_path)
    if img is None:
        return []

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements: list[ScreenElement] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue

        x, y, cw, ch = cv2.boundingRect(cnt)
        aspect = cw / ch if ch > 0 else 0

        # Skip elements that are full-width (likely backgrounds/headers)
        if cw > w * 0.8:
            continue
        # Skip very thin lines
        if cw < 20 or ch < 20:
            continue

        if 0.6 <= aspect <= 1.6:
            el_type = ElementType.ICON
            label = f"icon_{len(elements)}"
        elif aspect > 1.6:
            el_type = ElementType.BUTTON
            label = f"button_{len(elements)}"
        else:
            el_type = ElementType.ICON
            label = f"tall_icon_{len(elements)}"

        # Compute a confidence proxy from how "clean" the contour is
        perimeter = cv2.arcLength(cnt, True)
        circularity = 4 * np.pi * area / (perimeter * perimeter) if perimeter > 0 else 0
        conf = min(circularity * 1.5, 1.0)

        elements.append(ScreenElement(
            element_type=el_type,
            label=label,
            confidence=round(conf, 2),
            x1=x, y1=y, x2=x + cw, y2=y + ch,
        ))

    return elements


def _detect_icons_by_template(image_path: str,
                              screen_w: int, screen_h: int) -> list[ScreenElement]:
    """Detect common UI icons by analyzing isolated small shapes in corner regions.

    Instead of template matching (which requires icon images), this looks for
    small, high-contrast, roughly-square elements in typical icon positions.
    """
    img = cv2.imread(image_path)
    if img is None:
        return []

    h, w = img.shape[:2]
    elements: list[ScreenElement] = []

    corner_regions = [
        ("top_right", int(w * 0.7), 0, w, int(h * 0.15)),
        ("top_left", 0, 0, int(w * 0.3), int(h * 0.15)),
        ("bottom_right", int(w * 0.7), int(h * 0.85), w, h),
        ("bottom_left", 0, int(h * 0.85), int(w * 0.3), h),
    ]

    for region_name, rx1, ry1, rx2, ry2 in corner_regions:
        crop = img[ry1:ry2, rx1:rx2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blurred, 50, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 800 or area > 30000:
                continue

            x, y, cw, ch = cv2.boundingRect(cnt)
            aspect = cw / ch if ch > 0 else 0
            if aspect < 0.5 or aspect > 2.0:
                continue
            if cw < 15 or ch < 15:
                continue

            abs_x1, abs_y1 = rx1 + x, ry1 + y
            abs_x2, abs_y2 = abs_x1 + cw, abs_y1 + ch

            perimeter = cv2.arcLength(cnt, True)
            circularity = 4 * np.pi * area / (perimeter * perimeter) if perimeter > 0 else 0

            label = f"corner_icon_{region_name}"
            elements.append(ScreenElement(
                element_type=ElementType.ICON,
                label=label,
                confidence=round(min(circularity * 1.5, 1.0), 2),
                x1=abs_x1, y1=abs_y1,
                x2=abs_x2, y2=abs_y2,
            ))

    return elements


# ---------------------------------------------------------------------------
# OCR text detection
# ---------------------------------------------------------------------------

_ocr_reader = None


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _ocr_reader


def _detect_text_elements(image_path: str,
                          min_confidence: float = 0.10) -> list[ScreenElement]:
    """Run EasyOCR on original + enhanced image and return text elements."""
    from PIL import Image, ImageEnhance

    reader = _get_ocr_reader()

    img_pil = Image.open(image_path)
    w_orig, h_orig = img_pil.size

    # Run on original
    results_orig = reader.readtext(image_path, low_text=0.3, text_threshold=0.4)

    # Run on enhanced (2x upscale + contrast)
    enhanced_path = image_path + ".enh.png"
    try:
        big = img_pil.resize((w_orig * 2, h_orig * 2), Image.LANCZOS)
        big = ImageEnhance.Contrast(big).enhance(1.8)
        big = ImageEnhance.Sharpness(big).enhance(2.0)
        big.save(enhanced_path)
        results_enh = reader.readtext(enhanced_path, low_text=0.3, text_threshold=0.4)
    except Exception:
        results_enh = []
    finally:
        if os.path.exists(enhanced_path):
            os.unlink(enhanced_path)

    seen: set[str] = set()
    elements: list[ScreenElement] = []

    for results, scale in [(results_orig, 1), (results_enh, 2)]:
        for bbox, text, conf in results:
            if conf < min_confidence:
                continue
            text_clean = text.strip()
            if not text_clean:
                continue
            key = text_clean.lower()
            if key in seen:
                continue
            seen.add(key)

            x1 = int(bbox[0][0]) // scale
            y1 = int(bbox[0][1]) // scale
            x2 = int(bbox[2][0]) // scale
            y2 = int(bbox[2][1]) // scale

            elements.append(ScreenElement(
                element_type=ElementType.TEXT,
                label=text_clean,
                confidence=conf,
                x1=x1, y1=y1, x2=x2, y2=y2,
            ))

    return elements


# ---------------------------------------------------------------------------
# Combined analysis
# ---------------------------------------------------------------------------

def _deduplicate(elements: list[ScreenElement],
                 overlap_threshold: float = 0.5) -> list[ScreenElement]:
    """Remove overlapping detections, preferring higher confidence."""
    elements.sort(key=lambda e: e.confidence, reverse=True)
    keep: list[ScreenElement] = []
    for el in elements:
        overlaps = False
        for kept in keep:
            ix1 = max(el.x1, kept.x1)
            iy1 = max(el.y1, kept.y1)
            ix2 = min(el.x2, kept.x2)
            iy2 = min(el.y2, kept.y2)
            if ix1 < ix2 and iy1 < iy2:
                intersection = (ix2 - ix1) * (iy2 - iy1)
                smaller_area = min(el.area, kept.area)
                if smaller_area > 0 and intersection / smaller_area > overlap_threshold:
                    overlaps = True
                    break
        if not overlaps:
            keep.append(el)
    keep.sort(key=lambda e: (e.y1, e.x1))
    return keep


def analyze_screen(image_path: str, run_ocr: bool = True) -> list[ScreenElement]:
    """Full screen analysis: visual element detection + OCR.

    Args:
        image_path: Path to screenshot PNG.
        run_ocr: Whether to also run OCR (slower but finds text labels).

    Returns:
        Deduplicated list of all detected elements, sorted top-to-bottom.
    """
    all_elements: list[ScreenElement] = []

    img = cv2.imread(image_path)
    if img is None:
        return []
    screen_h, screen_w = img.shape[:2]

    visual = _detect_visual_elements(image_path)
    all_elements.extend(visual)

    corner_icons = _detect_icons_by_template(image_path, screen_w, screen_h)
    all_elements.extend(corner_icons)

    if run_ocr:
        text_els = _detect_text_elements(image_path)
        all_elements.extend(text_els)

    return _deduplicate(all_elements)


def analyze_emulator_screen(run_ocr: bool = True) -> tuple[list[ScreenElement], str]:
    """Take a fresh screenshot from the emulator and analyze it.

    Returns:
        (elements, screenshot_path) — the screenshot is kept for reference.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()

    subprocess.run(["adb", "shell", "screencap", "-p", "/sdcard/_sa_tmp.png"],
                   capture_output=True, timeout=10)
    subprocess.run(["adb", "pull", "/sdcard/_sa_tmp.png", tmp_path],
                   capture_output=True, timeout=10)
    subprocess.run(["adb", "shell", "rm", "/sdcard/_sa_tmp.png"],
                   capture_output=True, timeout=5)

    elements = analyze_screen(tmp_path, run_ocr=run_ocr)
    return elements, tmp_path


# ---------------------------------------------------------------------------
# High-level actions
# ---------------------------------------------------------------------------

def find_by_keywords(elements: list[ScreenElement],
                     *keywords: str) -> list[ScreenElement]:
    """Find TEXT elements matching any keyword (case-insensitive)."""
    matches = []
    for el in elements:
        if el.element_type != ElementType.TEXT:
            continue
        label_lower = el.label.lower()
        if any(kw.lower() in label_lower for kw in keywords):
            matches.append(el)
    matches.sort(key=lambda e: e.confidence, reverse=True)
    return matches


def find_by_region(elements: list[ScreenElement],
                   region: ScreenRegion,
                   screen_w: int = 1080,
                   screen_h: int = 2400,
                   element_type: ElementType | None = None) -> list[ScreenElement]:
    """Find elements in a specific screen region."""
    matches = []
    for el in elements:
        if element_type and el.element_type != element_type:
            continue
        if classify_region(el, screen_w, screen_h) == region:
            matches.append(el)
    matches.sort(key=lambda e: e.confidence, reverse=True)
    return matches


def find_settings_icon(elements: list[ScreenElement],
                       screen_w: int = 1080,
                       screen_h: int = 2400) -> ScreenElement | None:
    """Find the most likely settings/gear icon (typically top-right corner)."""
    # First check for text matches
    text_matches = find_by_keywords(elements, "settings", "setting", "gear", "options")
    if text_matches:
        return text_matches[0]

    # Look for icons in top-right corner (most common position for settings)
    top_right = find_by_region(
        elements, ScreenRegion.TOP_RIGHT, screen_w, screen_h, ElementType.ICON)
    if top_right:
        return top_right[0]

    # Fallback: top-left
    top_left = find_by_region(
        elements, ScreenRegion.TOP_LEFT, screen_w, screen_h, ElementType.ICON)
    if top_left:
        return top_left[0]

    return None


def find_close_or_dismiss(elements: list[ScreenElement]) -> ScreenElement | None:
    """Find close/dismiss/X buttons."""
    text_matches = find_by_keywords(
        elements, "close", "dismiss", "x", "skip", "ok", "got it",
        "continue", "accept", "allow", "no thanks", "not now")
    if text_matches:
        return text_matches[0]
    return None


def find_navigation_targets(elements: list[ScreenElement]) -> list[ScreenElement]:
    """Find elements likely to lead deeper into the app (support, about, etc.)."""
    return find_by_keywords(
        elements,
        "support", "about", "info", "help",
        "privacy", "privacy policy",
        "terms", "terms of service", "terms and conditions",
        "legal", "eula", "menu", "more",
    )


def tap_element(el: ScreenElement) -> None:
    """Tap the center of an element on the emulator."""
    subprocess.run(
        [ADB, "shell", "input", "tap", str(el.center_x), str(el.center_y)],
        capture_output=True, timeout=10,
    )
    logger.info("Tapped %s at (%d, %d)", el.label, el.center_x, el.center_y)

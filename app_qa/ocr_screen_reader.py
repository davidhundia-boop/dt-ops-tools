#!/usr/bin/env python3
"""
OCR-based screen reader for Android emulator screenshots.

Uses EasyOCR to extract text and bounding-box coordinates from screenshots,
enabling interaction with game-engine UIs where UI Automator cannot see elements.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


@dataclass
class OcrElement:
    text: str
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

    def __repr__(self) -> str:
        return (f'OcrElement("{self.text}" @ ({self.center_x},{self.center_y}) '
                f'conf={self.confidence:.2f})')


def _preprocess_for_ocr(image_path: str) -> str:
    """Enhance screenshot for better OCR: upscale + sharpen + contrast boost.

    Returns path to the preprocessed image (temp file).
    """
    from PIL import Image, ImageEnhance, ImageFilter

    img = Image.open(image_path)
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = ImageEnhance.Sharpness(img).enhance(2.0)

    out_path = image_path + ".enhanced.png"
    img.save(out_path)
    return out_path


def read_screen(image_path: str, min_confidence: float = 0.10,
                enhance: bool = True) -> list[OcrElement]:
    """Run OCR on a screenshot and return detected text elements.

    Args:
        image_path: Path to a PNG/JPEG screenshot file.
        min_confidence: Minimum confidence threshold (0.0–1.0).
        enhance: Whether to preprocess the image for better OCR accuracy.

    Returns:
        List of OcrElement sorted by vertical position (top to bottom).
    """
    reader = _get_reader()

    targets = [image_path]
    enhanced_path = None
    if enhance:
        try:
            enhanced_path = _preprocess_for_ocr(image_path)
            targets.append(enhanced_path)
        except Exception:
            pass

    seen_texts: set[str] = set()
    elements: list[OcrElement] = []

    for idx, target in enumerate(targets):
        scale = 2 if idx == 1 else 1
        raw = reader.readtext(target, low_text=0.3, text_threshold=0.4)
        for bbox, text, conf in raw:
            if conf < min_confidence:
                continue
            text_clean = text.strip()
            if not text_clean:
                continue
            x1 = int(bbox[0][0]) // scale
            y1 = int(bbox[0][1]) // scale
            x2 = int(bbox[2][0]) // scale
            y2 = int(bbox[2][1]) // scale
            key = text_clean.lower()
            if key in seen_texts:
                continue
            seen_texts.add(key)
            elements.append(OcrElement(
                text=text_clean,
                confidence=conf,
                x1=x1, y1=y1, x2=x2, y2=y2,
            ))

    if enhanced_path and os.path.exists(enhanced_path):
        os.unlink(enhanced_path)

    elements.sort(key=lambda e: (e.y1, e.x1))
    return elements


def read_emulator_screen(min_confidence: float = 0.15) -> list[OcrElement]:
    """Take a fresh screenshot from the connected emulator and run OCR.

    Returns:
        List of OcrElement from the current screen.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["adb", "shell", "screencap", "-p", "/sdcard/_ocr_tmp.png"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["adb", "pull", "/sdcard/_ocr_tmp.png", tmp_path],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["adb", "shell", "rm", "/sdcard/_ocr_tmp.png"],
            capture_output=True, timeout=5,
        )
        return read_screen(tmp_path, min_confidence)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def find_text(elements: list[OcrElement], *keywords: str,
              case_sensitive: bool = False) -> list[OcrElement]:
    """Find elements whose text contains any of the given keywords.

    Args:
        elements: OCR results from read_screen / read_emulator_screen.
        *keywords: One or more substrings to search for.
        case_sensitive: Whether the match is case-sensitive.

    Returns:
        Matching elements sorted by confidence (highest first).
    """
    matches: list[OcrElement] = []
    for el in elements:
        text = el.text if case_sensitive else el.text.lower()
        for kw in keywords:
            target = kw if case_sensitive else kw.lower()
            if target in text:
                matches.append(el)
                break
    matches.sort(key=lambda e: e.confidence, reverse=True)
    return matches


def find_and_tap(
    *keywords: str,
    min_confidence: float = 0.15,
    case_sensitive: bool = False,
) -> OcrElement | None:
    """Take a screenshot, find text matching keywords, and tap the best match.

    Returns the tapped element, or None if no match found.
    """
    elements = read_emulator_screen(min_confidence)
    matches = find_text(elements, *keywords, case_sensitive=case_sensitive)
    if not matches:
        logger.info("OCR find_and_tap: no match for %s", keywords)
        return None

    best = matches[0]
    logger.info("OCR tapping '%s' at (%d, %d) conf=%.2f",
                best.text, best.center_x, best.center_y, best.confidence)
    subprocess.run(
        ["adb", "shell", "input", "tap", str(best.center_x), str(best.center_y)],
        capture_output=True, timeout=10,
    )
    return best


def dump_screen_text() -> str:
    """Take a screenshot and return all visible text as a single string."""
    elements = read_emulator_screen(min_confidence=0.10)
    return " ".join(el.text for el in elements)

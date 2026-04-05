"""
Vision Agent — LLM-driven Android app navigation for legal content verification.

Architecture:
  - Persistent ADB shell (one process, many commands) → ~0.02s per action
  - Screenshot via exec-out or pull fallback → ~0.5-1.5s per capture
  - LLM (Gemini) sees screenshot, decides next action → ~1-3s per call
  - No XML parsing — LLM is both sensor and navigator
  - Target: ~3-5s per iteration, ~20-60s per app
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from google import genai
from google.genai import types as genai_types

log = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"
_PNG_MAGIC = b"\x89PNG"


# ---------------------------------------------------------------------------
# Persistent ADB shell — eliminates per-command process spawn overhead
# ---------------------------------------------------------------------------

class PersistentShell:
    """Single long-lived `adb shell` process. Commands complete in ~0.1s
    instead of ~3-4s with separate subprocess calls."""

    def __init__(self, serial: str | None = None):
        cmd = ["adb"]
        if serial:
            cmd += ["-s", serial]
        cmd += ["shell"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._q: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._lock = threading.Lock()
        self._n = 0
        time.sleep(0.3)
        self._drain()

    def _read_loop(self):
        try:
            for raw_line in iter(self._proc.stdout.readline, b""):
                self._q.put(raw_line.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def _drain(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def run(self, command: str, timeout: float = 10.0) -> str:
        with self._lock:
            self._n += 1
            marker = f"_M{self._n}_"
            self._drain()
            self._proc.stdin.write(f"{command}\necho {marker}\n".encode())
            self._proc.stdin.flush()
            lines: list[str] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    line = self._q.get(timeout=min(0.5, deadline - time.time()))
                    if marker in line:
                        return "\n".join(lines)
                    lines.append(line.rstrip("\r\n"))
                except queue.Empty:
                    continue
            return "\n".join(lines)

    def fire(self, command: str) -> None:
        """Send command, don't wait for output."""
        with self._lock:
            self._drain()
            self._proc.stdin.write(f"{command}\n".encode())
            self._proc.stdin.flush()

    def close(self):
        try:
            self._proc.stdin.write(b"exit\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


# ---------------------------------------------------------------------------
# VisionAgent
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an automated QA agent navigating an Android app via an emulator.
Your mission: verify that Privacy Policy (PP) and Terms & Conditions (T&C) \
are accessible somewhere in the app's UI.

Phases (you manage these yourself based on what you see):
1. ONBOARDING: Tap through permission dialogs, consent screens, tutorials. \
   Tap Skip / Continue / Accept / Don't allow / OK — whatever advances. \
   If you see a Google/Facebook sign-in popup, press back.
2. LEGAL SEARCH: Once on the main screen, navigate to Settings / Profile / \
   About / Legal / Privacy menus. Look for PP and T&C links or text.
3. LINKED PAGE: If you found PP but not T&C (or vice versa), tap the found \
   link and scroll down — the other is often in the footer of the same page.

Rules:
- found_pp / found_tc = true when you SEE the words on screen (title, link, or body text).
- If the only options are email/password/Sign-in with no skip/guest, declare login_wall.
- Never tap ads.
- action "done" when you've found both, or exhausted navigation options.

Respond with ONLY valid JSON (no markdown fences):
{"action":"tap|back|swipe_down|done|login_wall",\
"target":[x,y],"reasoning":"brief",\
"found_pp":false,"found_tc":false}"""


class VisionAgent:
    def __init__(
        self,
        package: str,
        screenshot_dir: str,
        api_key: str,
        model: str = "gemini-2.5-flash",
        device_serial: str | None = None,
    ):
        self.package = package
        self.screenshot_dir = screenshot_dir
        self.device_serial = device_serial
        self._ss_n = 0

        os.makedirs(screenshot_dir, exist_ok=True)
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.shell = PersistentShell(device_serial)

    # --- fast primitives ------------------------------------------------

    def screenshot(self) -> bytes:
        """Capture PNG screenshot. Uses exec-out (fast) with pull fallback
        for Windows where binary pipe gets CR/LF corrupted."""
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += ["exec-out", "screencap", "-p"]
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        if r.stdout[:4] == _PNG_MAGIC:
            return r.stdout
        log.debug("exec-out returned invalid PNG (%d bytes), using pull fallback", len(r.stdout))
        return self._screenshot_pull()

    def _screenshot_pull(self) -> bytes:
        """Fallback: screencap on device → adb pull → read bytes."""
        remote = "/sdcard/_vqa_screen.png"
        self.shell.fire(f"screencap -p {remote}")
        time.sleep(0.3)
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, "s.png")
            cmd = ["adb"]
            if self.device_serial:
                cmd += ["-s", self.device_serial]
            cmd += ["pull", remote, local]
            subprocess.run(cmd, capture_output=True, timeout=10)
            if os.path.exists(local):
                return Path(local).read_bytes()
        return b""

    def _save(self, raw: bytes, label: str) -> str:
        self._ss_n += 1
        p = os.path.join(
            self.screenshot_dir,
            f"{self.package}_{label}_{self._ss_n:03d}.png",
        )
        Path(p).write_bytes(raw)
        return p

    @staticmethod
    def _downscale(raw: bytes) -> bytes:
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((540, 1200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def tap(self, x: int, y: int):
        self.shell.fire(f"input tap {x} {y}")

    def back(self):
        self.shell.fire("input keyevent 4")

    def swipe_down(self):
        self.shell.fire("input swipe 540 1800 540 600 500")

    def foreground(self) -> str:
        out = self.shell.run(
            "dumpsys activity activities | grep -i resumed | head -1", timeout=5,
        )
        m = re.search(r"u0 ([a-zA-Z0-9_.]+)/", out)
        return m.group(1) if m else ""

    # --- LLM call -------------------------------------------------------

    def _ask(self, img_bytes: bytes, context: str) -> dict:
        prompt = f"{_SYSTEM_PROMPT}\n\nContext: {context}"
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                genai_types.Part.from_text(prompt),
            ],
        )
        raw = (response.text or "").strip()
        if not raw:
            return self._default_response("empty LLM response")
        raw = re.sub(r"^```(?:json)?\s*", "", raw).rstrip("`").strip()
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return self._default_response(f"bad JSON: {raw[:80]}")
        t = d.get("target", [0, 0])
        if not isinstance(t, list) or len(t) < 2:
            t = [0, 0]
        d["target"] = [int(t[0]), int(t[1])]
        d.setdefault("action", "done")
        d.setdefault("found_pp", False)
        d.setdefault("found_tc", False)
        d.setdefault("reasoning", "")
        return d

    @staticmethod
    def _default_response(reason: str) -> dict:
        return {
            "action": "done", "target": [0, 0],
            "reasoning": reason, "found_pp": False, "found_tc": False,
        }

    # --- main loop ------------------------------------------------------

    def run(self, max_steps: int = 20, max_seconds: int = 90) -> dict:
        pp = False
        tc = False
        pp_path: list[str] = []
        tc_path: list[str] = []
        login_wall = False
        screenshots: list[str] = []
        t0 = time.time()

        for step in range(max_steps):
            elapsed = time.time() - t0
            if elapsed > max_seconds or (pp and tc):
                break

            raw = self.screenshot()
            if len(raw) < 1000:
                time.sleep(0.5)
                continue

            small = self._downscale(raw)
            fg = self.foreground()

            ctx = (
                f"App: {self.package} | Foreground: {fg} | "
                f"PP: {'FOUND' if pp else 'missing'} | "
                f"T&C: {'FOUND' if tc else 'missing'} | "
                f"Step {step+1}/{max_steps} | {elapsed:.0f}s elapsed"
            )

            try:
                d = self._ask(small, ctx)
            except Exception as exc:
                log.warning("LLM error step %d: %s", step + 1, exc)
                time.sleep(1)
                continue

            log.info(
                "Step %d (%.1fs): %s — %s",
                step + 1, time.time() - t0, d.get("action"), d.get("reasoning", ""),
            )

            if d["found_pp"] and not pp:
                pp = True
                pp_path = [d.get("reasoning", "vision")]
                screenshots.append(self._save(raw, "pp"))
            if d["found_tc"] and not tc:
                tc = True
                tc_path = [d.get("reasoning", "vision")]
                screenshots.append(self._save(raw, "tc"))

            action = d["action"]
            if action == "done":
                break
            if action == "login_wall":
                login_wall = True
                screenshots.append(self._save(raw, "login_wall"))
                break
            if action == "tap":
                x, y = d["target"]
                if x > 0 and y > 0:
                    self.tap(x, y)
            elif action == "back":
                self.back()
            elif action == "swipe_down":
                self.swipe_down()

            time.sleep(0.3)  # minimum settle time after action

        if not screenshots:
            raw = self.screenshot()
            if len(raw) > 1000:
                screenshots.append(self._save(raw, "final"))

        self.shell.close()
        return _format(pp, tc, pp_path, tc_path, login_wall, screenshots, time.time() - t0)


def _format(
    pp: bool, tc: bool,
    pp_path: list[str], tc_path: list[str],
    login_wall: bool, screenshots: list[str], elapsed: float,
) -> dict:
    def _item(found: bool, path: list[str]) -> dict:
        if found:
            return {
                "ui_found": True, "ui_path": path,
                "ui_method": "vision_agent",
                "ui_url": None,
                "screenshot": screenshots[-1] if screenshots else None,
                "notes": [],
            }
        return {
            "ui_found": False, "ui_path": [],
            "ui_method": None, "ui_url": None,
            "screenshot": screenshots[-1] if screenshots else None,
            "notes": ["Blocked by login wall"] if login_wall else [],
        }

    return {
        "privacy_policy": _item(pp, pp_path),
        "terms_and_conditions": _item(tc, tc_path),
        "navigation_info": {
            "onboarding_dismissed": not login_wall,
            "login_wall": login_wall,
            "navigation_time_seconds": round(elapsed, 1),
            "screenshots": screenshots,
        },
    }

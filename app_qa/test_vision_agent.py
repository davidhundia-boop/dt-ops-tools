#!/usr/bin/env python3
"""
Test harness for VisionAgent — validates the full pipeline with timing.

Usage:
  # With API key (real LLM):
  set GEMINI_API_KEY=your-key
  python test_vision_agent.py "C:\\path\\to\\flashscore.apk"

  # Dry-run (no LLM — validates screenshot, shell, timing only):
  python test_vision_agent.py --dry-run

Prints per-step and total timing to evaluate against the 20-60s target.
"""
import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from vision_agent import PersistentShell, VisionAgent, _PNG_MAGIC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _adb(args: list[str], timeout: int = 30):
    return subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=timeout)


def benchmark_shell():
    """Measure PersistentShell vs subprocess speed."""
    fire_cmds = [
        "input tap 540 1200",
        "input keyevent 4",
        "input tap 100 100",
        "input tap 540 1200",
        "input keyevent 4",
    ]
    run_cmd = "dumpsys activity activities | grep -i resumed | head -1"

    # subprocess baseline (fire-type commands)
    t0 = time.time()
    for c in fire_cmds:
        subprocess.run(["adb", "shell"] + c.split(), capture_output=True, timeout=10)
    t_sub_fire = time.time() - t0

    # subprocess baseline (run-type command)
    t0 = time.time()
    subprocess.run(["adb", "shell"] + run_cmd.split(), capture_output=True, timeout=10)
    t_sub_run = time.time() - t0

    # persistent shell
    shell = PersistentShell()

    t0 = time.time()
    for c in fire_cmds:
        shell.fire(c)
    t_ps_fire = time.time() - t0

    time.sleep(0.5)  # let fire commands complete on device

    t0 = time.time()
    shell.run(run_cmd, timeout=10)
    t_ps_run = time.time() - t0

    shell.close()

    n_fire = len(fire_cmds)
    log.info("=== Shell Benchmark ===")
    log.info("  Actions (tap/back) × %d:", n_fire)
    log.info("    subprocess:    %.3fs total, %.4fs/cmd", t_sub_fire, t_sub_fire / n_fire)
    log.info("    PS fire():     %.3fs total, %.4fs/cmd", t_ps_fire, t_ps_fire / n_fire)
    log.info("    Speedup: %.0fx", t_sub_fire / t_ps_fire if t_ps_fire > 0 else 0)
    log.info("  Query (foreground):")
    log.info("    subprocess:    %.3fs", t_sub_run)
    log.info("    PS run():      %.3fs", t_ps_run)
    return t_sub_fire / n_fire, t_ps_fire / n_fire


def benchmark_screenshot(serial: str | None = None):
    """Measure screenshot capture time both methods."""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]

    # exec-out
    t0 = time.time()
    r = subprocess.run(cmd + ["exec-out", "screencap", "-p"], capture_output=True, timeout=10)
    t_exec = time.time() - t0
    ok_exec = r.stdout[:4] == _PNG_MAGIC

    # pull fallback
    subprocess.run(cmd + ["shell", "screencap", "-p", "/sdcard/_bench_ss.png"],
                   capture_output=True, timeout=10)
    t0 = time.time()
    r2 = subprocess.run(cmd + ["pull", "/sdcard/_bench_ss.png", os.path.join(os.environ.get("TEMP", "/tmp"), "_bench_ss.png")],
                        capture_output=True, timeout=10)
    t_pull = time.time() - t0

    log.info("=== Screenshot Benchmark ===")
    log.info("  exec-out: %.2fs (%d bytes, valid PNG: %s)", t_exec, len(r.stdout), ok_exec)
    log.info("  pull:     %.2fs", t_pull)
    if not ok_exec:
        log.warning("  exec-out produces INVALID PNG on this platform — pull fallback will be used")
    return ok_exec, t_exec, t_pull


def dry_run():
    """No API key needed — tests infra speed only."""
    log.info("=" * 60)
    log.info("DRY RUN — no LLM calls, testing infra timing only")
    log.info("=" * 60)

    sub_per, ps_per = benchmark_shell()
    ok_exec, t_exec, t_pull = benchmark_screenshot()

    ss_time = t_exec if ok_exec else t_pull + 0.3  # pull has extra screencap time
    settle = 0.3  # post-action settle
    llm_est = 2.0  # estimated Gemini Flash latency
    estimated_per_step = ps_per + ss_time + settle + llm_est
    estimated_7_steps = estimated_per_step * 7
    estimated_20_steps = estimated_per_step * 20

    log.info("")
    log.info("=== Projected Agent Timing ===")
    log.info("  Per step: action(%.4fs) + screenshot(%.2fs) + settle(%.1fs) + LLM(~%.0fs) = ~%.1fs",
             ps_per, ss_time, settle, llm_est, estimated_per_step)
    log.info("  Easy app (7 steps):  ~%.0fs", estimated_7_steps)
    log.info("  Hard app (20 steps): ~%.0fs", estimated_20_steps)

    target_met = estimated_7_steps <= 60
    log.info("  Target (20-60s): %s", "LIKELY MET" if target_met else "AT RISK")


def full_test(apk_path: str, api_key: str, serial: str | None = None):
    """Full VisionAgent run with timing instrumentation."""
    from in_app_legal_verifier import install_apk, _detect_package_name, uninstall_app

    log.info("=" * 60)
    log.info("FULL TEST: %s", os.path.basename(apk_path))
    log.info("=" * 60)

    pkg = _detect_package_name(apk_path)
    if not pkg:
        log.error("Could not detect package name")
        return

    log.info("Package: %s", pkg)
    log.info("Installing...")
    t0 = time.time()

    _adb(["shell", "am", "force-stop", pkg])
    uninstall_app(pkg)

    if not install_apk(apk_path):
        log.error("Install failed")
        return
    log.info("Installed in %.1fs", time.time() - t0)

    log.info("Launching...")
    t_launch = time.time()
    _adb(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"])
    time.sleep(3)
    log.info("Launched in %.1fs", time.time() - t_launch)

    ss_dir = os.path.join(os.path.dirname(__file__), "screenshots", "test_run", pkg)
    os.makedirs(ss_dir, exist_ok=True)

    log.info("Starting VisionAgent...")
    t_agent = time.time()

    agent = VisionAgent(
        package=pkg,
        screenshot_dir=ss_dir,
        api_key=api_key,
        device_serial=serial,
    )
    result = agent.run()
    agent_time = time.time() - t_agent

    pp = result.get("privacy_policy", {})
    tc = result.get("terms_and_conditions", {})
    nav = result.get("navigation_info", {})

    log.info("")
    log.info("=== RESULTS ===")
    log.info("  Privacy Policy:     %s", "FOUND" if pp.get("ui_found") else "NOT FOUND")
    log.info("  Terms & Conditions: %s", "FOUND" if tc.get("ui_found") else "NOT FOUND")
    if nav.get("login_wall"):
        log.info("  BLOCKED: Login wall")
    log.info("  Agent time: %.1fs", agent_time)
    log.info("  Internal time: %.1fs", nav.get("navigation_time_seconds", 0))
    log.info("  Screenshots: %s", nav.get("screenshots", []))

    pp_ok = pp.get("ui_found", False)
    tc_ok = tc.get("ui_found", False)
    if pp_ok and tc_ok:
        verdict = "PASS"
    elif nav.get("login_wall"):
        verdict = "INCONCLUSIVE (login wall)"
    elif pp_ok or tc_ok:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    log.info("  Verdict: %s", verdict)
    target_met = agent_time <= 60
    log.info("  Target (≤60s): %s", "MET" if target_met else "MISSED (%.0fs over)" % (agent_time - 60))

    uninstall_app(pkg)
    return result


def main():
    parser = argparse.ArgumentParser(description="VisionAgent test harness")
    parser.add_argument("apk", nargs="?", help="Path to APK file")
    parser.add_argument("--dry-run", action="store_true", help="Benchmark infra only, no LLM")
    parser.add_argument("--serial", help="ADB device serial")
    args = parser.parse_args()

    r = _adb(["devices"])
    if "device" not in r.stdout:
        log.error("No emulator/device connected")
        return

    if args.dry_run or not args.apk:
        dry_run()
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("Set GEMINI_API_KEY env var for full test (or use --dry-run)")
        return

    full_test(args.apk, api_key, args.serial)


if __name__ == "__main__":
    main()

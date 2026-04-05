"""Tests for in_app_legal_verifier ADB utilities."""
import pytest
from unittest.mock import patch, MagicMock
from in_app_legal_verifier import (
    check_device_connected,
    classify_dismiss_action,
    compute_verdict,
    DISMISS_PATTERNS,
    find_elements_by_keywords,
    find_legal_screens_from_elements,
    install_apk,
    is_game_canvas,
    launch_app,
    parse_ui_elements,
    uninstall_app,
    UiElement,
    verify_legal_content,
)

SAMPLE_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="Settings" resource-id="com.example:id/settings_btn"
        class="android.widget.TextView" package="com.example"
        content-desc="" clickable="true" bounds="[100,200][300,250]" />
  <node index="1" text="Privacy Policy" resource-id="com.example:id/pp_link"
        class="android.widget.TextView" package="com.example"
        content-desc="" clickable="true" bounds="[100,300][400,350]" />
  <node index="2" text="Play Game" resource-id="com.example:id/play_btn"
        class="android.widget.Button" package="com.example"
        content-desc="" clickable="true" bounds="[100,400][400,450]" />
  <node index="3" text="" resource-id="com.example:id/gear"
        class="android.widget.ImageView" package="com.example"
        content-desc="Settings" clickable="true" bounds="[900,50][950,100]" />
</hierarchy>'''


class TestParseUiElements:
    def test_extracts_clickable_elements(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        assert len(elements) == 4
        assert elements[0].text == "Settings"
        assert elements[0].clickable is True

    def test_extracts_bounds(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        settings = elements[0]
        assert settings.center_x == 200
        assert settings.center_y == 225


class TestFindElementsByKeywords:
    def test_finds_priority_1_legal_link(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        matches = find_elements_by_keywords(elements, priority=1)
        assert len(matches) == 1
        assert matches[0].text == "Privacy Policy"

    def test_finds_priority_2_settings(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        matches = find_elements_by_keywords(elements, priority=2)
        assert len(matches) == 2  # "Settings" text + gear icon content-desc
        texts = {m.text or m.content_desc for m in matches}
        assert "Settings" in texts

    def test_no_matches_for_priority_4(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        matches = find_elements_by_keywords(elements, priority=4)
        assert len(matches) == 0


class TestClassifyDismissAction:
    def test_skip_button_detected(self):
        el = UiElement(text="Skip", content_desc="", resource_id="",
                       class_name="android.widget.Button", clickable=True,
                       bounds_raw="[100,100][200,150]")
        action = classify_dismiss_action(el)
        assert action == "skip"

    def test_accept_button_detected(self):
        el = UiElement(text="I Agree", content_desc="", resource_id="",
                       class_name="android.widget.Button", clickable=True,
                       bounds_raw="[100,100][200,150]")
        action = classify_dismiss_action(el)
        assert action == "consent"

    def test_login_wall_detected(self):
        el = UiElement(text="Sign In", content_desc="", resource_id="",
                       class_name="android.widget.Button", clickable=True,
                       bounds_raw="[100,100][200,150]")
        action = classify_dismiss_action(el)
        assert action == "login_wall"

    def test_irrelevant_element_returns_none(self):
        el = UiElement(text="Play Game", content_desc="", resource_id="",
                       class_name="android.widget.Button", clickable=True,
                       bounds_raw="[100,100][200,150]")
        action = classify_dismiss_action(el)
        assert action is None


GAME_CANVAS_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.view.SurfaceView"
        package="com.example.game" content-desc="" clickable="false"
        bounds="[0,0][1080,1920]" />
</hierarchy>'''

NORMAL_UI_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="Settings" resource-id="com.example:id/btn"
        class="android.widget.Button" package="com.example"
        content-desc="" clickable="true" bounds="[100,200][300,250]" />
</hierarchy>'''


class TestIsGameCanvas:
    def test_detects_surface_view_only(self):
        assert is_game_canvas(GAME_CANVAS_HIERARCHY) is True

    def test_normal_ui_is_not_game_canvas(self):
        assert is_game_canvas(NORMAL_UI_HIERARCHY) is False


SETTINGS_THEN_LEGAL_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="Settings" resource-id="" class="android.widget.TextView"
        package="com.example" content-desc="" clickable="true"
        bounds="[100,200][300,250]" />
  <node index="1" text="Share" resource-id="" class="android.widget.TextView"
        package="com.example" content-desc="" clickable="true"
        bounds="[100,300][300,350]" />
</hierarchy>'''


class TestFindLegalScreensFromElements:
    def test_direct_legal_link_found(self):
        elements = parse_ui_elements(SAMPLE_HIERARCHY)
        result = find_legal_screens_from_elements(elements)
        assert result.pp_element is not None
        assert result.pp_element.text == "Privacy Policy"

    def test_settings_entry_found_when_no_direct(self):
        elements = parse_ui_elements(SETTINGS_THEN_LEGAL_HIERARCHY)
        result = find_legal_screens_from_elements(elements)
        assert result.pp_element is None
        assert result.entry_point is not None
        assert result.entry_point.text == "Settings"


WEBVIEW_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.webkit.WebView"
        package="com.example" content-desc="Privacy Policy" clickable="false"
        bounds="[0,0][1080,1920]" />
</hierarchy>'''

TEXT_LEGAL_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="Privacy Policy" resource-id="" class="android.widget.TextView"
        package="com.example" content-desc="" clickable="false"
        bounds="[50,100][500,140]" />
  <node index="1" text="We collect personal information to provide our services.
Third parties may access your data. You agree to our data processing terms."
        resource-id="" class="android.widget.TextView"
        package="com.example" content-desc="" clickable="false"
        bounds="[50,160][1000,800]" />
</hierarchy>'''

NO_LEGAL_HIERARCHY = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="General Settings" resource-id="" class="android.widget.TextView"
        package="com.example" content-desc="" clickable="false"
        bounds="[50,100][500,140]" />
</hierarchy>'''


class TestVerifyLegalContent:
    def test_webview_detected(self):
        result = verify_legal_content(WEBVIEW_HIERARCHY, "pp")
        assert result["verified"] is True
        assert result["method"] == "webview"

    def test_text_content_detected(self):
        result = verify_legal_content(TEXT_LEGAL_HIERARCHY, "pp")
        assert result["verified"] is True
        assert result["method"] == "text_content"

    def test_no_legal_content(self):
        result = verify_legal_content(NO_LEGAL_HIERARCHY, "pp")
        assert result["verified"] is False


class TestCheckDeviceConnected:
    @patch("in_app_legal_verifier.subprocess.run")
    def test_returns_true_when_device_present(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n\n",
        )
        assert check_device_connected() is True

    @patch("in_app_legal_verifier.subprocess.run")
    def test_returns_false_when_no_device(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\n\n",
        )
        assert check_device_connected() is False


class TestInstallApk:
    @patch("in_app_legal_verifier.subprocess.run")
    def test_install_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Success\n")
        assert install_apk("/tmp/app.apk") is True

    @patch("in_app_legal_verifier.subprocess.run")
    def test_install_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="Failure\n", stderr="error")
        assert install_apk("/tmp/app.apk") is False


class TestLaunchApp:
    @patch("in_app_legal_verifier.subprocess.run")
    def test_launch_sends_monkey_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="u0 com.example.app/com.example.app.MainActivity\n  mResumedActivity",
        )
        result = launch_app("com.example.app", wait_timeout=5)
        calls = [c[0][0] for c in mock_run.call_args_list]
        monkey_call = [c for c in calls if "monkey" in c]
        assert len(monkey_call) >= 1
        assert "com.example.app" in monkey_call[0]


class TestUninstallApp:
    @patch("in_app_legal_verifier.subprocess.run")
    def test_uninstall_sends_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Success\n")
        uninstall_app("com.example.app")
        args = mock_run.call_args[0][0]
        assert "uninstall" in args
        assert "com.example.app" in args


class TestComputeVerdict:
    def test_strong_pass(self):
        v = compute_verdict(static_found=True, ui_found=True, blocker=None)
        assert v["verdict"] == "PASS"
        assert v["confidence"] == "STRONG"

    def test_confirmed_pass(self):
        v = compute_verdict(static_found=False, ui_found=True, blocker=None)
        assert v["verdict"] == "PASS"
        assert v["confidence"] == "CONFIRMED"

    def test_static_only_fail(self):
        v = compute_verdict(static_found=True, ui_found=False, blocker=None)
        assert v["verdict"] == "FAIL"
        assert v["confidence"] == "STATIC_ONLY"

    def test_not_found_fail(self):
        v = compute_verdict(static_found=False, ui_found=False, blocker=None)
        assert v["verdict"] == "FAIL"
        assert v["confidence"] == "NOT_FOUND"

    def test_login_wall_inconclusive(self):
        v = compute_verdict(static_found=False, ui_found=False, blocker="LOGIN_WALL")
        assert v["verdict"] == "INCONCLUSIVE"
        assert v["confidence"] == "LOGIN_WALL"

    def test_tutorial_blocked_inconclusive(self):
        v = compute_verdict(static_found=True, ui_found=False, blocker="TUTORIAL_BLOCKED")
        assert v["verdict"] == "INCONCLUSIVE"
        assert v["confidence"] == "TUTORIAL_BLOCKED"

    def test_unverified_fail(self):
        v = compute_verdict(static_found=False, ui_found=False, blocker="UNVERIFIED")
        assert v["verdict"] == "FAIL"
        assert v["confidence"] == "UNVERIFIED"

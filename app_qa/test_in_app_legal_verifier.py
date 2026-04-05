"""Tests for in_app_legal_verifier ADB utilities."""
import pytest
from unittest.mock import patch, MagicMock
from in_app_legal_verifier import (
    check_device_connected,
    classify_dismiss_action,
    DISMISS_PATTERNS,
    find_elements_by_keywords,
    install_apk,
    launch_app,
    parse_ui_elements,
    uninstall_app,
    UiElement,
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
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        launch_app("com.example.app")
        args = mock_run.call_args_list[-1][0][0]
        assert "monkey" in args
        assert "com.example.app" in args


class TestUninstallApp:
    @patch("in_app_legal_verifier.subprocess.run")
    def test_uninstall_sends_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Success\n")
        uninstall_app("com.example.app")
        args = mock_run.call_args[0][0]
        assert "uninstall" in args
        assert "com.example.app" in args

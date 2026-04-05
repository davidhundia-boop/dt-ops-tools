"""Tests for in_app_legal_verifier ADB utilities."""
import pytest
from unittest.mock import patch, MagicMock
from in_app_legal_verifier import check_device_connected, install_apk, launch_app, uninstall_app


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

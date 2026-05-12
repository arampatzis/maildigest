"""Tests for maildigest.cli."""

import os
from unittest.mock import patch

from click.testing import CliRunner

from maildigest.cli import _build_plist, _PLIST_LABEL, main

_DOMAIN = f"gui/{os.getuid()}"


class TestBuildPlist:
    def test_contains_digest_bin_path(self):
        plist = _build_plist("/usr/local/bin/maildigest", 9, 0)
        assert "/usr/local/bin/maildigest" in plist

    def test_contains_run_argument(self):
        plist = _build_plist("/usr/local/bin/maildigest", 9, 0)
        assert "<string>run</string>" in plist

    def test_correct_hour_and_minute(self):
        plist = _build_plist("/usr/local/bin/maildigest", 8, 30)
        assert "<integer>8</integer>" in plist
        assert "<integer>30</integer>" in plist

    def test_contains_label(self):
        plist = _build_plist("/usr/local/bin/maildigest", 9, 0)
        assert _PLIST_LABEL in plist

    def test_is_valid_xml(self):
        import xml.etree.ElementTree as ET
        plist = _build_plist("/usr/local/bin/maildigest", 9, 0)
        ET.fromstring(plist)


def _mock_launchctl_ok(mock_run):
    """Configure mock subprocess.run to simulate a successful launchctl call."""
    mock_run.return_value.returncode = 0
    mock_run.return_value.stderr = ""


class TestInstallCommand:
    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_creates_plist_file(self, mock_which, mock_run, tmp_path, monkeypatch):
        mock_which.return_value = "/usr/local/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        result = CliRunner().invoke(main, ["install"])

        assert result.exit_code == 0
        assert (tmp_path / f"{_PLIST_LABEL}.plist").exists()

    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_calls_launchctl_bootstrap(self, mock_which, mock_run, tmp_path, monkeypatch):
        mock_which.return_value = "/usr/local/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        CliRunner().invoke(main, ["install"])

        expected_plist = str(tmp_path / f"{_PLIST_LABEL}.plist")
        cmd = mock_run.call_args[0][0]
        assert cmd == ["launchctl", "bootstrap", _DOMAIN, expected_plist]

    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_custom_time_written_to_plist(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        mock_which.return_value = "/usr/local/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        CliRunner().invoke(main, ["install", "--time", "08:30"])

        content = (tmp_path / f"{_PLIST_LABEL}.plist").read_text()
        assert "<integer>8</integer>" in content
        assert "<integer>30</integer>" in content

    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_invalid_time_format_rejected(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        mock_which.return_value = "/usr/local/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        result = CliRunner().invoke(main, ["install", "--time", "25:00"])
        assert result.exit_code != 0

        result = CliRunner().invoke(main, ["install", "--time", "9am"])
        assert result.exit_code != 0

    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_digest_bin_path_written_to_plist(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        mock_which.return_value = "/opt/homebrew/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        CliRunner().invoke(main, ["install"])

        content = (tmp_path / f"{_PLIST_LABEL}.plist").read_text()
        assert "/opt/homebrew/bin/maildigest" in content

    @patch("maildigest.cli.subprocess.run")
    @patch("maildigest.cli.shutil.which")
    def test_reinstall_unloads_then_reloads(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        mock_which.return_value = "/usr/local/bin/maildigest"
        _mock_launchctl_ok(mock_run)
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        monkeypatch.setattr("maildigest.cli._LAUNCHD_LOG_DIR", tmp_path / "logs")

        plist_path = tmp_path / f"{_PLIST_LABEL}.plist"
        plist_path.write_text("old plist")

        result = CliRunner().invoke(main, ["install", "--time", "13:40"])

        assert result.exit_code == 0
        calls = [call[0][0] for call in mock_run.call_args_list]
        assert any("bootout" in c for c in calls)
        assert any("bootstrap" in c for c in calls)
        assert calls.index(next(c for c in calls if "bootout" in c)) < \
               calls.index(next(c for c in calls if "bootstrap" in c))


class TestUninstallCommand:
    @patch("maildigest.cli.subprocess.run")
    def test_unloads_and_deletes_plist(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)
        _mock_launchctl_ok(mock_run)
        plist_path = tmp_path / f"{_PLIST_LABEL}.plist"
        plist_path.write_text("dummy")

        result = CliRunner().invoke(main, ["uninstall"])

        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd == ["launchctl", "bootout", _DOMAIN, str(plist_path)]
        assert not plist_path.exists()

    @patch("maildigest.cli.subprocess.run")
    def test_no_op_when_plist_missing(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setattr("maildigest.cli._LAUNCHAGENTS", tmp_path)

        result = CliRunner().invoke(main, ["uninstall"])

        assert result.exit_code == 0
        mock_run.assert_not_called()

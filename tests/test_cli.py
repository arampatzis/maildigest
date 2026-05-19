"""Tests for maildigest.cli."""

from click.testing import CliRunner

from maildigest.cli import main


class TestMainGroup:
    def test_help_exits_zero(self):
        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0

    def test_daemon_command_registered(self):
        result = CliRunner().invoke(main, ["daemon", "--help"])
        assert result.exit_code == 0

    def test_service_group_registered(self):
        result = CliRunner().invoke(main, ["service", "--help"])
        assert result.exit_code == 0

    def test_service_install_registered(self):
        result = CliRunner().invoke(main, ["service", "install", "--help"])
        assert result.exit_code == 0

    def test_service_uninstall_registered(self):
        result = CliRunner().invoke(main, ["service", "uninstall", "--help"])
        assert result.exit_code == 0

    def test_service_log_registered(self):
        result = CliRunner().invoke(main, ["service", "log", "--help"])
        assert result.exit_code == 0

    def test_config_group_registered(self):
        result = CliRunner().invoke(main, ["config", "--help"])
        assert result.exit_code == 0

    def test_config_setup_registered(self):
        result = CliRunner().invoke(main, ["config", "setup", "--help"])
        assert result.exit_code == 0

    def test_config_check_registered(self):
        result = CliRunner().invoke(main, ["config", "check", "--help"])
        assert result.exit_code == 0

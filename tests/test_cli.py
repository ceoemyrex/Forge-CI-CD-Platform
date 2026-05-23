"""Tests for cli/forge.py — verifies all required commands are registered."""

from click.testing import CliRunner

from cli.forge import cli


def test_cli_has_required_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "run", "logs", "publish", "resolve", "ls", "admin"):
        assert cmd in result.output, f"Missing command: {cmd}"


def test_admin_has_create_token():
    runner = CliRunner()
    result = runner.invoke(cli, ["admin", "--help"])
    assert result.exit_code == 0
    assert "create-token" in result.output

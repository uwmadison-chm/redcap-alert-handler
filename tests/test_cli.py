from importlib.metadata import version as pkg_version

from typer.testing import CliRunner

from redcap_alert_handler.cli.main import app

runner = CliRunner()


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_bare_invocation_shows_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_version_prints_and_exits():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"rah {pkg_version('redcap-alert-handler')}" in result.output


def test_verbose_and_quiet_together_does_not_crash():
    result = runner.invoke(app, ["-v", "-q"])
    assert result.exit_code == 0


def test_no_color_and_verbose_together():
    result = runner.invoke(app, ["--no-color", "-v"])
    assert result.exit_code == 0

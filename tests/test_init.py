"""Tests for wachtmeater/__init__.py — version, logging filter, env loading."""

import os
from typing import Any

import wachtmeater
from wachtmeater.config import WachtmeaterConfig


def test_version_is_string() -> None:
    assert isinstance(wachtmeater.__version__, str)
    assert len(wachtmeater.__version__) > 0


# -- _loguru_skiplog_filter --------------------------------------------------


def test_loguru_skiplog_filter_passes_normal() -> None:
    record: dict[str, Any] = {"extra": {"skiplog": False}}
    assert wachtmeater._loguru_skiplog_filter(record) is True


def test_loguru_skiplog_filter_blocks_skiplog() -> None:
    record: dict[str, Any] = {"extra": {"skiplog": True}}
    assert wachtmeater._loguru_skiplog_filter(record) is False


def test_loguru_skiplog_filter_missing_extra() -> None:
    record: dict[str, Any] = {}
    assert wachtmeater._loguru_skiplog_filter(record) is True


def test_loguru_skiplog_filter_missing_skiplog_key() -> None:
    record: dict[str, Any] = {"extra": {}}
    assert wachtmeater._loguru_skiplog_filter(record) is True


# -- configure_logging -------------------------------------------------------


def test_configure_logging_runs_without_error() -> None:
    wachtmeater.configure_logging()


# -- read_dot_env_to_environ -------------------------------------------------


def test_read_dot_env_basic(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_WACHT_TEST=bar\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOO_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["FOO_WACHT_TEST"] == "bar"
    monkeypatch.delenv("FOO_WACHT_TEST", raising=False)


def test_read_dot_env_quoted_values(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED_WACHT_TEST="hello world"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUOTED_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["QUOTED_WACHT_TEST"] == "hello world"
    monkeypatch.delenv("QUOTED_WACHT_TEST", raising=False)


def test_read_dot_env_comments_and_blanks_skipped(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nVALID_WACHT_TEST=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VALID_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["VALID_WACHT_TEST"] == "1"
    monkeypatch.delenv("VALID_WACHT_TEST", raising=False)


def test_read_dot_env_setdefault_does_not_override(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("PRESET_WACHT_TEST=new\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRESET_WACHT_TEST", "original")
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["PRESET_WACHT_TEST"] == "original"


def test_read_dot_env_lines_without_equals_skipped(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("NO_EQUALS_LINE\nGOOD_WACHT_TEST=yes\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOD_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["GOOD_WACHT_TEST"] == "yes"
    assert "NO_EQUALS_LINE" not in os.environ
    monkeypatch.delenv("GOOD_WACHT_TEST", raising=False)


# -- TOML config loading ----------------------------------------------------


def test_read_toml_basic(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text('[test]\nTOML_BASIC_WACHT_TEST = "hello"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOML_BASIC_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["TOML_BASIC_WACHT_TEST"] == "hello"
    monkeypatch.delenv("TOML_BASIC_WACHT_TEST", raising=False)


def test_read_toml_multiple_sections(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text('[alpha]\nTOML_ALPHA_WACHT_TEST = "a"\n\n' '[beta]\nTOML_BETA_WACHT_TEST = "b"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOML_ALPHA_WACHT_TEST", raising=False)
    monkeypatch.delenv("TOML_BETA_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["TOML_ALPHA_WACHT_TEST"] == "a"
    assert os.environ["TOML_BETA_WACHT_TEST"] == "b"
    monkeypatch.delenv("TOML_ALPHA_WACHT_TEST", raising=False)
    monkeypatch.delenv("TOML_BETA_WACHT_TEST", raising=False)


def test_read_toml_setdefault_does_not_override(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text('[test]\nTOML_PRESET_WACHT_TEST = "new"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOML_PRESET_WACHT_TEST", "original")
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["TOML_PRESET_WACHT_TEST"] == "original"


def test_read_toml_integer_value_becomes_string(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text("[test]\nTOML_PORT_WACHT_TEST = 587\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOML_PORT_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["TOML_PORT_WACHT_TEST"] == "587"
    monkeypatch.delenv("TOML_PORT_WACHT_TEST", raising=False)


def test_read_flat_env_still_works(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("FLAT_ENV_WACHT_TEST=works\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLAT_ENV_WACHT_TEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["FLAT_ENV_WACHT_TEST"] == "works"
    monkeypatch.delenv("FLAT_ENV_WACHT_TEST", raising=False)


# -- cfg populated after read_dot_env_to_environ ----------------------------


def test_cfg_populated_after_read_dot_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text('[sip]\nport = 7777\ndest = "+490000"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIP_PORT", raising=False)
    monkeypatch.delenv("SIP_DEST", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert isinstance(wachtmeater.cfg, WachtmeaterConfig)
    assert wachtmeater.cfg.sip.port == 7777


def test_cfg_from_flat_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("CHECK_INTERVAL=999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHECK_INTERVAL", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert isinstance(wachtmeater.cfg, WachtmeaterConfig)
    assert wachtmeater.cfg.monitoring.check_interval == 999


def test_toml_section_aware_mapping(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """TOML clean keys (e.g. 'server' under [smtp]) map to correct env vars."""
    toml_file = tmp_path / "wachtmeater.toml"
    toml_file.write_text('[smtp]\nserver = "mail.test.com"\nport = 465\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMTP_SERVER", raising=False)
    monkeypatch.delenv("SMTP_SERVER_PORT", raising=False)
    wachtmeater.read_dot_env_to_environ()
    assert os.environ["SMTP_SERVER"] == "mail.test.com"
    assert os.environ["SMTP_SERVER_PORT"] == "465"

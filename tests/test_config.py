"""Tests for wachtmeater/config.py — dataclass definitions, type coercion, from_environ."""

import os

import pytest

from wachtmeater.config import (
    AlertDefaultsConfig,
    MonitoringConfig,
    SipConfig,
    SmtpConfig,
    WachtmeaterConfig,
    _coerce,
)

# -- _coerce -----------------------------------------------------------------


class TestCoerce:
    def test_int(self) -> None:
        assert _coerce("5161", int) == 5161

    def test_float(self) -> None:
        assert _coerce("2.1", float) == pytest.approx(2.1)

    def test_bool_true_variants(self) -> None:
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            assert _coerce(val, bool) is True

    def test_bool_false_variants(self) -> None:
        for val in ("false", "False", "FALSE", "0", "no", ""):
            assert _coerce(val, bool) is False

    def test_str_passthrough(self) -> None:
        assert _coerce("hello", str) == "hello"


# -- Section dataclass from_environ -----------------------------------------


class TestSipConfigFromEnviron:
    def test_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_PORT", "9999")
        monkeypatch.setenv("SIP_DEST", "+4912345")
        cfg = SipConfig.from_environ()
        assert cfg.port == 9999
        assert isinstance(cfg.port, int)

    def test_bool_coercion_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_CALL_TRANSCRIBE", "false")
        cfg = SipConfig.from_environ()
        assert cfg.call_transcribe is False

    def test_bool_coercion_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIP_CALL_TRANSCRIBE", "true")
        cfg = SipConfig.from_environ()
        assert cfg.call_transcribe is True


class TestFromEnvironUsesDefaults:
    def test_smtp_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear any env vars that would override defaults
        for key in ("SMTP_SERVER", "SMTP_SERVER_PORT"):
            monkeypatch.delenv(key, raising=False)
        cfg = SmtpConfig.from_environ()
        assert cfg.server == "smtp.example.com"
        assert cfg.port == 587

    def test_monitoring_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("CHECK_INTERVAL", "STALL_WINDOW", "AMBIENT_TEMP_DROP_THRESHOLD"):
            monkeypatch.delenv(key, raising=False)
        cfg = MonitoringConfig.from_environ()
        assert cfg.check_interval == 600
        assert cfg.stall_window == 3
        assert cfg.ambient_temp_drop_threshold == 10


class TestAlertDefaultsConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "ALERT_DEFAULT_TEMPDOWN_ENABLED",
            "ALERT_DEFAULT_RUHEPHASE_ENABLED",
            "ALERT_DEFAULT_RUHEPHASE_TARGET_TEMP",
            "ALERT_DEFAULT_STALL_ENABLED",
            "ALERT_DEFAULT_STALL_MIN_DELTA",
            "ALERT_DEFAULT_WRAP_ENABLED",
            "ALERT_DEFAULT_WRAP_TARGET_TEMP",
            "ALERT_DEFAULT_AMBIENT_RANGE_ENABLED",
            "ALERT_DEFAULT_AMBIENT_RANGE_MIN",
            "ALERT_DEFAULT_AMBIENT_RANGE_MAX",
            "ALERT_DEFAULT_COOKEND_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = AlertDefaultsConfig.from_environ()
        assert cfg.tempalert_tempdown_enabled is True
        assert cfg.tempalert_ruhephase_enabled is False
        assert cfg.ruhephase_target_temp == 0.0
        assert cfg.tempalert_stall_enabled is False
        assert cfg.stall_min_delta == 1.0
        assert cfg.tempalert_wrap_enabled is False
        assert cfg.wrap_target_temp == 0.0
        assert cfg.tempalert_ambient_range_enabled is False
        assert cfg.ambient_range_min == 0.0
        assert cfg.ambient_range_max == 0.0
        assert cfg.tempalert_cookend_enabled is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALERT_DEFAULT_STALL_ENABLED", "true")
        monkeypatch.setenv("ALERT_DEFAULT_STALL_MIN_DELTA", "2.5")
        monkeypatch.setenv("ALERT_DEFAULT_TEMPDOWN_ENABLED", "false")
        cfg = AlertDefaultsConfig.from_environ()
        assert cfg.tempalert_stall_enabled is True
        assert cfg.stall_min_delta == pytest.approx(2.5)
        assert cfg.tempalert_tempdown_enabled is False


class TestWachtmeaterConfigAllSections:
    def test_all_sections_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure SIP_DEST has a value so downstream assertions work
        monkeypatch.setenv("SIP_DEST", "+4900000")
        cfg = WachtmeaterConfig.from_environ()
        assert hasattr(cfg, "smtp")
        assert hasattr(cfg, "imap")
        assert hasattr(cfg, "ollama")
        assert hasattr(cfg, "meater")
        assert hasattr(cfg, "browser")
        assert hasattr(cfg, "sip")
        assert hasattr(cfg, "monitoring")
        assert hasattr(cfg, "matrix")
        assert hasattr(cfg, "auth")
        assert hasattr(cfg, "k8s")
        assert hasattr(cfg, "alerts")
        assert isinstance(cfg.sip, SipConfig)
        assert isinstance(cfg.alerts, AlertDefaultsConfig)

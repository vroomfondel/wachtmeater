"""Tests for wachtmeater/meater_watcher.py.

Requires minimatrix (imported at module level by meater_watcher).
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wachtmeater.config import AlertDefaultsConfig, WatcherState

mw = pytest.importorskip("wachtmeater.meater_watcher")


# ============================================================================
# _alert_summary
# ============================================================================


class TestAlertSummary:
    """Tests for _alert_summary(state)."""

    def test_defaults(self, base_state: WatcherState) -> None:
        lines = mw._alert_summary(base_state)
        # tempdown ON, cookend ON, rest OFF
        assert any("TempDown: AN" in l for l in lines)
        assert any("Cook-Ende: AN" in l for l in lines)
        assert any("Ruhephase: AUS" in l for l in lines)

    def test_tempdown_disabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        lines = mw._alert_summary(base_state)
        assert any("TempDown: AUS" in l for l in lines)

    def test_ruhephase_enabled_with_target(self, base_state: WatcherState) -> None:
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = 55.0
        lines = mw._alert_summary(base_state)
        assert any("Ruhephase: AN" in l and "55" in l for l in lines)

    def test_ruhephase_enabled_no_target(self, base_state: WatcherState) -> None:
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = None
        lines = mw._alert_summary(base_state)
        assert any("Ruhephase: AUS" in l for l in lines)

    def test_stall_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_min_delta = 2.0
        lines = mw._alert_summary(base_state)
        assert any("Stall: AN" in l and "2.0" in l for l in lines)

    def test_stall_already_alerted(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = True
        lines = mw._alert_summary(base_state)
        assert any("bereits ausgeloest" in l for l in lines)

    def test_wrap_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        lines = mw._alert_summary(base_state)
        assert any("Wrap-Reminder: AN" in l and "75" in l for l in lines)

    def test_wrap_already_alerted(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        base_state.wrap_alerted = True
        lines = mw._alert_summary(base_state)
        assert any("bereits ausgeloest" in l for l in lines)

    def test_cookend_ended(self, base_state: WatcherState) -> None:
        base_state.tempalert_cookend_enabled = True
        base_state.cook_ended = True
        lines = mw._alert_summary(base_state)
        assert any("Cook beendet erkannt" in l for l in lines)

    def test_ambient_range_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        lines = mw._alert_summary(base_state)
        assert any("Ambient-Bereich: AN" in l and "100" in l and "130" in l for l in lines)


# ============================================================================
# handle_command
# ============================================================================


class TestHandleCommand:
    """Tests for handle_command(body, state)."""

    @patch.object(mw, "save_state")
    def test_help(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("help", base_state) == mw.HELP_TEXT

    @patch.object(mw, "save_state")
    def test_hilfe(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("hilfe", base_state) == mw.HELP_TEXT

    @patch.object(mw, "save_state")
    def test_question_mark(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("?", base_state) == mw.HELP_TEXT

    @patch.object(mw, "save_state")
    def test_status(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("status", base_state) == "__status__"

    @patch.object(mw, "save_state")
    def test_stop(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("stop", base_state) == "__stop__"

    @patch.object(mw, "save_state")
    def test_quit(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("quit", base_state) == "__stop__"

    @patch.object(mw, "save_state")
    def test_beenden(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("beenden", base_state) == "__stop__"

    @patch.object(mw, "save_state")
    def test_exit(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("exit", base_state) == "__stop__"

    @patch.object(mw, "save_state")
    def test_enable_tempdown(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        result = mw.handle_command("enable tempdown", base_state)
        assert base_state.tempalert_tempdown_enabled is True
        assert result is not None and "aktiviert" in result

    @patch.object(mw, "save_state")
    def test_disable_tempdown(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable tempdown", base_state)
        assert base_state.tempalert_tempdown_enabled is False
        assert result is not None and "deaktiviert" in result

    @patch.object(mw, "save_state")
    def test_enable_ruhephase(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable ruhephase 55", base_state)
        assert base_state.tempalert_ruhephase_enabled is True
        assert base_state.ruhephase_target_temp == 55.0
        assert result is not None

    @patch.object(mw, "save_state")
    def test_disable_ruhephase(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable ruhephase", base_state)
        assert base_state.tempalert_ruhephase_enabled is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_enable_stall_default_delta(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable stall", base_state)
        assert base_state.tempalert_stall_enabled is True
        assert base_state.stall_min_delta == 1.0
        assert result is not None

    @patch.object(mw, "save_state")
    def test_enable_stall_custom_delta(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable stall 2.5", base_state)
        assert base_state.stall_min_delta == 2.5
        assert result is not None

    @patch.object(mw, "save_state")
    def test_disable_stall(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable stall", base_state)
        assert base_state.tempalert_stall_enabled is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_enable_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable wrap 75", base_state)
        assert base_state.tempalert_wrap_enabled is True
        assert base_state.wrap_target_temp == 75.0
        assert result is not None

    @patch.object(mw, "save_state")
    def test_disable_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable wrap", base_state)
        assert base_state.tempalert_wrap_enabled is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_enable_ambient(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable ambient 100 130", base_state)
        assert base_state.tempalert_ambient_range_enabled is True
        assert base_state.ambient_range_min == 100.0
        assert base_state.ambient_range_max == 130.0
        assert result is not None

    @patch.object(mw, "save_state")
    def test_ambient_lo_ge_hi_error(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable ambient 130 100", base_state)
        assert result is not None and "Fehler" in result

    @patch.object(mw, "save_state")
    def test_disable_ambient(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable ambient", base_state)
        assert base_state.tempalert_ambient_range_enabled is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_enable_cookend(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("enable cookend", base_state)
        assert base_state.tempalert_cookend_enabled is True
        assert result is not None

    @patch.object(mw, "save_state")
    def test_disable_cookend(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("disable cookend", base_state)
        assert base_state.tempalert_cookend_enabled is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_reset_stall(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.stall_alerted = True
        result = mw.handle_command("reset stall", base_state)
        assert base_state.stall_alerted is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_reset_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.wrap_alerted = True
        result = mw.handle_command("reset wrap", base_state)
        assert base_state.wrap_alerted is False
        assert result is not None

    @patch.object(mw, "save_state")
    def test_unknown_command(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("xyzzy", base_state) is None

    @patch.object(mw, "save_state")
    def test_case_insensitivity(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert mw.handle_command("STATUS", base_state) == "__status__"
        assert mw.handle_command("Help", base_state) == mw.HELP_TEXT

    @patch.object(mw, "save_state")
    def test_tempdown_alias(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = mw.handle_command("tempdown an", base_state)
        assert base_state.tempalert_tempdown_enabled is True
        assert result is not None


# ============================================================================
# load_state / save_state
# ============================================================================


class TestStateIO:
    """Tests for load_state and save_state."""

    def test_load_state_no_file(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "nonexistent.json"
        with patch.object(mw, "STATE_FILE", fake_file):
            state = mw.load_state()
        assert state.tempalert_tempdown_enabled is True
        assert state.consecutive_errors == 0

    def test_load_state_existing_file(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"max_ambient_temp": 140.0}))
        with patch.object(mw, "STATE_FILE", state_file):
            state = mw.load_state()
        assert state.max_ambient_temp == 140.0
        # defaults applied for missing keys
        assert state.tempalert_tempdown_enabled is True

    def test_save_state_writes_json(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state = WatcherState()
        with patch.object(mw, "STATE_FILE", state_file):
            mw.save_state(state)
        data = json.loads(state_file.read_text())
        assert "last_check" in data
        assert data["last_check"] is not None

    def test_save_state_creates_parent_dirs(self, tmp_path: Path) -> None:
        state_file = tmp_path / "sub" / "dir" / "state.json"
        state = WatcherState()
        with patch.object(mw, "STATE_FILE", state_file):
            mw.save_state(state)
        assert state_file.exists()

    def test_save_state_roundtrip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state = WatcherState()
        state.max_internal_temp = 85.0
        with patch.object(mw, "STATE_FILE", state_file):
            mw.save_state(state)
            loaded = mw.load_state()
        assert loaded.max_internal_temp == 85.0

    def test_load_no_file_uses_config_defaults(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "nonexistent.json"
        custom_alerts = AlertDefaultsConfig(
            tempalert_tempdown_enabled=False,
            tempalert_stall_enabled=True,
            stall_min_delta=2.5,
            tempalert_cookend_enabled=False,
            ruhephase_target_temp=55.0,
            wrap_target_temp=75.0,
            ambient_range_min=100.0,
            ambient_range_max=130.0,
        )
        mock_cfg = MagicMock()
        mock_cfg.alerts = custom_alerts
        with patch.object(mw, "STATE_FILE", fake_file), patch.object(mw, "cfg", mock_cfg):
            state = mw.load_state()
        assert state.tempalert_tempdown_enabled is False
        assert state.tempalert_stall_enabled is True
        assert state.stall_min_delta == 2.5
        assert state.tempalert_cookend_enabled is False
        assert state.ruhephase_target_temp == 55.0
        assert state.wrap_target_temp == 75.0
        assert state.ambient_range_min == 100.0
        assert state.ambient_range_max == 130.0

    def test_load_existing_file_ignores_config(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"tempalert_stall_enabled": False}))
        custom_alerts = AlertDefaultsConfig(tempalert_stall_enabled=True)
        mock_cfg = MagicMock()
        mock_cfg.alerts = custom_alerts
        with patch.object(mw, "STATE_FILE", state_file), patch.object(mw, "cfg", mock_cfg):
            state = mw.load_state()
        # State file value wins over config
        assert state.tempalert_stall_enabled is False

    def test_sentinel_zero_becomes_none(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "nonexistent.json"
        # All float targets at 0.0 → should become None in WatcherState
        custom_alerts = AlertDefaultsConfig(
            ruhephase_target_temp=0.0,
            wrap_target_temp=0.0,
            ambient_range_min=0.0,
            ambient_range_max=0.0,
        )
        mock_cfg = MagicMock()
        mock_cfg.alerts = custom_alerts
        with patch.object(mw, "STATE_FILE", fake_file), patch.object(mw, "cfg", mock_cfg):
            state = mw.load_state()
        assert state.ruhephase_target_temp is None
        assert state.wrap_target_temp is None
        assert state.ambient_range_min is None
        assert state.ambient_range_max is None


class TestApplyAlertDefaults:
    def test_applies_all_fields(self) -> None:
        state = WatcherState()
        alert_cfg = AlertDefaultsConfig(
            tempalert_tempdown_enabled=False,
            tempalert_ruhephase_enabled=True,
            ruhephase_target_temp=55.0,
            tempalert_stall_enabled=True,
            stall_min_delta=3.0,
            tempalert_wrap_enabled=True,
            wrap_target_temp=75.0,
            tempalert_ambient_range_enabled=True,
            ambient_range_min=100.0,
            ambient_range_max=130.0,
            tempalert_cookend_enabled=False,
        )
        mw._apply_alert_defaults(state, alert_cfg)
        assert state.tempalert_tempdown_enabled is False
        assert state.tempalert_ruhephase_enabled is True
        assert state.ruhephase_target_temp == 55.0
        assert state.tempalert_stall_enabled is True
        assert state.stall_min_delta == 3.0
        assert state.tempalert_wrap_enabled is True
        assert state.wrap_target_temp == 75.0
        assert state.tempalert_ambient_range_enabled is True
        assert state.ambient_range_min == 100.0
        assert state.ambient_range_max == 130.0
        assert state.tempalert_cookend_enabled is False


# ============================================================================
# get_meater_data
# ============================================================================


class TestGetMeaterData:
    """Tests for get_meater_data — patch extract_via_browser."""

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    def test_success(self, mock_extract: MagicMock) -> None:
        from wachtmeater.meater_monitor import CookData

        mock_extract.return_value = CookData(
            internal_temp_c=72,
            ambient_temp_c=115,
            target_temp_c=96,
            remaining_time="2h",
            elapsed_time="4h",
            status="cooking",
            cook_name="Brisket",
            screenshot="/tmp/test.png",
            started_at="2026-03-07T03:12:00",
            peak_temp_c=73,
        )
        result = mw.get_meater_data()
        assert result["internal_temp_c"] == 72
        assert result["status"] == "cooking"
        assert result["cook_name"] == "Brisket"

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    def test_extract_raises_returns_error(self, mock_extract: MagicMock) -> None:
        mock_extract.side_effect = RuntimeError("browser connection failed")
        result = mw.get_meater_data()
        assert "error" in result
        assert "browser connection failed" in result["error"]

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    def test_timeout_exception(self, mock_extract: MagicMock) -> None:
        mock_extract.side_effect = TimeoutError("Timeout")
        result = mw.get_meater_data()
        assert "error" in result
        assert "Timeout" in result["error"]

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    def test_generic_exception(self, mock_extract: MagicMock) -> None:
        mock_extract.side_effect = OSError("no such file")
        result = mw.get_meater_data()
        assert "no such file" in result["error"]


# ============================================================================
# run_meater_check
# ============================================================================


class TestRunMeaterCheck:
    """Tests for run_meater_check — patch external calls."""

    def _patch_externals(self) -> dict[str, Any]:
        """Return a dict of patch targets and their mock objects."""
        return {
            "get_meater_data": patch.object(mw, "get_meater_data"),
            "call_pitmaster": patch.object(mw, "call_pitmaster"),
            "save_state": patch.object(mw, "save_state"),
        }

    def _make_data(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "cook_name": "Brisket",
            "internal_temp_c": 72,
            "ambient_temp_c": 115,
            "target_temp_c": 96,
            "remaining_time": "2h 30m",
            "elapsed_time": "4h",
            "status": "cooking",
            "screenshot": "/tmp/test.png",
        }
        base.update(overrides)
        return base

    def test_error_cookend_threshold(self, base_state: WatcherState) -> None:
        base_state.tempalert_cookend_enabled = True
        base_state.consecutive_errors = 2  # one more → threshold (3)
        mock_send = MagicMock()
        with (
            patch.object(mw, "get_meater_data", return_value={"error": "timeout"}),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            result = mw.run_meater_check(base_state, send=mock_send)
        assert result == "__cook_ended__"
        assert base_state.cook_ended is True
        mock_call.assert_called_once()
        mock_send.assert_called_once()

    def test_normal_path_updates_max(self, base_state: WatcherState) -> None:
        data = self._make_data()
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster"),
            patch.object(mw, "save_state"),
        ):
            result = mw.run_meater_check(base_state)
        assert result != "__cook_ended__"
        assert base_state.max_ambient_temp == 115
        assert base_state.max_internal_temp == 72

    def test_tempdown_triggers(self, cooking_state: WatcherState) -> None:
        cooking_state.tempalert_tempdown_enabled = True
        cooking_state.max_ambient_temp = 130.0
        # Current ambient drops by 20 from max (>= threshold of 10)
        data = self._make_data(ambient_temp_c=110)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(cooking_state)
        mock_call.assert_called()
        # The alarm message should mention temperature drop
        call_msg = mock_call.call_args[0][0]
        assert "ACHTUNG" in call_msg

    def test_ruhephase_triggers_and_disables(self, cooking_state: WatcherState) -> None:
        cooking_state.tempalert_ruhephase_enabled = True
        cooking_state.ruhephase_target_temp = 75.0
        data = self._make_data(internal_temp_c=74)  # below target
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(cooking_state)
        mock_call.assert_called()
        assert cooking_state.tempalert_ruhephase_enabled is False

    def test_stall_triggers(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_min_delta = 1.0
        # History shows plateau (STALL_WINDOW=3 checks, need >3 entries)
        base_state.internal_temp_history = [70.0, 70.0, 70.0, 70.0]
        data = self._make_data(internal_temp_c=70)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert base_state.stall_alerted is True

    def test_stall_does_not_retrigger(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = True  # already alerted
        base_state.internal_temp_history = [70.0, 70.0, 70.0, 70.0]
        data = self._make_data(internal_temp_c=70)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        # call_pitmaster should not be called for stall (already alerted)
        for call in mock_call.call_args_list:
            assert "Stall" not in call[0][0]

    def test_stall_resets_on_recovery(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = False
        base_state.stall_min_delta = 1.0
        # Big rise → rise >= min_delta * 2 → reset
        base_state.internal_temp_history = [70.0, 71.0, 72.0, 73.0]
        data = self._make_data(internal_temp_c=75)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster"),
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        assert base_state.stall_alerted is False

    def test_wrap_triggers(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        data = self._make_data(internal_temp_c=76)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert base_state.wrap_alerted is True

    def test_ambient_too_low(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        data = self._make_data(ambient_temp_c=90)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert "niedrig" in mock_call.call_args[0][0]

    def test_ambient_too_high(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        data = self._make_data(ambient_temp_c=140)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert "hoch" in mock_call.call_args[0][0]

    def test_ambient_in_bounds(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        data = self._make_data(ambient_temp_c=115)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        # call_pitmaster should not be called for ambient alert
        for call in mock_call.call_args_list:
            assert "Ambient" not in call[0][0]

    def test_target_reached(self, base_state: WatcherState) -> None:
        data = self._make_data(internal_temp_c=96, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert "fertig" in mock_call.call_args[0][0]

    def test_target_reached_capped_at_3(self, base_state: WatcherState) -> None:
        base_state.target_reached_calls = 3
        data = self._make_data(internal_temp_c=96, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        # Should NOT call for target reached since we already hit 3
        for call in mock_call.call_args_list:
            assert "fertig" not in call[0][0]

    def test_probe_removed_cook_ended(self, base_state: WatcherState) -> None:
        base_state.tempalert_cookend_enabled = True
        base_state.max_internal_temp = 80.0
        data = self._make_data(internal_temp_c=30)  # below COOKEND_PROBE_REMOVED_TEMP
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster") as mock_call,
            patch.object(mw, "save_state"),
        ):
            result = mw.run_meater_check(base_state)
        assert result == "__cook_ended__"
        assert base_state.cook_ended is True

    def test_resets_consecutive_errors_on_success(self, base_state: WatcherState) -> None:
        base_state.consecutive_errors = 2
        data = self._make_data()
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(mw, "call_pitmaster"),
            patch.object(mw, "save_state"),
        ):
            mw.run_meater_check(base_state)
        assert base_state.consecutive_errors == 0


# ============================================================================
# WatcherError
# ============================================================================


class TestWatcherError:
    """Tests for WatcherError exception class."""

    def test_is_exception(self) -> None:
        assert issubclass(mw.WatcherError, Exception)

    def test_message(self) -> None:
        exc = mw.WatcherError("boom")
        assert str(exc) == "boom"


# ============================================================================
# event_loop — error tracking and startup greeting
# ============================================================================


def _make_mock_messaging(sync_side_effect: Any = None) -> MagicMock:
    """Create a mock that satisfies the MessagingBackend protocol."""
    m = MagicMock()
    m.connect = AsyncMock()
    m.get_or_create_room = AsyncMock(return_value="!room:test")
    m.get_rooms = MagicMock(return_value=["!room:test"])
    m.get_bot_user_id = MagicMock(return_value="@bot:test")
    m.send_message = AsyncMock()
    m.send_image = AsyncMock()
    m.register_message_callback = MagicMock()
    if sync_side_effect is not None:
        m.start_sync = sync_side_effect
    else:
        m.start_sync = AsyncMock()
    m.stop_sync = MagicMock()
    m.close = AsyncMock()
    return m


class TestEventLoopErrorTracking:
    """Tests that event_loop raises WatcherError on matrix_sync failure."""

    def test_matrix_sync_error_raises_watcher_error(self) -> None:
        """When start_sync raises, event_loop should raise WatcherError."""

        async def _failing_sync() -> None:
            await asyncio.sleep(0.05)
            raise ConnectionError("sync lost")

        mock_messaging = _make_mock_messaging(sync_side_effect=_failing_sync)

        with (
            patch.object(mw, "MEATER_URL", "https://meater.io/test-uuid"),
            patch.object(mw, "load_state", return_value=WatcherState()),
            patch.object(mw, "save_state"),
            patch.object(mw, "call_pitmaster"),
            patch.object(mw, "run_meater_check", return_value="ok"),
            patch.object(mw, "cfg") as mock_cfg,
        ):
            mock_cfg.matrix.room = "!room:test"
            mock_cfg.matrix.auto_create_room_for_meater_uuid = False
            mock_cfg.matrix.pitmaster = ""

            with pytest.raises(mw.WatcherError, match="sync lost"):
                asyncio.run(mw.event_loop(skip_startup_test_call=True, messaging=mock_messaging))


class TestStartupGreeting:
    """Tests that event_loop sends startup greeting after Matrix login."""

    def test_startup_greeting_sent(self) -> None:
        """After login, send_message should be called with HELP_TEXT."""

        async def _stop_sync() -> None:
            await asyncio.sleep(0.05)

        mock_messaging = _make_mock_messaging(sync_side_effect=_stop_sync)

        with (
            patch.object(mw, "MEATER_URL", "https://meater.io/test-uuid"),
            patch.object(mw, "load_state", return_value=WatcherState()),
            patch.object(mw, "save_state"),
            patch.object(mw, "call_pitmaster"),
            patch.object(mw, "run_meater_check", return_value="ok"),
            patch.object(mw, "cfg") as mock_cfg,
        ):
            mock_cfg.matrix.room = "!room:test"
            mock_cfg.matrix.auto_create_room_for_meater_uuid = False
            mock_cfg.matrix.pitmaster = ""

            asyncio.run(mw.event_loop(skip_startup_test_call=True, messaging=mock_messaging))

        # Check that send_message was called with the greeting
        calls = mock_messaging.send_message.call_args_list
        assert len(calls) >= 1
        greeting_call = calls[0]
        assert greeting_call[0][0] == "!room:test"
        assert "MEATER Watcher ist gestartet" in greeting_call[0][1]
        assert mw.HELP_TEXT in greeting_call[0][1]

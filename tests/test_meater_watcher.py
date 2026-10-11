"""Tests for the watcher (wachtmeater/meater_watcher.py and the modules split off from it).

Requires minimatrix (imported at module level by meater_watcher).
"""

import math
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wachtmeater.config import AlertDefaultsConfig
from wachtmeater.watcher_state import WatcherState

from wachtmeater import alerting, commands, signal_stats, status_text, timeutil, watcher_settings, watcher_state

mw = pytest.importorskip("wachtmeater.meater_watcher")


def _field(state: WatcherState, name: str) -> object:
    """Read a state field without mypy's attribute narrowing.

    After ``state.x = 1.0`` or ``assert state.x is True`` mypy keeps the
    narrowed type across the call that mutates *state*, flagging follow-up
    asserts as unreachable.
    """
    return getattr(state, name)


# ============================================================================
# alert_summary
# ============================================================================


class TestAlertSummary:
    """Tests for status_text.alert_summary(state)."""

    def test_defaults(self, base_state: WatcherState) -> None:
        lines = status_text.alert_summary(base_state)
        # tempdown ON, cookend ON, rest OFF
        assert any("TempDown: AN" in l for l in lines)
        assert any("Cook-Ende: AN" in l for l in lines)
        assert any("Ruhephase: AUS" in l for l in lines)

    def test_tempdown_disabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        lines = status_text.alert_summary(base_state)
        assert any("TempDown: AUS" in l for l in lines)

    def test_ruhephase_enabled_with_target(self, base_state: WatcherState) -> None:
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = 55.0
        lines = status_text.alert_summary(base_state)
        assert any("Ruhephase: AN" in l and "55" in l for l in lines)

    def test_ruhephase_enabled_no_target(self, base_state: WatcherState) -> None:
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = None
        lines = status_text.alert_summary(base_state)
        assert any("Ruhephase: AUS" in l for l in lines)

    def test_stall_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_max_rate = 2.0
        lines = status_text.alert_summary(base_state)
        assert any("Stall: AN" in l and "< 2 C/h" in l for l in lines)

    def test_stall_already_alerted(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = True
        lines = status_text.alert_summary(base_state)
        assert any("bereits ausgeloest" in l for l in lines)

    def test_wrap_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        lines = status_text.alert_summary(base_state)
        assert any("Wrap-Reminder: AN" in l and "75" in l for l in lines)

    def test_wrap_already_alerted(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        base_state.wrap_alerted = True
        lines = status_text.alert_summary(base_state)
        assert any("bereits ausgeloest" in l for l in lines)

    def test_cookend_ended(self, base_state: WatcherState) -> None:
        base_state.tempalert_cookend_enabled = True
        base_state.cook_ended = True
        lines = status_text.alert_summary(base_state)
        assert any("Cook beendet erkannt" in l for l in lines)

    def test_ambient_range_enabled(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        lines = status_text.alert_summary(base_state)
        assert any("Ambient-Bereich: AN" in l and "100" in l and "130" in l for l in lines)

    def test_startup_test_call_enabled(self, base_state: WatcherState) -> None:
        base_state.startup_test_call_enabled = True
        lines = status_text.alert_summary(base_state)
        assert any("Startup-Testanruf: AN" in l for l in lines)

    def test_startup_test_call_disabled(self, base_state: WatcherState) -> None:
        base_state.startup_test_call_enabled = False
        lines = status_text.alert_summary(base_state)
        assert any("Startup-Testanruf: AUS" in l for l in lines)

    def test_check_interval_default(self, base_state: WatcherState) -> None:
        base_state.check_interval_override = None
        lines = status_text.alert_summary(base_state)
        assert any(l == f"Check-Intervall: {watcher_settings.CHECK_INTERVAL}s" for l in lines)

    def test_check_interval_override(self, base_state: WatcherState) -> None:
        base_state.check_interval_override = 45
        lines = status_text.alert_summary(base_state)
        assert any("Check-Intervall: 45s (override" in l for l in lines)

    def test_pull_override_off(self, base_state: WatcherState) -> None:
        base_state.target_override = None
        lines = status_text.alert_summary(base_state)
        assert any(l == "Pull-Alert (rausnehmen): AUS" for l in lines)

    def test_pull_override_on(self, base_state: WatcherState) -> None:
        base_state.target_override = 90.0
        lines = status_text.alert_summary(base_state)
        assert any("Pull-Alert (rausnehmen): AN" in l and "90" in l for l in lines)

    def test_pull_override_already_fired(self, base_state: WatcherState) -> None:
        base_state.target_override = 90.0
        base_state.target_reached_calls = 3
        lines = status_text.alert_summary(base_state)
        assert any("Pull-Alert" in l and "bereits ausgeloest" in l for l in lines)


# ============================================================================
# handle_command
# ============================================================================


class TestHandleCommand:
    """Tests for handle_command(body, state)."""

    @patch.object(watcher_state, "save_state")
    def test_help(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("help", base_state) == commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_hilfe(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("hilfe", base_state) == commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_question_mark(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("?", base_state) == commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_status(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("status", base_state) == "__status__"

    @patch.object(watcher_state, "save_state")
    def test_slash_help(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("/help", base_state) == commands.HELP_TEXT
        assert commands.handle_command("/hilfe", base_state) == commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_eta(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("eta", base_state) == "__eta__"
        assert commands.handle_command("ETA", base_state) == "__eta__"
        assert commands.handle_command("restzeit", base_state) == "__eta__"

    def test_eta_listed_in_help(self) -> None:
        assert "eta" in commands.HELP_TEXT
        assert "/help" in commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_testcall_default(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("testcall", base_state)
        assert result == "__testcall__\nWachtmeater Testanruf"

    @patch.object(watcher_state, "save_state")
    def test_testcall_with_text_preserves_case(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("testcall Hallo Klaus, das Brisket ist Fertig!", base_state)
        assert result == "__testcall__\nHallo Klaus, das Brisket ist Fertig!"

    @patch.object(watcher_state, "save_state")
    def test_testcall_uppercase_keyword_still_matches(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("TestCall Probealarm", base_state)
        assert result == "__testcall__\nProbealarm"

    @patch.object(watcher_state, "save_state")
    def test_testcall_strips_surrounding_whitespace(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("  testcall   foo bar  ", base_state)
        assert result == "__testcall__\nfoo bar"

    @patch.object(watcher_state, "save_state")
    def test_stop(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("stop", base_state) == "__stop__"

    @patch.object(watcher_state, "save_state")
    def test_quit(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("quit", base_state) == "__stop__"

    @patch.object(watcher_state, "save_state")
    def test_beenden(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("beenden", base_state) == "__stop__"

    @patch.object(watcher_state, "save_state")
    def test_exit(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("exit", base_state) == "__stop__"

    @patch.object(watcher_state, "save_state")
    def test_set_interval(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("set interval 120", base_state)
        assert base_state.check_interval_override == 120
        assert result is not None and "gesetzt" in result

    @patch.object(watcher_state, "save_state")
    def test_set_interval_bare_keyword(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("interval 300", base_state) is not None
        assert base_state.check_interval_override == 300

    @patch.object(watcher_state, "save_state")
    def test_set_interval_below_minimum_rejected(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.check_interval_override = 120
        result = commands.handle_command(f"set interval {watcher_settings.MIN_CHECK_INTERVAL - 1}", base_state)
        assert result is not None and "mindestens" in result
        assert base_state.check_interval_override == 120  # unchanged

    @patch.object(watcher_state, "save_state")
    def test_reset_interval(self, _save: MagicMock, base_state: WatcherState) -> None:
        commands.handle_command("set interval 99", base_state)  # establish an override first
        result = commands.handle_command("reset interval", base_state)
        assert result is not None and "Standard" in result
        assert base_state.check_interval_override is None

    @patch.object(watcher_state, "save_state")
    def test_set_interval_non_numeric_is_not_a_command(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("set interval abc", base_state) is None

    @patch.object(watcher_state, "save_state")
    def test_disable_startcall(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.startup_test_call_enabled = True
        result = commands.handle_command("disable startcall", base_state)
        assert base_state.startup_test_call_enabled is False
        assert result is not None and "deaktiviert" in result

    @patch.object(watcher_state, "save_state")
    def test_enable_startcall(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.startup_test_call_enabled = False
        result = commands.handle_command("enable startcall", base_state)
        assert base_state.startup_test_call_enabled is True
        assert result is not None and "aktiviert" in result

    @patch.object(watcher_state, "save_state")
    def test_startcall_german_alias(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.startup_test_call_enabled = True
        assert commands.handle_command("startcall aus", base_state) is not None
        assert base_state.startup_test_call_enabled is False

    @patch.object(watcher_state, "save_state")
    def test_startcall_does_not_shadow_manual_testcall(self, _save: MagicMock, base_state: WatcherState) -> None:
        # The manual "testcall" command must still trigger a call, not be
        # confused with the startup-testcall toggle.
        result = commands.handle_command("testcall", base_state)
        assert result == "__testcall__\nWachtmeater Testanruf"

    @patch.object(watcher_state, "save_state")
    def test_enable_tempdown(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        result = commands.handle_command("enable tempdown", base_state)
        assert base_state.tempalert_tempdown_enabled is True
        assert result is not None and "aktiviert" in result

    @patch.object(watcher_state, "save_state")
    def test_disable_tempdown(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable tempdown", base_state)
        assert base_state.tempalert_tempdown_enabled is False
        assert result is not None and "deaktiviert" in result

    @patch.object(watcher_state, "save_state")
    def test_enable_ruhephase(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable ruhephase 55", base_state)
        assert base_state.tempalert_ruhephase_enabled is True
        assert base_state.ruhephase_target_temp == 55.0
        assert base_state.ruhephase_started_at is not None
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_ruhephase_disables_tempdown(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = True
        result = commands.handle_command("enable ruhephase 55", base_state)
        assert base_state.tempalert_tempdown_enabled is False
        assert result is not None and "TempDown" in result

    @patch.object(watcher_state, "save_state")
    def test_enable_ruhephase_tempdown_already_off(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        result = commands.handle_command("ruhephase an 55", base_state)
        assert base_state.tempalert_ruhephase_enabled is True
        assert base_state.tempalert_tempdown_enabled is False
        # No coupling note when there was nothing to switch off.
        assert result is not None and "TempDown" not in result

    @patch.object(watcher_state, "save_state")
    def test_disable_ruhephase_leaves_tempdown_untouched(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.tempalert_tempdown_enabled = False
        commands.handle_command("disable ruhephase", base_state)
        assert base_state.tempalert_tempdown_enabled is False

    @patch.object(watcher_state, "save_state")
    def test_disable_ruhephase(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable ruhephase", base_state)
        assert base_state.tempalert_ruhephase_enabled is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_stall_default_rate(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable stall", base_state)
        assert base_state.tempalert_stall_enabled is True
        assert base_state.stall_max_rate == mw.cfg.alerts.stall_max_rate
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_stall_custom_rate(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable stall 2.5", base_state)
        assert base_state.stall_max_rate == 2.5
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_disable_stall(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable stall", base_state)
        assert base_state.tempalert_stall_enabled is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable wrap 75", base_state)
        assert base_state.tempalert_wrap_enabled is True
        assert base_state.wrap_target_temp == 75.0
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_disable_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable wrap", base_state)
        assert base_state.tempalert_wrap_enabled is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_pull(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.target_reached_calls = 3  # previously fired
        result = commands.handle_command("enable pull 88", base_state)
        assert base_state.target_override == 88.0
        assert base_state.target_reached_calls == 0  # re-armed
        assert result is not None and "Pull-Alert aktiviert" in result

    @patch.object(watcher_state, "save_state")
    def test_enable_pull_german_alias(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("rausnehmen 91.5", base_state) is not None
        assert base_state.target_override == 91.5

    @patch.object(watcher_state, "save_state")
    def test_disable_pull(self, _save: MagicMock, base_state: WatcherState) -> None:
        commands.handle_command("enable pull 88", base_state)  # establish an override first
        result = commands.handle_command("disable pull", base_state)
        assert result is not None and "deaktiviert" in result
        assert base_state.target_override is None

    @patch.object(watcher_state, "save_state")
    def test_pull_without_temp_is_not_a_command(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("enable pull", base_state) is None

    @patch.object(watcher_state, "save_state")
    def test_enable_ambient(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable ambient 100 130", base_state)
        assert base_state.tempalert_ambient_range_enabled is True
        assert base_state.ambient_range_min == 100.0
        assert base_state.ambient_range_max == 130.0
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_ambient_lo_ge_hi_error(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable ambient 130 100", base_state)
        assert result is not None and "Fehler" in result

    @patch.object(watcher_state, "save_state")
    def test_disable_ambient(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable ambient", base_state)
        assert base_state.tempalert_ambient_range_enabled is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_enable_cookend(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("enable cookend", base_state)
        assert base_state.tempalert_cookend_enabled is True
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_disable_cookend(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("disable cookend", base_state)
        assert base_state.tempalert_cookend_enabled is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_reset_stall(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.stall_alerted = True
        result = commands.handle_command("reset stall", base_state)
        assert base_state.stall_alerted is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_reset_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.wrap_alerted = True
        result = commands.handle_command("reset wrap", base_state)
        assert base_state.wrap_alerted is False
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_reset_ambient(self, _save: MagicMock, cooking_state: WatcherState) -> None:
        result = commands.handle_command("reset ambient", cooking_state)
        assert cooking_state.max_ambient_temp is None
        assert result is not None
        assert "120.0" in result

    @patch.object(watcher_state, "save_state")
    def test_reset_ambient_when_unset(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.max_ambient_temp = None
        result = commands.handle_command("reset ambient", base_state)
        assert base_state.max_ambient_temp is None
        assert result is not None

    @patch.object(watcher_state, "save_state")
    def test_reset_ambient_aliases(self, _save: MagicMock, cooking_state: WatcherState) -> None:
        for alias in ("reset maxambient", "reset max ambient", "reset ambient max", "ambient reset"):
            cooking_state.max_ambient_temp = 120.0
            assert commands.handle_command(alias, cooking_state) is not None
            assert cooking_state.max_ambient_temp is None

    @patch.object(watcher_state, "save_state")
    def test_reset_ambient_keeps_range_alert(self, _save: MagicMock, cooking_state: WatcherState) -> None:
        # Resetting the high-water mark must not touch the ambient range alert.
        cooking_state.tempalert_ambient_range_enabled = True
        cooking_state.ambient_range_min = 100.0
        cooking_state.ambient_range_max = 140.0
        commands.handle_command("reset ambient", cooking_state)
        assert cooking_state.tempalert_ambient_range_enabled is True
        assert cooking_state.ambient_range_min == 100.0

    @patch.object(watcher_state, "save_state")
    def test_pause_default_minutes(self, _save: MagicMock, base_state: WatcherState) -> None:
        with patch.object(timeutil, "now", return_value=1000.0):
            result = commands.handle_command("Pause", base_state)
        assert base_state.pause_until == 1000.0 + watcher_settings.PAUSE_DEFAULT_MINUTES * 60
        assert result is not None and "resume" in result
        assert base_state.wrap_alerted is False

    @patch.object(watcher_state, "save_state")
    def test_pause_custom_minutes(self, _save: MagicMock, base_state: WatcherState) -> None:
        with patch.object(timeutil, "now", return_value=1000.0):
            commands.handle_command("pause 5", base_state)
        assert base_state.pause_until == 1300.0

    @patch.object(watcher_state, "save_state")
    def test_pause_zero_rejected(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("pause 0", base_state)
        assert result is not None and result.startswith("Fehler")
        assert base_state.pause_until is None

    @patch.object(watcher_state, "save_state")
    def test_wrapped_pauses_and_marks_wrap_done(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.alert_calls_sent = ["wrap"]
        with patch.object(timeutil, "now", return_value=1000.0):
            result = commands.handle_command("wrapped 30", base_state)
        assert base_state.pause_until == 1000.0 + 30 * 60
        assert base_state.wrap_alerted is True
        assert base_state.alert_calls_sent == []
        assert result is not None and "Wrap als erledigt" in result

    @patch.object(watcher_state, "save_state")
    def test_wrapped_without_minutes_only_sets_status(self, _save: MagicMock, base_state: WatcherState) -> None:
        with patch.object(timeutil, "now", return_value=1000.0):
            result = commands.handle_command("wrapped", base_state)
        assert base_state.pause_until is None
        assert base_state.wrap_alerted is True
        assert base_state.wrapped_at == 1000.0
        assert result is not None and "Pause" not in result

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_backdates_without_pause(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 12, 21).timestamp()
        base_state.alert_calls_sent = ["wrap"]
        with patch.object(timeutil, "now", return_value=now):
            result = commands.handle_command("wrapped at 11:05", base_state)
        assert base_state.wrapped_at == datetime(2026, 10, 11, 11, 5).timestamp()
        assert base_state.wrap_alerted is True
        assert base_state.alert_calls_sent == []
        assert base_state.pause_until is None
        assert result is not None and "(11:05)" in result and "ab 12:05" in result

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_future_clock_means_yesterday(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 0, 30).timestamp()
        with patch.object(timeutil, "now", return_value=now):
            commands.handle_command("gewrappt um 23:40", base_state)
        assert base_state.wrapped_at == datetime(2026, 10, 10, 23, 40).timestamp()

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_endphase_ignores_later_disturbance(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 12, 50).timestamp()
        base_state.trend_ignore_before = datetime(2026, 10, 11, 12, 41).timestamp()
        with patch.object(timeutil, "now", return_value=now):
            result = commands.handle_command("wrapped at 11:05", base_state)
        assert result is not None and "ab 12:05" in result
        assert base_state.trend_ignore_before == datetime(2026, 10, 11, 12, 41).timestamp()

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_replaces_previous_wrap_guard(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 12, 44).timestamp()
        base_state.wrapped_at = datetime(2026, 10, 11, 12, 21).timestamp()
        base_state.trend_ignore_before = datetime(2026, 10, 11, 12, 22).timestamp()
        with patch.object(timeutil, "now", return_value=now):
            result = commands.handle_command("wrapped at 05:30", base_state)
        state = base_state.to_dict()  # fresh read; mypy keeps the narrowed attribute
        assert state["trend_ignore_before"] is None
        assert state["wrapped_at"] == datetime(2026, 10, 11, 5, 30).timestamp()
        assert result is not None and "ab 06:30" in result and "von 12:22 verworfen" in result

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_keeps_guard_before_previous_wrap(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 12, 44).timestamp()
        guard = datetime(2026, 10, 11, 10, 0).timestamp()
        base_state.wrapped_at = datetime(2026, 10, 11, 12, 21).timestamp()
        base_state.trend_ignore_before = guard
        with patch.object(timeutil, "now", return_value=now):
            result = commands.handle_command("wrapped at 05:30", base_state)
        assert base_state.trend_ignore_before == guard
        assert result is not None and "ab 06:30" in result and "verworfen" not in result

    @patch.object(watcher_state, "save_state")
    def test_wrapped_at_invalid_clock_rejected(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("wrapped at 25:00", base_state)
        assert result is not None and result.startswith("Fehler")
        assert base_state.wrapped_at is None

    @patch.object(watcher_state, "save_state")
    def test_resume_without_pause(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("resume", base_state) == "Keine Pause aktiv."

    @patch.object(watcher_state, "save_state")
    def test_resume_resets_max_ambient(self, _save: MagicMock, cooking_state: WatcherState) -> None:
        cooking_state.pause_until = 5000.0
        with patch.object(timeutil, "now", return_value=1000.0):
            result = commands.handle_command("resume", cooking_state)
        assert _field(cooking_state, "pause_until") is None
        assert cooking_state.max_ambient_temp is None
        assert cooking_state.trend_ignore_before == 1000.0
        assert result is not None and "Pause beendet" in result

    @patch.object(watcher_state, "save_state")
    def test_pause_shown_in_summary(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.pause_until = 1000.0 + 12 * 60
        with patch.object(timeutil, "now", return_value=1000.0):
            lines = status_text.alert_summary(base_state)
        assert any(l.startswith("Pause: AKTIV (noch 12 Min.") for l in lines)

    @patch.object(watcher_state, "save_state")
    def test_set_pit(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("set pit 125", base_state)
        assert base_state.pit_temp == 125.0
        assert watcher_state.effective_pit_temp(base_state) == 125.0
        assert result is not None and "125" in result

    @patch.object(watcher_state, "save_state")
    def test_pit_out_of_range_rejected(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("pit 20", base_state)
        assert result is not None and result.startswith("Fehler")
        assert base_state.pit_temp is None

    @patch.object(watcher_state, "save_state")
    def test_reset_pit_uses_config_default(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.pit_temp = 125.0
        commands.handle_command("reset pit", base_state)
        assert _field(base_state, "pit_temp") is None
        assert watcher_state.effective_pit_temp(base_state) == watcher_settings.PIT_TEMP_DEFAULT == 110.0

    @patch.object(watcher_state, "save_state")
    def test_pit_in_summary(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert "Smoker-Temp (Pit): 110 C (Standard)" in status_text.alert_summary(base_state)

    @patch.object(watcher_state, "save_state")
    def test_wrapped_anchors_endphase_and_reset_wrap_clears(self, _save: MagicMock, base_state: WatcherState) -> None:
        with patch.object(timeutil, "now", return_value=1000.0):
            result = commands.handle_command("wrapped", base_state)
        assert base_state.wrapped_at == 1000.0
        assert result is not None and "Endphasen-Prognose" in result
        commands.handle_command("reset wrap", base_state)
        assert _field(base_state, "wrapped_at") is None

    @patch.object(watcher_state, "save_state")
    def test_pause_does_not_anchor_endphase(self, _save: MagicMock, base_state: WatcherState) -> None:
        commands.handle_command("pause", base_state)
        assert base_state.wrapped_at is None

    @patch.object(watcher_state, "save_state")
    def test_unknown_command(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("xyzzy", base_state) is None

    @patch.object(watcher_state, "save_state")
    def test_case_insensitivity(self, _save: MagicMock, base_state: WatcherState) -> None:
        assert commands.handle_command("STATUS", base_state) == "__status__"
        assert commands.handle_command("Help", base_state) == commands.HELP_TEXT

    @patch.object(watcher_state, "save_state")
    def test_tempdown_alias(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("tempdown an", base_state)
        assert base_state.tempalert_tempdown_enabled is True
        assert result is not None


# ============================================================================
# load_state / save_state
# ============================================================================


class TestStateIO:
    """Tests for load_state and save_state."""

    def test_load_state_no_file(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "nonexistent.json"
        with patch.object(watcher_state, "STATE_FILE", fake_file):
            state = watcher_state.load_state()
        assert state.tempalert_tempdown_enabled is True
        assert state.consecutive_errors == 0

    def test_load_state_existing_file(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"max_ambient_temp": 140.0}))
        with patch.object(watcher_state, "STATE_FILE", state_file):
            state = watcher_state.load_state()
        assert state.max_ambient_temp == 140.0
        # defaults applied for missing keys
        assert state.tempalert_tempdown_enabled is True

    def test_save_state_writes_json(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state = WatcherState()
        with patch.object(watcher_state, "STATE_FILE", state_file):
            watcher_state.save_state(state)
        data = json.loads(state_file.read_text())
        assert "last_check" in data
        assert data["last_check"] is not None

    def test_save_state_creates_parent_dirs(self, tmp_path: Path) -> None:
        state_file = tmp_path / "sub" / "dir" / "state.json"
        state = WatcherState()
        with patch.object(watcher_state, "STATE_FILE", state_file):
            watcher_state.save_state(state)
        assert state_file.exists()

    def test_save_state_roundtrip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state = WatcherState()
        state.max_internal_temp = 85.0
        with patch.object(watcher_state, "STATE_FILE", state_file):
            watcher_state.save_state(state)
            loaded = watcher_state.load_state()
        assert loaded.max_internal_temp == 85.0

    def test_load_no_file_uses_config_defaults(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "nonexistent.json"
        custom_alerts = AlertDefaultsConfig(
            tempalert_tempdown_enabled=False,
            tempalert_stall_enabled=True,
            stall_max_rate=2.5,
            tempalert_cookend_enabled=False,
            ruhephase_target_temp=55.0,
            wrap_target_temp=75.0,
            ambient_range_min=100.0,
            ambient_range_max=130.0,
        )
        mock_cfg = MagicMock()
        mock_cfg.alerts = custom_alerts
        with patch.object(watcher_state, "STATE_FILE", fake_file), patch.object(watcher_state, "cfg", mock_cfg):
            state = watcher_state.load_state()
        assert state.tempalert_tempdown_enabled is False
        assert state.tempalert_stall_enabled is True
        assert state.stall_max_rate == 2.5
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
        with patch.object(watcher_state, "STATE_FILE", state_file), patch.object(watcher_state, "cfg", mock_cfg):
            state = watcher_state.load_state()
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
        with patch.object(watcher_state, "STATE_FILE", fake_file), patch.object(watcher_state, "cfg", mock_cfg):
            state = watcher_state.load_state()
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
            stall_max_rate=3.0,
            tempalert_wrap_enabled=True,
            wrap_target_temp=75.0,
            tempalert_ambient_range_enabled=True,
            ambient_range_min=100.0,
            ambient_range_max=130.0,
            tempalert_cookend_enabled=False,
        )
        watcher_state.apply_alert_defaults(state, alert_cfg)
        assert state.tempalert_tempdown_enabled is False
        assert state.tempalert_ruhephase_enabled is True
        assert state.ruhephase_target_temp == 55.0
        assert state.tempalert_stall_enabled is True
        assert state.stall_max_rate == 3.0
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
    """Tests for get_meater_data — patch extract_cook_data."""

    @patch("wachtmeater.meater_monitor.extract_cook_data")
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

    @patch("wachtmeater.meater_monitor.extract_cook_data")
    def test_extract_raises_returns_error(self, mock_extract: MagicMock) -> None:
        mock_extract.side_effect = RuntimeError("browser connection failed")
        result = mw.get_meater_data()
        assert "error" in result
        assert "browser connection failed" in result["error"]

    @patch("wachtmeater.meater_monitor.extract_cook_data")
    def test_timeout_exception(self, mock_extract: MagicMock) -> None:
        mock_extract.side_effect = TimeoutError("Timeout")
        result = mw.get_meater_data()
        assert "error" in result
        assert "Timeout" in result["error"]

    @patch("wachtmeater.meater_monitor.extract_cook_data")
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
            "call_pitmaster": patch.object(alerting, "call_pitmaster"),
            "save_state": patch.object(watcher_state, "save_state"),
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

    def test_cook_gone_error_reaches_cookend_threshold(self, base_state: WatcherState) -> None:
        # A genuine "cook URL no longer exists" error counts toward cook-end.
        base_state.tempalert_cookend_enabled = True
        base_state.consecutive_errors = 2  # one more → threshold (3)
        mock_send = MagicMock()
        with (
            patch.object(
                mw,
                "get_meater_data",
                return_value={"error": "HTTP 404", "error_kind": "cook_gone"},
            ),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(base_state, send=mock_send)
        assert result == "__cook_ended__"
        assert base_state.cook_ended is True
        mock_call.assert_called_once()
        mock_send.assert_called_once()

    def test_eta_only_sends_compact_message_without_screenshot(self, base_state: WatcherState) -> None:
        mock_send = MagicMock()
        with (
            patch.object(mw, "get_meater_data", return_value=self._make_data()),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=mock_send, eta_only=True)
        mock_send.assert_called_once()
        text, image = mock_send.call_args.args
        assert image is None
        assert text.startswith("MEATER ETA: Brisket")
        assert "Remaining: 2h 30m" in text
        assert "Target: 96.0 C" in text
        # The full status block (URL, alert summary, elapsed) is left out.
        assert "URL:" not in text
        assert "Elapsed:" not in text

    def test_full_status_still_sends_screenshot(self, base_state: WatcherState) -> None:
        mock_send = MagicMock()
        with (
            patch.object(mw, "get_meater_data", return_value=self._make_data()),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=mock_send)
        text, image = mock_send.call_args.args
        assert image == "/tmp/test.png"
        assert "Remaining: 2h 30m" in text
        assert "Elapsed: 4h" in text

    def test_transient_error_never_ends_cook(self, base_state: WatcherState) -> None:
        # A CDP/timeout (our-side) error must NOT be mistaken for cook-end,
        # even when already at the error threshold.
        base_state.tempalert_cookend_enabled = True
        base_state.consecutive_errors = 2  # already one below threshold (3)
        mock_send = MagicMock()
        with (
            patch.object(
                mw,
                "get_meater_data",
                return_value={"error": "CDP connect failed", "error_kind": "transient"},
            ),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(base_state, send=mock_send)
        assert result != "__cook_ended__"
        assert base_state.cook_ended is False
        # Transient errors are neutral: they neither escalate nor bump the counter.
        assert base_state.consecutive_errors == 2
        mock_call.assert_not_called()

    def test_error_without_kind_defaults_to_transient(self, base_state: WatcherState) -> None:
        # Backwards-compat: an error dict with no error_kind is treated as
        # transient (safe default) and never ends the cook.
        base_state.tempalert_cookend_enabled = True
        base_state.consecutive_errors = 2
        with (
            patch.object(mw, "get_meater_data", return_value={"error": "boom"}),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(base_state)
        assert result != "__cook_ended__"
        assert base_state.cook_ended is False
        mock_call.assert_not_called()

    def test_normal_path_updates_max(self, base_state: WatcherState) -> None:
        data = self._make_data()
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(base_state)
        assert result != "__cook_ended__"
        assert base_state.max_ambient_temp == 115
        assert base_state.max_internal_temp == 72

    def test_reset_ambient_during_check_is_not_clobbered(self, cooking_state: WatcherState) -> None:
        # The check runs in a worker thread; a `reset ambient` landing while the
        # scrape is in flight must survive the write-back at the end of the check.
        data = self._make_data(ambient_temp_c=95)

        def _fetch() -> dict[str, Any]:
            cooking_state.max_ambient_temp = None  # operator resets mid-check
            return data

        with (
            patch.object(mw, "get_meater_data", side_effect=_fetch),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(cooking_state)
        # Re-seeded from the current reading, not restored to the stale 120.0.
        assert cooking_state.max_ambient_temp == 95

    def test_tempdown_triggers(self, cooking_state: WatcherState) -> None:
        cooking_state.tempalert_tempdown_enabled = True
        cooking_state.max_ambient_temp = 130.0
        # Current ambient drops by 20 from max (>= threshold of 10)
        data = self._make_data(ambient_temp_c=110)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
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
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(cooking_state)
        mock_call.assert_called()
        assert cooking_state.tempalert_ruhephase_enabled is False

    NOW = 1_800_000_000.0

    def _seed_internal(self, state: WatcherState, temps: list[float], step: int = 600) -> None:
        """Seed timestamped core samples ending one *step* before ``NOW``."""
        n = len(temps)
        state.internal_samples = [[self.NOW - (n - i) * step, t] for i, t in enumerate(temps)]

    def _run_stall(self, state: WatcherState, internal: float, send: MagicMock | None = None) -> MagicMock:
        data = self._make_data(internal_temp_c=internal)
        with (
            patch.object(timeutil, "now", return_value=self.NOW),
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(state, send=send)
        return mock_call

    def test_stall_triggers(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_max_rate = 4.0
        # 70.0 -> 71.2 over 30 min = 2.4 C/h, inside the core range
        self._seed_internal(base_state, [70.0, 70.4, 70.8])
        send = MagicMock()
        mock_call = self._run_stall(base_state, 71.2, send)
        mock_call.assert_called()
        assert base_state.stall_alerted is True
        msgs = [c.args[0] for c in send.call_args_list]
        assert any("STALL ERKANNT" in m and "C/h" in m and "Jetzt wrappen?" in m for m in msgs)

    def test_stall_ignores_falling_temperature(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        self._seed_internal(base_state, [72.0, 71.8, 71.6])
        mock_call = self._run_stall(base_state, 71.4)
        assert base_state.stall_alerted is False
        assert not any("Stall" in c.args[0] for c in mock_call.call_args_list)

    def test_stall_outside_core_range_is_ignored(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        self._seed_internal(base_state, [50.0, 50.1, 50.2])
        self._run_stall(base_state, 50.3)
        assert base_state.stall_alerted is False

    def test_stall_needs_full_time_window(self, base_state: WatcherState) -> None:
        # Three flat readings at a 60 s interval only cover 3 minutes — far
        # short of the 30-minute window, so no verdict yet.
        base_state.tempalert_stall_enabled = True
        self._seed_internal(base_state, [70.0, 70.0, 70.0], step=60)
        self._run_stall(base_state, 70.0)
        assert base_state.stall_alerted is False

    def test_stall_window_is_interval_independent(self, base_state: WatcherState) -> None:
        # Same 2.4 C/h plateau sampled every 60 s over 30 minutes fires too.
        base_state.tempalert_stall_enabled = True
        self._seed_internal(base_state, [70.0 + 0.04 * i for i in range(30)], step=60)
        self._run_stall(base_state, 71.2)
        assert base_state.stall_alerted is True

    def test_stall_after_wrap_does_not_ask_to_wrap(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.wrap_alerted = True
        self._seed_internal(base_state, [70.0, 70.4, 70.8])
        send = MagicMock()
        self._run_stall(base_state, 71.2, send)
        stall_msgs = [c.args[0] for c in send.call_args_list if "STALL" in c.args[0]]
        assert stall_msgs and all("wrappen?" not in m for m in stall_msgs)

    def test_stall_does_not_retrigger(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = True  # already alerted
        self._seed_internal(base_state, [70.0, 70.0, 70.0])
        mock_call = self._run_stall(base_state, 70.0)
        # call_pitmaster should not be called for stall (already alerted)
        for call in mock_call.call_args_list:
            assert "Stall" not in call[0][0]

    def test_stall_resets_on_recovery(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        base_state.stall_alerted = True
        base_state.stall_max_rate = 4.0
        # 70 -> 75 in 30 min = 10 C/h >= 2 x threshold -> re-armed
        self._seed_internal(base_state, [70.0, 71.7, 73.3])
        self._run_stall(base_state, 75.0)
        assert base_state.stall_alerted is False

    def test_stall_message_failure_is_retried(self, base_state: WatcherState) -> None:
        base_state.tempalert_stall_enabled = True
        self._seed_internal(base_state, [70.0, 70.4, 70.8])

        def _send(text: str, image: str | None) -> None:
            if "STALL" in text:
                raise RuntimeError("homeserver down")

        mock_call = self._run_stall(base_state, 71.2, MagicMock(side_effect=_send))
        assert base_state.stall_alerted is False  # not delivered -> not marked
        assert sum("Stall" in c.args[0] for c in mock_call.call_args_list) == 1
        # Next check: message gets through, but the phone does not ring twice.
        self.NOW += 600
        self._seed_internal(base_state, [70.4, 70.8, 71.2])
        send_ok = MagicMock()
        mock_call = self._run_stall(base_state, 71.6, send_ok)
        assert _field(base_state, "stall_alerted") is True
        assert any("STALL" in c.args[0] for c in send_ok.call_args_list)
        assert not any("Stall" in c.args[0] for c in mock_call.call_args_list)
        assert base_state.alert_calls_sent == []

    def test_wrap_triggers(self, base_state: WatcherState) -> None:
        base_state.tempalert_wrap_enabled = True
        base_state.wrap_target_temp = 75.0
        data = self._make_data(internal_temp_c=76)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert base_state.wrap_alerted is True

    def test_pull_override_triggers_below_meater_target(self, base_state: WatcherState) -> None:
        # Override at 88; internal 89 reaches it even though the MEATER target
        # (96) is not yet hit. Alert goes out as message AND call.
        base_state.target_override = 88.0
        data = self._make_data(internal_temp_c=89, target_temp_c=96)
        mock_send = MagicMock()
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=mock_send)
        call_msg = mock_call.call_args[0][0]
        assert "Rausnehmen" in call_msg and "88" in call_msg
        assert any("Rausnehmen" in c.args[0] for c in mock_send.call_args_list)
        assert base_state.target_reached_calls == 1

    def test_pull_override_replaces_meater_target_message(self, base_state: WatcherState) -> None:
        # When an override is set and reached, the wording is the take-out
        # message, not the MEATER "Brisket ist fertig" one.
        base_state.target_override = 90.0
        data = self._make_data(internal_temp_c=97, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=MagicMock())
        msgs = [c.args[0] for c in mock_call.call_args_list]
        assert any("Rausnehmen" in m for m in msgs)
        assert not any("ist fertig" in m for m in msgs)

    def test_no_pull_override_uses_meater_target(self, base_state: WatcherState) -> None:
        # Without an override, the MEATER target (96) governs; internal 89 < 96
        # so no target-reached notification fires.
        data = self._make_data(internal_temp_c=89, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=MagicMock())
        msgs = [c.args[0] for c in mock_call.call_args_list]
        assert not any("Rausnehmen" in m or "ist fertig" in m for m in msgs)

    def test_ambient_too_low(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        data = self._make_data(ambient_temp_c=90)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
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
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
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
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state)
        # call_pitmaster should not be called for ambient alert
        for call in mock_call.call_args_list:
            assert "Ambient" not in call[0][0]

    def test_target_reached(self, base_state: WatcherState) -> None:
        data = self._make_data(internal_temp_c=96, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state)
        mock_call.assert_called()
        assert "fertig" in mock_call.call_args[0][0]

    def test_target_reached_capped_at_3(self, base_state: WatcherState) -> None:
        base_state.target_reached_calls = 3
        data = self._make_data(internal_temp_c=96, target_temp_c=96)
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
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
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(base_state)
        assert result == "__cook_ended__"
        assert base_state.cook_ended is True

    def _run_with_send(self, state: WatcherState, send: MagicMock, **data: Any) -> tuple[str, MagicMock]:
        with (
            patch.object(mw, "get_meater_data", return_value=self._make_data(**data)),
            patch.object(alerting, "call_pitmaster") as mock_call,
            patch.object(watcher_state, "save_state"),
        ):
            result = mw.run_meater_check(state, send=send)
        return result, mock_call

    @staticmethod
    def _failing_send(needle: str) -> MagicMock:
        def _send(text: str, image: str | None) -> None:
            if needle in text:
                raise RuntimeError("M_FORBIDDEN")

        return MagicMock(side_effect=_send)

    @staticmethod
    def _texts(send: MagicMock) -> list[str]:
        return [c.args[0] for c in send.call_args_list]

    def test_tempdown_posts_message(self, cooking_state: WatcherState) -> None:
        cooking_state.max_ambient_temp = 130.0
        send = MagicMock()
        self._run_with_send(cooking_state, send, ambient_temp_c=110)
        assert any(t.startswith("ACHTUNG") for t in self._texts(send))

    def test_tempdown_muted_during_pause(self, cooking_state: WatcherState) -> None:
        cooking_state.max_ambient_temp = 130.0
        cooking_state.pause_until = 2_000_000_000.0
        send = MagicMock()
        with patch.object(timeutil, "now", return_value=1_900_000_000.0):
            _, mock_call = self._run_with_send(cooking_state, send, ambient_temp_c=80)
        assert not any("ACHTUNG" in t for t in self._texts(send))
        mock_call.assert_not_called()

    def test_ambient_range_muted_during_pause_but_pull_stays_active(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        base_state.target_override = 90.0
        base_state.pause_until = 2_000_000_000.0
        send = MagicMock()
        with patch.object(timeutil, "now", return_value=1_900_000_000.0):
            _, mock_call = self._run_with_send(base_state, send, ambient_temp_c=80, internal_temp_c=91)
        texts = self._texts(send)
        assert not any("Ambient zu niedrig" in t for t in texts)
        assert any("Rausnehmen" in t for t in texts)
        assert base_state.target_reached_calls == 1

    def test_pause_expiry_resets_max_ambient(self, cooking_state: WatcherState) -> None:
        cooking_state.max_ambient_temp = 130.0
        cooking_state.pause_until = 1_000.0
        send = MagicMock()
        with patch.object(timeutil, "now", return_value=2_000.0):
            result, mock_call = self._run_with_send(cooking_state, send, ambient_temp_c=100)
        assert _field(cooking_state, "pause_until") is None
        assert cooking_state.max_ambient_temp == 100
        assert "Pause beendet" in result
        mock_call.assert_not_called()

    def test_ruhephase_posts_message_and_stays_armed_on_send_failure(self, cooking_state: WatcherState) -> None:
        cooking_state.tempalert_ruhephase_enabled = True
        cooking_state.ruhephase_target_temp = 75.0
        _, mock_call = self._run_with_send(cooking_state, self._failing_send("Ruhephase"), internal_temp_c=74)
        assert cooking_state.tempalert_ruhephase_enabled is True
        mock_call.assert_called_once()
        send = MagicMock()
        _, mock_call = self._run_with_send(cooking_state, send, internal_temp_c=73)
        assert any("Ruhephase beendet" in t for t in self._texts(send))
        assert _field(cooking_state, "tempalert_ruhephase_enabled") is False
        mock_call.assert_not_called()  # the call already went out

    def test_ambient_range_posts_message(self, base_state: WatcherState) -> None:
        base_state.tempalert_ambient_range_enabled = True
        base_state.ambient_range_min = 100.0
        base_state.ambient_range_max = 130.0
        send = MagicMock()
        self._run_with_send(base_state, send, ambient_temp_c=140)
        assert any("Ambient zu hoch" in t for t in self._texts(send))

    def test_target_reached_without_override_posts_message(self, base_state: WatcherState) -> None:
        send = MagicMock()
        self._run_with_send(base_state, send, internal_temp_c=96, target_temp_c=96)
        assert any("fertig" in t for t in self._texts(send))
        assert base_state.target_reached_calls == 1

    def test_pull_not_counted_when_message_fails(self, base_state: WatcherState) -> None:
        base_state.target_override = 88.0
        _, mock_call = self._run_with_send(base_state, self._failing_send("Rausnehmen"), internal_temp_c=89)
        mock_call.assert_called_once()  # pull alarm still rings
        assert base_state.target_reached_calls == 0

    def test_cookend_done_posts_message(self, base_state: WatcherState) -> None:
        send = MagicMock()
        result, _ = self._run_with_send(base_state, send, status="done")
        assert result == "__cook_ended__"
        assert any("Status 'done'" in t for t in self._texts(send))

    def test_probe_removed_not_ended_until_message_delivered(self, base_state: WatcherState) -> None:
        base_state.max_internal_temp = 80.0
        result, mock_call = self._run_with_send(base_state, self._failing_send("Cook-Ende"), internal_temp_c=30)
        assert result != "__cook_ended__"
        assert base_state.cook_ended is False
        mock_call.assert_called_once()
        result, mock_call = self._run_with_send(base_state, MagicMock(), internal_temp_c=30)
        assert result == "__cook_ended__"
        mock_call.assert_not_called()

    def test_offline_notice_retried_until_delivered(self, base_state: WatcherState) -> None:
        offline = {"status": "offline", "cook_name": "Brisket"}
        failing = MagicMock(side_effect=RuntimeError("down"))
        with (
            patch.object(mw, "get_meater_data", return_value=offline),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
        ):
            mw.run_meater_check(base_state, send=failing)
            assert base_state.offline_notified is False
            send = MagicMock()
            mw.run_meater_check(base_state, send=send)
            assert _field(base_state, "offline_notified") is True
            mw.run_meater_check(base_state, send=send)
        assert sum("offline" in t for t in self._texts(send)) == 1

    def test_remaining_zero_shown_as_unknown(self, base_state: WatcherState) -> None:
        result, _ = self._run_with_send(base_state, MagicMock(), remaining_time="0m", remaining_minutes=0)
        assert "Remaining: 0m" not in result
        assert "Remaining: unbekannt" in result

    def test_remaining_positive_shown(self, base_state: WatcherState) -> None:
        result, _ = self._run_with_send(base_state, MagicMock(), remaining_time="2h 30m", remaining_minutes=150)
        assert "Remaining: 2h 30m" in result

    def test_target_line_without_override(self, base_state: WatcherState) -> None:
        result, _ = self._run_with_send(base_state, MagicMock(), target_temp_c=98)
        assert "Target: 98.0 C\n" in result

    def test_warmup_spike_ignored_for_tempdown(self, base_state: WatcherState) -> None:
        # 26.09.: ambient overshoots to 117 C at core 26 C, settles at ~105 C.
        send = MagicMock()
        _, mock_call = self._run_with_send(base_state, send, internal_temp_c=25.7, ambient_temp_c=117.1)
        assert base_state.max_ambient_temp is None
        _, mock_call = self._run_with_send(base_state, send, internal_temp_c=41.0, ambient_temp_c=105.0)
        assert _field(base_state, "max_ambient_temp") == 105.0
        assert not any("ACHTUNG" in t for t in self._texts(send))
        mock_call.assert_not_called()

    def test_tempdown_still_fires_after_warmup(self, base_state: WatcherState) -> None:
        send = MagicMock()
        self._run_with_send(base_state, send, internal_temp_c=45.0, ambient_temp_c=110.0)
        _, mock_call = self._run_with_send(base_state, send, internal_temp_c=46.0, ambient_temp_c=95.0)
        assert any(t.startswith("ACHTUNG") for t in self._texts(send))
        mock_call.assert_called_once()

    def test_warmup_disabled_tracks_from_first_reading(self, base_state: WatcherState) -> None:
        with patch.object(mw, "TEMPDOWN_WARMUP_CORE_TEMP", 0.0):
            self._run_with_send(base_state, MagicMock(), internal_temp_c=20.0, ambient_temp_c=117.0)
        assert base_state.max_ambient_temp == 117.0

    def test_linear_forecast_capped(self, base_state: WatcherState) -> None:
        # 0.12 C/h over the last hour, 6 C to go -> 50 h -> "nicht absehbar"
        base_state.internal_samples = [[1_000_000.0 + i * 600, 90.0 + 0.02 * i] for i in range(7)]
        line = status_text.forecast_line(base_state, 1_000_000.0 + 3600, 90.12, 96.0)
        assert line is not None and "nicht absehbar" in line

    def test_endphase_flat_window_capped(self, base_state: WatcherState) -> None:
        # 02.05.: core stood at 93 C for an hour -> ETA years ahead before the fix
        t0 = 1_000_000.0
        base_state.wrapped_at = t0 - 3 * 3600
        base_state.internal_samples = [[t0 - 2 * 3600 + i * 600, 93.0] for i in range(13)]
        lines = status_text.endphase_lines(base_state, t0, 93.0, 94.0)
        assert len(lines) == 1 and lines[0].endswith("nicht absehbar (Kern steigt kaum)")

    def test_endphase_uses_pit_and_fixed_start(self, base_state: WatcherState) -> None:
        t0 = 1_000_000.0
        base_state.wrapped_at = t0 - 4 * 3600
        base_state.pit_temp = 105.0
        # Exact Newton curve from wrap+60: A=105, c=0.1/h, T0=80
        start = base_state.wrapped_at + 3600
        base_state.internal_samples = [
            [start - 1800, 70.0],
            *[[start + i * 600, 105.0 - 25.0 * math.exp(-0.1 * i / 6)] for i in range(19)],
        ]
        now = start + 18 * 600
        core = base_state.internal_samples[-1][1]
        lines = status_text.endphase_lines(base_state, now, core, 96.0)
        expected = now + math.log((105.0 - core) / (105.0 - 96.0)) / 0.1 * 3600
        assert len(lines) == 1
        assert f"A=Pit 105 C, Fit {timeutil.clock(start, now)}-" in lines[0]
        assert "RMS 0.00 C" in lines[0]
        assert lines[0].endswith(f"~{timeutil.clock(expected, now)}")

    def test_endphase_outlier_in_last_reading(self, base_state: WatcherState) -> None:
        # Same curve with a little noise, last reading 1 C too low (glitch):
        # main ETA from the curve value, measured-value ETA as a hint line.
        t0 = 1_000_000.0
        base_state.wrapped_at = t0 - 4 * 3600
        base_state.pit_temp = 105.0
        start = base_state.wrapped_at + 3600
        noise = [0.05, -0.04, 0.03, -0.05, 0.04, -0.03, 0.05, -0.04, 0.02, -0.05]
        curve = [105.0 - 25.0 * math.exp(-0.1 * i / 6) + noise[i % 10] for i in range(19)]
        curve[-1] -= 1.0
        base_state.internal_samples = [[start + i * 600, k] for i, k in enumerate(curve)]
        now = start + 18 * 600
        lines = status_text.endphase_lines(base_state, now, curve[-1], 96.0)
        clean = 105.0 - 25.0 * math.exp(-0.3)
        expected = now + math.log((105.0 - clean) / (105.0 - 96.0)) / 0.1 * 3600
        assert len(lines) == 2
        eta = datetime.strptime(lines[0].rsplit("~", 1)[1], "%H:%M")
        ref = datetime.strptime(timeutil.clock(expected, now), "%H:%M")
        assert abs((eta - ref).total_seconds()) <= 5 * 60
        assert lines[1].startswith(f"  Letzter Messwert {curve[-1]:.1f} C weicht von der Kurve")
        assert "ab Messwert: ~" in lines[1]

    def _rest_state(self, base_state: WatcherState, start: float, n: int) -> list[float]:
        # Pull at *start*, 20 min carry-over rise, then Newton cooling to 22 C
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = 65.0
        base_state.ruhephase_started_at = start
        peak = start + 1200
        curve = [22.0 + 72.0 * math.exp(-0.2 * i / 6) for i in range(n)]
        base_state.internal_samples = [
            [start - 600, 95.0],  # before 'enable ruhephase': ignored
            [start, 93.0],
            [start + 600, 93.6],
            *[[peak + i * 600, k] for i, k in enumerate(curve)],
        ]
        return curve

    def test_restphase_collecting(self, base_state: WatcherState) -> None:
        start = 1_000_000.0
        curve = self._rest_state(base_state, start, 3)
        now = start + 1200 + 1200
        lines = status_text.restphase_lines(base_state, now, curve[-1])
        assert len(lines) == 1 and "sammelt Daten" in lines[0] and "94.0 C" in lines[0]

    def test_restphase_forecast_from_peak(self, base_state: WatcherState) -> None:
        start = 1_000_000.0
        curve = self._rest_state(base_state, start, 13)
        peak = start + 1200
        now = peak + 12 * 600
        with patch.object(status_text, "ROOM_TEMP", 22.0):
            lines = status_text.restphase_lines(base_state, now, curve[-1])
        expected = now + math.log((curve[-1] - 22.0) / (65.0 - 22.0)) / 0.2 * 3600
        assert len(lines) == 1
        assert f"A=Umgebung 22 C, Fit {timeutil.clock(peak, now)}-" in lines[0]
        assert lines[0].endswith(f"~{timeutil.clock(expected, now)}")

    def test_restphase_replaces_pull_forecasts(self, base_state: WatcherState) -> None:
        start = 1_000_000.0
        curve = self._rest_state(base_state, start, 13)
        base_state.wrapped_at = start - 6 * 3600
        now = start + 1200 + 12 * 600
        lines = status_text.eta_lines(base_state, now, {}, curve[-1], 96.0)
        assert lines[1].startswith("Ruhephasen-Prognose")
        assert not any("Endphasen" in l or "Watcher-Prognose" in l for l in lines)

    def test_restphase_off_or_reached(self, base_state: WatcherState) -> None:
        start = 1_000_000.0
        curve = self._rest_state(base_state, start, 13)
        now = start + 1200 + 12 * 600
        assert status_text.restphase_lines(base_state, now, 64.0) == []
        base_state.tempalert_ruhephase_enabled = False
        assert status_text.restphase_lines(base_state, now, curve[-1]) == []

    def test_restphase_config_default_needs_falling_core(self, base_state: WatcherState) -> None:
        # Enabled by config default (no 'enable ruhephase' time): silent while heating
        base_state.tempalert_ruhephase_enabled = True
        base_state.ruhephase_target_temp = 65.0
        base_state.internal_samples = [[1_000_000.0 + i * 600, 70.0 + i] for i in range(6)]
        assert status_text.restphase_lines(base_state, 1_003_000.0, 75.0) == []

    def test_resets_consecutive_errors_on_success(self, base_state: WatcherState) -> None:
        base_state.consecutive_errors = 2
        data = self._make_data()
        with (
            patch.object(mw, "get_meater_data", return_value=data),
            patch.object(alerting, "call_pitmaster"),
            patch.object(watcher_state, "save_state"),
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
    from wachtmeater.messaging import RoomSelection

    m.get_or_create_room = AsyncMock(return_value=RoomSelection(broadcast=None, cook="!room:test"))
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
            patch.object(watcher_state, "load_state", return_value=WatcherState()),
            patch.object(watcher_state, "save_state"),
            patch.object(alerting, "call_pitmaster"),
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
            patch.object(watcher_state, "load_state", return_value=WatcherState()),
            patch.object(watcher_state, "save_state"),
            patch.object(alerting, "call_pitmaster"),
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
        assert commands.HELP_TEXT in greeting_call[0][1]

    def test_initial_check_runs_with_sender_after_connect(self) -> None:
        """The initial check runs with the messaging sender so it posts status + screenshot."""

        async def _stop_sync() -> None:
            await asyncio.sleep(0.05)

        mock_messaging = _make_mock_messaging(sync_side_effect=_stop_sync)

        with (
            patch.object(mw, "MEATER_URL", "https://meater.io/test-uuid"),
            patch.object(watcher_state, "load_state", return_value=WatcherState()),
            patch.object(watcher_state, "save_state"),
            patch.object(alerting, "call_pitmaster"),
            patch.object(mw, "run_meater_check", return_value="ok") as mock_check,
            patch.object(mw, "cfg") as mock_cfg,
        ):
            mock_cfg.matrix.room = "!room:test"
            mock_cfg.matrix.auto_create_room_for_meater_uuid = False
            mock_cfg.matrix.pitmaster = ""

            asyncio.run(mw.event_loop(skip_startup_test_call=True, messaging=mock_messaging))

        # The initial check must run AFTER connect, with the sender closure as
        # its second argument (so status text + screenshot reach the room).
        mock_messaging.connect.assert_awaited()
        assert mock_check.call_count >= 1
        first_call = mock_check.call_args_list[0]
        assert len(first_call.args) == 2  # (state, send_callback)
        assert callable(first_call.args[1])


class TestUpdateRoomRouting:
    """Tests which rooms cook updates are sent to."""

    @staticmethod
    def _run(*, updates_to_broadcast_room: bool, cook_room: str | None) -> list[str]:
        """Run event_loop once and return the rooms a status update reached.

        Args:
            updates_to_broadcast_room: Value of the config flag under test.
            cook_room: Per-cook room ID, or ``None`` for broadcast-only setups.

        Returns:
            Room IDs that received the ``STATUS`` update message.
        """
        from wachtmeater.messaging import RoomSelection

        async def _stop_sync() -> None:
            await asyncio.sleep(0.05)

        mock_messaging = _make_mock_messaging(sync_side_effect=_stop_sync)
        mock_messaging.get_or_create_room = AsyncMock(
            return_value=RoomSelection(broadcast="!broadcast:test", cook=cook_room)
        )

        def _check(state: WatcherState, send: Any) -> str:
            send("STATUS", None)
            return "ok"

        with (
            patch.object(mw, "MEATER_URL", "https://meater.io/test-uuid"),
            patch.object(watcher_state, "load_state", return_value=WatcherState()),
            patch.object(watcher_state, "save_state"),
            patch.object(alerting, "call_pitmaster"),
            patch.object(mw, "run_meater_check", side_effect=_check),
            patch.object(mw, "cfg") as mock_cfg,
        ):
            mock_cfg.matrix.room = "!broadcast:test"
            mock_cfg.matrix.auto_create_room_for_meater_uuid = cook_room is not None
            mock_cfg.matrix.updates_to_broadcast_room = updates_to_broadcast_room
            mock_cfg.matrix.pitmaster = ""

            asyncio.run(mw.event_loop(skip_startup_test_call=True, messaging=mock_messaging))

        return [c[0][0] for c in mock_messaging.send_message.call_args_list if c[0][1] == "STATUS"]

    def test_updates_go_to_both_rooms_when_enabled(self) -> None:
        """With the flag on, updates reach the broadcast room and the cook room."""
        rooms = self._run(updates_to_broadcast_room=True, cook_room="!cook:test")
        assert rooms == ["!broadcast:test", "!cook:test"]

    def test_updates_stay_in_cook_room_when_disabled(self) -> None:
        """With the flag off, the broadcast room is skipped."""
        rooms = self._run(updates_to_broadcast_room=False, cook_room="!cook:test")
        assert rooms == ["!cook:test"]

    def test_flag_is_ignored_without_cook_room(self) -> None:
        """Without a per-cook room the broadcast room stays the only target."""
        rooms = self._run(updates_to_broadcast_room=False, cook_room=None)
        assert rooms == ["!broadcast:test"]


# ============================================================================
# Periodic check scheduling
# ============================================================================


class TestPeriodicSchedule:
    """Checks run on fixed deadlines; their own duration does not add drift."""

    def test_check_duration_does_not_accumulate(self) -> None:
        import time as _time

        interval = 0.2
        check_cost = 0.08
        starts: list[float] = []
        loop_holder: dict[str, Any] = {}

        def _slow_check(state: WatcherState, send: Any = None) -> str:
            starts.append(_time.monotonic())
            _time.sleep(check_cost)
            return "ok"

        async def _sync() -> None:
            # 1 initial check + 5 periodic ones, then stop
            while len(starts) < 6:
                await asyncio.sleep(0.01)

        mock_messaging = _make_mock_messaging(sync_side_effect=_sync)
        with (
            patch.object(mw, "MEATER_URL", "https://meater.io/test-uuid"),
            patch.object(watcher_state, "load_state", return_value=WatcherState()),
            patch.object(watcher_state, "save_state"),
            patch.object(alerting, "call_pitmaster"),
            patch.object(mw, "run_meater_check", side_effect=_slow_check),
            patch.object(mw, "effective_check_interval", return_value=interval),
            patch.object(mw, "cfg") as mock_cfg,
        ):
            mock_cfg.matrix.room = "!room:test"
            mock_cfg.matrix.auto_create_room_for_meater_uuid = False
            mock_cfg.matrix.pitmaster = ""
            asyncio.run(mw.event_loop(skip_startup_test_call=True, messaging=mock_messaging))

        periodic = starts[1:6]
        span = periodic[-1] - periodic[0]
        # 4 gaps: fixed-rate ~0.8 s; the old wait-after-check scheme ~1.12 s.
        assert span == pytest.approx(4 * interval, abs=0.1)


def test_append_sample_keeps_whole_cook() -> None:
    now = 200_000.0
    samples = [[now - 30 * 3600, 20.0], [now - 49 * 3600, 10.0]]
    kept = signal_stats.append_sample(samples, now, 90.0)
    assert kept == [[now - 30 * 3600, 20.0], [now, 90.0]]


class TestAmbientRiseDisturbance:
    """A fire flare-up restarts trend and fit; recovery from a dip does not."""

    def _state(self, ambients: list[tuple[float, float]], core: float = 90.0) -> WatcherState:
        st = WatcherState()
        st.ambient_samples = [[t, a] for t, a in ambients]
        st.internal_samples = [[t, core] for t, _ in ambients]
        return st

    def test_spike_above_window_max(self) -> None:
        st = self._state([(0.0, 104.0), (600.0, 106.6), (1200.0, 108.2)])
        reason = signal_stats.detect_disturbance(st, 1800.0, 92.9, 113.9)
        assert reason is not None and "Ambient-Spitze" in reason

    def test_recovery_after_dip_is_no_spike(self) -> None:
        st = self._state([(0.0, 105.0), (600.0, 85.0), (1200.0, 95.0)], core=76.0)
        assert signal_stats.detect_disturbance(st, 1800.0, 76.0, 104.0) is None

    def test_warmup_rise_is_ignored(self) -> None:
        st = self._state([(0.0, 40.0), (600.0, 60.0), (1200.0, 80.0)], core=20.0)
        assert signal_stats.detect_disturbance(st, 1800.0, 20.0, 110.0) is None

    # Pit swinging ±4 C every reading (e.g. a pellet grill cycling)
    NOISY = [100.0, 106.0, 99.0, 105.0, 98.0, 107.0, 101.0]

    def test_noisy_pit_swing_is_no_spike(self) -> None:
        # 5.5 C above the window max would trip the fixed 5 C threshold.
        st = self._state([(i * 600.0, a) for i, a in enumerate(self.NOISY)])
        assert signal_stats.detect_disturbance(st, 4200.0, 92.0, 112.5) is None

    def test_noisy_pit_real_flare_up_still_detected(self) -> None:
        st = self._state([(i * 600.0, a) for i, a in enumerate(self.NOISY)])
        reason = signal_stats.detect_disturbance(st, 4200.0, 92.0, 135.0)
        assert reason is not None and "Ambient-Spitze" in reason


class TestNoiseSigma:
    def test_steady_trend_is_no_noise(self) -> None:
        samples = [[i * 600.0, 70.0 + 0.4 * i] for i in range(5)]
        assert signal_stats.noise_sigma(samples) == pytest.approx(0.0, abs=1e-9)

    def test_single_dip_barely_moves_estimate(self) -> None:
        calm = [[i * 600.0, a] for i, a in enumerate([102.0, 103.1, 101.8, 102.6, 102.2, 103.0, 101.9])]
        dipped = [*calm[:4], [2400.0, 78.0], *calm[5:]]
        assert signal_stats.noise_sigma(dipped) < 2 * signal_stats.noise_sigma(calm) < 2.0

    def test_alternating_swings_count_as_noise(self) -> None:
        samples = [[i * 600.0, a] for i, a in enumerate(TestAmbientRiseDisturbance.NOISY)]
        assert signal_stats.noise_sigma(samples) > 3.0

    def test_too_few_readings(self) -> None:
        assert signal_stats.noise_sigma([[0.0, 100.0], [600.0, 120.0]]) == 0.0


class TestTrendAbCommand:
    @patch.object(watcher_state, "save_state")
    def test_trend_ab_sets_trend_start(self, _save: MagicMock, base_state: WatcherState) -> None:
        now = datetime(2026, 10, 11, 13, 30).timestamp()
        with patch.object(timeutil, "now", return_value=now):
            result = commands.handle_command("trend ab 12:20", base_state)
        assert base_state.trend_ignore_before == datetime(2026, 10, 11, 12, 20).timestamp()
        assert result == "Trend und Stall-Rate werten ab 12:20 aus."

    @patch.object(watcher_state, "save_state")
    def test_trend_reset_clears(self, _save: MagicMock, base_state: WatcherState) -> None:
        base_state.trend_ignore_before = 1000.0
        result = commands.handle_command("trend reset", base_state)
        assert base_state.to_dict()["trend_ignore_before"] is None  # fresh read; mypy keeps the narrowed attribute
        assert result == "Trend und Stall-Rate werten wieder alle Daten aus."

    @patch.object(watcher_state, "save_state")
    def test_trend_ab_invalid_clock(self, _save: MagicMock, base_state: WatcherState) -> None:
        result = commands.handle_command("trend ab 24:00", base_state)
        assert result is not None and result.startswith("Fehler")

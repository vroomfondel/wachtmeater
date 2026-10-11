"""Watcher settings derived from the typed config (``wachtmeater.cfg``).

Read once at import; shared by the watcher modules (state file, alerts,
statistics, status text, commands).
"""

from pathlib import Path

from wachtmeater import cfg

MEATER_URL: str = cfg.meater.url

MEATER_UUID: str = MEATER_URL.split("/")[-1]

STATE_FILE_DIR: Path = Path(cfg.meater.state_file_dir or "/tmp")

STATE_FILE: Path = STATE_FILE_DIR / (cfg.meater.state_file_name or f"meater-state-{MEATER_UUID}.json")

AMBIENT_TEMP_DROP_THRESHOLD: int = cfg.monitoring.ambient_temp_drop_threshold
CHECK_INTERVAL: int = cfg.monitoring.check_interval
MIN_CHECK_INTERVAL: int = 30
"""Lower bound for the ``set interval`` command, to avoid hammering the cook page."""

# Stall detection: rise rate (C/h) over a time window, only inside a plausible
# core range.  Time-based so it does not depend on the check interval.
STALL_WINDOW_MINUTES: int = cfg.monitoring.stall_window_minutes
STALL_CORE_MIN: float = cfg.monitoring.stall_core_min
STALL_CORE_MAX: float = cfg.monitoring.stall_core_max
# Disturbance (lid open / wrapping / probe re-seated) -> pause stall evaluation
STALL_DISTURBANCE_AMBIENT_DROP: float = cfg.monitoring.stall_disturbance_ambient_drop
STALL_DISTURBANCE_CORE_DROP: float = cfg.monitoring.stall_disturbance_core_drop
STALL_DISTURBANCE_AMBIENT_RISE: float = cfg.monitoring.stall_disturbance_ambient_rise
STALL_DISTURBANCE_PAUSE_CHECKS: int = cfg.monitoring.stall_disturbance_pause_checks

# TempDown: max ambient is only tracked from this core temperature on, so
# warm-up overshoots (e.g. 117 C at core 26 C, then ~105 C) do not trip the
# alert for hours.  0 = track from the first reading.
TEMPDOWN_WARMUP_CORE_TEMP: float = cfg.monitoring.tempdown_warmup_core_temp

# ``pause`` default duration
PAUSE_DEFAULT_MINUTES: int = cfg.monitoring.pause_default_minutes

# Watcher's own ETA forecast (linear trend) window
FORECAST_WINDOW_MINUTES: int = cfg.monitoring.forecast_window_minutes


# End-phase forecast (exponential fit from ``wrapped`` on, ``set pit``)
PIT_TEMP_DEFAULT: float = cfg.monitoring.pit_temp
# Earliest fit start after ``wrapped``; the actual start is searched adaptively
ENDPHASE_SETTLE_MINUTES: int = cfg.monitoring.endphase_settle_minutes
ENDPHASE_MIN_MINUTES: int = cfg.monitoring.endphase_min_minutes
ENDPHASE_MAX_POINTS: int = 120
"""Samples are thinned to this many for the end-phase fit (bounds the cost per check)."""

# Cook-end detection: consecutive errors before declaring cook ended
COOKEND_ERROR_THRESHOLD: int = cfg.monitoring.cookend_error_threshold
# Internal temp below this after having been above 50 C => probe removed
COOKEND_PROBE_REMOVED_TEMP: float = cfg.monitoring.cookend_probe_removed_temp
# Consecutive offline cycles before SIP-escalating a station outage
STATION_OFFLINE_CALL_THRESHOLD: int = cfg.monitoring.station_offline_call_threshold

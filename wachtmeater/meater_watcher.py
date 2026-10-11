#!/home/node/.openclaw/workspace/.venv/bin/python3
"""MEATER Brisket Watcher with async event loop and Matrix command interface.

Runs a persistent asyncio event loop that:

1.  Periodically monitors a MEATER cook by calling the monitor module
    directly (every 10 minutes by default).
2.  Listens for commands in a Matrix room (MeaterWatcher) and reacts in
    real time.

Supported Matrix commands (case-insensitive):
    - ``status``                    — Fetch and post current MEATER status.
    - ``enable tempdown``           — Enable ambient temp drop alert (fire out).
    - ``disable tempdown``          — Disable ambient temp drop alert.
    - ``enable ruhephase <temp>``   — Enable rest/cooldown alert with target C.
      Automatically disables the tempdown alert, since the cooldown during the
      rest phase is intended and would otherwise raise a false "fire is out"
      alarm.  Re-arm tempdown manually afterwards.
    - ``disable ruhephase``         — Disable rest/cooldown alert (does not
      restore the tempdown alert).
    - ``enable stall [<rate>]``     — Enable stall detection (optional: rise
      rate threshold in C/h, default 4).  Fires when the core rises slower
      than that over the stall time window inside the plausible core range;
      falling temperatures and disturbances (lid open, probe re-seated) are
      not stalls.
    - ``disable stall``             — Disable stall detection.
    - ``enable wrap <temp>``        — Enable wrap reminder at internal temp C.
    - ``disable wrap``              — Disable wrap reminder.
    - ``enable pull <temp>``        — Take-out alert (message + call) at core
      temp C; overrides the MEATER target for the target-reached alert.
    - ``disable pull``              — Disable the take-out override.
    - ``enable ambient <min> <max>``— Enable ambient range alert (min/max C).
    - ``disable ambient``           — Disable ambient range alert.
    - ``reset ambient``             — Clear the tracked ambient high-water mark
      (re-arms the tempdown alert after a deliberate temperature drop).
    - ``pause [<min>]``             — Mute TempDown, stall and ambient range
      for *min* minutes (default 20), e.g. while wrapping; the ambient
      high-water mark is reset when the pause ends.  Pull, offline and
      cook-end alerts stay active.
    - ``wrapped [<min>]``           — Marks the wrap as done and anchors the
      end-phase forecast (exponential fit, see ``wachtmeater.core_forecast``);
      with *min* it also pauses like ``pause``.
    - ``wrapped at HH:MM``          — Same, backdated to the last HH:MM (no
      pause), for reporting the wrap afterwards.
    - ``trend ab HH:MM``            — Evaluate trend and stall rate only
      from HH:MM on (manual disturbance); ``trend reset`` clears it.
    - ``set pit <temp>``            — Real smoker temperature in C for the
      end-phase forecast (default from config, 110 C).
    - ``reset pit``                 — Back to the config default.
    - ``resume``                    — End a pause early.
    - ``enable cookend``            — Enable cook-end detection (auto-stop).
    - ``disable cookend``           — Disable cook-end detection.
    - ``enable offline``            — Enable station-offline alert (SIP on
      prolonged outage).
    - ``disable offline``           — Disable station-offline alert.
    - ``set interval <secs>``       — Override the periodic check interval
      (seconds); applies from the next cycle and persists per cook.
    - ``reset interval``            — Restore the config-default interval.
    - ``enable startcall``          — Enable the startup test call (applied
      on the next watcher start).
    - ``disable startcall``         — Disable the startup test call.
    - ``hilfe`` / ``help``          — Show available commands.
    - ``stop`` / ``quit`` / ``beenden`` — Shut down the watcher loop.

All alert settings are persisted per MEATER UUID in the JSON state file.
"""

import asyncio
import json
import math
import os
import re
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict

from loguru import logger

from wachtmeater import cfg, read_dot_env_to_environ
from wachtmeater.config import AlertDefaultsConfig, WatcherState
from wachtmeater.core_forecast import MAX_ETA_HOURS, Reading, fit_fixed_ambient, thin
from wachtmeater.messaging import MessagingBackend

read_dot_env_to_environ()

SyncSender = Callable[[str, str | None], None]
"""Synchronous sender callable: ``(text, image_path_or_None) -> None``."""


class MeaterData(TypedDict, total=False):
    """Flat dictionary of MEATER cook values returned by the monitor."""

    internal_temp_c: float | None
    ambient_temp_c: float | None
    target_temp_c: float | None
    remaining_time: str | None
    remaining_minutes: int | None
    elapsed_time: str | None
    status: str
    cook_name: str | None
    screenshot: str | None
    started_at: str | None
    peak_temp_c: float | None
    source: str
    error: str
    error_kind: Literal["transient", "cook_gone"]


class WatcherError(Exception):
    """Raised when the watcher stops due to an error (non-zero exit)."""


# ---------------------------------------------------------------------------
# Configuration from typed config
# ---------------------------------------------------------------------------
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

# A trend window counts as covered when its samples span at least this share
# of the window; samples up to WINDOW_SLACK beyond the window are accepted so
# check jitter does not drop the oldest reading.
WINDOW_MIN_COVERAGE: float = 0.9
WINDOW_SLACK: float = 1.1
# Whole cook is kept so a backdated ``wrapped at`` still finds its readings.
SAMPLE_RETENTION_SECONDS: int = 48 * 3600
# Hampel identifier: a disturbance must exceed HAMPEL_K robust standard
# deviations of the reading noise (MAD estimate), at least the configured
# STALL_DISTURBANCE_* floor.  1.4826 makes the MAD consistent with sigma for
# normally distributed noise.
HAMPEL_K: float = 3.0
MAD_TO_SIGMA: float = 1.4826

# End-phase forecast (exponential fit from ``wrapped`` on, ``set pit``)
PIT_TEMP_DEFAULT: float = cfg.monitoring.pit_temp
# Earliest fit start after ``wrapped``; the actual start is searched adaptively
ENDPHASE_SETTLE_MINUTES: int = cfg.monitoring.endphase_settle_minutes
ENDPHASE_MIN_MINUTES: int = cfg.monitoring.endphase_min_minutes
ENDPHASE_MAX_POINTS: int = 120
"""Samples are thinned to this many for the fit (pure-Python cost per check)."""

# Cook-end detection: consecutive errors before declaring cook ended
COOKEND_ERROR_THRESHOLD: int = cfg.monitoring.cookend_error_threshold
# Internal temp below this after having been above 50 C => probe removed
COOKEND_PROBE_REMOVED_TEMP: float = cfg.monitoring.cookend_probe_removed_temp
# Consecutive offline cycles before SIP-escalating a station outage
STATION_OFFLINE_CALL_THRESHOLD: int = cfg.monitoring.station_offline_call_threshold

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _apply_alert_defaults(state: WatcherState, alert_cfg: AlertDefaultsConfig) -> None:
    """Apply config-driven alert defaults to a fresh ``WatcherState``.

    Copies every alert-related flag and threshold from *alert_cfg* into
    *state*, converting zero-valued temperatures to ``None``.

    Args:
        state: Mutable watcher state to populate.
        alert_cfg: Configuration-driven defaults read from environment
            variables.
    """
    state.tempalert_tempdown_enabled = alert_cfg.tempalert_tempdown_enabled
    state.tempalert_ruhephase_enabled = alert_cfg.tempalert_ruhephase_enabled
    state.ruhephase_target_temp = alert_cfg.ruhephase_target_temp or None
    state.tempalert_stall_enabled = alert_cfg.tempalert_stall_enabled
    state.stall_max_rate = alert_cfg.stall_max_rate
    state.tempalert_wrap_enabled = alert_cfg.tempalert_wrap_enabled
    state.wrap_target_temp = alert_cfg.wrap_target_temp or None
    state.tempalert_ambient_range_enabled = alert_cfg.tempalert_ambient_range_enabled
    state.ambient_range_min = alert_cfg.ambient_range_min or None
    state.ambient_range_max = alert_cfg.ambient_range_max or None
    state.tempalert_cookend_enabled = alert_cfg.tempalert_cookend_enabled
    state.tempalert_station_offline_enabled = alert_cfg.tempalert_station_offline_enabled


def load_state() -> WatcherState:
    """Load temperature tracking state from the JSON state file.

    Returns:
        A ``WatcherState`` instance with defaults applied for any missing keys.
    """
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return WatcherState.from_dict(json.load(f))
    state = WatcherState()
    _apply_alert_defaults(state, cfg.alerts)
    return state


def save_state(state: WatcherState) -> None:
    """Persist current state to the JSON state file.

    Args:
        state: The state to serialize. ``last_check`` is updated to the
            current timestamp before writing.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w") as f:
        json.dump(state.to_dict(), f, indent=2)


def effective_pit_temp(state: WatcherState) -> float:
    """Return the real smoker temperature used for the end-phase forecast.

    Args:
        state: Current watcher state.

    Returns:
        The per-cook ``set pit`` value, otherwise the config default.
    """
    return state.pit_temp if state.pit_temp is not None else PIT_TEMP_DEFAULT


def effective_check_interval(state: WatcherState) -> int:
    """Return the active check interval in seconds.

    Args:
        state: Current watcher state.

    Returns:
        The per-cook ``check_interval_override`` when set, otherwise the
        config-driven default ``CHECK_INTERVAL``.
    """
    return state.check_interval_override or CHECK_INTERVAL


# ---------------------------------------------------------------------------
# MEATER data + alerting helpers
# ---------------------------------------------------------------------------


def get_meater_data() -> MeaterData:
    """Fetch current MEATER cook data by calling ``extract_cook_data`` directly.

    Uses the hybrid fetcher: MEATER Cloud WebSocket first (sub-degree
    temperatures, no browser needed), DOM scraping as fallback.

    Returns:
        Flat dictionary with cook fields on success, or a dictionary
        with an ``"error"`` key describing the failure.
    """
    from wachtmeater.meater_monitor import (
        CookNotFoundError,
        extract_cook_data,
    )

    try:
        cook = extract_cook_data(MEATER_URL)
        data: MeaterData = {
            "internal_temp_c": cook.internal_temp_c,
            "ambient_temp_c": cook.ambient_temp_c,
            "target_temp_c": cook.target_temp_c,
            "remaining_time": cook.remaining_time,
            "remaining_minutes": cook.remaining_minutes,
            "elapsed_time": cook.elapsed_time,
            "status": cook.status,
            "cook_name": cook.cook_name,
            "screenshot": str(cook.screenshot) if cook.screenshot else None,
            "started_at": cook.started_at,
            "peak_temp_c": cook.peak_temp_c,
            "source": cook.source,
        }
        return data
    except CookNotFoundError as e:
        # The cook URL / cook ID is genuinely gone — legitimate cook-end signal.
        return {"error": str(e), "error_kind": "cook_gone"}
    except Exception as e:
        # CdpUnavailableError, CloudUnavailableError and any other unexpected
        # failure are treated as transient, our-side problems.  They must NOT be
        # mistaken for cook-end: a CDP timeout previously caused the cook to be
        # falsely declared done.
        return {"error": str(e), "error_kind": "transient"}


def call_pitmaster(message: str) -> bool:
    """Trigger a phone call to Pitmaster with the given alert message.

    Args:
        message: Alert text to deliver via call_pitmaster.

    Returns:
        ``True`` if the call succeeded, ``False`` otherwise.
    """
    from wachtmeater.call_pitmaster import call_pitmaster

    try:
        result = call_pitmaster(message)
        logger.info(f"Called Pitmaster ({cfg.sip.dest}): {message}")
        logger.debug(f"Response: {result}")
        return True
    except Exception as e:
        logger.error(f"Failed to call: {e}")
        return False


def _now() -> float:
    """Return the current wall-clock time as epoch seconds.

    Wall clock (not monotonic) because samples and the pause deadline are
    persisted across watcher restarts.  Patched in tests to replay a cook.

    Returns:
        Seconds since the epoch.
    """
    return time.time()


def _send_alert(send: SyncSender | None, text: str, image_path: str | None = None) -> bool:
    """Post an alert message to Matrix and report whether it was delivered.

    Args:
        send: Sync sender, or ``None`` when no messaging backend is attached
            (then there is nothing to deliver and the alert counts as sent).
        text: Alert message body.
        image_path: Optional image to attach.

    Returns:
        ``True`` if the message was delivered (or there is no backend),
        ``False`` if sending raised — the caller retries on the next check.
    """
    if send is None:
        return True
    try:
        send(text, image_path)
        return True
    except Exception as e:
        logger.error(f"Matrix: Alarm-Nachricht nicht zugestellt ({e}) — neuer Versuch beim naechsten Check: {text!r}")
        return False


def _deliver_once(
    state: WatcherState,
    key: str,
    text: str,
    send: SyncSender | None,
    call_text: str | None = None,
    image_path: str | None = None,
) -> bool:
    """Deliver a one-shot alert: Matrix message plus at most one SIP call.

    The caller sets its "already fired" flag only when this returns ``True``,
    so an undelivered message is retried on the next check.  The SIP call is
    placed once per alert episode — *key* is remembered in
    ``state.alert_calls_sent`` until the message got through, so retries do
    not ring the phone again.

    Args:
        state: Watcher state (``alert_calls_sent`` is updated).
        key: Alert identifier, e.g. ``"wrap"``.
        text: Matrix message body.
        send: Sync sender (may be ``None``).
        call_text: Text for the SIP call, or ``None`` for message-only alerts.
        image_path: Optional image to attach to the message.

    Returns:
        ``True`` once the message is delivered.
    """
    delivered = _send_alert(send, text, image_path)
    if call_text is not None and key not in state.alert_calls_sent:
        call_pitmaster(call_text)
        state.alert_calls_sent.append(key)
    if delivered:
        _clear_pending_call(state, key)
    return delivered


def _clear_pending_call(state: WatcherState, key: str) -> None:
    """Forget that the SIP call for alert *key* was already placed.

    Args:
        state: Watcher state.
        key: Alert identifier.
    """
    state.alert_calls_sent = [k for k in state.alert_calls_sent if k != key]


def _trend_window(
    samples: list[list[float]],
    now: float,
    window_minutes: float,
    ignore_before: float | None = None,
) -> list[list[float]] | None:
    """Select the ``[ts, value]`` samples covering the last *window_minutes*.

    Args:
        samples: Timestamped readings, oldest first.
        now: Current epoch seconds.
        window_minutes: Window length.
        ignore_before: Samples at or before this epoch are skipped.

    Returns:
        The samples in the window, or ``None`` if they do not span at least
        ``WINDOW_MIN_COVERAGE`` of it (too little data, e.g. after a restart,
        an outage or a disturbance).
    """
    window = window_minutes * 60
    pts = [
        p for p in samples if p[0] >= now - window * WINDOW_SLACK and (ignore_before is None or p[0] > ignore_before)
    ]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < window * WINDOW_MIN_COVERAGE:
        return None
    return pts


def _slope_per_hour(pts: list[list[float]]) -> float:
    """Least-squares slope of ``[ts, value]`` samples in units per hour.

    A regression instead of newest-minus-oldest keeps single noisy readings
    from dominating.

    Args:
        pts: At least two samples with distinct timestamps.

    Returns:
        Slope in value units per hour.
    """
    n = len(pts)
    mean_t = sum(p[0] for p in pts) / n
    mean_v = sum(p[1] for p in pts) / n
    num = sum((p[0] - mean_t) * (p[1] - mean_v) for p in pts)
    den = sum((p[0] - mean_t) ** 2 for p in pts)
    return num / den * 3600 if den else 0.0


def _noise_sigma(samples: list[list[float]]) -> float:
    """Robust estimate of the per-reading noise of ``[ts, value]`` samples.

    The readings are detrended with a Theil-Sen line (slope = median of all
    pairwise slopes), so a steady rise (warm-up, end phase) is not mistaken
    for noise; the spread of the residuals is then measured by their MAD.
    Both are medians, so a single lid-open dip or probe re-seat in the window
    barely moves the estimate (the Hampel identifier).

    Args:
        samples: Timestamped readings, oldest first.

    Returns:
        Estimated standard deviation of one reading in C (``0.0`` with fewer
        than three readings).
    """
    if len(samples) < 3:
        return 0.0
    slopes = [(q[1] - p[1]) / (q[0] - p[0]) for i, p in enumerate(samples) for q in samples[i + 1 :] if q[0] != p[0]]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(p[1] - slope * p[0] for p in samples)
    residuals = [p[1] - (intercept + slope * p[0]) for p in samples]
    return MAD_TO_SIGMA * statistics.median(abs(r) for r in residuals)


def _detect_disturbance(
    state: WatcherState,
    now: float,
    current_internal: float | None,
    current_ambient: float | None,
) -> str | None:
    """Detect a lid-open / wrap / probe-reseat / flare-up event from the new reading.

    Must run before the current reading is appended to the samples.

    Each check uses a Hampel-style threshold: the configured
    ``STALL_DISTURBANCE_*`` value, raised to ``HAMPEL_K`` times the noise of
    the last ``FORECAST_WINDOW_MINUTES`` (``_noise_sigma``) when the signal is
    noisier than that — a jumpy pit then does not restart the trend on every
    swing.

    Args:
        state: Watcher state holding the previous samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        current_ambient: Current ambient temperature.

    Returns:
        A short German reason, or ``None`` if the reading looks undisturbed.
    """
    since = now - FORECAST_WINDOW_MINUTES * 60 * WINDOW_SLACK
    if current_ambient is not None and state.ambient_samples:
        samples = [p for p in state.ambient_samples if p[0] >= since] or state.ambient_samples[-1:]
        window = [p[1] for p in samples]
        noise = HAMPEL_K * _noise_sigma(samples)
        # Median, not mean: an earlier dip in the window must not pull the level down.
        level = statistics.median(window)
        drop = max(STALL_DISTURBANCE_AMBIENT_DROP, noise)
        if level - current_ambient >= drop:
            return f"Ambient {current_ambient:.1f} C liegt {level - current_ambient:.1f} C unter dem Median ({level:.1f} C)"
        # Rise above the window maximum, not the median: recovering from a lid-open dip must not count.
        peak = max(window)
        warming_up = current_internal is None or current_internal < TEMPDOWN_WARMUP_CORE_TEMP
        rise = max(STALL_DISTURBANCE_AMBIENT_RISE, noise)
        if not warming_up and current_ambient - peak >= rise:
            return f"Ambient-Spitze {current_ambient:.1f} C liegt {current_ambient - peak:.1f} C ueber dem Hoechstwert ({peak:.1f} C)"
    if current_internal is not None and state.internal_samples:
        samples = [p for p in state.internal_samples if p[0] >= since]
        prev_internal = state.internal_samples[-1][1]
        if prev_internal - current_internal >= max(STALL_DISTURBANCE_CORE_DROP, HAMPEL_K * _noise_sigma(samples)):
            return f"Kerntemperatur-Sprung {prev_internal:.1f} -> {current_internal:.1f} C"
    return None


def _append_sample(
    samples: list[list[float]],
    now: float,
    value: float | None,
) -> list[list[float]]:
    """Append a reading and drop samples older than the retention period.

    Args:
        samples: Existing ``[ts, value]`` samples.
        now: Current epoch seconds.
        value: New reading (``None`` is not recorded).

    Returns:
        The pruned sample list including the new reading.
    """
    if value is not None:
        samples = [*samples, [now, value]]
    cutoff = now - SAMPLE_RETENTION_SECONDS
    return [p for p in samples if p[0] >= cutoff]


def _end_pause(state: WatcherState, now: float) -> None:
    """End a ``pause`` / ``wrapped`` window.

    Resets the ambient high-water mark (the next reading re-seeds it, so the
    lid-open dip cannot trigger TempDown) and restarts trend evaluation from
    now, so the dip does not count as a stall either.

    Args:
        state: Watcher state.
        now: Current epoch seconds.
    """
    state.pause_until = None
    state.max_ambient_temp = None
    state.trend_ignore_before = now
    state.stall_pause_checks_left = 0


def _pause_minutes_left(state: WatcherState, now: float) -> int:
    """Return the remaining pause time in whole minutes (rounded up).

    Args:
        state: Watcher state.
        now: Current epoch seconds.

    Returns:
        Minutes left, ``0`` when not paused.
    """
    if state.pause_until is None:
        return 0
    return max(0, math.ceil((state.pause_until - now) / 60))


def _remaining_known(remaining: str | None, remaining_minutes: int | None) -> bool:
    """Tell whether MEATER reported a usable remaining time.

    MEATER sends 0 s (formatted ``"0m"``) or nothing while it has no
    estimate, e.g. after the probe was re-inserted — that must not read as
    "done".

    Args:
        remaining: Formatted remaining time.
        remaining_minutes: Remaining time in minutes, if parsed.

    Returns:
        ``True`` if a positive remaining time is known.
    """
    if not remaining:
        return False
    if remaining_minutes is not None:
        return remaining_minutes > 0
    return remaining not in ("0m", "0h 0m") and "estimat" not in remaining.lower()


def _clock(ts: float, now: float) -> str:
    """Format an epoch as ``HH:MM``, with the date when it is not today.

    Args:
        ts: Epoch seconds to format.
        now: Current epoch seconds (defines "today").

    Returns:
        Local wall-clock string.
    """
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M" if dt.date() == datetime.fromtimestamp(now).date() else "%d.%m. %H:%M")


def _last_clock_time(hour: int, minute: int, now: float) -> float:
    """Return the most recent local wall-clock time ``hour:minute`` not after *now*.

    Args:
        hour: Hour of day (0-23).
        minute: Minute (0-59).
        now: Current epoch seconds.

    Returns:
        Epoch seconds of today's ``hour:minute``, or yesterday's when today's
        lies in the future.
    """
    now_dt = datetime.fromtimestamp(now)
    dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt > now_dt:
        dt -= timedelta(days=1)
    return dt.timestamp()


def _mark_wrapped(state: WatcherState, wrapped_at: float) -> str:
    """Mark the wrap as done and anchor the end-phase forecast at *wrapped_at*.

    Args:
        state: Watcher state (mutated, not saved).
        wrapped_at: Epoch of the wrap — now, or backdated via ``wrapped at``.

    Returns:
        The confirmation text for Matrix.
    """
    now = _now()
    state.wrap_alerted = True
    state.wrapped_at = wrapped_at
    _clear_pending_call(state, "wrap")
    earliest = _endphase_earliest(state)
    assert earliest is not None  # wrapped_at was just set
    return (
        f"Wrap als erledigt markiert ({_clock(wrapped_at, now)}). Endphasen-Prognose fittet ab "
        f"{_clock(earliest, now)} mit A = Pit {effective_pit_temp(state):g} C (aendern mit 'set pit <C>'), "
        f"Prognose fruehestens nach {ENDPHASE_MIN_MINUTES} Min. Daten."
    )


def _drop_previous_wrap_guard(state: WatcherState) -> float | None:
    """Clear a trend restart that belongs to the wrap being replaced.

    A ``resume`` / pause end / disturbance at or after the previous
    ``wrapped_at`` stems from that wrap episode; a backdated
    ``wrapped at`` supersedes it.  Restarts before it are kept.

    Args:
        state: Watcher state (mutated, not saved).

    Returns:
        The dropped ``trend_ignore_before``, or ``None`` if nothing was cleared.
    """
    guard = state.trend_ignore_before
    if state.wrapped_at is None or guard is None or guard < state.wrapped_at:
        return None
    state.trend_ignore_before = None
    return guard


def _trend_restart_reply(state: WatcherState, now: float) -> str:
    """Confirm a manual trend restart.

    Args:
        state: Watcher state after the change.
        now: Current epoch seconds.

    Returns:
        The confirmation text for Matrix.
    """
    if state.trend_ignore_before is None:
        return "Trend und Stall-Rate werten wieder alle Daten aus."
    return f"Trend und Stall-Rate werten ab {_clock(state.trend_ignore_before, now)} aus."


def _endphase_earliest(state: WatcherState) -> float | None:
    """Return the fixed start of the end-phase fit.

    ``ENDPHASE_SETTLE_MINUTES`` after ``wrapped``; the fit uses the readings
    from there on (its start value ``T0`` is fitted, not pinned to the first
    reading).

    Args:
        state: Watcher state.

    Returns:
        The fit start, or ``None`` before ``wrapped``.
    """
    if state.wrapped_at is None:
        return None
    return state.wrapped_at + ENDPHASE_SETTLE_MINUTES * 60


def _endphase_lines(state: WatcherState, now: float, current_internal: float | None, target: float | None) -> list[str]:
    """Build the end-phase forecast line (exponential fit after the wrap).

    Newton's law with ``A`` = pit temperature, anchored at the core reading
    ``ENDPHASE_SETTLE_MINUTES`` after the wrap; only ``c`` is fitted, the ETA
    runs from the current reading.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        target: Effective pull target (override or MEATER target).

    Returns:
        Status lines; empty before ``wrapped``, without a target or once the
        target is reached.
    """
    start = _endphase_earliest(state)
    if start is None or current_internal is None or not target or current_internal >= target:
        return []
    pit = effective_pit_temp(state)
    min_span = ENDPHASE_MIN_MINUTES * 60
    readings: list[Reading] = [(p[0], p[1], None) for p in state.internal_samples if p[0] >= start]
    if len(readings) < 3 or readings[-1][0] - readings[0][0] < min_span:
        first = readings[0][0] if readings else max(start, now)
        return [
            f"Endphasen-Prognose: sammelt Daten (Fit ab {_clock(start, now)}, "
            f"erste Prognose fruehestens ~{_clock(first + min_span, now)})"
        ]
    window = thin(readings, ENDPHASE_MAX_POINTS)
    t0, t_last = window[0][0], window[-1][0]
    fit = fit_fixed_ambient([(r[0] - t0) / 3600 for r in window], [r[1] for r in window], pit, target)
    head = (
        f"Endphasen-Prognose bis {target:.1f} C (A=Pit {pit:g} C, Fit {_clock(t0, now)}-{_clock(t_last, now)}, "
        f"{len(window)} Punkte, RMS {fit.rms:.2f} C)"
    )
    if fit.eta_hours is None:
        return [f"{head}: nie (Pit <= Ziel)"]
    if fit.eta_hours > MAX_ETA_HOURS:
        return [f"{head}: nicht absehbar (Kern steigt kaum)"]
    return [f"{head}: ~{_clock(t_last + fit.eta_hours * 3600, now)}"]


def _forecast_line(state: WatcherState, now: float, current_internal: float | None, target: float | None) -> str | None:
    """Build the watcher's own ETA forecast line to the pull target.

    Linear trend over the last ``FORECAST_WINDOW_MINUTES`` of core readings.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        target: Effective pull target (override or MEATER target).

    Returns:
        A clearly labelled forecast line, or ``None`` when there is no target,
        not enough data, or no meaningful rise.
    """
    if current_internal is None or not target or current_internal >= target:
        return None
    pts = _trend_window(state.internal_samples, now, FORECAST_WINDOW_MINUTES, state.trend_ignore_before)
    if pts is None:
        return None
    rate = _slope_per_hour(pts)
    if rate < 0.1:
        return None
    span = round((pts[-1][0] - pts[0][0]) / 60)
    hours = (target - current_internal) / rate
    when = "nicht absehbar (Kern steigt kaum)" if hours > MAX_ETA_HOURS else f"~{_clock(now + hours * 3600, now)}"
    return f"Watcher-Prognose bis {target:.1f} C: {when} (Trend {rate:.1f} C/h ueber {span} Min.)"


def _eta_lines(
    state: WatcherState,
    now: float,
    data: MeaterData,
    current_internal: float | None,
    target: float | None,
) -> list[str]:
    """Build all remaining-time lines: MEATER's estimate plus own forecasts.

    Shared by the full status message and the compact ``eta`` reply.

    Args:
        state: Current watcher state (samples, wrap time, pit temp).
        now: Current epoch time.
        data: The MEATER reading of this check.
        current_internal: Current core temperature in C.
        target: Effective pull target in C (override or MEATER target).

    Returns:
        The ``Remaining:`` line, followed by the watcher's trend forecast and
        the end-phase forecast block when available.
    """
    remaining = data.get("remaining_time")
    if _remaining_known(remaining, data.get("remaining_minutes")):
        lines = [f"Remaining: {remaining}"]
    else:
        lines = ["Remaining: unbekannt (MEATER liefert gerade keine Restzeit)"]
    forecast = _forecast_line(state, now, current_internal, target)
    if forecast:
        lines.append(forecast)
    lines.extend(_endphase_lines(state, now, current_internal, target))
    return lines


# ---------------------------------------------------------------------------
# Alert status summary (for status command)
# ---------------------------------------------------------------------------


def _alert_summary(state: WatcherState) -> list[str]:
    """Build a human-readable summary of all alert modes.

    Args:
        state: Current watcher state.

    Returns:
        List of status lines, one per alert mode.
    """
    lines = []

    # Pause (wrap / lid open) — listed first, it mutes several alerts below
    if state.pause_until is not None:
        lines.append(
            f"Pause: AKTIV (noch {_pause_minutes_left(state, _now())} Min.; TempDown, Stall und "
            f"Ambient-Bereich stumm; 'resume' beendet)"
        )

    # TempDown
    if state.tempalert_tempdown_enabled:
        lines.append(f"TempDown: AN (Schwelle: {AMBIENT_TEMP_DROP_THRESHOLD} C)")
    else:
        lines.append("TempDown: AUS")

    # Ruhephase
    if state.tempalert_ruhephase_enabled and state.ruhephase_target_temp is not None:
        lines.append(f"Ruhephase: AN (Ziel: {state.ruhephase_target_temp} C)")
    else:
        lines.append("Ruhephase: AUS")

    # Stall
    if state.tempalert_stall_enabled:
        s = (
            f"Stall: AN (Alarm bei < {state.stall_max_rate:g} C/h Anstieg ueber {STALL_WINDOW_MINUTES} Min., "
            f"Kern {STALL_CORE_MIN:g}-{STALL_CORE_MAX:g} C)"
        )
        if state.stall_alerted:
            s += " [bereits ausgeloest]"
        if state.stall_pause_checks_left > 0:
            s += f" [pausiert nach Stoerung: noch {state.stall_pause_checks_left} Check(s)]"
        lines.append(s)
    else:
        lines.append("Stall: AUS")

    # Wrap
    if state.tempalert_wrap_enabled and state.wrap_target_temp is not None:
        s = f"Wrap-Reminder: AN (bei {state.wrap_target_temp} C)"
        if state.wrap_alerted:
            s += " [bereits ausgeloest]"
        lines.append(s)
    else:
        lines.append("Wrap-Reminder: AUS")

    # Pull / take-out target override (overrides the MEATER target)
    if state.target_override is not None:
        s = f"Pull-Alert (rausnehmen): AN (Ziel: {state.target_override} C, override MEATER)"
        if state.target_reached_calls >= 3:
            s += " [bereits ausgeloest]"
        lines.append(s)
    else:
        lines.append("Pull-Alert (rausnehmen): AUS")

    # Ambient range
    if state.tempalert_ambient_range_enabled:
        lines.append(f"Ambient-Bereich: AN ({state.ambient_range_min}-{state.ambient_range_max} C)")
    else:
        lines.append("Ambient-Bereich: AUS")

    # Cook-end
    if state.tempalert_cookend_enabled:
        s = "Cook-Ende: AN"
        if state.cook_ended:
            s += " [Cook beendet erkannt]"
        lines.append(s)
    else:
        lines.append("Cook-Ende: AUS")

    # Station offline
    if state.tempalert_station_offline_enabled:
        s = f"Station-Offline-Alert: AN (SIP nach {STATION_OFFLINE_CALL_THRESHOLD} Cycles)"
        if state.offline_streak > 0:
            s += f" [aktuell offline: {state.offline_streak} Cycle(s)]"
        lines.append(s)
    else:
        lines.append("Station-Offline-Alert: AUS")

    # Real smoker temperature for the end-phase forecast
    pit_src = "set pit" if state.pit_temp is not None else "Standard"
    s = f"Smoker-Temp (Pit): {effective_pit_temp(state):g} C ({pit_src})"
    if state.wrapped_at is not None:
        s += f" [gewrappt {_clock(state.wrapped_at, _now())}]"
    lines.append(s)

    # Check interval
    interval = effective_check_interval(state)
    if state.check_interval_override is not None:
        lines.append(f"Check-Intervall: {interval}s (override; Standard {CHECK_INTERVAL}s)")
    else:
        lines.append(f"Check-Intervall: {interval}s")

    # Startup test call (applies to the next watcher start)
    if state.startup_test_call_enabled:
        lines.append("Startup-Testanruf: AN")
    else:
        lines.append("Startup-Testanruf: AUS")

    return lines


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------


def run_meater_check(
    state: WatcherState,
    send: SyncSender | None = None,
    eta_only: bool = False,
) -> str:
    """Fetch MEATER data, evaluate alarms, and update *state* in-place.

    Every alarm posts a Matrix message.  One-shot alarms (stall, wrap,
    ruhephase, pull, cook-end, offline notice) only mark themselves as fired
    once that message was delivered; otherwise they are retried on the next
    check (the SIP call is not repeated for the retry).

    Args:
        state: Mutable state; updated with latest temperatures, counters,
            and alert flags.
        send: Optional callable ``(text, image_path_or_None) -> None``
            for posting messages/images to Matrix via the existing session.
            Must raise when delivery fails.
        eta_only: When ``True`` (``eta`` command), the check runs as usual
            but posts only a compact remaining-time message without a
            screenshot instead of the full status.

    Returns:
        A human-readable status string suitable for Matrix, or the
        sentinel ``"__cook_ended__"`` when cook-end is detected (signals
        the event loop to shut down).
    """
    now = _now()
    timestamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("MEATER Watcher Check")

    # A ``pause`` / ``wrapped`` window that ran out ends here — before the
    # ambient snapshot below, so the reset high-water mark is re-seeded from
    # this reading.
    pause_ended = state.pause_until is not None and now >= state.pause_until
    if pause_ended:
        _end_pause(state, now)
        logger.info("Pause abgelaufen: Alarme wieder aktiv, Max-Ambient zurueckgesetzt.")

    max_ambient = state.max_ambient_temp
    max_internal = state.max_internal_temp
    last_internal = state.last_internal_temp
    target_reached_calls = state.target_reached_calls

    data = get_meater_data()

    # --- Cook-end detection: consecutive fetch errors ---
    if "error" in data:
        msg = f"Fehler beim Abrufen der MEATER-Daten: {data['error']}"
        logger.warning(msg)

        # Only a genuine "cook URL / cook ID no longer exists" error counts
        # toward cook-end.  Transient our-side failures (CDP unreachable,
        # connect/navigation timeouts) must NOT — a CDP timeout previously
        # got mistaken for cook-end and falsely declared the cook done.
        error_kind = data.get("error_kind", "transient")
        if error_kind != "cook_gone":
            logger.warning("Transienter Abruf-Fehler (CDP/Timeout) — zaehlt NICHT als Cook-Ende.")
            return msg

        if state.tempalert_cookend_enabled:
            errs = state.consecutive_errors + 1
            state.consecutive_errors = errs
            save_state(state)
            if errs >= COOKEND_ERROR_THRESHOLD:
                end_msg = (
                    f"Cook-Ende erkannt: {errs} aufeinanderfolgende Fehler beim "
                    f"Abrufen der MEATER-Daten. Die Cook-URL ist nicht mehr "
                    f"erreichbar (Cook wurde geloescht oder ist abgelaufen)."
                )
                logger.warning(end_msg)
                # Not delivered -> do not end yet; the next check (still an
                # error) retries the message.
                if _deliver_once(
                    state,
                    "cookend",
                    end_msg,
                    send,
                    call_text="MEATER Cook scheint beendet zu sein. Die Cook-URL existiert nicht mehr.",
                ):
                    state.cook_ended = True
                    save_state(state)
                    return "__cook_ended__"
                save_state(state)

        return msg

    # --- Station offline detection (MEATER Cloud "Searching for cook…") ---
    if data.get("status") == "offline":
        state.offline_streak += 1
        logger.warning(f"MEATER Station offline (streak={state.offline_streak})")

        last_internal = state.last_internal_temp
        last_ambient = state.last_ambient_temp
        offline_minutes = state.offline_streak * (effective_check_interval(state) // 60)

        lines = [f"⚠ MEATER Station offline (Cycle {state.offline_streak}, ~{offline_minutes} Min.)"]
        if last_internal is not None:
            lines.append(f"Letzter Internal: {last_internal:.1f} C")
        if last_ambient is not None:
            lines.append(f"Letzter Ambient: {last_ambient:.1f} C")
        lines.append(f"Time: {timestamp}")
        offline_msg = "\n".join(lines)

        # Post to Matrix once per outage — retried on the next offline cycle
        # if it did not get through.
        if not state.offline_notified and _send_alert(send, offline_msg):
            state.offline_notified = True

        # SIP-escalate once per outage when threshold is reached.
        if (
            state.tempalert_station_offline_enabled
            and not state.station_offline_call_sent
            and state.offline_streak >= STATION_OFFLINE_CALL_THRESHOLD
        ):
            call_pitmaster(
                f"MEATER Station seit {offline_minutes} Minuten offline. " f"Bitte Internet bzw. Block pruefen."
            )
            state.station_offline_call_sent = True

        save_state(state)
        return offline_msg

    # Recovery: previous cycle(s) were offline, this one succeeded.
    if state.offline_streak > 0:
        recovery_msg = f"✅ MEATER Station wieder online (war {state.offline_streak} Cycle(s) offline)."
        logger.info(recovery_msg)
        _send_alert(send, recovery_msg)
        state.offline_streak = 0
        state.station_offline_call_sent = False
        state.offline_notified = False

    # Reset consecutive error counter on success
    state.consecutive_errors = 0

    current_internal = data.get("internal_temp_c")
    current_ambient = data.get("ambient_temp_c")
    target_temp = data.get("target_temp_c")
    elapsed = data.get("elapsed_time")
    cook_status = data.get("status", "unknown")
    cook_name = data.get("cook_name", "Unknown")
    screenshot: str = data.get("screenshot") or f"/data/meater-screenshot-{MEATER_UUID}.png"
    paused = state.pause_until is not None
    # Warm-up: until the core reaches TEMPDOWN_WARMUP_CORE_TEMP the ambient
    # high-water mark is neither tracked nor checked.
    warming_up = current_internal is not None and current_internal < TEMPDOWN_WARMUP_CORE_TEMP

    # Update max values
    if current_ambient is not None and not warming_up:
        if max_ambient is None or current_ambient > max_ambient:
            max_ambient = current_ambient
    if current_internal is not None:
        if max_internal is None or current_internal > max_internal:
            max_internal = current_internal

    # Disturbance check (against the previous readings), then record the
    # timestamped samples used for the stall rate and the ETA forecast.
    # After a disturbance the trend restarts from the next reading and the
    # stall evaluation sits out a few checks.
    disturbance = _detect_disturbance(state, now, current_internal, current_ambient)
    state.internal_samples = _append_sample(state.internal_samples, now, current_internal)
    state.ambient_samples = _append_sample(state.ambient_samples, now, current_ambient)
    stall_cooling_down = False
    if disturbance:
        logger.info(f"Stoerereignis erkannt ({disturbance}) — Trend/Stall-Bewertung startet neu.")
        state.trend_ignore_before = now
        state.stall_pause_checks_left = STALL_DISTURBANCE_PAUSE_CHECKS
    elif state.stall_pause_checks_left > 0:
        state.stall_pause_checks_left -= 1
        stall_cooling_down = True

    effective_target = state.target_override if state.target_override is not None else target_temp

    # Build status text
    lines = [f"MEATER Status: {cook_name}", f"URL: {MEATER_URL}"]
    if current_internal is not None:
        lines.append(f"Internal (Fleisch): {current_internal:.1f} C")
    if current_ambient is not None:
        lines.append(f"Ambient (Rauch): {current_ambient:.1f} C")
    if state.target_override is not None:
        meater_target = f"{target_temp:.1f} C" if target_temp is not None else "unbekannt"
        lines.append(f"Target: {meater_target} (MEATER) → Pull bei {state.target_override:.1f} C (aktiv)")
    elif target_temp is not None:
        lines.append(f"Target: {target_temp:.1f} C")
    if warming_up:
        lines.append(f"Max Ambient: Anheizphase, erfasst ab Kern {TEMPDOWN_WARMUP_CORE_TEMP:g} C")
    elif max_ambient is not None:
        lines.append(f"Max Ambient: {max_ambient:.1f} C")
    if max_internal is not None:
        lines.append(f"Max Internal: {max_internal:.1f} C")
    eta_lines = _eta_lines(state, now, data, current_internal, effective_target)
    lines.extend(eta_lines)
    if elapsed:
        lines.append(f"Elapsed: {elapsed}")
    lines.append(f"Status: {cook_status}")
    # Only worth a line when we fell back — it explains why the decimals
    # suddenly went away.
    if data.get("source") == "browser":
        lines.append("Quelle: Browser-Fallback (nur ganze Grad)")
    if pause_ended:
        lines.append("Pause beendet: TempDown/Stall/Ambient-Bereich wieder aktiv, Max-Ambient zurueckgesetzt.")
    lines.append("---")
    lines.extend(_alert_summary(state))
    lines.append(f"Time: {timestamp}")
    status_text = "\n".join(lines)

    logger.info(f"Status:\n{status_text}")

    # Post to Matrix
    if send is not None:
        try:
            if eta_only:
                eta_head = [f"MEATER ETA: {cook_name}"]
                if current_internal is not None:
                    eta_head.append(f"Internal (Fleisch): {current_internal:.1f} C")
                if effective_target is not None:
                    eta_head.append(f"Target: {effective_target:.1f} C")
                send("\n".join(eta_head + eta_lines + [f"Time: {timestamp}"]), None)
            else:
                send(status_text, screenshot)
        except Exception as e:
            logger.error(f"Matrix: unexpected error ({e})")

    # ===================================================================
    # ALERTS
    # ===================================================================

    # 1) Ambient temperature drop (tempdown / fire out) — repeats every check
    #    while the drop persists; muted during ``pause``.
    if state.tempalert_tempdown_enabled and not paused and not warming_up:
        if max_ambient is not None and current_ambient is not None:
            ambient_drop = max_ambient - current_ambient
            if ambient_drop >= AMBIENT_TEMP_DROP_THRESHOLD:
                alarm_msg = (
                    f"ACHTUNG: Smoker Temperatur ist um {ambient_drop:.1f} C vom "
                    f"Hoechstwert gefallen! Max: {max_ambient:.1f} C, jetzt: {current_ambient:.1f} C. "
                    f"Feuer koennte ausgegangen sein!"
                )
                logger.warning(f"ALARM TempDown: {alarm_msg}")
                _send_alert(send, alarm_msg)
                call_pitmaster(alarm_msg)

    # 2) Ruhephase (cooldown monitoring)
    if state.tempalert_ruhephase_enabled and state.ruhephase_target_temp is not None:
        rp_target = state.ruhephase_target_temp
        if current_internal is not None and current_internal <= rp_target:
            alarm_msg = (
                f"Ruhephase beendet! Fleischtemperatur ({current_internal:.1f} C) hat "
                f"Ziel-Abkuehltemperatur ({rp_target} C) erreicht."
            )
            logger.warning(f"ALARM Ruhephase: {alarm_msg}")
            if _deliver_once(state, "ruhephase", alarm_msg, send, call_text=alarm_msg):
                state.tempalert_ruhephase_enabled = False
                logger.info("Ruhephase-Alert automatisch deaktiviert.")

    # 3) Stall detection: rise rate over a time window inside the plausible
    #    core range.  Falling temperatures, disturbances and ``pause`` are
    #    no stall.
    if state.tempalert_stall_enabled and not (disturbance or stall_cooling_down or paused):
        pts = _trend_window(state.internal_samples, now, STALL_WINDOW_MINUTES, state.trend_ignore_before)
        if pts is not None:
            rate = _slope_per_hour(pts)
            max_rate = state.stall_max_rate
            if state.stall_alerted:
                if rate >= max_rate * 2:
                    # Stall seems to be over — re-arm so it can trigger again
                    logger.info(f"Stall vorbei ({rate:.1f} C/h) — Stall-Alert wieder scharf.")
                    state.stall_alerted = False
            elif (
                current_internal is not None
                and STALL_CORE_MIN <= current_internal <= STALL_CORE_MAX
                and 0 <= rate < max_rate
            ):
                span = round((pts[-1][0] - pts[0][0]) / 60)
                alarm_msg = (
                    f"Stall erkannt! Interne Temperatur steigt nur um {rate:.1f} C/h "
                    f"(Schwelle {max_rate:g} C/h; letzte {span} Min. von {pts[0][1]:.1f} C "
                    f"auf {pts[-1][1]:.1f} C)."
                )
                # Once the wrap reminder fired or the user reported ``wrapped``,
                # asking to wrap again is wrong.
                if state.wrap_alerted:
                    alarm_msg += " Bereits gewrappt — Smoker-Temperatur pruefen und Geduld."
                else:
                    alarm_msg += " Jetzt wrappen?"
                logger.warning(f"ALARM Stall: {alarm_msg}")
                if _deliver_once(state, "stall", f"STALL ERKANNT: {alarm_msg}", send, call_text=alarm_msg):
                    state.stall_alerted = True

    # 4) Wrap reminder
    if state.tempalert_wrap_enabled and state.wrap_target_temp is not None and not state.wrap_alerted:
        wrap_temp = state.wrap_target_temp
        if current_internal is not None and current_internal >= wrap_temp:
            alarm_msg = (
                f"Wrap-Reminder! Kerntemperatur hat {current_internal:.1f} C erreicht "
                f"(Ziel: {wrap_temp} C). Jetzt in Butcher Paper/Folie einwickeln!"
            )
            logger.warning(f"ALARM Wrap: {alarm_msg}")
            if _deliver_once(state, "wrap", alarm_msg, send, call_text=alarm_msg):
                state.wrap_alerted = True

    # 5) Ambient range (too hot / too cold) — repeats every check; muted
    #    during ``pause``.
    if state.tempalert_ambient_range_enabled and current_ambient is not None and not paused:
        lo = state.ambient_range_min
        hi = state.ambient_range_max
        if lo is not None and current_ambient < lo:
            alarm_msg = f"Ambient zu niedrig! {current_ambient:.1f} C unter Minimum {lo} C. " f"Feuer nachschueren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            _send_alert(send, alarm_msg)
            call_pitmaster(alarm_msg)
        if hi is not None and current_ambient > hi:
            alarm_msg = f"Ambient zu hoch! {current_ambient:.1f} C ueber Maximum {hi} C. " f"Luftzufuhr reduzieren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            _send_alert(send, alarm_msg)
            call_pitmaster(alarm_msg)

    # 6) Target temperature reached.  A user-defined ``target_override`` (the
    #    "pull / take out now" temperature) takes precedence over the MEATER
    #    target.  Up to 3 notifications (message + call); one only counts once
    #    its message was delivered.
    if current_internal and effective_target and current_internal >= effective_target:
        logger.warning("ZIELTEMPERATUR ERREICHT!")
        if target_reached_calls < 3:
            if state.target_override is not None:
                alarm_msg = (
                    f"Rausnehmen! Deine Ziel-Kerntemperatur von {effective_target:.1f} C ist "
                    f"erreicht (aktuell {current_internal:.1f} C). Fleisch jetzt entnehmen!"
                )
                logger.warning(f"ALARM Pull: {alarm_msg}")
            else:
                alarm_msg = (
                    f"Brisket ist fertig! Zieltemperatur von {effective_target:.1f} C erreicht. "
                    f"Aktuelle Temperatur: {current_internal:.1f} C"
                )
            delivered = _send_alert(send, alarm_msg)
            call_pitmaster(alarm_msg)
            if delivered:
                target_reached_calls += 1

    # 7) Cook-end detection: probe removed (temp drops to near room temp)
    if state.tempalert_cookend_enabled and not state.cook_ended:
        if (
            max_internal is not None
            and max_internal > 50
            and current_internal is not None
            and current_internal < COOKEND_PROBE_REMOVED_TEMP
        ):
            end_msg = (
                f"Cook-Ende erkannt: Kerntemperatur auf {current_internal:.1f} C gefallen "
                f"(Max war {max_internal:.1f} C). Probe wurde vermutlich entfernt."
            )
            logger.warning(end_msg)
            if _deliver_once(
                state,
                "cookend",
                end_msg,
                send,
                call_text="MEATER Probe wurde vermutlich entfernt. Cook ist beendet.",
            ):
                state.cook_ended = True

        # Also detect meater-monitor status "done"
        if cook_status == "done" and not state.cook_ended:
            end_msg = "Cook-Ende erkannt: MEATER meldet Status 'done'."
            logger.warning(end_msg)
            if _deliver_once(state, "cookend", end_msg, send):
                state.cook_ended = True

        # Also detect meater cloud "finished" state (cook page shows summary)
        if cook_status == "finished" and not state.cook_ended:
            end_msg = "Cook-Ende erkannt: MEATER Cloud zeigt Cook als beendet an."
            logger.warning(end_msg)
            if _deliver_once(
                state,
                "cookend",
                end_msg,
                send,
                call_text="MEATER Cook ist offiziell beendet laut MEATER Cloud.",
                image_path=screenshot,
            ):
                state.cook_ended = True
                save_state(state)
                return "__cook_ended__"

    # Internal temperature change info
    if last_internal is not None and current_internal is not None:
        internal_diff = current_internal - last_internal
        logger.debug(f"Internal Change: {internal_diff:+.1f} C (from {last_internal:.1f} C)")

    # Update state.
    #
    # The check runs in a worker thread while operator commands mutate ``state``
    # on the event loop, so a ``reset ambient`` may have landed after we took the
    # snapshot in ``max_ambient``.  A now-empty high-water mark means exactly
    # that (or that it was never set) — in both cases the current reading is the
    # correct new maximum, so don't write the stale snapshot back.
    if state.max_ambient_temp is None:
        max_ambient = None if warming_up else current_ambient

    state.last_internal_temp = current_internal
    state.last_ambient_temp = current_ambient
    state.max_ambient_temp = max_ambient
    state.max_internal_temp = max_internal
    state.target_reached_calls = target_reached_calls
    save_state(state)
    logger.debug(f"State saved (Max Ambient: {max_ambient} C, Quelle: {data.get('source', 'unknown')})")

    if state.cook_ended:
        return "__cook_ended__"

    return status_text


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT: str = f"""\
MEATER Watcher Befehle:

status                    - Aktuellen Status abrufen
eta                       - Nur Restzeit/Prognosen abrufen (MEATER-Restzeit, Watcher-Trend, Endphase)
enable tempdown           - Temperaturabfall-Alert AN
disable tempdown          - Temperaturabfall-Alert AUS
enable ruhephase <temp>   - Ruhephase-Alert AN (Zieltemp in C; schaltet TempDown automatisch AUS)
disable ruhephase         - Ruhephase-Alert AUS
enable stall [<rate>]     - Stall-Erkennung AN (opt: Schwelle in C/h, Standard {cfg.alerts.stall_max_rate:g}; Alarm wenn der Kern
                            ueber {STALL_WINDOW_MINUTES} Min. langsamer steigt, nur bei {STALL_CORE_MIN:g}-{STALL_CORE_MAX:g} C,
                            Abfall zaehlt nicht)
disable stall             - Stall-Erkennung AUS
enable wrap <temp>        - Wrap-Reminder AN (bei Kerntemp in C)
disable wrap              - Wrap-Reminder AUS
enable pull <temp>        - Rausnehmen-Alarm AN (override MEATER-Zieltemp, Nachricht + Anruf)
disable pull              - Rausnehmen-Alarm AUS (wieder MEATER-Zieltemp)
enable ambient <min> <max>- Ambient-Bereich AN (min/max C)
disable ambient           - Ambient-Bereich AUS
enable cookend            - Cook-Ende-Erkennung AN
disable cookend           - Cook-Ende-Erkennung AUS
enable offline            - Station-Offline-Alert AN
disable offline           - Station-Offline-Alert AUS
reset stall               - Stall-Alert zuruecksetzen
reset wrap                - Wrap-Alert zuruecksetzen
reset ambient             - Max-Ambient-Hoechstwert zuruecksetzen
pause [<min>]             - Deckel auf/Wrappen: TempDown, Stall, Ambient-Bereich stumm (Standard {PAUSE_DEFAULT_MINUTES} Min.),
                            danach Max-Ambient automatisch zurueckgesetzt (Pull/Offline/Cook-Ende bleiben aktiv)
wrapped [<min>]           - Markiert Wrap als erledigt und startet die Endphasen-Prognose (Fit ab {ENDPHASE_SETTLE_MINUTES} Min.
                            nach dem Wrap, A = Pit); mit <min> zusaetzlich wie pause
wrapped at HH:MM          - Wrap nachtragen: wie wrapped, aber rueckdatiert auf HH:MM (ohne Pause)
trend ab HH:MM            - Trend + Stall-Rate erst ab HH:MM auswerten (z.B. nach Hitzespitze); 'trend reset' hebt auf
set pit <temp>            - Echte Smokertemperatur in C fuer die Endphasen-Prognose (Standard {PIT_TEMP_DEFAULT:g})
reset pit                 - Smokertemperatur auf Standard zuruecksetzen
resume                    - Pause vorzeitig beenden
testcall [<text>]         - Testanruf bei Pitmaster mit beliebigem Text
set interval <sek>        - Check-Intervall in Sekunden setzen
reset interval            - Check-Intervall auf Standard zuruecksetzen
enable startcall          - Startup-Testanruf beim Start AN
disable startcall         - Startup-Testanruf beim Start AUS
hilfe / help / /help      - Diese Hilfe anzeigen
stop / quit / beenden     - Watcher beenden\
"""


# ---------------------------------------------------------------------------
# Matrix command handler
# ---------------------------------------------------------------------------


def handle_command(body: str, state: WatcherState) -> str | None:
    """Parse a Matrix message and return a response string.

    Args:
        body: Raw message text from the Matrix room.
        state: Mutable state; modified in-place for settings changes and
            persisted via ``save_state``.

    Returns:
        A response string for the user, a sentinel (``"__status__"``, ``"__eta__"``,
        ``"__stop__"``, ``"__testcall__\\n<text>"``), or ``None`` if the
        message is not a recognised command.
    """
    raw = body.strip()
    text = raw.lower()

    # --- testcall (parsed before lowering so the spoken text keeps its case) ---
    m = re.match(r"testcall(?:\s+(.+))?$", raw, re.IGNORECASE)
    if m:
        spoken = m.group(1).strip() if m.group(1) else "Wachtmeater Testanruf"
        return f"__testcall__\n{spoken}"

    # --- help ---
    if text in ("hilfe", "help", "?", "/hilfe", "/help"):
        return HELP_TEXT

    # --- status ---
    if text == "status":
        return "__status__"

    # --- eta (remaining time / forecasts only) ---
    if text in ("eta", "restzeit", "prognose"):
        return "__eta__"

    # --- enable/disable tempdown ---
    if text in ("enable tempdown", "tempalert tempdown enable", "tempdown an", "tempdown on"):
        state.tempalert_tempdown_enabled = True
        save_state(state)
        return f"TempDown-Alert aktiviert (Schwelle: {AMBIENT_TEMP_DROP_THRESHOLD} C)."

    if text in ("disable tempdown", "tempalert tempdown disable", "tempdown aus", "tempdown off"):
        state.tempalert_tempdown_enabled = False
        save_state(state)
        return "TempDown-Alert deaktiviert."

    # --- enable/disable ruhephase ---
    m = re.match(r"(?:enable ruhephase|ruhephase an|ruhephase on|ruhephase enable)\s+(\d+(?:\.\d+)?)", text)
    if m:
        target = float(m.group(1))
        state.tempalert_ruhephase_enabled = True
        state.ruhephase_target_temp = target
        _clear_pending_call(state, "ruhephase")
        logger.info(f"Ruhephase-Alert aktiviert (Ziel-Abkuehltemperatur: {target} C).")
        reply = f"Ruhephase-Alert aktiviert. Ziel-Abkuehltemperatur: {target} C"

        # Coupling: in der Ruhephase kuehlt der Smoker gewollt ab.  Der
        # TempDown-Alert wuerde das als "Feuer ausgegangen" werten und einen
        # Fehlalarm samt Anruf ausloesen -> automatisch abschalten.
        if state.tempalert_tempdown_enabled:
            state.tempalert_tempdown_enabled = False
            logger.info(
                f"Kopplung: TempDown-Alert (Schwelle {AMBIENT_TEMP_DROP_THRESHOLD} C) "
                f"automatisch deaktiviert, weil 'enable ruhephase {target}' das "
                f"Abkuehlen zum Sollzustand macht - sonst Fehlalarm 'Feuer "
                f"ausgegangen'. Reaktivierung nur manuell via 'enable tempdown'."
            )
            reply += (
                f"\nKopplung: TempDown-Alert automatisch deaktiviert "
                f"(Abkuehlen ist jetzt gewollt, Alarm haette ab "
                f"{AMBIENT_TEMP_DROP_THRESHOLD} C Abfall vom Hoechstwert gefeuert). "
                f"Nach der Ruhephase mit 'enable tempdown' wieder scharfschalten."
            )
        else:
            logger.info("Kopplung: TempDown-Alert war bereits deaktiviert - keine Aenderung.")

        save_state(state)
        return reply

    if text in ("disable ruhephase", "ruhephase aus", "ruhephase off", "ruhephase disable"):
        state.tempalert_ruhephase_enabled = False
        _clear_pending_call(state, "ruhephase")
        save_state(state)
        return "Ruhephase-Alert deaktiviert."

    # --- enable/disable stall ---
    # The optional number is the rise-rate threshold in C/h (formerly a raw
    # per-window delta); the syntax is unchanged.
    m = re.match(r"(?:enable stall|stall an|stall on|stall enable)(?:\s+(\d+(?:\.\d+)?))?$", text)
    if m:
        rate = float(m.group(1)) if m.group(1) else cfg.alerts.stall_max_rate
        state.tempalert_stall_enabled = True
        state.stall_max_rate = rate
        state.stall_alerted = False
        _clear_pending_call(state, "stall")
        save_state(state)
        return (
            f"Stall-Erkennung aktiviert. Alarm wenn die Kerntemperatur ueber {STALL_WINDOW_MINUTES} Min. "
            f"langsamer als {rate:g} C/h steigt (nur bei {STALL_CORE_MIN:g}-{STALL_CORE_MAX:g} C Kern; "
            f"Temperaturabfall und Stoerungen wie Deckel auf zaehlen nicht)."
        )

    if text in ("disable stall", "stall aus", "stall off", "stall disable"):
        state.tempalert_stall_enabled = False
        state.stall_alerted = False
        _clear_pending_call(state, "stall")
        save_state(state)
        return "Stall-Erkennung deaktiviert."

    if text == "reset stall":
        state.stall_alerted = False
        _clear_pending_call(state, "stall")
        save_state(state)
        return "Stall-Alert zurueckgesetzt. Kann erneut ausloesen."

    # --- enable/disable wrap ---
    m = re.match(r"(?:enable wrap|wrap an|wrap on|wrap enable)\s+(\d+(?:\.\d+)?)", text)
    if m:
        temp = float(m.group(1))
        state.tempalert_wrap_enabled = True
        state.wrap_target_temp = temp
        state.wrap_alerted = False
        _clear_pending_call(state, "wrap")
        save_state(state)
        return f"Wrap-Reminder aktiviert. Alarm bei Kerntemperatur {temp} C."

    if text in ("disable wrap", "wrap aus", "wrap off", "wrap disable"):
        state.tempalert_wrap_enabled = False
        state.wrap_alerted = False
        _clear_pending_call(state, "wrap")
        save_state(state)
        return "Wrap-Reminder deaktiviert."

    if text == "reset wrap":
        state.wrap_alerted = False
        state.wrapped_at = None
        _clear_pending_call(state, "wrap")
        save_state(state)
        return "Wrap-Alert zurueckgesetzt. Kann erneut ausloesen."

    # --- set / disable pull (take-out) target override ---
    m = re.match(r"(?:enable pull|pull an|pull on|pull enable|set pull|rausnehmen)\s+(\d+(?:\.\d+)?)", text)
    if m:
        temp = float(m.group(1))
        state.target_override = temp
        state.target_reached_calls = 0  # re-arm the take-out notifications
        save_state(state)
        return (
            f"Pull-Alert aktiviert. Alarm (Nachricht + Anruf) zum Rausnehmen bei "
            f"Kerntemperatur {temp} C — ueberschreibt die MEATER-Zieltemperatur."
        )

    if text in ("disable pull", "pull aus", "pull off", "pull disable", "reset pull"):
        state.target_override = None
        save_state(state)
        return "Pull-Alert deaktiviert. Es gilt wieder die MEATER-Zieltemperatur."

    # --- enable/disable ambient range ---
    m = re.match(r"(?:enable ambient|ambient an|ambient on|ambient enable)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", text)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2))
        if lo >= hi:
            return f"Fehler: Minimum ({lo}) muss kleiner als Maximum ({hi}) sein."
        state.tempalert_ambient_range_enabled = True
        state.ambient_range_min = lo
        state.ambient_range_max = hi
        save_state(state)
        return f"Ambient-Bereich aktiviert: {lo} C - {hi} C."

    if text in ("disable ambient", "ambient aus", "ambient off", "ambient disable"):
        state.tempalert_ambient_range_enabled = False
        save_state(state)
        return "Ambient-Bereich-Alert deaktiviert."

    # --- reset max ambient high-water mark ---
    if text in (
        "reset ambient",
        "reset maxambient",
        "reset max ambient",
        "reset ambient max",
        "ambient reset",
    ):
        previous = state.max_ambient_temp
        state.max_ambient_temp = None
        save_state(state)
        if previous is None:
            return "Max Ambient war nicht gesetzt — nichts zurueckzusetzen."
        return (
            f"Max Ambient zurueckgesetzt (war {previous:.1f} C). Der naechste Check setzt "
            f"den Hoechstwert neu — TempDown-Alert misst ab dann von vorne."
        )

    # --- wrapped at HH:MM (backdate the wrap, no pause) ---
    m = re.match(r"(?:wrapped|gewrappt)\s+(?:at|um)\s+(\d{1,2}):(\d{2})$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return "Fehler: Uhrzeit als HH:MM angeben, z.B. 'wrapped at 11:15'."
        now = _now()
        dropped = _drop_previous_wrap_guard(state)
        reply = _mark_wrapped(state, _last_clock_time(hour, minute, now))
        if dropped is not None:
            reply += f"\nVorheriger Wrap ersetzt: Trend-Neustart von {_clock(dropped, now)} verworfen."
        save_state(state)
        return reply

    # --- pause / wrapped / resume (lid open, wrapping) ---
    m = re.match(r"(pause|pausieren|deckel|wrapped|gewrappt)(?:\s+(\d+))?$", text)
    if m:
        is_wrapped = m.group(1) in ("wrapped", "gewrappt")
        reply_parts: list[str] = []
        # ``wrapped`` alone only sets the status; a pause needs explicit minutes.
        if not is_wrapped or m.group(2):
            minutes = int(m.group(2)) if m.group(2) else PAUSE_DEFAULT_MINUTES
            if minutes <= 0:
                return "Fehler: Pause muss mindestens 1 Minute dauern."
            state.pause_until = _now() + minutes * 60
            reply_parts.append(
                f"Pause fuer {minutes} Min.: TempDown, Stall und Ambient-Bereich sind stumm. "
                f"Danach wird Max-Ambient automatisch zurueckgesetzt. Pull-, Offline- und "
                f"Cook-Ende-Alarm bleiben aktiv. Vorzeitig beenden mit 'resume'."
            )
        if is_wrapped:
            reply_parts.append(_mark_wrapped(state, _now()))
        save_state(state)
        return "\n".join(reply_parts)

    if text in ("resume", "weiter", "pause aus", "pause off", "pause beenden"):
        if state.pause_until is None:
            return "Keine Pause aktiv."
        _end_pause(state, _now())
        save_state(state)
        return (
            "Pause beendet: TempDown, Stall und Ambient-Bereich wieder aktiv. Max-Ambient "
            "zurueckgesetzt — der naechste Check setzt den Hoechstwert neu."
        )

    # --- enable/disable cookend ---
    if text in ("enable cookend", "cookend an", "cookend on", "cookend enable"):
        state.tempalert_cookend_enabled = True
        state.cook_ended = False
        state.consecutive_errors = 0
        _clear_pending_call(state, "cookend")
        save_state(state)
        return "Cook-Ende-Erkennung aktiviert."

    if text in ("disable cookend", "cookend aus", "cookend off", "cookend disable"):
        state.tempalert_cookend_enabled = False
        save_state(state)
        return "Cook-Ende-Erkennung deaktiviert."

    # --- enable/disable station-offline alert ---
    if text in (
        "enable offline",
        "offline an",
        "offline on",
        "offline enable",
        "enable station_offline",
        "enable station-offline",
    ):
        state.tempalert_station_offline_enabled = True
        save_state(state)
        return "Station-Offline-Alert aktiviert."

    if text in (
        "disable offline",
        "offline aus",
        "offline off",
        "offline disable",
        "disable station_offline",
        "disable station-offline",
    ):
        state.tempalert_station_offline_enabled = False
        save_state(state)
        return "Station-Offline-Alert deaktiviert."

    # --- trend ab HH:MM / trend reset (manual trend restart) ---
    m = re.match(r"trend\s+(?:ab|from|since)\s+(\d{1,2}):(\d{2})$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return "Fehler: Uhrzeit als HH:MM angeben, z.B. 'trend ab 12:20'."
        now = _now()
        state.trend_ignore_before = _last_clock_time(hour, minute, now)
        save_state(state)
        return _trend_restart_reply(state, now)

    if text in ("trend reset", "reset trend"):
        state.trend_ignore_before = None
        save_state(state)
        return _trend_restart_reply(state, _now())

    # --- set / reset real smoker (pit) temperature ---
    m = re.match(r"(?:set\s+)?(?:pit|smoker)\s+(\d+(?:\.\d+)?)\s*$", text)
    if m:
        pit = float(m.group(1))
        if not 50 <= pit <= 300:
            return "Fehler: Smokertemperatur muss zwischen 50 und 300 C liegen."
        state.pit_temp = pit
        save_state(state)
        return (
            f"Smokertemperatur auf {pit:g} C gesetzt (Obergrenze fuer die Endphasen-Prognose; "
            f"Standard {PIT_TEMP_DEFAULT:g} C)."
        )

    if text in ("reset pit", "reset smoker", "disable pit"):
        state.pit_temp = None
        save_state(state)
        return f"Smokertemperatur auf Standard zurueckgesetzt ({PIT_TEMP_DEFAULT:g} C)."

    # --- set / reset check interval ---
    if text in ("reset interval", "reset intervall", "set interval default", "set intervall default"):
        state.check_interval_override = None
        save_state(state)
        return f"Check-Intervall auf Standard zurueckgesetzt ({CHECK_INTERVAL}s)."

    m = re.match(r"(?:set\s+)?intervall?\s+(\d+)\s*$", text)
    if m:
        secs = int(m.group(1))
        if secs < MIN_CHECK_INTERVAL:
            return f"Fehler: Intervall muss mindestens {MIN_CHECK_INTERVAL}s sein."
        state.check_interval_override = secs
        save_state(state)
        return f"Check-Intervall auf {secs}s gesetzt (gilt ab dem naechsten Zyklus)."

    # --- enable/disable startup test call ---
    if text in (
        "enable startcall",
        "startcall an",
        "startcall on",
        "startcall enable",
        "enable startup-testcall",
        "enable startup_testcall",
    ):
        state.startup_test_call_enabled = True
        save_state(state)
        return "Startup-Testanruf aktiviert (gilt ab dem naechsten Start)."

    if text in (
        "disable startcall",
        "startcall aus",
        "startcall off",
        "startcall disable",
        "disable startup-testcall",
        "disable startup_testcall",
    ):
        state.startup_test_call_enabled = False
        save_state(state)
        return "Startup-Testanruf deaktiviert (gilt ab dem naechsten Start)."

    # --- stop ---
    if text in ("stop", "quit", "beenden", "exit"):
        return "__stop__"

    return None


# ---------------------------------------------------------------------------
# Async event loop
# ---------------------------------------------------------------------------


async def event_loop(
    skip_startup_test_call: bool,
    messaging: MessagingBackend | None = None,
) -> None:
    """Run the main async event loop: Matrix listener + periodic MEATER checks.

    Args:
        skip_startup_test_call: If ``True``, skip the initial test phone
            call to Pitmaster on startup.
        messaging: Optional messaging backend.  When ``None`` (the default),
            a ``MatrixMessagingAdapter`` is created automatically.
    """

    if not MEATER_URL:
        raise WatcherError("MEATER_URL must be configured")

    state = load_state()

    # Resume: the operator recreated this job to revive a cook that was
    # (falsely) marked as ended.  Clear the cook-end flag and switch off
    # auto cook-end detection so the very same condition (e.g. internal >=
    # target, "finished" page, probe removed) does not immediately end the
    # cook again on the initial check.  The user can re-enable detection
    # with ``enable cookend`` once the cook has genuinely moved on.
    if os.environ.get("WACHTMEATER_RESUME") == "1":
        logger.warning("Resume angefordert: cook_ended zurueckgesetzt, " "Cook-Ende-Erkennung deaktiviert.")
        state.cook_ended = False
        state.tempalert_cookend_enabled = False
        state.consecutive_errors = 0
        state.offline_streak = 0
        state.station_offline_call_sent = False
        save_state(state)

    # Startup test call — suppressed by the CLI flag or the persisted
    # per-cook setting (toggled via the ``enable/disable startcall`` command).
    if not skip_startup_test_call and state.startup_test_call_enabled:
        logger.info("Startup-Testanruf...")
        try:
            await asyncio.to_thread(call_pitmaster, "Meater Watcher gestartet. Dies ist ein Testanruf.")
        except Exception as e:
            logger.error(f"Startup-Testanruf fehlgeschlagen: {e}")

    # --- Set up messaging backend ---
    if messaging is None:
        from wachtmeater.matrix_adapter import MatrixMessagingAdapter

        messaging = MatrixMessagingAdapter()

    await messaging.connect()

    # --- Room selection / auto-creation ---
    selection = await messaging.get_or_create_room(
        configured_room=cfg.matrix.room,
        auto_create=cfg.matrix.auto_create_room_for_meater_uuid,
        meater_uuid=MEATER_UUID,
        pitmaster_mxid=cfg.matrix.pitmaster,
        persisted_room_id=state.matrix_room_id,
        promote_pitmaster_to_admin=cfg.matrix.pitmaster_admin_in_cook_room,
    )
    if selection.cook and selection.cook != state.matrix_room_id:
        state.matrix_room_id = selection.cook
        save_state(state)

    # Update targets.  With a per-cook room present, the configured
    # broadcast room is only included when explicitly enabled — otherwise
    # the cook stays confined to its own room.
    if selection.cook and not cfg.matrix.updates_to_broadcast_room:
        room_ids = [selection.cook]
    else:
        room_ids = selection.all_rooms

    # Determine bot's MXID from the backend
    bot_mxid = messaging.get_bot_user_id()
    logger.info(f"Matrix: eingeloggt als {bot_mxid}")
    logger.info(f"Matrix: {len(messaging.get_rooms())} Raum/Raeume beigetreten")
    logger.info(
        f"Matrix: broadcasting an {len(room_ids)} Raum/Raeume "
        f"(broadcast={selection.broadcast}, cook={selection.cook}, "
        f"updates_to_broadcast_room={cfg.matrix.updates_to_broadcast_room})"
    )

    # Send startup greeting with available commands.  If the job created
    # its own per-cook room, only greet there — repeating the command list
    # in the main broadcast room would be redundant.
    startup_msg = f"Hallo! MEATER Watcher ist gestartet.\n\n{HELP_TEXT}"
    if selection.cook:
        greeting_rooms = [selection.cook]
    else:
        greeting_rooms = room_ids if room_ids else messaging.get_rooms()
    for rid in greeting_rooms:
        try:
            await messaging.send_message(rid, startup_msg)
        except Exception as e:
            logger.warning(f"Startup-Gruss konnte nicht gesendet werden an {rid}: {e}")

    logger.info("Startup-Gruss an Matrix gesendet.")

    # Build synchronous sender closure for run_meater_check
    loop = asyncio.get_running_loop()

    def _send_via_messaging(text: str, image_path: str | None) -> None:
        """Send text and optional image to all target rooms synchronously.

        Bridges the sync ``run_meater_check`` world to the async messaging
        backend by scheduling coroutines on the running event loop.  Text is
        sent in strict mode so a failed delivery raises and alerts get
        retried; a failed image upload is only logged (the text is out).

        Args:
            text: Message body to send.
            image_path: Optional filesystem path to an image to attach.

        Raises:
            Exception: If the text could not be delivered to every target room.
        """
        targets = room_ids if room_ids else messaging.get_rooms()
        for rid in targets:
            fut = asyncio.run_coroutine_threadsafe(messaging.send_message(rid, text, strict=True), loop)
            fut.result(timeout=30)
            if image_path:
                fut2 = asyncio.run_coroutine_threadsafe(messaging.send_image(rid, image_path), loop)
                try:
                    fut2.result(timeout=90)
                except Exception as e:
                    logger.error(f"Matrix: Bild-Upload an {rid} fehlgeschlagen: {e}")

    # Initial MEATER check — now that Matrix is connected, post the current
    # status (text + screenshot) through the same sender used for periodic
    # checks, so the room sees the live cook state right after the greeting.
    logger.info("Erster MEATER-Check...")
    initial_result = await asyncio.to_thread(run_meater_check, state, _send_via_messaging)
    if initial_result == "__cook_ended__":
        logger.warning("Cook bereits beim ersten Check als beendet erkannt.")
        for rid in room_ids if room_ids else messaging.get_rooms():
            try:
                await messaging.send_message(rid, "Cook-Ende erkannt. MEATER Watcher wird beendet.")
            except Exception:
                pass
        await messaging.close()
        return

    # Shared stop event
    stop_event = asyncio.Event()
    _error: list[str] = []

    # Message callback
    async def on_message(incoming_room_id: str, sender: str, body: str, room_display_name: str) -> None:
        """Handle an incoming Matrix room message.

        Parses the message as a watcher command.  Recognised commands
        trigger state changes, status checks, or a graceful shutdown.
        Messages from the bot itself are silently ignored.

        Args:
            incoming_room_id: Matrix room ID the message arrived in.
            sender: Full MXID of the message author.
            body: Raw message text.
            room_display_name: Human-readable name of the room.
        """
        if sender == bot_mxid:
            return

        logger.info(f"Matrix [{room_display_name}] <{sender}>: {body}")

        response = handle_command(body, state)
        if response is None:
            return  # not a command

        if response == "__stop__":
            goodbye = "MEATER Watcher wird beendet. Tschuess!"
            await messaging.send_message(incoming_room_id, goodbye)
            logger.info("Stop-Befehl erhalten. Beende...")
            stop_event.set()
            return

        if response in ("__status__", "__eta__"):
            check_result = await asyncio.to_thread(
                run_meater_check, state, _send_via_messaging, eta_only=response == "__eta__"
            )
            if check_result == "__cook_ended__":
                await messaging.send_message(
                    incoming_room_id,
                    "Cook-Ende erkannt. Watcher wird beendet.",
                )
                stop_event.set()
            return

        if response.startswith("__testcall__\n"):
            spoken = response.removeprefix("__testcall__\n")
            asyncio.create_task(asyncio.to_thread(call_pitmaster, spoken))
            await messaging.send_message(
                incoming_room_id,
                f"Testanruf wird ausgeloest an {cfg.sip.dest}: {spoken!r}",
            )
            return

        # Regular response — post back to the room
        try:
            await messaging.send_message(incoming_room_id, response)
        except Exception as e:
            logger.error(f"Matrix: Fehler beim Antworten: {e}")

    messaging.register_message_callback(on_message)

    # --- Periodic check task ---
    async def periodic_check() -> None:
        """Run MEATER checks on a fixed schedule until stopped.

        Deadlines live on the loop's monotonic clock and advance by the
        interval from the previous *deadline*, not from when the check
        finished — so fetch and screenshot time no longer add up as drift.
        A check that overruns whole slots skips them instead of bunching up.
        """
        next_due = loop.time() + effective_check_interval(state)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, next_due - loop.time()))
                break
            except asyncio.TimeoutError:
                interval = effective_check_interval(state)
                next_due += interval
                while next_due <= loop.time():
                    next_due += interval
                logger.info(f"Periodischer Check (alle {interval}s)...")
                check_result = await asyncio.to_thread(run_meater_check, state, _send_via_messaging)
                if check_result == "__cook_ended__":
                    logger.warning("Cook-Ende erkannt im periodischen Check. Beende...")
                    try:
                        # Notify the configured update targets
                        for rid in room_ids if room_ids else messaging.get_rooms():
                            await messaging.send_message(
                                rid,
                                "Cook-Ende erkannt. MEATER Watcher wird beendet.",
                            )
                    except Exception:
                        pass
                    stop_event.set()

    # --- Run sync_forever and periodic check concurrently ---
    async def matrix_sync() -> None:
        """Keep the messaging backend syncing until an error or stop signal."""
        try:
            await messaging.start_sync()
        except Exception as e:
            if not stop_event.is_set():
                logger.error(f"Matrix sync Fehler: {e}")
                _error.append(str(e))
        finally:
            if not stop_event.is_set():
                stop_event.set()

    sync_task = asyncio.create_task(matrix_sync())
    check_task = asyncio.create_task(periodic_check())

    # Wait for stop signal
    await stop_event.wait()

    # Cleanup
    logger.info("Stoppe Matrix-Sync...")
    messaging.stop_sync()
    sync_task.cancel()
    check_task.cancel()
    try:
        await sync_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await check_task
    except (asyncio.CancelledError, Exception):
        pass
    await messaging.close()
    logger.info("MEATER Watcher beendet.")

    if _error:
        raise WatcherError(f"Watcher stopped due to error: {_error[0]}")

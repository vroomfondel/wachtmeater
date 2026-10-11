"""Text blocks of the status message: remaining-time lines and alert summary.

``eta_lines`` combines the MEATER estimate, the watcher's linear trend
forecast and the end-phase forecast (``wachtmeater.core_forecast``);
``alert_summary`` lists the alert settings and their state.
"""

from wachtmeater import timeutil
from wachtmeater.core_forecast import MAX_ETA_HOURS, Reading, fit_fixed_ambient, thin
from wachtmeater.meater_monitor import MeaterData
from wachtmeater.signal_stats import slope_per_hour, trend_window
from wachtmeater.timeutil import clock
from wachtmeater.watcher_settings import (
    AMBIENT_TEMP_DROP_THRESHOLD,
    CHECK_INTERVAL,
    ENDPHASE_MAX_POINTS,
    ENDPHASE_MIN_MINUTES,
    ENDPHASE_SETTLE_MINUTES,
    FORECAST_WINDOW_MINUTES,
    STALL_CORE_MAX,
    STALL_CORE_MIN,
    STALL_WINDOW_MINUTES,
    STATION_OFFLINE_CALL_THRESHOLD,
)
from wachtmeater.watcher_state import WatcherState, effective_check_interval, effective_pit_temp, pause_minutes_left


def remaining_known(remaining: str | None, remaining_minutes: int | None) -> bool:
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


def endphase_earliest(state: WatcherState) -> float | None:
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


def endphase_lines(state: WatcherState, now: float, current_internal: float | None, target: float | None) -> list[str]:
    """Build the end-phase forecast line (exponential fit after the wrap).

    Newton's law with ``A`` = pit temperature over the readings from
    ``ENDPHASE_SETTLE_MINUTES`` after the wrap on; ``T0`` and ``c`` are
    fitted (robust, see ``wachtmeater.core_forecast``).  The ETA runs from
    the current reading; if that reading is an outlier (clearly off the
    curve), it runs from the curve value instead and a second line shows the
    ETA from the measured value.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        target: Effective pull target (override or MEATER target).

    Returns:
        Status lines; empty before ``wrapped``, without a target or once the
        target is reached.
    """
    start = endphase_earliest(state)
    if start is None or current_internal is None or not target or current_internal >= target:
        return []
    pit = effective_pit_temp(state)
    min_span = ENDPHASE_MIN_MINUTES * 60
    readings: list[Reading] = [(p[0], p[1], None) for p in state.internal_samples if p[0] >= start]
    if len(readings) < 3 or readings[-1][0] - readings[0][0] < min_span:
        first = readings[0][0] if readings else max(start, now)
        return [
            f"Endphasen-Prognose: sammelt Daten (Fit ab {clock(start, now)}, "
            f"erste Prognose fruehestens ~{clock(first + min_span, now)})"
        ]
    window = thin(readings, ENDPHASE_MAX_POINTS)
    t0, t_last = window[0][0], window[-1][0]
    fit = fit_fixed_ambient([(r[0] - t0) / 3600 for r in window], [r[1] for r in window], pit, target)
    head = (
        f"Endphasen-Prognose bis {target:.1f} C (A=Pit {pit:g} C, Fit {clock(t0, now)}-{clock(t_last, now)}, "
        f"{len(window)} Punkte, RMS {fit.rms:.2f} C)"
    )
    if fit.eta_hours is None:
        return [f"{head}: nie (Pit <= Ziel)"]
    if fit.eta_hours > MAX_ETA_HOURS:
        return [f"{head}: nicht absehbar (Kern steigt kaum)"]
    lines = [f"{head}: ~{clock(t_last + fit.eta_hours * 3600, now)}"]
    if fit.last_deviates:
        measured = fit.eta_hours_measured
        eta = (
            f"~{clock(t_last + measured * 3600, now)}"
            if measured is not None and measured <= MAX_ETA_HOURS
            else "nicht absehbar"
        )
        lines.append(
            f"  Letzter Messwert {fit.core_measured:.1f} C weicht von der Kurve ({fit.core_model:.1f} C) ab "
            f"(Ausreisser?), Prognose ab Kurvenwert; ab Messwert: {eta}"
        )
    return lines


def forecast_line(state: WatcherState, now: float, current_internal: float | None, target: float | None) -> str | None:
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
    pts = trend_window(state.internal_samples, now, FORECAST_WINDOW_MINUTES, state.trend_ignore_before)
    if pts is None:
        return None
    rate = slope_per_hour(pts)
    if rate < 0.1:
        return None
    span = round((pts[-1][0] - pts[0][0]) / 60)
    hours = (target - current_internal) / rate
    when = "nicht absehbar (Kern steigt kaum)" if hours > MAX_ETA_HOURS else f"~{clock(now + hours * 3600, now)}"
    return f"Watcher-Prognose bis {target:.1f} C: {when} (Trend {rate:.1f} C/h ueber {span} Min.)"


def eta_lines(
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
    if remaining_known(remaining, data.get("remaining_minutes")):
        lines = [f"Remaining: {remaining}"]
    else:
        lines = ["Remaining: unbekannt (MEATER liefert gerade keine Restzeit)"]
    forecast = forecast_line(state, now, current_internal, target)
    if forecast:
        lines.append(forecast)
    lines.extend(endphase_lines(state, now, current_internal, target))
    return lines


def alert_summary(state: WatcherState) -> list[str]:
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
            f"Pause: AKTIV (noch {pause_minutes_left(state, timeutil.now())} Min.; TempDown, Stall und "
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
        s += f" [gewrappt {clock(state.wrapped_at, timeutil.now())}]"
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

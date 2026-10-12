"""Row blocks of the status message: remaining-time rows and alert summary.

``eta_rows`` combines the MEATER estimate, the watcher's linear trend
forecast and the end-phase forecast (``wachtmeater.core_forecast``) — or,
during the rest phase, the cooling forecast to the rest target;
``alert_rows`` lists the alert settings and their state.  Rows carry a short
label/value for the HTML table plus the classic plain-text line
(``wachtmeater.status_table``).
"""

from wachtmeater import timeutil
from wachtmeater.core_forecast import MAX_ETA_HOURS, Reading, fit_fixed_ambient, thin
from wachtmeater.meater_monitor import MeaterData
from wachtmeater.signal_stats import slope_per_hour, trend_window
from wachtmeater.status_table import StatusRow
from wachtmeater.timeutil import clock
from wachtmeater.watcher_settings import (
    AMBIENT_TEMP_DROP_THRESHOLD,
    CHECK_INTERVAL,
    ENDPHASE_MAX_POINTS,
    ENDPHASE_MIN_MINUTES,
    ENDPHASE_SETTLE_MINUTES,
    FORECAST_WINDOW_MINUTES,
    RESTPHASE_MIN_MINUTES,
    ROOM_TEMP,
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


def endphase_rows(
    state: WatcherState, now: float, current_internal: float | None, target: float | None
) -> list[StatusRow]:
    """Build the end-phase forecast row (exponential fit after the wrap).

    Newton's law with ``A`` = pit temperature over the readings from
    ``ENDPHASE_SETTLE_MINUTES`` after the wrap on; ``T0`` and ``c`` are
    fitted (robust, see ``wachtmeater.core_forecast``).  The ETA runs from
    the current reading; if that reading is an outlier (clearly off the
    curve), it runs from the curve value instead and a second plain-text
    line (notes in HTML) shows the ETA from the measured value.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        target: Effective pull target (override or MEATER target).

    Returns:
        One row; empty before ``wrapped``, without a target or once the
        target is reached.
    """
    start = endphase_earliest(state)
    if start is None or current_internal is None or not target or current_internal >= target:
        return []
    label = "Endphasen-Prognose"
    pit = effective_pit_temp(state)
    min_span = ENDPHASE_MIN_MINUTES * 60
    readings: list[Reading] = [(p[0], p[1], None) for p in state.internal_samples if p[0] >= start]
    if len(readings) < 3 or readings[-1][0] - readings[0][0] < min_span:
        first = readings[0][0] if readings else max(start, now)
        pending = (f"Fit ab {clock(start, now)}", f"erste Prognose fruehestens ~{clock(first + min_span, now)}")
        return [StatusRow(label, "sammelt Daten", pending, plain=f"{label}: sammelt Daten ({', '.join(pending)})")]
    window = thin(readings, ENDPHASE_MAX_POINTS)
    t0, t_last = window[0][0], window[-1][0]
    fit = fit_fixed_ambient([(r[0] - t0) / 3600 for r in window], [r[1] for r in window], pit, target)
    fit_note = f"Fit {clock(t0, now)}-{clock(t_last, now)}, {len(window)} Punkte, RMS {fit.rms:.2f} C"
    notes: tuple[str, ...] = (f"bis {target:.1f} C, A=Pit {pit:g} C", fit_note)
    head = f"{label} bis {target:.1f} C (A=Pit {pit:g} C, {fit_note})"
    if fit.eta_hours is None:
        value = "nie (Pit <= Ziel)"
    elif fit.eta_hours > MAX_ETA_HOURS:
        value = "nicht absehbar (Kern steigt kaum)"
    else:
        value = f"~{clock(t_last + fit.eta_hours * 3600, now)}"
    plain = f"{head}: {value}"
    if value.startswith("~") and fit.last_deviates:
        notes, plain = _deviation(notes, plain, fit.core_measured, fit.core_model, fit.eta_hours_measured, t_last, now)
    return [StatusRow(label, value, notes, plain=plain)]


def _deviation(
    notes: tuple[str, ...],
    plain: str,
    measured: float,
    model: float,
    eta_hours_measured: float | None,
    t_last: float,
    now: float,
) -> tuple[tuple[str, ...], str]:
    """Add the outlier hint (last reading off the fitted curve) to a forecast row.

    Args:
        notes: The row's notes so far.
        plain: The row's plain text so far.
        measured: Last measured core temperature.
        model: Curve value at the last reading.
        eta_hours_measured: ETA in hours from the measured value, if any.
        t_last: Time of the last reading.
        now: Current epoch seconds.

    Returns:
        The extended ``(notes, plain)``.
    """
    eta = (
        f"~{clock(t_last + eta_hours_measured * 3600, now)}"
        if eta_hours_measured is not None and eta_hours_measured <= MAX_ETA_HOURS
        else "nicht absehbar"
    )
    notes += (f"Messwert {measured:.1f} C weicht ab (Kurve {model:.1f} C)", f"Ausreisser? Ab Messwert: {eta}")
    plain += (
        f"\n  Letzter Messwert {measured:.1f} C weicht von der Kurve ({model:.1f} C) ab "
        f"(Ausreisser?), Prognose ab Kurvenwert; ab Messwert: {eta}"
    )
    return notes, plain


def restphase_rows(state: WatcherState, now: float, current_internal: float | None) -> list[StatusRow]:
    """Build the rest-phase forecast row (exponential cooling fit).

    Newton's law of cooling towards ``A`` = ``ROOM_TEMP`` over the readings
    from the core peak after ``enable ruhephase`` on (the carry-over rise
    right after the pull is skipped); ``T0`` and ``c`` are fitted, as in the
    end phase.  Without an ``enable ruhephase`` time (alert enabled by
    config default) the rest phase only counts as started once the core has
    fallen at least 1 C below its peak.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.

    Returns:
        One row; empty when the rest-phase alert is off, has no target, the
        rest phase has not started or the target is reached.
    """
    target = state.ruhephase_target_temp
    if not state.tempalert_ruhephase_enabled or target is None or current_internal is None:
        return []
    if current_internal <= target:
        return []
    label = "Ruhephasen-Prognose"
    anchor = state.ruhephase_started_at
    samples = [p for p in state.internal_samples if anchor is None or p[0] >= anchor]
    if not samples:
        if anchor is None:
            return []
        return [
            StatusRow(
                label, "sammelt Daten", (f"bis {target:.1f} C",), plain=f"{label} bis {target:.1f} C: sammelt Daten"
            )
        ]
    peak = max(p[1] for p in samples)
    if anchor is None and peak - current_internal < 1.0:
        return []
    # From the last reading at the peak on: the carry-over rise is not cooling.
    peak_idx = max(i for i, p in enumerate(samples) if p[1] == peak)
    readings: list[Reading] = [(p[0], p[1], None) for p in samples[peak_idx:]]
    min_span = RESTPHASE_MIN_MINUTES * 60
    if len(readings) < 3 or readings[-1][0] - readings[0][0] < min_span:
        pending = (
            f"bis {target:.1f} C",
            f"Fit ab Kern-Maximum {peak:.1f} C um {clock(readings[0][0], now)}",
            f"erste Prognose fruehestens ~{clock(readings[0][0] + min_span, now)}",
        )
        return [
            StatusRow(
                label,
                "sammelt Daten",
                pending,
                plain=f"{label} {pending[0]}: sammelt Daten ({pending[1]}, {pending[2]})",
            )
        ]
    window = thin(readings, ENDPHASE_MAX_POINTS)
    t0, t_last = window[0][0], window[-1][0]
    fit = fit_fixed_ambient(
        [(r[0] - t0) / 3600 for r in window], [r[1] for r in window], ROOM_TEMP, target, cooling=True
    )
    fit_note = f"Fit {clock(t0, now)}-{clock(t_last, now)}, {len(window)} Punkte, RMS {fit.rms:.2f} C"
    notes: tuple[str, ...] = (f"bis {target:.1f} C, A=Umgebung {ROOM_TEMP:g} C", fit_note)
    head = f"{label} bis {target:.1f} C (A=Umgebung {ROOM_TEMP:g} C, {fit_note})"
    if fit.eta_hours is None:
        value = "nie (Umgebung >= Ziel)"
    elif fit.eta_hours > MAX_ETA_HOURS:
        value = "nicht absehbar (Kern faellt kaum)"
    else:
        value = f"~{clock(t_last + fit.eta_hours * 3600, now)}"
    plain = f"{head}: {value}"
    if value.startswith("~") and fit.last_deviates:
        notes, plain = _deviation(notes, plain, fit.core_measured, fit.core_model, fit.eta_hours_measured, t_last, now)
    return [StatusRow(label, value, notes, plain=plain)]


def forecast_row(
    state: WatcherState, now: float, current_internal: float | None, target: float | None
) -> StatusRow | None:
    """Build the watcher's own ETA forecast row to the pull target.

    Linear trend over the last ``FORECAST_WINDOW_MINUTES`` of core readings.

    Args:
        state: Watcher state holding the samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        target: Effective pull target (override or MEATER target).

    Returns:
        A clearly labelled forecast row, or ``None`` when there is no target,
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
    trend = f"Trend {rate:.1f} C/h ueber {span} Min."
    return StatusRow(
        "Watcher-Prognose",
        when,
        (f"bis {target:.1f} C", trend),
        plain=f"Watcher-Prognose bis {target:.1f} C: {when} ({trend})",
    )


def eta_rows(
    state: WatcherState,
    now: float,
    data: MeaterData,
    current_internal: float | None,
    target: float | None,
) -> list[StatusRow]:
    """Build all remaining-time rows: MEATER's estimate plus own forecasts.

    Shared by the full status message and the compact ``eta`` reply.  During
    the rest phase the cooling forecast replaces the forecasts to the pull
    target, which make no sense on a falling core.

    Args:
        state: Current watcher state (samples, wrap time, pit temp).
        now: Current epoch time.
        data: The MEATER reading of this check.
        current_internal: Current core temperature in C.
        target: Effective pull target in C (override or MEATER target).

    Returns:
        The ``Remaining`` row, followed by the rest-phase forecast or the
        watcher's trend forecast and the end-phase forecast when available.
    """
    remaining = data.get("remaining_time")
    if remaining_known(remaining, data.get("remaining_minutes")):
        rows = [StatusRow("Remaining", str(remaining))]
    else:
        note = "MEATER liefert gerade keine Restzeit"
        rows = [StatusRow("Remaining", "unbekannt", (note,), plain=f"Remaining: unbekannt ({note})")]
    rest = restphase_rows(state, now, current_internal)
    if rest:
        return rows + rest
    forecast = forecast_row(state, now, current_internal, target)
    if forecast:
        rows.append(forecast)
    rows.extend(endphase_rows(state, now, current_internal, target))
    return rows


def _flagged(
    label: str, value: str, flags: list[str], *, notes: tuple[str, ...] = (), plain: str | None = None
) -> StatusRow:
    """Build a row whose state flags read ``[flag]`` in plain text and are notes in HTML.

    Args:
        label: Row label.
        value: Row value.
        flags: State flags, e.g. ``"bereits ausgeloest"``.
        notes: Further HTML notes shown before the flags (already part of *plain*).
        plain: Plain text before the flags; defaults to ``"label: value"``.

    Returns:
        The row.
    """
    base = plain if plain is not None else f"{label}: {value}"
    return StatusRow(label, value, (*notes, *flags), plain=base + "".join(f" [{f}]" for f in flags))


def alert_rows(state: WatcherState) -> list[StatusRow]:
    """Build a human-readable summary of all alert modes.

    Args:
        state: Current watcher state.

    Returns:
        List of status rows, one per alert mode.
    """
    rows = []

    # Pause (wrap / lid open) — listed first, it mutes several alerts below
    if state.pause_until is not None:
        left = f"noch {pause_minutes_left(state, timeutil.now())} Min."
        muted = "TempDown, Stall und Ambient-Bereich stumm"
        rows.append(
            StatusRow(
                "Pause",
                f"AKTIV ({left})",
                (muted, "'resume' beendet"),
                plain=f"Pause: AKTIV ({left}; {muted}; 'resume' beendet)",
            )
        )

    # TempDown
    if state.tempalert_tempdown_enabled:
        threshold = f"Schwelle: {AMBIENT_TEMP_DROP_THRESHOLD} C"
        rows.append(StatusRow("TempDown", "AN", (threshold,), plain=f"TempDown: AN ({threshold})"))
    else:
        rows.append(StatusRow("TempDown", "AUS"))

    # Ruhephase
    if state.tempalert_ruhephase_enabled and state.ruhephase_target_temp is not None:
        rows.append(StatusRow("Ruhephase", f"AN (Ziel: {state.ruhephase_target_temp} C)"))
    else:
        rows.append(StatusRow("Ruhephase", "AUS"))

    # Stall
    if state.tempalert_stall_enabled:
        rate = f"< {state.stall_max_rate:g} C/h"
        window = f"Anstieg ueber {STALL_WINDOW_MINUTES} Min., Kern {STALL_CORE_MIN:g}-{STALL_CORE_MAX:g} C"
        flags = []
        if state.stall_alerted:
            flags.append("bereits ausgeloest")
        if state.tempalert_ruhephase_enabled:
            flags.append("stumm in der Ruhephase")
        elif state.stall_pause_checks_left > 0:
            flags.append(f"pausiert nach Stoerung: noch {state.stall_pause_checks_left} Check(s)")
        rows.append(
            _flagged("Stall", f"AN ({rate})", flags, notes=(window,), plain=f"Stall: AN (Alarm bei {rate} {window})")
        )
    else:
        rows.append(StatusRow("Stall", "AUS"))

    # Wrap
    if state.tempalert_wrap_enabled and state.wrap_target_temp is not None:
        flags = []
        if state.wrap_alerted:
            flags.append("bereits ausgeloest")
        elif state.tempalert_ruhephase_enabled:
            flags.append("stumm in der Ruhephase")
        rows.append(_flagged("Wrap-Reminder", f"AN (bei {state.wrap_target_temp} C)", flags))
    else:
        rows.append(StatusRow("Wrap-Reminder", "AUS"))

    # Pull / take-out target override (overrides the MEATER target)
    label = "Pull-Alert (rausnehmen)"
    if state.target_override is not None:
        target = f"Ziel: {state.target_override} C"
        flags = ["bereits ausgeloest"] if state.target_reached_calls >= 3 else []
        rows.append(
            _flagged(
                label,
                f"AN ({target})",
                flags,
                notes=("override MEATER",),
                plain=f"{label}: AN ({target}, override MEATER)",
            )
        )
    else:
        rows.append(StatusRow(label, "AUS"))

    # Ambient range
    if state.tempalert_ambient_range_enabled:
        rows.append(StatusRow("Ambient-Bereich", f"AN ({state.ambient_range_min}-{state.ambient_range_max} C)"))
    else:
        rows.append(StatusRow("Ambient-Bereich", "AUS"))

    # Cook-end
    if state.tempalert_cookend_enabled:
        rows.append(_flagged("Cook-Ende", "AN", ["Cook beendet erkannt"] if state.cook_ended else []))
    else:
        rows.append(StatusRow("Cook-Ende", "AUS"))

    # Station offline
    label = "Station-Offline-Alert"
    if state.tempalert_station_offline_enabled:
        sip = f"SIP nach {STATION_OFFLINE_CALL_THRESHOLD} Cycles"
        flags = [f"aktuell offline: {state.offline_streak} Cycle(s)"] if state.offline_streak > 0 else []
        rows.append(_flagged(label, "AN", flags, notes=(sip,), plain=f"{label}: AN ({sip})"))
    else:
        rows.append(StatusRow(label, "AUS"))

    # Real smoker temperature for the end-phase forecast
    pit_src = "set pit" if state.pit_temp is not None else "Standard"
    flags = [f"gewrappt {clock(state.wrapped_at, timeutil.now())}"] if state.wrapped_at is not None else []
    rows.append(_flagged("Smoker-Temp (Pit)", f"{effective_pit_temp(state):g} C ({pit_src})", flags))

    # Check interval
    interval = f"{effective_check_interval(state)}s"
    if state.check_interval_override is not None:
        default = f"override; Standard {CHECK_INTERVAL}s"
        rows.append(
            StatusRow("Check-Intervall", interval, (default,), plain=f"Check-Intervall: {interval} ({default})")
        )
    else:
        rows.append(StatusRow("Check-Intervall", interval))

    # Startup test call (applies to the next watcher start)
    rows.append(StatusRow("Startup-Testanruf", "AN" if state.startup_test_call_enabled else "AUS"))

    return rows

#!/home/node/.openclaw/workspace/.venv/bin/python3
"""MEATER Brisket Watcher with async event loop and Matrix command interface.

Runs a persistent asyncio event loop that:

1.  Periodically monitors a MEATER cook by calling the monitor module
    directly (every 10 minutes by default).
2.  Listens for commands in a Matrix room (MeaterWatcher) and reacts in
    real time.

The watcher is split into modules:

* ``wachtmeater.commands`` — the Matrix chat commands (full list there).
* ``wachtmeater.watcher_state`` — per-cook state and its JSON state file.
* ``wachtmeater.watcher_settings`` — settings derived from the config.
* ``wachtmeater.alerting`` — alert message + SIP call delivery.
* ``wachtmeater.signal_stats`` — trend, stall rate, disturbance detection.
* ``wachtmeater.status_text`` — remaining-time lines and alert summary.
* ``wachtmeater.core_forecast`` — end-phase forecast (exponential fit).

This module fetches the MEATER data, runs the periodic check with all
alerts (``run_meater_check``) and drives the event loop (``event_loop``).

All alert settings are persisted per MEATER UUID in the JSON state file.
"""

import asyncio
import os
from datetime import datetime

from loguru import logger

from wachtmeater import alerting, cfg, read_dot_env_to_environ, timeutil, watcher_state
from wachtmeater.alerting import SyncSender, deliver_once, send_alert
from wachtmeater.commands import HELP_TEXT, handle_command
from wachtmeater.meater_monitor import MeaterData
from wachtmeater.messaging import MessagingBackend
from wachtmeater.signal_stats import append_sample, detect_disturbance, slope_per_hour, trend_window
from wachtmeater.status_text import alert_summary, eta_lines
from wachtmeater.watcher_settings import (
    AMBIENT_TEMP_DROP_THRESHOLD,
    COOKEND_ERROR_THRESHOLD,
    COOKEND_PROBE_REMOVED_TEMP,
    MEATER_URL,
    MEATER_UUID,
    STALL_CORE_MAX,
    STALL_CORE_MIN,
    STALL_DISTURBANCE_PAUSE_CHECKS,
    STALL_WINDOW_MINUTES,
    STATION_OFFLINE_CALL_THRESHOLD,
    TEMPDOWN_WARMUP_CORE_TEMP,
)
from wachtmeater.watcher_state import WatcherState, effective_check_interval, end_pause

read_dot_env_to_environ()


class WatcherError(Exception):
    """Raised when the watcher stops due to an error (non-zero exit)."""


# ---------------------------------------------------------------------------
# MEATER data
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
    now = timeutil.now()
    timestamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("MEATER Watcher Check")

    # A ``pause`` / ``wrapped`` window that ran out ends here — before the
    # ambient snapshot below, so the reset high-water mark is re-seeded from
    # this reading.
    pause_ended = state.pause_until is not None and now >= state.pause_until
    if pause_ended:
        end_pause(state, now)
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
            watcher_state.save_state(state)
            if errs >= COOKEND_ERROR_THRESHOLD:
                end_msg = (
                    f"Cook-Ende erkannt: {errs} aufeinanderfolgende Fehler beim "
                    f"Abrufen der MEATER-Daten. Die Cook-URL ist nicht mehr "
                    f"erreichbar (Cook wurde geloescht oder ist abgelaufen)."
                )
                logger.warning(end_msg)
                # Not delivered -> do not end yet; the next check (still an
                # error) retries the message.
                if deliver_once(
                    state,
                    "cookend",
                    end_msg,
                    send,
                    call_text="MEATER Cook scheint beendet zu sein. Die Cook-URL existiert nicht mehr.",
                ):
                    state.cook_ended = True
                    watcher_state.save_state(state)
                    return "__cook_ended__"
                watcher_state.save_state(state)

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
        if not state.offline_notified and send_alert(send, offline_msg):
            state.offline_notified = True

        # SIP-escalate once per outage when threshold is reached.
        if (
            state.tempalert_station_offline_enabled
            and not state.station_offline_call_sent
            and state.offline_streak >= STATION_OFFLINE_CALL_THRESHOLD
        ):
            alerting.call_pitmaster(
                f"MEATER Station seit {offline_minutes} Minuten offline. " f"Bitte Internet bzw. Block pruefen."
            )
            state.station_offline_call_sent = True

        watcher_state.save_state(state)
        return offline_msg

    # Recovery: previous cycle(s) were offline, this one succeeded.
    if state.offline_streak > 0:
        recovery_msg = f"✅ MEATER Station wieder online (war {state.offline_streak} Cycle(s) offline)."
        logger.info(recovery_msg)
        send_alert(send, recovery_msg)
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
    disturbance = detect_disturbance(state, now, current_internal, current_ambient)
    state.internal_samples = append_sample(state.internal_samples, now, current_internal)
    state.ambient_samples = append_sample(state.ambient_samples, now, current_ambient)
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
    eta_block = eta_lines(state, now, data, current_internal, effective_target)
    lines.extend(eta_block)
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
    lines.extend(alert_summary(state))
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
                send("\n".join(eta_head + eta_block + [f"Time: {timestamp}"]), None)
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
                send_alert(send, alarm_msg)
                alerting.call_pitmaster(alarm_msg)

    # 2) Ruhephase (cooldown monitoring)
    if state.tempalert_ruhephase_enabled and state.ruhephase_target_temp is not None:
        rp_target = state.ruhephase_target_temp
        if current_internal is not None and current_internal <= rp_target:
            alarm_msg = (
                f"Ruhephase beendet! Fleischtemperatur ({current_internal:.1f} C) hat "
                f"Ziel-Abkuehltemperatur ({rp_target} C) erreicht."
            )
            logger.warning(f"ALARM Ruhephase: {alarm_msg}")
            if deliver_once(state, "ruhephase", alarm_msg, send, call_text=alarm_msg):
                state.tempalert_ruhephase_enabled = False
                logger.info("Ruhephase-Alert automatisch deaktiviert.")

    # 3) Stall detection: rise rate over a time window inside the plausible
    #    core range.  Falling temperatures, disturbances and ``pause`` are
    #    no stall.
    if state.tempalert_stall_enabled and not (disturbance or stall_cooling_down or paused):
        pts = trend_window(state.internal_samples, now, STALL_WINDOW_MINUTES, state.trend_ignore_before)
        if pts is not None:
            rate = slope_per_hour(pts)
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
                if deliver_once(state, "stall", f"STALL ERKANNT: {alarm_msg}", send, call_text=alarm_msg):
                    state.stall_alerted = True

    # 4) Wrap reminder — muted in the rest phase: the meat is off the smoker,
    #    and the core is far above the wrap temperature.
    if (
        state.tempalert_wrap_enabled
        and state.wrap_target_temp is not None
        and not state.wrap_alerted
        and not state.tempalert_ruhephase_enabled
    ):
        wrap_temp = state.wrap_target_temp
        if current_internal is not None and current_internal >= wrap_temp:
            alarm_msg = (
                f"Wrap-Reminder! Kerntemperatur hat {current_internal:.1f} C erreicht "
                f"(Ziel: {wrap_temp} C). Jetzt in Butcher Paper/Folie einwickeln!"
            )
            logger.warning(f"ALARM Wrap: {alarm_msg}")
            if deliver_once(state, "wrap", alarm_msg, send, call_text=alarm_msg):
                state.wrap_alerted = True

    # 5) Ambient range (too hot / too cold) — repeats every check; muted
    #    during ``pause``.
    if state.tempalert_ambient_range_enabled and current_ambient is not None and not paused:
        lo = state.ambient_range_min
        hi = state.ambient_range_max
        if lo is not None and current_ambient < lo:
            alarm_msg = f"Ambient zu niedrig! {current_ambient:.1f} C unter Minimum {lo} C. " f"Feuer nachschueren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            send_alert(send, alarm_msg)
            alerting.call_pitmaster(alarm_msg)
        if hi is not None and current_ambient > hi:
            alarm_msg = f"Ambient zu hoch! {current_ambient:.1f} C ueber Maximum {hi} C. " f"Luftzufuhr reduzieren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            send_alert(send, alarm_msg)
            alerting.call_pitmaster(alarm_msg)

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
            delivered = send_alert(send, alarm_msg)
            alerting.call_pitmaster(alarm_msg)
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
            if deliver_once(
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
            if deliver_once(state, "cookend", end_msg, send):
                state.cook_ended = True

        # Also detect meater cloud "finished" state (cook page shows summary)
        if cook_status == "finished" and not state.cook_ended:
            end_msg = "Cook-Ende erkannt: MEATER Cloud zeigt Cook als beendet an."
            logger.warning(end_msg)
            if deliver_once(
                state,
                "cookend",
                end_msg,
                send,
                call_text="MEATER Cook ist offiziell beendet laut MEATER Cloud.",
                image_path=screenshot,
            ):
                state.cook_ended = True
                watcher_state.save_state(state)
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
    watcher_state.save_state(state)
    logger.debug(f"State saved (Max Ambient: {max_ambient} C, Quelle: {data.get('source', 'unknown')})")

    if state.cook_ended:
        return "__cook_ended__"

    return status_text


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

    state = watcher_state.load_state()

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
        watcher_state.save_state(state)

    # Startup test call — suppressed by the CLI flag or the persisted
    # per-cook setting (toggled via the ``enable/disable startcall`` command).
    if not skip_startup_test_call and state.startup_test_call_enabled:
        logger.info("Startup-Testanruf...")
        try:
            await asyncio.to_thread(alerting.call_pitmaster, "Meater Watcher gestartet. Dies ist ein Testanruf.")
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
        watcher_state.save_state(state)

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
            asyncio.create_task(asyncio.to_thread(alerting.call_pitmaster, spoken))
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

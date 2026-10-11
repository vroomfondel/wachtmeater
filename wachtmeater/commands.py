"""Matrix chat commands of the watcher (``status``, ``enable …``, ``pause``, ``wrapped`` …).

``handle_command`` parses one message, changes the ``WatcherState`` and
returns the reply text.  Commands that need the event loop (``status``,
``eta``, ``stop``, ``testcall``) return a sentinel instead, which
``wachtmeater.meater_watcher.event_loop`` acts on.

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
"""

import re

from loguru import logger

from wachtmeater import cfg, timeutil, watcher_state
from wachtmeater.alerting import clear_pending_call
from wachtmeater.status_text import endphase_earliest
from wachtmeater.timeutil import clock, last_clock_time
from wachtmeater.watcher_settings import (
    AMBIENT_TEMP_DROP_THRESHOLD,
    CHECK_INTERVAL,
    ENDPHASE_MIN_MINUTES,
    ENDPHASE_SETTLE_MINUTES,
    MIN_CHECK_INTERVAL,
    PAUSE_DEFAULT_MINUTES,
    PIT_TEMP_DEFAULT,
    STALL_CORE_MAX,
    STALL_CORE_MIN,
    STALL_WINDOW_MINUTES,
)
from wachtmeater.watcher_state import WatcherState, effective_pit_temp, end_pause

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


def mark_wrapped(state: WatcherState, wrapped_at: float) -> str:
    """Mark the wrap as done and anchor the end-phase forecast at *wrapped_at*.

    Args:
        state: Watcher state (mutated, not saved).
        wrapped_at: Epoch of the wrap — now, or backdated via ``wrapped at``.

    Returns:
        The confirmation text for Matrix.
    """
    now = timeutil.now()
    state.wrap_alerted = True
    state.wrapped_at = wrapped_at
    clear_pending_call(state, "wrap")
    earliest = endphase_earliest(state)
    assert earliest is not None  # wrapped_at was just set
    return (
        f"Wrap als erledigt markiert ({clock(wrapped_at, now)}). Endphasen-Prognose fittet ab "
        f"{clock(earliest, now)} mit A = Pit {effective_pit_temp(state):g} C (aendern mit 'set pit <C>'), "
        f"Prognose fruehestens nach {ENDPHASE_MIN_MINUTES} Min. Daten."
    )


def drop_previous_wrap_guard(state: WatcherState) -> float | None:
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


def trend_restart_reply(state: WatcherState, now: float) -> str:
    """Confirm a manual trend restart.

    Args:
        state: Watcher state after the change.
        now: Current epoch seconds.

    Returns:
        The confirmation text for Matrix.
    """
    if state.trend_ignore_before is None:
        return "Trend und Stall-Rate werten wieder alle Daten aus."
    return f"Trend und Stall-Rate werten ab {clock(state.trend_ignore_before, now)} aus."


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
        watcher_state.save_state(state)
        return f"TempDown-Alert aktiviert (Schwelle: {AMBIENT_TEMP_DROP_THRESHOLD} C)."

    if text in ("disable tempdown", "tempalert tempdown disable", "tempdown aus", "tempdown off"):
        state.tempalert_tempdown_enabled = False
        watcher_state.save_state(state)
        return "TempDown-Alert deaktiviert."

    # --- enable/disable ruhephase ---
    m = re.match(r"(?:enable ruhephase|ruhephase an|ruhephase on|ruhephase enable)\s+(\d+(?:\.\d+)?)", text)
    if m:
        target = float(m.group(1))
        state.tempalert_ruhephase_enabled = True
        state.ruhephase_target_temp = target
        clear_pending_call(state, "ruhephase")
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

        watcher_state.save_state(state)
        return reply

    if text in ("disable ruhephase", "ruhephase aus", "ruhephase off", "ruhephase disable"):
        state.tempalert_ruhephase_enabled = False
        clear_pending_call(state, "ruhephase")
        watcher_state.save_state(state)
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
        clear_pending_call(state, "stall")
        watcher_state.save_state(state)
        return (
            f"Stall-Erkennung aktiviert. Alarm wenn die Kerntemperatur ueber {STALL_WINDOW_MINUTES} Min. "
            f"langsamer als {rate:g} C/h steigt (nur bei {STALL_CORE_MIN:g}-{STALL_CORE_MAX:g} C Kern; "
            f"Temperaturabfall und Stoerungen wie Deckel auf zaehlen nicht)."
        )

    if text in ("disable stall", "stall aus", "stall off", "stall disable"):
        state.tempalert_stall_enabled = False
        state.stall_alerted = False
        clear_pending_call(state, "stall")
        watcher_state.save_state(state)
        return "Stall-Erkennung deaktiviert."

    if text == "reset stall":
        state.stall_alerted = False
        clear_pending_call(state, "stall")
        watcher_state.save_state(state)
        return "Stall-Alert zurueckgesetzt. Kann erneut ausloesen."

    # --- enable/disable wrap ---
    m = re.match(r"(?:enable wrap|wrap an|wrap on|wrap enable)\s+(\d+(?:\.\d+)?)", text)
    if m:
        temp = float(m.group(1))
        state.tempalert_wrap_enabled = True
        state.wrap_target_temp = temp
        state.wrap_alerted = False
        clear_pending_call(state, "wrap")
        watcher_state.save_state(state)
        return f"Wrap-Reminder aktiviert. Alarm bei Kerntemperatur {temp} C."

    if text in ("disable wrap", "wrap aus", "wrap off", "wrap disable"):
        state.tempalert_wrap_enabled = False
        state.wrap_alerted = False
        clear_pending_call(state, "wrap")
        watcher_state.save_state(state)
        return "Wrap-Reminder deaktiviert."

    if text == "reset wrap":
        state.wrap_alerted = False
        state.wrapped_at = None
        clear_pending_call(state, "wrap")
        watcher_state.save_state(state)
        return "Wrap-Alert zurueckgesetzt. Kann erneut ausloesen."

    # --- set / disable pull (take-out) target override ---
    m = re.match(r"(?:enable pull|pull an|pull on|pull enable|set pull|rausnehmen)\s+(\d+(?:\.\d+)?)", text)
    if m:
        temp = float(m.group(1))
        state.target_override = temp
        state.target_reached_calls = 0  # re-arm the take-out notifications
        watcher_state.save_state(state)
        return (
            f"Pull-Alert aktiviert. Alarm (Nachricht + Anruf) zum Rausnehmen bei "
            f"Kerntemperatur {temp} C — ueberschreibt die MEATER-Zieltemperatur."
        )

    if text in ("disable pull", "pull aus", "pull off", "pull disable", "reset pull"):
        state.target_override = None
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
        return f"Ambient-Bereich aktiviert: {lo} C - {hi} C."

    if text in ("disable ambient", "ambient aus", "ambient off", "ambient disable"):
        state.tempalert_ambient_range_enabled = False
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
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
        now = timeutil.now()
        dropped = drop_previous_wrap_guard(state)
        reply = mark_wrapped(state, last_clock_time(hour, minute, now))
        if dropped is not None:
            reply += f"\nVorheriger Wrap ersetzt: Trend-Neustart von {clock(dropped, now)} verworfen."
        watcher_state.save_state(state)
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
            state.pause_until = timeutil.now() + minutes * 60
            reply_parts.append(
                f"Pause fuer {minutes} Min.: TempDown, Stall und Ambient-Bereich sind stumm. "
                f"Danach wird Max-Ambient automatisch zurueckgesetzt. Pull-, Offline- und "
                f"Cook-Ende-Alarm bleiben aktiv. Vorzeitig beenden mit 'resume'."
            )
        if is_wrapped:
            reply_parts.append(mark_wrapped(state, timeutil.now()))
        watcher_state.save_state(state)
        return "\n".join(reply_parts)

    if text in ("resume", "weiter", "pause aus", "pause off", "pause beenden"):
        if state.pause_until is None:
            return "Keine Pause aktiv."
        end_pause(state, timeutil.now())
        watcher_state.save_state(state)
        return (
            "Pause beendet: TempDown, Stall und Ambient-Bereich wieder aktiv. Max-Ambient "
            "zurueckgesetzt — der naechste Check setzt den Hoechstwert neu."
        )

    # --- enable/disable cookend ---
    if text in ("enable cookend", "cookend an", "cookend on", "cookend enable"):
        state.tempalert_cookend_enabled = True
        state.cook_ended = False
        state.consecutive_errors = 0
        clear_pending_call(state, "cookend")
        watcher_state.save_state(state)
        return "Cook-Ende-Erkennung aktiviert."

    if text in ("disable cookend", "cookend aus", "cookend off", "cookend disable"):
        state.tempalert_cookend_enabled = False
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
        return "Station-Offline-Alert deaktiviert."

    # --- trend ab HH:MM / trend reset (manual trend restart) ---
    m = re.match(r"trend\s+(?:ab|from|since)\s+(\d{1,2}):(\d{2})$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return "Fehler: Uhrzeit als HH:MM angeben, z.B. 'trend ab 12:20'."
        now = timeutil.now()
        state.trend_ignore_before = last_clock_time(hour, minute, now)
        watcher_state.save_state(state)
        return trend_restart_reply(state, now)

    if text in ("trend reset", "reset trend"):
        state.trend_ignore_before = None
        watcher_state.save_state(state)
        return trend_restart_reply(state, timeutil.now())

    # --- set / reset real smoker (pit) temperature ---
    m = re.match(r"(?:set\s+)?(?:pit|smoker)\s+(\d+(?:\.\d+)?)\s*$", text)
    if m:
        pit = float(m.group(1))
        if not 50 <= pit <= 300:
            return "Fehler: Smokertemperatur muss zwischen 50 und 300 C liegen."
        state.pit_temp = pit
        watcher_state.save_state(state)
        return (
            f"Smokertemperatur auf {pit:g} C gesetzt (Obergrenze fuer die Endphasen-Prognose; "
            f"Standard {PIT_TEMP_DEFAULT:g} C)."
        )

    if text in ("reset pit", "reset smoker", "disable pit"):
        state.pit_temp = None
        watcher_state.save_state(state)
        return f"Smokertemperatur auf Standard zurueckgesetzt ({PIT_TEMP_DEFAULT:g} C)."

    # --- set / reset check interval ---
    if text in ("reset interval", "reset intervall", "set interval default", "set intervall default"):
        state.check_interval_override = None
        watcher_state.save_state(state)
        return f"Check-Intervall auf Standard zurueckgesetzt ({CHECK_INTERVAL}s)."

    m = re.match(r"(?:set\s+)?intervall?\s+(\d+)\s*$", text)
    if m:
        secs = int(m.group(1))
        if secs < MIN_CHECK_INTERVAL:
            return f"Fehler: Intervall muss mindestens {MIN_CHECK_INTERVAL}s sein."
        state.check_interval_override = secs
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
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
        watcher_state.save_state(state)
        return "Startup-Testanruf deaktiviert (gilt ab dem naechsten Start)."

    # --- stop ---
    if text in ("stop", "quit", "beenden", "exit"):
        return "__stop__"

    return None

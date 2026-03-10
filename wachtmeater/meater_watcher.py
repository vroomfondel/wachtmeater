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
    - ``disable ruhephase``         — Disable rest/cooldown alert.
    - ``enable stall [<min_delta>]``— Enable stall detection (optional: min
      rise in C over the stall window, default 1).
    - ``disable stall``             — Disable stall detection.
    - ``enable wrap <temp>``        — Enable wrap reminder at internal temp C.
    - ``disable wrap``              — Disable wrap reminder.
    - ``enable ambient <min> <max>``— Enable ambient range alert (min/max C).
    - ``disable ambient``           — Disable ambient range alert.
    - ``enable cookend``            — Enable cook-end detection (auto-stop).
    - ``disable cookend``           — Disable cook-end detection.
    - ``hilfe`` / ``help``          — Show available commands.
    - ``stop`` / ``quit`` / ``beenden`` — Shut down the watcher loop.

All alert settings are persisted per MEATER UUID in the JSON state file.
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from loguru import logger

from wachtmeater import cfg, read_dot_env_to_environ
from wachtmeater.config import AlertDefaultsConfig, WatcherState
from wachtmeater.messaging import MessagingBackend

read_dot_env_to_environ()

SyncSender = Callable[[str, str | None], None]
"""Synchronous sender callable: ``(text, image_path_or_None) -> None``."""


class MeaterData(TypedDict, total=False):
    """Flat dictionary of MEATER cook values returned by the monitor."""

    internal_temp_c: int | None
    ambient_temp_c: int | None
    target_temp_c: int | None
    remaining_time: str | None
    elapsed_time: str | None
    status: str
    cook_name: str | None
    screenshot: str | None
    started_at: str | None
    peak_temp_c: int | None
    error: str


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

# Stall detection defaults
STALL_WINDOW: int = cfg.monitoring.stall_window

# Cook-end detection: consecutive errors before declaring cook ended
COOKEND_ERROR_THRESHOLD: int = cfg.monitoring.cookend_error_threshold
# Internal temp below this after having been above 50 C => probe removed
COOKEND_PROBE_REMOVED_TEMP: float = cfg.monitoring.cookend_probe_removed_temp

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
    state.stall_min_delta = alert_cfg.stall_min_delta
    state.tempalert_wrap_enabled = alert_cfg.tempalert_wrap_enabled
    state.wrap_target_temp = alert_cfg.wrap_target_temp or None
    state.tempalert_ambient_range_enabled = alert_cfg.tempalert_ambient_range_enabled
    state.ambient_range_min = alert_cfg.ambient_range_min or None
    state.ambient_range_max = alert_cfg.ambient_range_max or None
    state.tempalert_cookend_enabled = alert_cfg.tempalert_cookend_enabled


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


# ---------------------------------------------------------------------------
# MEATER data + alerting helpers
# ---------------------------------------------------------------------------


def get_meater_data() -> MeaterData:
    """Fetch current MEATER cook data by calling ``extract_via_browser`` directly.

    Returns:
        Flat dictionary with cook fields on success, or a dictionary
        with an ``"error"`` key describing the failure.
    """
    try:
        from wachtmeater.meater_monitor import extract_via_browser

        cook = extract_via_browser(MEATER_URL)
        data: MeaterData = {
            "internal_temp_c": cook.internal_temp_c,
            "ambient_temp_c": cook.ambient_temp_c,
            "target_temp_c": cook.target_temp_c,
            "remaining_time": cook.remaining_time,
            "elapsed_time": cook.elapsed_time,
            "status": cook.status,
            "cook_name": cook.cook_name,
            "screenshot": str(cook.screenshot) if cook.screenshot else None,
            "started_at": cook.started_at,
            "peak_temp_c": cook.peak_temp_c,
        }
        return data
    except Exception as e:
        return {"error": str(e)}


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
        s = f"Stall: AN (min. {state.stall_min_delta} C / {STALL_WINDOW} Checks)"
        if state.stall_alerted:
            s += " [bereits ausgeloest]"
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

    return lines


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------


def run_meater_check(
    state: WatcherState,
    send: SyncSender | None = None,
) -> str:
    """Fetch MEATER data, evaluate alarms, and update *state* in-place.

    Args:
        state: Mutable state; updated with latest temperatures, counters,
            and alert flags.
        send: Optional callable ``(text, image_path_or_None) -> None``
            for posting messages/images to Matrix via the existing session.

    Returns:
        A human-readable status string suitable for Matrix, or the
        sentinel ``"__cook_ended__"`` when cook-end is detected (signals
        the event loop to shut down).
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("MEATER Watcher Check")

    max_ambient = state.max_ambient_temp
    max_internal = state.max_internal_temp
    last_internal = state.last_internal_temp
    target_reached_calls = state.target_reached_calls

    data = get_meater_data()

    # --- Cook-end detection: consecutive fetch errors ---
    if "error" in data:
        msg = f"Fehler beim Abrufen der MEATER-Daten: {data['error']}"
        logger.warning(msg)

        if state.tempalert_cookend_enabled:
            errs = state.consecutive_errors + 1
            state.consecutive_errors = errs
            save_state(state)
            if errs >= COOKEND_ERROR_THRESHOLD:
                end_msg = (
                    f"Cook-Ende erkannt: {errs} aufeinanderfolgende Fehler beim "
                    f"Abrufen der MEATER-Daten. Der Cook wurde vermutlich beendet "
                    f"oder die URL ist nicht mehr erreichbar."
                )
                logger.warning(end_msg)
                if send is not None:
                    try:
                        send(end_msg, None)
                    except Exception:
                        pass
                call_pitmaster("MEATER Cook scheint beendet zu sein. " "Mehrere Abrufversuche sind fehlgeschlagen.")
                state.cook_ended = True
                save_state(state)
                return "__cook_ended__"

        return msg

    # Reset consecutive error counter on success
    state.consecutive_errors = 0

    current_internal = data.get("internal_temp_c")
    current_ambient = data.get("ambient_temp_c")
    target_temp = data.get("target_temp_c")
    remaining = data.get("remaining_time")
    elapsed = data.get("elapsed_time")
    cook_status = data.get("status", "unknown")
    cook_name = data.get("cook_name", "Unknown")
    screenshot: str = data.get("screenshot") or f"/data/meater-screenshot-{MEATER_UUID}.png"

    # Update max values
    if current_ambient is not None:
        if max_ambient is None or current_ambient > max_ambient:
            max_ambient = current_ambient
    if current_internal is not None:
        if max_internal is None or current_internal > max_internal:
            max_internal = current_internal

    # Track internal temp history for stall detection
    history = state.internal_temp_history
    if current_internal is not None:
        history.append(current_internal)
        # Keep only last STALL_WINDOW + 1 entries
        if len(history) > STALL_WINDOW + 1:
            history = history[-(STALL_WINDOW + 1) :]
        state.internal_temp_history = history

    # Build status text
    lines = [f"MEATER Status: {cook_name}"]
    if current_internal is not None:
        lines.append(f"Internal (Fleisch): {current_internal} C")
    if current_ambient is not None:
        lines.append(f"Ambient (Rauch): {current_ambient} C")
    if target_temp is not None:
        lines.append(f"Target: {target_temp} C")
    if max_ambient is not None:
        lines.append(f"Max Ambient: {max_ambient} C")
    if max_internal is not None:
        lines.append(f"Max Internal: {max_internal} C")
    if remaining:
        lines.append(f"Remaining: {remaining}")
    if elapsed:
        lines.append(f"Elapsed: {elapsed}")
    lines.append(f"Status: {cook_status}")
    lines.append("---")
    lines.extend(_alert_summary(state))
    lines.append(f"Time: {timestamp}")
    status_text = "\n".join(lines)

    logger.info(f"Status:\n{status_text}")

    # Post to Matrix
    if send is not None:
        try:
            send(status_text, screenshot)
        except Exception as e:
            logger.error(f"Matrix: unexpected error ({e})")

    # ===================================================================
    # ALERTS
    # ===================================================================

    # 1) Ambient temperature drop (tempdown / fire out)
    if state.tempalert_tempdown_enabled:
        if max_ambient is not None and current_ambient is not None:
            ambient_drop = max_ambient - current_ambient
            if ambient_drop >= AMBIENT_TEMP_DROP_THRESHOLD:
                alarm_msg = (
                    f"ACHTUNG: Smoker Temperatur ist um {ambient_drop:.1f} C vom "
                    f"Hoechstwert gefallen! Max: {max_ambient} C, jetzt: {current_ambient} C. "
                    f"Feuer koennte ausgegangen sein!"
                )
                logger.warning(f"ALARM TempDown: {alarm_msg}")
                call_pitmaster(alarm_msg)

    # 2) Ruhephase (cooldown monitoring)
    if state.tempalert_ruhephase_enabled and state.ruhephase_target_temp is not None:
        rp_target = state.ruhephase_target_temp
        if current_internal is not None and current_internal <= rp_target:
            alarm_msg = (
                f"Ruhephase beendet! Fleischtemperatur ({current_internal} C) hat "
                f"Ziel-Abkuehltemperatur ({rp_target} C) erreicht."
            )
            logger.warning(f"ALARM Ruhephase: {alarm_msg}")
            call_pitmaster(alarm_msg)
            state.tempalert_ruhephase_enabled = False
            logger.info("Ruhephase-Alert automatisch deaktiviert.")

    # 3) Stall detection
    if state.tempalert_stall_enabled and not state.stall_alerted:
        min_delta = state.stall_min_delta
        if len(history) > STALL_WINDOW:
            oldest = history[-(STALL_WINDOW + 1)]
            newest = history[-1]
            rise = newest - oldest
            if rise < min_delta:
                alarm_msg = (
                    f"Stall erkannt! Interne Temperatur ist in den letzten "
                    f"{STALL_WINDOW} Checks nur um {rise:.1f} C gestiegen "
                    f"(von {oldest} C auf {newest} C). Jetzt wrappen?"
                )
                logger.warning(f"ALARM Stall: {alarm_msg}")
                call_pitmaster(alarm_msg)
                if send is not None:
                    try:
                        send(f"STALL ERKANNT: {alarm_msg}", None)
                    except Exception:
                        pass
                state.stall_alerted = True
            elif rise >= min_delta * 2:
                # Stall seems to be over — reset so it can trigger again
                state.stall_alerted = False

    # 4) Wrap reminder
    if state.tempalert_wrap_enabled and state.wrap_target_temp is not None and not state.wrap_alerted:
        wrap_temp = state.wrap_target_temp
        if current_internal is not None and current_internal >= wrap_temp:
            alarm_msg = (
                f"Wrap-Reminder! Kerntemperatur hat {current_internal} C erreicht "
                f"(Ziel: {wrap_temp} C). Jetzt in Butcher Paper/Folie einwickeln!"
            )
            logger.warning(f"ALARM Wrap: {alarm_msg}")
            call_pitmaster(alarm_msg)
            state.wrap_alerted = True

    # 5) Ambient range (too hot / too cold)
    if state.tempalert_ambient_range_enabled and current_ambient is not None:
        lo = state.ambient_range_min
        hi = state.ambient_range_max
        if lo is not None and current_ambient < lo:
            alarm_msg = f"Ambient zu niedrig! {current_ambient} C unter Minimum {lo} C. " f"Feuer nachschueren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            call_pitmaster(alarm_msg)
        if hi is not None and current_ambient > hi:
            alarm_msg = f"Ambient zu hoch! {current_ambient} C ueber Maximum {hi} C. " f"Luftzufuhr reduzieren!"
            logger.warning(f"ALARM Ambient-Bereich: {alarm_msg}")
            call_pitmaster(alarm_msg)

    # 6) Target temperature reached
    if current_internal and target_temp and current_internal >= target_temp:
        logger.warning("ZIELTEMPERATUR ERREICHT!")
        if target_reached_calls < 3:
            try:
                call_pitmaster(
                    f"Brisket ist fertig! Zieltemperatur von {target_temp} C erreicht. "
                    f"Aktuelle Temperatur: {current_internal} C"
                )
                target_reached_calls += 1
            except Exception as e:
                logger.error(f"Target-reached call failed: {e}")

    # 7) Cook-end detection: probe removed (temp drops to near room temp)
    if state.tempalert_cookend_enabled and not state.cook_ended:
        if (
            max_internal is not None
            and max_internal > 50
            and current_internal is not None
            and current_internal < COOKEND_PROBE_REMOVED_TEMP
        ):
            end_msg = (
                f"Cook-Ende erkannt: Kerntemperatur auf {current_internal} C gefallen "
                f"(Max war {max_internal} C). Probe wurde vermutlich entfernt."
            )
            logger.warning(end_msg)
            if send is not None:
                try:
                    send(end_msg, None)
                except Exception:
                    pass
            call_pitmaster("MEATER Probe wurde vermutlich entfernt. Cook ist beendet.")
            state.cook_ended = True

        # Also detect meater-monitor status "done"
        if cook_status == "done" and not state.cook_ended:
            end_msg = f"Cook-Ende erkannt: MEATER meldet Status 'done'."
            logger.warning(end_msg)
            state.cook_ended = True

        # Also detect meater cloud "finished" state (cook page shows summary)
        if cook_status == "finished" and not state.cook_ended:
            end_msg = "Cook-Ende erkannt: MEATER Cloud zeigt Cook als beendet an."
            logger.warning(end_msg)
            if send is not None:
                try:
                    send(end_msg, screenshot)
                except Exception:
                    pass
            call_pitmaster("MEATER Cook ist offiziell beendet laut MEATER Cloud.")
            state.cook_ended = True
            save_state(state)
            return "__cook_ended__"

    # Internal temperature change info
    if last_internal is not None and current_internal is not None:
        internal_diff = current_internal - last_internal
        logger.debug(f"Internal Change: {internal_diff:+.1f} C (from {last_internal} C)")

    # Update state
    state.last_internal_temp = current_internal
    state.last_ambient_temp = current_ambient
    state.max_ambient_temp = max_ambient
    state.max_internal_temp = max_internal
    state.target_reached_calls = target_reached_calls
    save_state(state)
    logger.debug(f"State saved (Max Ambient: {max_ambient} C)")

    if state.cook_ended:
        return "__cook_ended__"

    return status_text


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT: str = """\
MEATER Watcher Befehle:

status                    - Aktuellen Status abrufen
enable tempdown           - Temperaturabfall-Alert AN
disable tempdown          - Temperaturabfall-Alert AUS
enable ruhephase <temp>   - Ruhephase-Alert AN (Zieltemp in C)
disable ruhephase         - Ruhephase-Alert AUS
enable stall [<delta>]    - Stall-Erkennung AN (opt: min. Anstieg in C)
disable stall             - Stall-Erkennung AUS
enable wrap <temp>        - Wrap-Reminder AN (bei Kerntemp in C)
disable wrap              - Wrap-Reminder AUS
enable ambient <min> <max>- Ambient-Bereich AN (min/max C)
disable ambient           - Ambient-Bereich AUS
enable cookend            - Cook-Ende-Erkennung AN
disable cookend           - Cook-Ende-Erkennung AUS
reset stall               - Stall-Alert zuruecksetzen
reset wrap                - Wrap-Alert zuruecksetzen
hilfe / help              - Diese Hilfe anzeigen
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
        A response string for the user, a sentinel (``"__status__"``,
        ``"__stop__"``), or ``None`` if the message is not a recognised
        command.
    """
    text = body.strip().lower()

    # --- help ---
    if text in ("hilfe", "help", "?"):
        return HELP_TEXT

    # --- status ---
    if text == "status":
        return "__status__"

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
        save_state(state)
        return f"Ruhephase-Alert aktiviert. Ziel-Abkuehltemperatur: {target} C"

    if text in ("disable ruhephase", "ruhephase aus", "ruhephase off", "ruhephase disable"):
        state.tempalert_ruhephase_enabled = False
        save_state(state)
        return "Ruhephase-Alert deaktiviert."

    # --- enable/disable stall ---
    m = re.match(r"(?:enable stall|stall an|stall on|stall enable)(?:\s+(\d+(?:\.\d+)?))?$", text)
    if m:
        delta = float(m.group(1)) if m.group(1) else 1.0
        state.tempalert_stall_enabled = True
        state.stall_min_delta = delta
        state.stall_alerted = False
        save_state(state)
        return (
            f"Stall-Erkennung aktiviert. Alarm wenn Kerntemperatur in "
            f"{STALL_WINDOW} Checks weniger als {delta} C steigt."
        )

    if text in ("disable stall", "stall aus", "stall off", "stall disable"):
        state.tempalert_stall_enabled = False
        state.stall_alerted = False
        save_state(state)
        return "Stall-Erkennung deaktiviert."

    if text == "reset stall":
        state.stall_alerted = False
        save_state(state)
        return "Stall-Alert zurueckgesetzt. Kann erneut ausloesen."

    # --- enable/disable wrap ---
    m = re.match(r"(?:enable wrap|wrap an|wrap on|wrap enable)\s+(\d+(?:\.\d+)?)", text)
    if m:
        temp = float(m.group(1))
        state.tempalert_wrap_enabled = True
        state.wrap_target_temp = temp
        state.wrap_alerted = False
        save_state(state)
        return f"Wrap-Reminder aktiviert. Alarm bei Kerntemperatur {temp} C."

    if text in ("disable wrap", "wrap aus", "wrap off", "wrap disable"):
        state.tempalert_wrap_enabled = False
        state.wrap_alerted = False
        save_state(state)
        return "Wrap-Reminder deaktiviert."

    if text == "reset wrap":
        state.wrap_alerted = False
        save_state(state)
        return "Wrap-Alert zurueckgesetzt. Kann erneut ausloesen."

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

    # --- enable/disable cookend ---
    if text in ("enable cookend", "cookend an", "cookend on", "cookend enable"):
        state.tempalert_cookend_enabled = True
        state.cook_ended = False
        state.consecutive_errors = 0
        save_state(state)
        return "Cook-Ende-Erkennung aktiviert."

    if text in ("disable cookend", "cookend aus", "cookend off", "cookend disable"):
        state.tempalert_cookend_enabled = False
        save_state(state)
        return "Cook-Ende-Erkennung deaktiviert."

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

    # Startup test call
    if not skip_startup_test_call:
        logger.info("Startup-Testanruf...")
        try:
            await asyncio.to_thread(call_pitmaster, "Meater Watcher gestartet. Dies ist ein Testanruf.")
        except Exception as e:
            logger.error(f"Startup-Testanruf fehlgeschlagen: {e}")

    # Initial MEATER check
    logger.info("Erster MEATER-Check...")
    result = await asyncio.to_thread(run_meater_check, state)
    if result == "__cook_ended__":
        logger.warning("Cook bereits beim ersten Check als beendet erkannt.")
        return

    # --- Set up messaging backend ---
    if messaging is None:
        from wachtmeater.matrix_adapter import MatrixMessagingAdapter

        messaging = MatrixMessagingAdapter()

    await messaging.connect()

    # --- Room selection / auto-creation ---
    room_id = await messaging.get_or_create_room(
        configured_room=cfg.matrix.room,
        auto_create=cfg.matrix.auto_create_room_for_meater_uuid,
        meater_uuid=MEATER_UUID,
        pitmaster_mxid=cfg.matrix.pitmaster,
        persisted_room_id=state.matrix_room_id,
    )
    if room_id and room_id != state.matrix_room_id:
        state.matrix_room_id = room_id
        save_state(state)

    # Determine bot's MXID from the backend
    bot_mxid = messaging.get_bot_user_id()
    logger.info(f"Matrix: eingeloggt als {bot_mxid}")
    logger.info(f"Matrix: {len(messaging.get_rooms())} Raum/Raeume beigetreten")

    # Send startup greeting with available commands
    startup_msg = f"Hallo! MEATER Watcher ist gestartet.\n\n{HELP_TEXT}"
    target_rooms = [room_id] if room_id else messaging.get_rooms()
    for rid in target_rooms:
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
        backend by scheduling coroutines on the running event loop.

        Args:
            text: Message body to send.
            image_path: Optional filesystem path to an image to attach.
        """
        target_rooms = [room_id] if room_id else messaging.get_rooms()
        for rid in target_rooms:
            fut = asyncio.run_coroutine_threadsafe(messaging.send_message(rid, text), loop)
            fut.result(timeout=30)
            if image_path:
                fut2 = asyncio.run_coroutine_threadsafe(messaging.send_image(rid, image_path), loop)
                fut2.result(timeout=90)

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

        if response == "__status__":
            check_result = await asyncio.to_thread(run_meater_check, state, _send_via_messaging)
            if check_result == "__cook_ended__":
                await messaging.send_message(
                    incoming_room_id,
                    "Cook-Ende erkannt. Watcher wird beendet.",
                )
                stop_event.set()
            return

        # Regular response — post back to the room
        try:
            await messaging.send_message(incoming_room_id, response)
        except Exception as e:
            logger.error(f"Matrix: Fehler beim Antworten: {e}")

    messaging.register_message_callback(on_message)

    # --- Periodic check task ---
    async def periodic_check() -> None:
        """Run MEATER checks at regular intervals until stopped."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
                break
            except asyncio.TimeoutError:
                logger.info(f"Periodischer Check (alle {CHECK_INTERVAL}s)...")
                check_result = await asyncio.to_thread(run_meater_check, state, _send_via_messaging)
                if check_result == "__cook_ended__":
                    logger.warning("Cook-Ende erkannt im periodischen Check. Beende...")
                    try:
                        # Notify all rooms
                        for rid in messaging.get_rooms():
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

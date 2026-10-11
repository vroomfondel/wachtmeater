"""Per-cook watcher state: the ``WatcherState`` dataclass and its JSON state file.

Also the values derived from the state (effective pit temperature and check
interval) and the pause bookkeeping.  Callers use ``watcher_state.load_state``
/ ``watcher_state.save_state`` via the module, so tests can patch them in one
place.
"""

import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any

from wachtmeater import cfg
from wachtmeater.config import AlertDefaultsConfig
from wachtmeater.watcher_settings import CHECK_INTERVAL, PIT_TEMP_DEFAULT, STATE_FILE


@dataclass
class WatcherState:
    """Per-cook persistent state (serialized to JSON)."""

    # Temperature tracking
    last_internal_temp: float | None = None
    last_ambient_temp: float | None = None
    max_ambient_temp: float | None = None
    max_internal_temp: float | None = None
    last_check: str | None = None
    target_reached_calls: int = 0
    # Timestamped readings ``[epoch_seconds, temp_c]`` for the time-based
    # stall detection, disturbance detection and the ETA forecast.
    internal_samples: list[list[float]] = field(default_factory=list)
    ambient_samples: list[list[float]] = field(default_factory=list)
    # Samples at or before this epoch are ignored for trend evaluation (stall
    # rate, ETA forecast) — set after a disturbance or a pause so the dip
    # does not count.
    trend_ignore_before: float | None = None
    consecutive_errors: int = 0
    # Alert modes
    tempalert_tempdown_enabled: bool = True
    tempalert_ruhephase_enabled: bool = False
    ruhephase_target_temp: float | None = None
    tempalert_stall_enabled: bool = False
    stall_max_rate: float = 4.0
    stall_alerted: bool = False
    # Checks left to skip stall evaluation after a disturbance
    stall_pause_checks_left: int = 0
    tempalert_wrap_enabled: bool = False
    wrap_target_temp: float | None = None
    wrap_alerted: bool = False
    tempalert_ambient_range_enabled: bool = False
    ambient_range_min: float | None = None
    ambient_range_max: float | None = None
    tempalert_cookend_enabled: bool = True
    cook_ended: bool = False
    matrix_room_id: str | None = None
    # Station offline detection
    tempalert_station_offline_enabled: bool = True
    offline_streak: int = 0
    station_offline_call_sent: bool = False
    offline_notified: bool = False
    # Keys of one-shot alerts whose SIP call already went out while the
    # Matrix message is still pending (prevents repeated calls on retry)
    alert_calls_sent: list[str] = field(default_factory=list)
    # ``pause`` / ``wrapped``: epoch until which TempDown, stall and the
    # ambient range alert are muted (None = not paused)
    pause_until: float | None = None
    # Epoch of the ``wrapped`` command — anchors the end-phase forecast fit
    wrapped_at: float | None = None
    # Per-cook real smoker temperature in C (None = config ``pit_temp``)
    pit_temp: float | None = None
    # Startup behaviour
    startup_test_call_enabled: bool = True
    # Per-cook check-interval override in seconds (None = use config default)
    check_interval_override: int | None = None
    # User-defined "pull / take out now" core-temp target in C that overrides
    # the MEATER target for the target-reached alert (None = use MEATER target)
    target_override: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WatcherState":
        """Construct a ``WatcherState`` from a plain dictionary.

        Unknown keys (e.g. from an older state file format) are silently
        ignored.

        Args:
            d: Dictionary, typically loaded from the JSON state file.

        Returns:
            A new ``WatcherState`` populated with the known keys from *d*.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """Serialize this state to a plain dictionary for JSON persistence.

        Returns:
            A dict representation of all fields (via ``dataclasses.asdict``).
        """
        return asdict(self)


def apply_alert_defaults(state: WatcherState, alert_cfg: AlertDefaultsConfig) -> None:
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
    apply_alert_defaults(state, cfg.alerts)
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


def end_pause(state: WatcherState, now: float) -> None:
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


def pause_minutes_left(state: WatcherState, now: float) -> int:
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

"""Replay of the real brisket cook 15f2e6f5 (10./11.10.2026) through the watcher.

Core/ambient readings every 10 minutes as exported from the Element chat.
``None`` marks the offline cycle at 02:43.  The checks are replayed on exact
600 s steps with index 20 (the offline cycle) at 02:43.
"""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from wachtmeater.config import WatcherState

mw = pytest.importorskip("wachtmeater.meater_watcher")

# fmt: off
CORE: list[float | None] = [
    14.3, 19.6, 24.5, 30.1, 34.6, 38.9, 42.5, 45.8, 48.7, 51.6, 54.1, 56.6, 58.8, 60.8,
    62.4, 64.3, 65.9, 67.1, 68.2, 69.1, None, 70.6, 71.2, 71.8, 72.3, 72.9, 73.4,
    73.9, 74.3, 74.8, 75.2, 75.6, 76.1, 76.4, 76.8, 77.2, 77.5, 77.2, 75.9, 75.9, 76.5,
    77.5, 78.6, 79.8, 80.6, 81.4, 82.2, 82.8, 83.4, 83.9, 84.4, 84.9, 85.4, 85.9, 86.2,
    86.5, 86.8, 87.2, 87.6, 87.9, 88.2, 88.6, 88.8, 89.1, 89.5, 89.8,
]
AMBIENT: list[float | None] = [
    82.0, 83.0, 84.7, 87.4, 89.2, 89.6, 90.8, 91.2, 91.8, 93.1, 94.5, 94.6, 96.8,
    97.6, 98.4, 98.8, 99.1, 100.8, 100.2, 100.8, None, 101.0, 100.9, 103.0, 101.6,
    100.6, 101.1, 100.0, 102.8, 102.4, 102.9, 104.9, 101.8, 100.6, 105.3, 102.1, 102.4,
    78.0, 94.5, 99.2, 99.1, 98.9, 104.2, 100.7, 101.6, 102.4, 102.4, 102.6, 101.9, 103.2,
    102.9, 103.9, 103.2, 103.7, 101.2, 104.7, 104.2, 103.1, 105.4, 103.8, 103.2, 103.2,
    105.8, 103.4, 103.8, 104.8,
]
# fmt: on

STEP = 600
OFFLINE_IDX = 20
T_OFFLINE = datetime(2026, 10, 11, 2, 43).timestamp()
T0 = T_OFFLINE - OFFLINE_IDX * STEP


def _t(idx: int) -> float:
    return T0 + idx * STEP


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class Replay:
    """Drive ``run_meater_check`` through the recorded cook."""

    def __init__(self) -> None:
        self.state = WatcherState()
        self.state.tempalert_tempdown_enabled = True
        self.state.tempalert_stall_enabled = True
        self.state.stall_max_rate = 4.0
        self.state.tempalert_wrap_enabled = True
        self.state.wrap_target_temp = 75.0
        self.state.target_override = 96.0
        self.state.startup_test_call_enabled = False
        self.messages: list[tuple[int, str]] = []
        self.calls: list[tuple[int, str]] = []
        self.status: dict[int, str] = {}
        self.idx = 0

    def _data(self, idx: int) -> dict[str, Any]:
        if CORE[idx] is None:
            return {"status": "offline", "cook_name": "Brisket"}
        return {
            "cook_name": "Brisket",
            "internal_temp_c": CORE[idx],
            "ambient_temp_c": AMBIENT[idx],
            "target_temp_c": 98.0,
            # MEATER stopped estimating after the probe was re-seated
            "remaining_time": "0m" if idx >= 38 else "5h 10m",
            "remaining_minutes": 0 if idx >= 38 else 310,
            "elapsed_time": "10h",
            "status": "cooking",
            "screenshot": None,
            "source": "cloud",
        }

    def command(self, idx: int, text: str, offset: float = 60.0) -> str | None:
        with patch.object(mw, "_now", return_value=_t(idx) + offset), patch.object(mw, "save_state"):
            reply: str | None = mw.handle_command(text, self.state)
        return reply

    def run(self, until: int, commands: dict[int, list[str]] | None = None) -> None:
        commands = commands or {}
        while self.idx < until:
            idx = self.idx

            def _send(text: str, image: str | None, _idx: int = idx) -> None:
                self.messages.append((_idx, text))

            def _call(text: str, _idx: int = idx) -> bool:
                self.calls.append((_idx, text))
                return True

            with (
                patch.object(mw, "_now", return_value=_t(idx)),
                patch.object(mw, "get_meater_data", return_value=self._data(idx)),
                patch.object(mw, "call_pitmaster", side_effect=_call),
                patch.object(mw, "save_state"),
            ):
                self.status[idx] = mw.run_meater_check(self.state, send=_send)
            for cmd in commands.get(idx, []):
                self.command(idx, cmd)
            self.idx += 1

    def alerts(self, needle: str) -> list[int]:
        return [i for i, m in self.messages if needle in m]


def test_series_lengths_match() -> None:
    assert len(CORE) == len(AMBIENT) == 66
    assert CORE.index(None) == AMBIENT.index(None) == OFFLINE_IDX


def test_exactly_one_stall_alert_during_the_real_stall() -> None:
    r = Replay()
    r.run(len(CORE))
    stall = r.alerts("STALL ERKANNT")
    assert len(stall) == 1, stall
    assert "02:30" <= _hm(_t(stall[0])) <= "03:30"
    # Stall fires before the wrap reminder -> asking to wrap is right here.
    assert "Jetzt wrappen?" in next(m for i, m in r.messages if "STALL" in m)


def test_no_stall_alert_from_wrap_disturbance_at_0548() -> None:
    # Re-arm stall detection right before the lid was opened: the ambient
    # dip (78 C) and the core drop 77.2 -> 75.9 at 05:43 must not be read as
    # a stall, and neither may the recovery afterwards.
    r = Replay()
    r.run(len(CORE), commands={36: ["reset stall"]})
    assert [i for i in r.alerts("STALL ERKANNT") if i > 36] == []


def test_wrap_reminder_posts_a_message_at_75c() -> None:
    r = Replay()
    r.run(len(CORE))
    wrap = r.alerts("Wrap-Reminder!")
    assert wrap == [30]  # 75.2 C
    assert any(i == 30 and "Wrap-Reminder!" in c for i, c in r.calls)
    assert r.state.wrap_alerted is True


def test_without_pause_wrapping_raises_tempdown() -> None:
    # Control: proves the pause test below is meaningful.
    r = Replay()
    r.run(len(CORE))
    assert 37 in r.alerts("ACHTUNG: Smoker Temperatur")


def test_pause_at_0530_suppresses_tempdown() -> None:
    r = Replay()
    # idx 36 is the 05:23 check; ``pause`` is sent a minute later (~05:30 window)
    r.run(len(CORE), commands={36: ["pause"]})
    assert r.alerts("ACHTUNG: Smoker Temperatur") == []
    assert not any("ACHTUNG" in c for _, c in r.calls)
    # The pause shows up with its remaining time while active ...
    assert "Pause: AKTIV (noch" in r.status[37]
    # ... and ends by itself, resetting max ambient to the then-current reading.
    assert "Pause beendet" in r.status[39]
    assert r.state.pause_until is None


def test_wrapped_marks_wrap_done_and_suppresses_tempdown() -> None:
    r = Replay()
    r.state.tempalert_wrap_enabled = False  # user wraps by hand, no reminder
    r.run(len(CORE), commands={36: ["wrapped"]})
    assert r.state.wrap_alerted is True
    assert r.alerts("ACHTUNG: Smoker Temperatur") == []


def test_resume_ends_pause_early() -> None:
    r = Replay()
    r.run(37, commands={36: ["pause 60"]})
    assert r.state.pause_until is not None
    reply = r.command(36, "resume", offset=300)
    assert reply is not None and "Pause beendet" in reply
    state = r.state.to_dict()  # fresh read; mypy keeps the narrowed attribute
    assert state["pause_until"] is None
    assert state["max_ambient_temp"] is None  # re-seeded by the next check


def test_offline_cycle_posts_once_without_call_then_recovers() -> None:
    r = Replay()
    r.run(OFFLINE_IDX + 2)
    assert r.alerts("MEATER Station offline") == [OFFLINE_IDX]
    assert r.alerts("wieder online") == [OFFLINE_IDX + 1]
    assert not any("offline" in c for _, c in r.calls)


def test_remaining_zero_is_shown_as_unknown_with_watcher_forecast() -> None:
    r = Replay()
    r.run(len(CORE))
    last = r.status[len(CORE) - 1]
    assert "Remaining: 0m" not in last
    assert "Remaining: unbekannt" in last
    line = next(l for l in last.splitlines() if l.startswith("Watcher-Prognose bis 96.0 C"))
    rate = float(line.split("Trend ")[1].split(" C/h")[0])
    assert 1.5 <= rate <= 2.5  # ~1.9 C/h from 08:30 on


def test_pull_override_target_line() -> None:
    r = Replay()
    r.run(25)
    assert "Target: 98.0 C (MEATER) → Pull bei 96.0 C (aktiv)" in r.status[24]


def test_alert_message_send_failure_is_retried_and_not_marked() -> None:
    # Homeserver rejects the wrap message once: flag stays unset, the next
    # check retries the message but does not call again.
    r = Replay()
    r.run(30)
    failing = MagicMock(side_effect=RuntimeError("M_LIMIT_EXCEEDED"))
    calls: list[str] = []

    def _call(text: str) -> bool:
        calls.append(text)
        return True

    with (
        patch.object(mw, "_now", return_value=_t(30)),
        patch.object(mw, "get_meater_data", return_value=r._data(30)),
        patch.object(mw, "call_pitmaster", side_effect=_call),
        patch.object(mw, "save_state"),
    ):
        mw.run_meater_check(r.state, send=failing)
    assert r.state.wrap_alerted is False
    assert sum("Wrap-Reminder!" in c for c in calls) == 1
    r.idx = 31
    r.run(32)
    assert r.alerts("Wrap-Reminder!") == [31]
    assert not any("Wrap-Reminder!" in c for _, c in r.calls)
    assert r.state.wrap_alerted is True

"""Replay of the real brisket cook 15f2e6f5 (10./11.10.2026) through the watcher.

The complete run as exported from the Element chat: every check from the
start at 23:17 to the cook end at 14:50, with its real timestamp, core and
ambient reading and MEATER's remaining time.  ``None`` readings mark the
offline cycles at 02:43 and 12:28; the last check (14:50) is the one where
the MEATER Cloud reported the cook as finished.  Restarts of the watcher
pod produced the extra checks at 12:44 and 12:48.

``REAL_COMMANDS`` are the user's commands at their real times.  Unless a
test replays them, it passes its own commands per check index.
"""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from wachtmeater.watcher_state import WatcherState

from wachtmeater import alerting, commands, timeutil, watcher_state

mw = pytest.importorskip("wachtmeater.meater_watcher")

# (time, core C, ambient C, MEATER remaining) — the 23:xx checks belong to
# 10.10., all later ones to 11.10.
# fmt: off
READINGS: list[tuple[str, float | None, float | None, str | None]] = [
    ("23:17:48", 14.3, 82.0, "0m"),
    ("23:28:06", 19.6, 83.0, "6h 10m"),
    ("23:38:22", 24.5, 84.7, "5h 2m"),
    ("23:48:39", 30.1, 87.4, "5h 9m"),
    ("23:58:57", 34.6, 89.2, "5h 7m"),
    ("00:09:13", 38.9, 89.6, "5h 31m"),
    ("00:19:29", 42.5, 90.8, "5h 45m"),
    ("00:29:47", 45.8, 91.2, "5h 55m"),
    ("00:40:03", 48.7, 91.8, "6h 7m"),
    ("00:50:20", 51.6, 93.1, "5h 59m"),
    ("01:00:33", 54.1, 94.5, "5h 50m"),
    ("01:10:49", 56.6, 94.6, "5h 57m"),
    ("01:21:05", 58.8, 96.8, "5h 54m"),
    ("01:31:22", 60.8, 97.6, "5h 57m"),
    ("01:41:38", 62.4, 98.4, "6h 10m"),
    ("01:51:53", 64.3, 98.8, "5h 52m"),
    ("02:02:10", 65.9, 99.1, "6h 21m"),
    ("02:12:27", 67.1, 100.8, "6h 43m"),
    ("02:22:47", 68.2, 100.2, "7h 17m"),
    ("02:33:04", 69.1, 100.8, "8h 25m"),
    ("02:43:21", None, None, None),
    ("02:53:38", 70.6, 101.0, "9h 59m"),
    ("03:03:55", 71.2, 100.9, "11h 15m"),
    ("03:14:11", 71.8, 103.0, "11h 0m"),
    ("03:24:27", 72.3, 101.6, "11h 48m"),
    ("03:34:43", 72.9, 100.6, "12h 30m"),
    ("03:44:58", 73.4, 101.1, "12h 46m"),
    ("03:55:14", 73.9, 100.0, "13h 12m"),
    ("04:05:31", 74.3, 102.8, "12h 58m"),
    ("04:15:48", 74.8, 102.4, "13h 20m"),
    ("04:26:05", 75.2, 102.9, "13h 5m"),
    ("04:36:20", 75.6, 104.9, "13h 2m"),
    ("04:46:37", 76.1, 101.8, "14h 6m"),
    ("04:56:53", 76.4, 100.6, "15h 16m"),
    ("05:07:08", 76.8, 105.3, "13h 58m"),
    ("05:17:25", 77.2, 102.1, "15h 3m"),
    ("05:27:41", 77.5, 102.4, "14h 30m"),
    ("05:37:58", 77.2, 78.0, "0m"),
    ("05:48:15", 75.9, 94.5, "0m"),
    ("05:58:30", 75.9, 99.2, "0m"),
    ("06:08:48", 76.5, 99.1, "0m"),
    ("06:19:05", 77.5, 98.9, "0m"),
    ("06:29:21", 78.6, 104.2, "0m"),
    ("06:39:38", 79.8, 100.7, "0m"),
    ("06:49:57", 80.6, 101.6, "0m"),
    ("07:00:12", 81.4, 102.4, "0m"),
    ("07:10:28", 82.2, 102.4, "0m"),
    ("07:20:43", 82.8, 102.6, "0m"),
    ("07:31:00", 83.4, 101.9, "0m"),
    ("07:41:16", 83.9, 103.2, "0m"),
    ("07:51:32", 84.4, 102.9, "0m"),
    ("08:01:48", 84.9, 103.9, "4h 27m"),
    ("08:12:04", 85.4, 103.2, "4h 12m"),
    ("08:22:21", 85.9, 103.7, "4h 3m"),
    ("08:32:37", 86.2, 101.2, "4h 31m"),
    ("08:42:53", 86.5, 104.7, "4h 18m"),
    ("08:53:10", 86.8, 104.2, "4h 30m"),
    ("09:03:25", 87.2, 103.1, "4h 5m"),
    ("09:13:39", 87.6, 105.4, "6h 3m"),
    ("09:23:53", 87.9, 103.8, "5h 45m"),
    ("09:34:08", 88.2, 103.2, "5h 50m"),
    ("09:44:23", 88.6, 103.2, "5h 39m"),
    ("09:54:39", 88.8, 105.8, "6h 29m"),
    ("10:04:59", 89.1, 103.4, "5h 30m"),
    ("10:15:19", 89.5, 103.8, "5h 20m"),
    ("10:25:36", 89.8, 104.8, "5h 4m"),
    ("10:36:01", 90.1, 104.8, "5h 11m"),
    ("10:46:20", 90.4, 101.9, "4h 44m"),
    ("10:56:41", 90.8, 107.4, "4h 21m"),
    ("11:06:57", 91.1, 104.1, "4h 21m"),
    ("11:17:14", 91.2, 105.0, "4h 35m"),
    ("11:27:32", 91.4, 104.0, "4h 35m"),
    ("11:37:52", 91.6, 106.6, "4h 33m"),
    ("11:48:11", 92.1, 108.2, "3h 41m"),
    ("11:58:30", 92.9, 113.9, "2h 19m"),
    ("12:08:49", 93.9, 109.3, "1h 10m"),
    ("12:18:19", 94.1, 105.2, "1h 9m"),
    ("12:28:43", None, None, None),
    ("12:38:40", 94.2, 105.7, "1h 26m"),
    ("12:44:26", 94.2, 105.3, "1h 27m"),
    ("12:48:26", 94.2, 105.7, "1h 42m"),
    ("12:55:02", 94.3, 107.8, "1h 51m"),
    ("13:05:16", 94.7, 106.6, "1h 36m"),
    ("13:15:16", 94.9, 105.6, "1h 36m"),
    ("13:25:15", 94.8, 107.8, "1h 59m"),
    ("13:35:16", 94.9, 104.4, "2h 3m"),
    ("13:45:16", 94.9, 107.6, "2h 6m"),
    ("13:55:16", 95.2, 105.9, "2h 3m"),
    ("14:05:15", 95.3, 108.4, "2h 21m"),
    ("14:15:16", 95.8, 112.0, "1h 24m"),
    ("14:25:16", 96.4, 111.4, "47m"),
    ("14:35:11", 96.8, 112.2, "14m"),
    ("14:45:11", 96.4, 97.2, "1h 35m"),
    ("14:50:12", 95.4, 95.8, None),
]
# fmt: on

REAL_COMMANDS: list[tuple[str, str]] = [
    ("23:18:11", "Enable pull 95"),
    ("05:27:51", "Disable tempdown"),
    ("05:36:56", "Reset ambient"),
    ("05:37:06", "Enable tempdown"),
    ("10:09:06", "Enable pull 96"),
    ("12:13:21", "enable pull 96.5"),
    ("12:18:12", "enable cookend"),
    ("12:21:00", "wrapped"),
    ("12:22:06", "resume"),
    ("12:44:36", "Wrapped at 05:30"),
    ("14:40:47", "enable Ruhephase 65 "),
]

CORE: list[float | None] = [r[1] for r in READINGS]
AMBIENT: list[float | None] = [r[2] for r in READINGS]
OFFLINE_IDXS = [i for i, c in enumerate(CORE) if c is None]
OFFLINE_IDX = OFFLINE_IDXS[0]
FINISHED_IDX = len(READINGS) - 1
PULL_IDX = next(i for i, r in enumerate(READINGS) if r[0].startswith("14:35"))
"""The check that raised the pull alarm (96.8 C)."""
# 96.5 C was crossed between 14:25 (96.4) and 14:35 (96.8): ~14:28.
ARRIVAL_965 = datetime(2026, 10, 11, 14, 27, 45).timestamp()


def _at(hms: str) -> float:
    """Epoch of a wall-clock time of the cook (``HH:MM`` or ``HH:MM:SS``)."""
    parts = [int(p) for p in hms.split(":")] + [0]
    day = 10 if parts[0] >= 20 else 11
    return datetime(2026, 10, day, parts[0], parts[1], parts[2]).timestamp()


def _t(idx: int) -> float:
    return _at(READINGS[idx][0])


def _i(hhmm: str) -> int:
    """Index of the check at wall-clock ``HH:MM``."""
    return next(i for i, r in enumerate(READINGS) if r[0].startswith(hhmm))


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _minutes(remaining: str | None) -> int | None:
    if remaining is None:
        return None
    hours, _, rest = remaining.rpartition("h")
    return int(hours or 0) * 60 + int(rest.strip().rstrip("m") or 0)


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
        self.replies: list[tuple[int, str]] = []
        self.status: dict[int, str] = {}
        self.idx = 0

    def _data(self, idx: int) -> dict[str, Any]:
        _, core, ambient, remaining = READINGS[idx]
        if core is None:
            return {"status": "offline", "cook_name": "Brisket"}
        finished = idx == FINISHED_IDX
        return {
            "cook_name": "Brisket",
            "internal_temp_c": core,
            "ambient_temp_c": ambient,
            "target_temp_c": 0.0 if finished else 98.0,
            "remaining_time": remaining or "",
            "remaining_minutes": _minutes(remaining),
            "elapsed_time": "10h",
            "status": "finished" if finished else "cooking",
            "screenshot": None,
            "source": "cloud",
        }

    def command(self, idx: int, text: str, offset: float = 60.0, at: float | None = None) -> str | None:
        now = at if at is not None else _t(idx) + offset
        with patch.object(timeutil, "now", return_value=now), patch.object(watcher_state, "save_state"):
            reply: str | None = commands.handle_command(text, self.state)
        self.replies.append((idx, reply or ""))
        return reply

    def run(
        self,
        until: int,
        commands: dict[int, list[str]] | None = None,
        timed: list[tuple[str, str]] | None = None,
    ) -> None:
        """Replay the checks up to (excluding) index *until*.

        Args:
            until: First check index not to run.
            commands: Commands sent a minute after the check with that index.
            timed: Commands at their wall-clock time (``REAL_COMMANDS``); each
                runs after the last check before it.
        """
        commands = commands or {}
        pending = sorted(((_at(hms), text) for hms, text in timed or []), key=lambda c: c[0])
        while self.idx < until:
            idx = self.idx

            def _send(text: str, image: str | None, _idx: int = idx) -> None:
                self.messages.append((_idx, text))

            def _call(text: str, _idx: int = idx) -> bool:
                self.calls.append((_idx, text))
                return True

            with (
                patch.object(timeutil, "now", return_value=_t(idx)),
                patch.object(mw, "get_meater_data", return_value=self._data(idx)),
                patch.object(alerting, "call_pitmaster", side_effect=_call),
                patch.object(watcher_state, "save_state"),
            ):
                self.status[idx] = mw.run_meater_check(self.state, send=_send)
            for cmd in commands.get(idx, []):
                self.command(idx, cmd)
            next_t = _t(idx + 1) if idx + 1 < len(READINGS) else float("inf")
            while pending and pending[0][0] < next_t:
                at, text = pending.pop(0)
                self.command(idx, text, at=at)
            self.idx += 1

    def alerts(self, needle: str) -> list[int]:
        return [i for i, m in self.messages if needle in m]


def test_series_covers_the_whole_cook() -> None:
    assert len(CORE) == len(AMBIENT) == 94
    assert OFFLINE_IDXS == [20, _i("12:28")] == [i for i, a in enumerate(AMBIENT) if a is None]
    assert READINGS[0][0] == "23:17:48" and READINGS[FINISHED_IDX][0] == "14:50:12"
    assert all(_t(i) < _t(i + 1) for i in range(len(READINGS) - 1))


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
    # idx 36 is the 05:27 check; ``pause`` is sent a minute later (~05:30 window).
    # Up to the pull: afterwards the falling pit legitimately raises TempDown.
    r.run(PULL_IDX + 1, commands={36: ["pause"]})
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
    r.run(PULL_IDX + 1, commands={36: ["wrapped 20"]})
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
    # MEATER reported "0m" from the wrap (05:37) until 07:51.
    r = Replay()
    r.run(_i("08:01") + 1)
    zero = r.status[_i("07:51")]
    assert "Remaining: 0m" not in zero
    assert "Remaining: unbekannt" in zero
    line = next(l for l in zero.splitlines() if l.startswith("Watcher-Prognose bis 96.0 C"))
    rate = float(line.split("Trend ")[1].split(" C/h")[0])
    assert 3.0 <= rate <= 4.5  # ~3.7 C/h while the core catches up after the wrap
    assert "Remaining: 4h 27m" in r.status[_i("08:01")]


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
        patch.object(timeutil, "now", return_value=_t(30)),
        patch.object(mw, "get_meater_data", return_value=r._data(30)),
        patch.object(alerting, "call_pitmaster", side_effect=_call),
        patch.object(watcher_state, "save_state"),
    ):
        mw.run_meater_check(r.state, send=failing)
    assert r.state.wrap_alerted is False
    assert sum("Wrap-Reminder!" in c for c in calls) == 1
    r.idx = 31
    r.run(32)
    assert r.alerts("Wrap-Reminder!") == [31]
    assert not any("Wrap-Reminder!" in c for _, c in r.calls)
    assert r.state.wrap_alerted is True


def _endphase(status: str) -> list[str]:
    lines = status.splitlines()
    i = next((n for n, l in enumerate(lines) if l.startswith("Endphasen-Prognose")), None)
    if i is None:
        return []
    block = [lines[i]]
    block += [l for l in lines[i + 1 :] if l.startswith("  ")][:4]
    return block


def test_no_endphase_forecast_without_wrapped() -> None:
    r = Replay()
    r.run(PULL_IDX, commands={36: ["pause"]})
    assert _endphase(r.status[PULL_IDX - 1]) == []


def test_endphase_forecast_after_wrapped() -> None:
    r = Replay()
    r.run(len(CORE), commands={36: ["wrapped"]})
    # Fit starts fixed 60 min after the wrap; a forecast needs 60 min of data.
    assert _endphase(r.status[_i("06:49")])[0].startswith("Endphasen-Prognose: sammelt Daten (Fit ab 06:28")
    early = _endphase(r.status[_i("08:42")])
    assert early[0].startswith("Endphasen-Prognose bis 96.0 C (A=Pit 110 C, Fit 06:29-08:42")
    block = _endphase(r.status[_i("10:25")])
    assert len(block) == 1
    assert block[0].startswith("Endphasen-Prognose bis 96.0 C (A=Pit 110 C, Fit 06:29-10:25, 24 Punkte")
    assert block[0].endswith("~13:51")
    # The fit keeps growing through the heat spike and the plateau at ~94.5 C.
    late = _endphase(r.status[_i("13:45")])
    assert late[0].startswith("Endphasen-Prognose bis 96.0 C (A=Pit 110 C, Fit 06:29-13:45, 44 Punkte")
    assert late[0].endswith("~14:31")
    # Target reached (96.4 C at 14:25): no forecast any more.
    assert _endphase(r.status[_i("14:25")]) == []
    assert r.state.wrapped_at is not None
    assert r.state.internal_samples[0][0] <= r.state.wrapped_at + 60 * 60 + 600


def test_set_pit_changes_endphase_forecast() -> None:
    base = Replay()
    base.run(len(CORE), commands={36: ["wrapped"]})
    hot = Replay()
    hot.run(len(CORE), commands={36: ["wrapped", "set pit 130"]})
    block = _endphase(hot.status[_i("10:25")])
    assert "Pit 130 C" in block[0]
    assert any("A=Pit 130 C" in l for l in block)
    assert block != _endphase(base.status[_i("10:25")])


def test_warmup_overshoot_does_not_count_for_tempdown() -> None:
    # The real series starts at core 14.3 C: max ambient is only tracked from
    # core 40 C on (idx 6, ambient 90.8 C), not from the 82 C warm-up start.
    r = Replay()
    r.run(7)
    assert "Max Ambient: Anheizphase" in r.status[5]
    assert r.state.max_ambient_temp == 90.8


# ---------------------------------------------------------------------------
# The whole run as it happened (``REAL_COMMANDS``) and the forecast back-test
# ---------------------------------------------------------------------------


def _real_run() -> Replay:
    """Replay the whole cook with the user's real commands (no wrap reminder, as live)."""
    r = Replay()
    r.state.tempalert_wrap_enabled = False
    r.state.target_override = None
    r.run(len(CORE), timed=REAL_COMMANDS)
    return r


def test_real_run_alerts_and_calls() -> None:
    r = _real_run()
    stall = r.alerts("STALL ERKANNT")
    assert stall == [_i("03:24")]  # the real stall, not the wrap disturbance at 05:48
    assert r.alerts("MEATER Station offline") == OFFLINE_IDXS
    assert r.alerts("wieder online") == [i + 1 for i in OFFLINE_IDXS]
    assert r.alerts("Rausnehmen!") == [PULL_IDX]
    assert "96.5 C ist erreicht (aktuell 96.8 C)" in next(m for _, m in r.messages if "Rausnehmen!" in m)
    # TempDown was off during the wrap and is coupled off by ``enable ruhephase``
    # at 14:40 — so the pit falling to 97 C after the pull (14:45) stays quiet.
    assert r.alerts("ACHTUNG") == []
    assert [i for i, _ in r.calls] == [stall[0], PULL_IDX, FINISHED_IDX]


def test_real_run_ends_on_cloud_finished() -> None:
    r = _real_run()
    assert r.status[FINISHED_IDX] == "__cook_ended__"
    assert r.alerts("Cook-Ende erkannt: MEATER Cloud zeigt Cook als beendet an.") == [FINISHED_IDX]
    assert r.state.cook_ended is True


def test_real_run_backdated_wrap_starts_endphase_forecast() -> None:
    r = _real_run()
    # The plain ``wrapped`` at 12:21 anchored the wrap there ...
    assert _endphase(r.status[_i("12:38")])[0].startswith("Endphasen-Prognose: sammelt Daten (Fit ab 13:21")
    # ... ``wrapped at 05:30`` (12:44) moved it back: the next check forecasts.
    first = _endphase(r.status[_i("12:48")])
    assert first[0].startswith("Endphasen-Prognose bis 96.5 C (A=Pit 110 C, Fit 06:39-12:48, 37 Punkte")
    assert first[0].endswith("~14:23")
    last = _endphase(r.status[_i("14:25")])
    assert last[0].endswith("~14:29")  # 96.5 C was reached ~14:28


def test_real_run_restphase_after_pull() -> None:
    r = _real_run()
    rest = r.status[_i("14:45")]
    assert "Ruhephasen-Prognose bis 65.0 C: sammelt Daten (Fit ab Kern-Maximum 96.4 C um 14:45" in rest
    assert "Endphasen-Prognose" not in rest and "Watcher-Prognose" not in rest


def test_heat_spike_at_1158_restarts_the_trend() -> None:
    # Ambient 113.9 C at 11:58 is >5 C above the last hour's maximum (108.2 C).
    r = Replay()
    r.run(_i("13:05") + 1)
    assert r.state.trend_ignore_before == _t(_i("11:58"))
    assert "Watcher-Prognose" not in r.status[_i("11:58")]
    assert "Watcher-Prognose" in r.status[_i("13:05")]


def _eta(line: str) -> float:
    """Epoch of the ``~HH:MM`` forecast at the end of a status line (11.10.)."""
    return _at(line.rsplit("~", 1)[1])


def test_endphase_backtest_wrapped_right_after_the_wrap() -> None:
    # Ideal use: ``wrapped at 05:30`` sent at 05:45, pull at 96.5 C.  From
    # 10:00 on the forecast (A = Pit 110 C) stays within ~30 min of the real
    # arrival (~14:28) — through the heat spike at 11:58 and the plateau at
    # ~94.5 C — while MEATER's estimate is off by up to ~2 h.  Before 10:00
    # it is too early (post-wrap catch-up in the fit), which is not pinned.
    r = Replay()
    r.state.tempalert_wrap_enabled = False
    r.state.target_override = 96.5
    r.run(PULL_IDX, timed=[("05:45:00", "wrapped at 05:30")])
    assert _endphase(r.status[_i("07:31")])[0].startswith("Endphasen-Prognose: sammelt Daten")
    assert _endphase(r.status[_i("07:41")])[0].startswith("Endphasen-Prognose bis 96.5 C")
    errors: dict[str, float] = {}
    for i in range(_i("10:04"), PULL_IDX):
        if CORE[i] is None:
            continue
        block = _endphase(r.status[i])
        errors[READINGS[i][0][:5]] = (_eta(block[0]) - ARRIVAL_965) / 60
    assert len(errors) == 27  # 10:04-14:25 without the offline cycle
    assert max(abs(e) for e in errors.values()) <= 35, errors
    late_morning = [e for hm, e in errors.items() if hm < "12:00"]
    assert sum(abs(e) for e in late_morning) / len(late_morning) <= 15, errors
    assert abs(errors["14:25"]) <= 3

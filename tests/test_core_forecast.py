"""Tests for wachtmeater/core_forecast.py.

Reference data: end phase of brisket cook 15f2e6f5 (11.10.2026, 08:01–10:25,
exact timestamps from the Element export).  Expected values are the scipy
``curve_fit`` results from the Obsidian note "Kerntemperatur-Prognose
(Endphase)".
"""

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from wachtmeater import core_forecast as cf
from wachtmeater.cli import build_parser
from wachtmeater.element_export import readings_from_element_export

# (origin_server_ts in ms, core C, MEATER ambient C)
ENDPHASE: list[tuple[int, float, float]] = [
    (1791698508671, 84.9, 103.9),
    (1791699124972, 85.4, 103.2),
    (1791699741578, 85.9, 103.7),
    (1791700357790, 86.2, 101.2),
    (1791700973004, 86.5, 104.7),
    (1791701590896, 86.8, 104.2),
    (1791702205187, 87.2, 103.1),
    (1791702819888, 87.6, 105.4),
    (1791703433591, 87.9, 103.8),
    (1791704048391, 88.2, 103.2),
    (1791704663192, 88.6, 103.2),
    (1791705279702, 88.8, 105.8),
    (1791705899589, 89.1, 103.4),
    (1791706519368, 89.5, 103.8),
    (1791707136485, 89.8, 104.8),
]
READINGS: list[cf.Reading] = [(ts / 1000, k, a) for ts, k, a in ENDPHASE]


@pytest.fixture(scope="module")
def fc() -> cf.Forecast:
    return cf.forecast(READINGS, target=96.5, pit=110.0)


def _hm(fc: cf.Forecast, hours: float | None) -> str:
    eta = fc.eta(hours)
    assert eta is not None
    return eta.strftime("%H:%M")


def test_linear_is_earliest_arrival(fc: cf.Forecast) -> None:
    assert fc.linear.rate == pytest.approx(1.99, abs=0.005)
    assert _hm(fc, fc.linear.eta_hours) == "13:45"


@pytest.mark.parametrize(
    ("ambient", "c", "rms", "eta"),
    [
        (110.0, 0.092, 0.091, "14:49"),
        (104.0, 0.124, 0.078, "15:34"),
        (102.0, 0.141, 0.073, "16:05"),
        (100.0, 0.162, 0.069, "17:01"),
    ],
)
def test_fixed_ambient_matches_note(fc: cf.Forecast, ambient: float, c: float, rms: float, eta: str) -> None:
    fit = next(f for f in fc.fixed if f.ambient == ambient)
    assert fit.c == pytest.approx(c, abs=0.0005)
    assert fit.rms == pytest.approx(rms, abs=0.0005)
    assert _hm(fc, fit.eta_hours) == eta


def test_pit_variant_first_and_fastest(fc: cf.Forecast) -> None:
    assert fc.fixed[0].ambient == 110.0
    etas = [f.eta_hours for f in fc.fixed]
    assert all(e is not None for e in etas)
    assert fc.fixed[0].eta_hours == min(e for e in etas if e is not None)


def test_free_fit_matches_note(fc: cf.Forecast) -> None:
    assert fc.free is not None
    assert fc.free.fixed is False
    assert fc.free.ambient == pytest.approx(98.5, abs=0.05)
    assert fc.free.ambient_stderr == pytest.approx(1.8, abs=0.05)
    assert fc.free.c == pytest.approx(0.183, abs=0.0005)
    assert _hm(fc, fc.free.eta_hours) == "18:26"


def test_report_format(fc: cf.Forecast) -> None:
    report = cf.format_report(fc)
    assert report.splitlines()[0] == (
        "Fit 08:01–10:25 (15 Punkte): Kern 84.9→89.8 °C, MEATER-Ambient Ø 103.8 °C, Pit 110 °C, Ziel 96.5 °C"
    )
    assert "A=110.0 °C : c=0.092/h rms=0.091 -> 14:49  <- A = Pit (schnellste Exp.-Variante)" in report
    assert "A frei (≤Pit): A=98.5±1.8 °C c=0.183/h -> 18:26" in report


def test_fixed_ambients_not_above_target_are_skipped() -> None:
    fc = cf.forecast(READINGS, target=101.0, pit=110.0)
    assert [f.ambient for f in fc.fixed] == [110.0, 104.0, 102.0]


def test_eta_hours_edge_cases() -> None:
    assert cf.eta_hours(100.0, 0.1, 97.0, 96.5) == 0.0  # already there
    assert cf.eta_hours(96.0, 0.1, 90.0, 96.5) is None  # A below target: never
    assert cf.eta_hours(100.0, 0.162, 89.8, 96.5) == pytest.approx(6.6, abs=0.05)


def test_free_fit_undetermined_with_pit_not_above_target() -> None:
    t_h = [(r[0] - READINGS[0][0]) / 3600 for r in READINGS]
    core = [r[1] for r in READINGS]
    assert cf.fit_free_ambient(t_h, core, 96.5, 96.5) is None


def test_needs_two_readings() -> None:
    with pytest.raises(ValueError):
        cf.forecast(READINGS[:1])


def test_select_since_uses_day_of_last_reading() -> None:
    assert cf.select_since(READINGS, "09:00")[0][1] == 87.2  # 09:03
    # 11:00 lies after the last reading (10:25) -> previous day 11:00 -> keeps all
    assert len(cf.select_since(READINGS, "11:00")) == len(READINGS)
    assert len(cf.select_since(READINGS, "07:00")) == len(READINGS)


def _write_export(tmp_path: Path) -> Path:
    messages = [
        {"origin_server_ts": 1791698000000, "content": {"body": "Hallo! MEATER Watcher ist gestartet."}},
        *(
            {
                "origin_server_ts": ts,
                "content": {
                    "body": f"MEATER Status: Brisket\nURL: x\nInternal (Fleisch): {k} C\nAmbient (Rauch): {a} C\n"
                },
            }
            for ts, k, a in reversed(ENDPHASE)
        ),
    ]
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Element - Wachtmeater - Chat Export/export.json", json.dumps({"messages": messages}))
    return path


def test_readings_from_element_export_zip(tmp_path: Path) -> None:
    readings = readings_from_element_export(_write_export(tmp_path))
    assert readings == READINGS  # sorted, status messages only


def test_readings_from_export_without_status_raises(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"messages": [{"origin_server_ts": 1, "content": {"body": "hi"}}]}))
    with pytest.raises(ValueError):
        readings_from_element_export(path)


def test_is_stable_on_note_window(fc: cf.Forecast) -> None:
    assert cf.is_stable(fc)  # 08:01-10:25: A=98.5±1.8, RMS 0.067


def _transient_lead() -> list[cf.Reading]:
    """One hour of post-wrap acceleration (~6 C/h) right before the end phase."""
    return [(READINGS[0][0] - (6 - i) * 600, 78.9 + i * 1.0, None) for i in range(6)]


def test_is_stable_rejects_pinned_or_undetermined_a() -> None:
    # Window including the post-wrap transient: the free fit is not stable
    with_transient = cf.forecast(_transient_lead() + READINGS, target=96.5, pit=110.0)
    assert not cf.is_stable(with_transient)
    # Only one hour of data: A is not determined (huge standard error)
    short = cf.forecast(READINGS[:7], target=96.5, pit=110.0)
    assert short.free is not None and short.free.ambient_stderr is not None
    assert short.free.ambient_stderr > cf.DEFAULT_STDERR_MAX
    assert not cf.is_stable(short)


def test_find_endphase_start_skips_transient() -> None:
    # Prepend a post-wrap acceleration; the search must skip it
    start = cf.find_endphase_start(_transient_lead() + READINGS, 96.5, 110.0)
    assert start == READINGS[0][0]


def test_find_endphase_start_none_with_too_little_data() -> None:
    assert cf.find_endphase_start(READINGS[:7], 96.5, 110.0) is None


def test_find_endphase_start_caps_candidates() -> None:
    calls = 0
    original = cf.forecast

    def _counting(*args: object, **kwargs: object) -> cf.Forecast:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    flat = [(i * 60.0, 50.0, None) for i in range(24 * 60)]  # 24 h, never stable
    from unittest.mock import patch

    with patch.object(cf, "forecast", side_effect=_counting):
        assert cf.find_endphase_start(flat, 96.5, 110.0, max_points=10) is None
    assert calls <= cf.DEFAULT_MAX_CANDIDATES + 1


def test_report_caps_far_etas() -> None:
    flat = [(i * 600.0, 93.0, None) for i in range(7)]
    report = cf.format_report(cf.forecast(flat, target=94.0, pit=110.0))
    assert "nicht absehbar" in report


def test_cli_eta_parser() -> None:
    args = build_parser().parse_args(["eta", "x.zip", "--pit", "120", "--since", "08:00"])
    assert (args.command, args.export, args.pit, args.target, args.since) == ("eta", "x.zip", 120.0, 96.5, "08:00")
    assert args.auto is False
    assert build_parser().parse_args(["eta", "x.zip", "--auto"]).pit is None  # -> config pit_temp


def test_report_time_is_local() -> None:
    assert datetime.fromtimestamp(READINGS[0][0]).strftime("%H:%M") == "08:01"

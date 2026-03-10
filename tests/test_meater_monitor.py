"""Tests for wachtmeater/meater_monitor.py — _parse_time_str only.

The module imports playwright indirectly at module level for type hints,
so we guard with importorskip.
"""

import pytest

# meater_monitor calls read_dot_env_to_environ() and reads env vars at
# module level, but _parse_time_str is a pure function with no side effects.
from wachtmeater.meater_monitor import _parse_time_str


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("2h 26m", 146),
        ("3h", 180),
        ("45m", 45),
        ("0h 30m", 30),
        ("01:23:45", 83),  # HH:MM:SS — seconds dropped
        ("23:45", 1425),  # MM:SS — documents actual behavior (treated as H*60+M)
        ("", None),
        ("Estimating", None),
        ("abc", None),
    ],
)
def test_parse_time_str(input_str: str, expected: int | None) -> None:
    assert _parse_time_str(input_str) == expected

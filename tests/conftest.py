"""Shared fixtures for the wachtmeater test suite."""

import os
import time

import pytest

from wachtmeater.watcher_state import WatcherState

# The reference cooks (Element exports, replays) were recorded in Berlin, and
# the expected wall-clock strings ("08:01", "~14:49") are Berlin local time.
# Pin the zone for the whole run so results do not depend on the machine —
# CI runners use UTC.  Set at import time, not in a fixture: test modules
# compute timestamps from local datetimes at module level, before any
# fixture runs.
os.environ["TZ"] = "Europe/Berlin"
time.tzset()


@pytest.fixture()
def base_state() -> WatcherState:
    """Return a fresh ``WatcherState`` with defaults."""
    return WatcherState()


@pytest.fixture()
def cooking_state(base_state: WatcherState) -> WatcherState:
    """Return a state with realistic cooking temps populated."""
    base_state.last_internal_temp = 72.0
    base_state.last_ambient_temp = 115.0
    base_state.max_ambient_temp = 120.0
    base_state.max_internal_temp = 72.0
    return base_state

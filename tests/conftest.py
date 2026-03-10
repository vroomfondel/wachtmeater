"""Shared fixtures for the wachtmeater test suite."""

import pytest

from wachtmeater.config import WatcherState


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
    base_state.internal_temp_history = [68.0, 69.0, 70.0, 71.0, 72.0]
    return base_state

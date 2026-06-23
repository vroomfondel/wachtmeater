"""Tests for wachtmeater/matrix_adapter.py.

Requires minimatrix/nio (imported at module level by matrix_adapter).
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nio.exceptions import LocalProtocolError

ma = pytest.importorskip("wachtmeater.matrix_adapter")


def _make_adapter() -> Any:
    """Build an adapter with a mock handler, bypassing the real __init__.

    ``__init__`` constructs a live ``MatrixClientHandler`` (needs a homeserver
    config); for sync-loop behaviour we only need a stub handler.
    """
    adapter = ma.MatrixMessagingAdapter.__new__(ma.MatrixMessagingAdapter)
    adapter._handler = MagicMock()
    adapter._handler.sync_forever = AsyncMock()
    adapter._handler.stop_sync = MagicMock()
    adapter._stop_requested = False
    return adapter


class TestStartSyncResilience:
    """start_sync should survive nio's benign key-query race."""

    def test_resumes_after_no_key_query_error(self) -> None:
        """A ``LocalProtocolError("No key query required.")`` is swallowed and
        the sync loop resumes, then returns cleanly on the next pass."""
        adapter = _make_adapter()
        calls = {"n": 0}

        async def _sync_forever(timeout: int = 0) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise LocalProtocolError("No key query required.")
            # Second pass: clean return (simulates a stop / normal exit).

        adapter._handler.sync_forever = AsyncMock(side_effect=_sync_forever)

        asyncio.run(adapter.start_sync())

        assert calls["n"] == 2  # resumed once after the benign error

    def test_other_local_protocol_error_propagates(self) -> None:
        """An unrelated LocalProtocolError must still be fatal."""
        adapter = _make_adapter()
        adapter._handler.sync_forever = AsyncMock(side_effect=LocalProtocolError("Not logged in."))

        with pytest.raises(LocalProtocolError, match="Not logged in"):
            asyncio.run(adapter.start_sync())

    def test_no_resume_after_stop_requested(self) -> None:
        """If stop was requested, the benign error must not loop forever."""
        adapter = _make_adapter()

        async def _sync_forever(timeout: int = 0) -> None:
            adapter._stop_requested = True
            raise LocalProtocolError("No key query required.")

        adapter._handler.sync_forever = AsyncMock(side_effect=_sync_forever)

        with pytest.raises(LocalProtocolError, match="No key query required"):
            asyncio.run(adapter.start_sync())

    def test_stop_sync_sets_flag_and_delegates(self) -> None:
        """stop_sync sets the adapter flag and calls the handler."""
        adapter = _make_adapter()
        adapter.stop_sync()

        assert adapter._stop_requested is True
        adapter._handler.stop_sync.assert_called_once()

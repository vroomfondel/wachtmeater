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


BOT = "@meater-watcher:matrix.example.com"
PITMASTER = "@pit-claas-master:matrix.example.com"


def _make_room_adapter() -> Any:
    """Adapter stub whose bot MXID is fixed, for room-creation tests."""
    adapter = _make_adapter()
    adapter.get_bot_user_id = MagicMock(return_value=BOT)
    return adapter


class TestCookRoomPowerLevels:
    """Tests for _cook_room_power_levels."""

    def test_promotes_pitmaster_to_admin(self) -> None:
        levels = _make_room_adapter()._cook_room_power_levels(PITMASTER, True)
        assert levels == {"users": {BOT: 100, PITMASTER: 100}}

    def test_bot_keeps_admin(self) -> None:
        """The override replaces the default users map wholesale.

        Omitting the bot would leave it at power level 0 in the room it just
        created — unable to invite, set the topic, or repair the levels.
        """
        levels = _make_room_adapter()._cook_room_power_levels(PITMASTER, True)
        assert levels is not None
        assert levels["users"][BOT] == 100

    def test_disabled_returns_none(self) -> None:
        # None => homeserver applies its defaults (creator 100, others 0).
        assert _make_room_adapter()._cook_room_power_levels(PITMASTER, False) is None

    def test_no_pitmaster_returns_none(self) -> None:
        assert _make_room_adapter()._cook_room_power_levels("", True) is None


class TestGetOrCreateRoomPowerLevels:
    """The power-level override must reach room_create — and only there."""

    def _adapter_with_create(self) -> Any:
        from nio import RoomCreateResponse

        adapter = _make_room_adapter()
        resp = MagicMock(spec=RoomCreateResponse)
        resp.room_id = "!cook:test"
        adapter._handler.client.room_create = AsyncMock(return_value=resp)
        adapter._handler.client.join = AsyncMock()
        return adapter

    def test_admin_override_passed_to_room_create(self) -> None:
        adapter = self._adapter_with_create()
        asyncio.run(
            adapter.get_or_create_room(
                configured_room="",
                auto_create=True,
                meater_uuid="8d401460-fab1-478f-b95e-2f56fabe43e2",
                pitmaster_mxid=PITMASTER,
                persisted_room_id=None,
                promote_pitmaster_to_admin=True,
            )
        )
        kwargs = adapter._handler.client.room_create.call_args.kwargs
        assert kwargs["power_level_override"] == {"users": {BOT: 100, PITMASTER: 100}}

    def test_no_override_when_disabled(self) -> None:
        adapter = self._adapter_with_create()
        asyncio.run(
            adapter.get_or_create_room(
                configured_room="",
                auto_create=True,
                meater_uuid="8d401460-fab1-478f-b95e-2f56fabe43e2",
                pitmaster_mxid=PITMASTER,
                persisted_room_id=None,
                promote_pitmaster_to_admin=False,
            )
        )
        kwargs = adapter._handler.client.room_create.call_args.kwargs
        assert kwargs["power_level_override"] is None

    def test_rejoined_room_is_not_recreated(self) -> None:
        """A persisted room is rejoined; its power levels stay untouched."""
        from nio import JoinResponse

        adapter = self._adapter_with_create()
        join_resp = MagicMock(spec=JoinResponse)
        join_resp.room_id = "!existing:test"
        adapter._handler.client.join = AsyncMock(return_value=join_resp)

        selection = asyncio.run(
            adapter.get_or_create_room(
                configured_room="",
                auto_create=True,
                meater_uuid="8d401460-fab1-478f-b95e-2f56fabe43e2",
                pitmaster_mxid=PITMASTER,
                persisted_room_id="!existing:test",
                promote_pitmaster_to_admin=True,
            )
        )
        assert selection.cook == "!existing:test"
        adapter._handler.client.room_create.assert_not_called()

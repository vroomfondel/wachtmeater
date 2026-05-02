"""Messaging backend protocol for wachtmeater.

Defines a structural (Protocol-based) interface so that meater_watcher.py
can communicate through any messaging backend without importing
matrix-specific modules.
"""

from collections.abc import Callable, Coroutine
from typing import NamedTuple, Protocol

MessageCallback = Callable[[str, str, str, str], Coroutine[None, None, None]]
"""Signature: (room_id, sender, body, room_display_name) -> async None"""


class RoomSelection(NamedTuple):
    """Result of room selection / auto-creation.

    Attributes:
        broadcast: Resolved Matrix room ID of the configured operator
            room (always an ``!id:srv`` form, even when the user supplied
            a ``#alias:srv``).  ``None`` when no broadcast room is
            configured or the join failed.
        cook: Resolved Matrix room ID of the per-cook room (rejoined or
            freshly created).  ``None`` when ``auto_create`` is disabled
            or the create failed.
    """

    broadcast: str | None
    cook: str | None

    @property
    def all_rooms(self) -> list[str]:
        """Return both rooms as a flat list, skipping ``None`` entries."""
        return [r for r in (self.broadcast, self.cook) if r]


class MessagingBackend(Protocol):
    """Structural interface for a messaging backend.

    Any class implementing these methods is a valid backend for
    ``meater_watcher.event_loop`` — no inheritance required.
    """

    async def connect(self) -> None:
        """Authenticate and establish a session with the messaging service."""
        ...

    async def get_or_create_room(
        self,
        *,
        configured_room: str,
        auto_create: bool,
        meater_uuid: str,
        pitmaster_mxid: str,
        persisted_room_id: str | None,
    ) -> RoomSelection:
        """Select, join, and/or create the broadcast and cook rooms.

        *configured_room* may be either a room ID (``!id:srv``) or a room
        alias (``#alias:srv``); aliases are resolved to IDs via the join
        response.  *auto_create* controls whether a per-cook room is
        rejoined or freshly created in addition.
        """
        ...

    def get_rooms(self) -> list[str]:
        """Return a list of joined room IDs."""
        ...

    def get_bot_user_id(self) -> str:
        """Return the bot's own user identifier."""
        ...

    async def send_message(self, room_id: str, text: str) -> None:
        """Send a text message to the given room."""
        ...

    async def send_image(self, room_id: str, image_path: str) -> None:
        """Upload and send an image to the given room."""
        ...

    def register_message_callback(self, callback: MessageCallback) -> None:
        """Register a coroutine to be called on each incoming message."""
        ...

    async def start_sync(self) -> None:
        """Begin the long-polling sync loop (blocks until stopped)."""
        ...

    def stop_sync(self) -> None:
        """Signal the sync loop to stop."""
        ...

    async def close(self) -> None:
        """Close the session and release resources."""
        ...

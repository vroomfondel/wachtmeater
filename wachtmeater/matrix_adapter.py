#!/usr/bin/env python3
"""Send status text and optional screenshot to all joined Matrix rooms.

Imported by meater_watcher; also usable via the unified CLI.

Usage:
    wachtmeater send-matrix "status text" [--image /path/to/screenshot.png]

Env vars (from .env):
    MATRIX_HOMESERVER   — Synapse URL (default: http://synapse.matrix.svc.cluster.local:8008)
    MATRIX_USER         — Matrix user localpart or full MXID
    MATRIX_PASSWORD     — password
    CRYPTO_STORE_PATH   — persistent dir for nio SQLite stores (default: /data/crypto_store)
    AUTH_METHOD         — password or jwt (default: password)
"""

from pathlib import Path
from typing import ClassVar

from nio import UploadResponse

from wachtmeater.messaging import MessageCallback, RoomSelection

from wachtmeater import cfg, read_dot_env_to_environ

read_dot_env_to_environ()


from loguru import logger

from minimatrix.matrix_client import MatrixClientHandler


class MatrixMessagingAdapter:
    """Matrix messaging backend using minimatrix/nio.

    Satisfies the ``MessagingBackend`` protocol defined in
    ``wachtmeater.messaging`` via structural typing (no inheritance needed).
    """

    logger: ClassVar = logger.bind(classname="MatrixMessagingAdapter")

    def __init__(self, crypto_store_path: str | None = None) -> None:
        """Initialise the adapter with a ``MatrixClientHandler``.

        Args:
            crypto_store_path: Override for the nio E2EE store directory.
                Defaults to ``cfg.matrix.crypto_store_path``.  The
                operator passes ``cfg.matrix.operator_crypto_store_path``
                here to avoid SQLite races with watcher pods that share
                the same Matrix user.
        """
        self._handler = MatrixClientHandler(
            homeserver=cfg.matrix.homeserver,
            user=cfg.matrix.user,
            crypto_store_path=crypto_store_path or cfg.matrix.crypto_store_path,
        )

    async def connect(self) -> None:
        """Authenticate, import old E2EE keys, and perform an initial sync."""
        login_kwargs: dict[str, str | None] = {
            "auth_method": cfg.auth.method,
            "password": cfg.matrix.password,
        }
        if cfg.auth.method == "jwt":
            login_kwargs.update(
                keycloak_url=cfg.auth.keycloak_url,
                keycloak_realm=cfg.auth.keycloak_realm,
                keycloak_client_id=cfg.auth.keycloak_client_id,
                keycloak_client_secret=cfg.auth.keycloak_client_secret,
                jwt_login_type=cfg.auth.jwt_login_type,
            )
        await self._handler.login(**login_kwargs)  # type: ignore[arg-type]
        await self._handler.import_keys_from_old_stores(delete_old=True)
        await self._handler.initial_sync(auto_join=True)

    async def get_or_create_room(
        self,
        *,
        configured_room: str,
        auto_create: bool,
        meater_uuid: str,
        pitmaster_mxid: str,
        persisted_room_id: str | None,
    ) -> RoomSelection:
        """Select, join, and/or create rooms.

        Returns a :class:`RoomSelection` with the (resolved) broadcast and
        cook room IDs.  The two sources combine independently:

        * If *configured_room* is non-empty it is joined and resolved to a
          room ID.  Both ``!id:srv`` and ``#alias:srv`` forms are
          accepted; aliases are resolved via the server's join response.
        * If *auto_create* is ``True`` a per-cook room is added — either
          by rejoining *persisted_room_id* or by creating a fresh
          encrypted room and inviting the pitmaster.

        Args:
            configured_room: Explicit room ID or alias for the broadcast room.
            auto_create: Whether to create/rejoin a per-cook room.
            meater_uuid: Cook UUID used in the room name and topic.
            pitmaster_mxid: Matrix user ID to invite into newly created rooms.
            persisted_room_id: Previously saved cook-room ID to rejoin.

        Returns:
            A ``RoomSelection`` whose fields are the resolved room IDs
            (or ``None`` when the corresponding source did not yield a
            usable room).
        """
        from nio import JoinResponse, RoomCreateResponse

        broadcast_id: str | None = None
        if configured_room:
            broadcast_id = await self._resolve_configured_room(configured_room)

        cook_id: str | None = None
        if auto_create:
            if persisted_room_id:
                join_resp = await self._handler.client.join(persisted_room_id)
                if isinstance(join_resp, JoinResponse):
                    cook_id = join_resp.room_id

            if cook_id is None:
                short_uuid = meater_uuid.replace("-", "")[:8].lower()
                resp = await self._handler.client.room_create(
                    name=f"Wachtmeater: {short_uuid}",
                    topic=f"MEATER Cook {meater_uuid}",
                    invite=[pitmaster_mxid] if pitmaster_mxid else [],
                    initial_state=[
                        {
                            "type": "m.room.encryption",
                            "state_key": "",
                            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                        }
                    ],
                )
                if isinstance(resp, RoomCreateResponse):
                    cook_id = resp.room_id

        # Avoid duplicate when a user accidentally configures the same room
        # as both broadcast and persisted cook room.
        if cook_id is not None and cook_id == broadcast_id:
            cook_id = None

        return RoomSelection(broadcast=broadcast_id, cook=cook_id)

    async def _resolve_configured_room(self, spec: str) -> str | None:
        """Resolve a configured room spec (ID or alias) to a room ID.

        Args:
            spec: ``!id:srv`` (room ID) or ``#alias:srv`` (room alias).
                Other shapes are logged and joined as-is — likely failing.

        Returns:
            The resolved room ID, or ``None`` if the join failed.
        """
        from nio import JoinResponse

        if spec.startswith("!"):
            # Already a room ID — only join if not yet a member.
            if spec in self._handler.client.rooms:
                return spec
            join_resp = await self._handler.client.join(spec)
            if isinstance(join_resp, JoinResponse):
                return str(join_resp.room_id)
            MatrixMessagingAdapter.logger.warning(f"Failed to join configured room {spec}: {join_resp}")
            return None

        if spec.startswith("#"):
            # Alias — always resolve via join response (idempotent if already a member).
            join_resp = await self._handler.client.join(spec)
            if isinstance(join_resp, JoinResponse):
                return str(join_resp.room_id)
            MatrixMessagingAdapter.logger.warning(f"Failed to join configured room alias {spec}: {join_resp}")
            return None

        MatrixMessagingAdapter.logger.warning(
            f"Configured room {spec!r} is neither a room ID (!id:srv) nor an alias (#alias:srv); "
            f"attempting join as-is."
        )
        join_resp = await self._handler.client.join(spec)
        if isinstance(join_resp, JoinResponse):
            return str(join_resp.room_id)
        return None

    def get_rooms(self) -> list[str]:
        """Return a list of joined room IDs."""
        return list(self._handler.client.rooms)

    def get_bot_user_id(self) -> str:
        """Return the bot's own Matrix user ID."""
        user_id: str = getattr(self._handler.client, "user_id", cfg.matrix.user)
        return user_id

    async def send_message(self, room_id: str, text: str) -> None:
        """Trust devices in *room_id* and send a text message."""
        await self._handler.trust_devices_in_room(room_id)
        await self._handler.send_message(room_id, text)

    async def send_image(self, room_id: str, image_path: str, filename: str | None = None) -> None:
        """Upload and send an image to a Matrix room.

        The image is uploaded encrypted if the target room has E2EE
        enabled.  A warning is logged and the call returns early when
        *image_path* does not exist on disk.

        Args:
            room_id: Target Matrix room ID.
            image_path: Filesystem path to the PNG image to send.
            filename: Optional override for the filename sent to the room
                (defaults to ``"meater-screenshot.png"``).
        """
        if not Path(image_path).exists():
            MatrixMessagingAdapter.logger.warning(f"Image not found: {image_path}")
            return

        await self._handler.trust_devices_in_room(room_id)

        room = self._handler.client.rooms.get(room_id)
        encrypted = room.encrypted if room else False

        filesize = Path(image_path).stat().st_size
        MatrixMessagingAdapter.logger.debug(f"Uploading screenshot ({filesize} bytes) ...")
        with open(image_path, "rb") as f:
            resp, maybe_keys = await self._handler.client.upload(
                f,
                content_type="image/png",
                filename=filename if filename else "meater-screenshot.png",
                encrypt=encrypted,
                filesize=filesize,
            )

        if isinstance(resp, UploadResponse):
            MatrixMessagingAdapter.logger.debug(f"Screenshot uploaded: {resp.content_uri}")
            if encrypted and maybe_keys:
                content = {
                    "msgtype": "m.image",
                    "body": "meater-screenshot.png",
                    "file": {
                        "url": resp.content_uri,
                        **maybe_keys,
                    },
                    "info": {
                        "mimetype": "image/png",
                        "size": filesize,
                    },
                }
            else:
                content = {
                    "msgtype": "m.image",
                    "body": "meater-screenshot.png",
                    "url": resp.content_uri,
                    "info": {
                        "mimetype": "image/png",
                        "size": filesize,
                    },
                }
            await self._handler.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )
        else:
            MatrixMessagingAdapter.logger.warning(f"Upload failed for {room_id}: {resp}")

    def register_message_callback(self, callback: MessageCallback) -> None:
        """Register a coroutine to be called on each incoming room message."""
        from nio.events.room_events import RoomMessageText
        from nio.rooms import MatrixRoom as _MatrixRoom

        async def _nio_adapter(room: "_MatrixRoom", event: RoomMessageText) -> None:
            await callback(
                room.room_id,
                event.sender,
                event.body,
                room.display_name or room.room_id,
            )

        self._handler.add_event_callback(_nio_adapter, RoomMessageText)

    async def start_sync(self) -> None:
        """Begin the long-polling sync loop (blocks until stopped)."""
        await self._handler.sync_forever(timeout=30000)

    def stop_sync(self) -> None:
        """Signal the sync loop to stop."""
        self._handler.stop_sync()

    async def send_one(self, status_text: str, image_path: str | None, room_id_arg: str | None = None) -> None:
        """Fire-and-forget: connect, send text+image to room(s), close.

        Convenience method for one-shot message delivery.  Connects to
        the homeserver, sends the status text (and optional image) to the
        specified room or all joined rooms, then disconnects.

        Args:
            status_text: Message body to send.
            image_path: Optional filesystem path to a screenshot to attach.
            room_id_arg: If given, send only to this room; otherwise send
                to every joined room.
        """
        await self.connect()
        MatrixMessagingAdapter.logger.info(f"Logged in on {cfg.matrix.homeserver}")
        MatrixMessagingAdapter.logger.info(f"Synced, {len(self.get_rooms())} rooms")

        if room_id_arg:
            if room_id_arg not in self._handler.client.rooms:
                await self._handler.client.join(room_id_arg)
            target_rooms = [room_id_arg]
        else:
            target_rooms = self.get_rooms()

        if not target_rooms:
            MatrixMessagingAdapter.logger.warning("No joined rooms, nothing to do")
            await self.close()
            return

        for rid in target_rooms:
            try:
                if image_path:
                    await self.send_image(rid, image_path)
                await self.send_message(rid, status_text)
                MatrixMessagingAdapter.logger.info(f"Sent to {rid}")
            except Exception as e:
                MatrixMessagingAdapter.logger.error(f"Failed for {rid}: {e}")

        await self.close()

    async def close(self) -> None:
        """Close the nio client session and release resources."""
        await self._handler.close()

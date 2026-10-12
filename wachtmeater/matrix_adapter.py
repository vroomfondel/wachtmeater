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
    MATRIX_CROSS_SIGNING_SECRET — derives the cross-signing keys; empty = no self-signing
"""

from pathlib import Path
from typing import ClassVar

from nio import RoomSendError, UploadResponse
from nio.exceptions import LocalProtocolError

from wachtmeater.messaging import MessageCallback, MessageSendError, RoomSelection

from wachtmeater import cfg, read_dot_env_to_environ

read_dot_env_to_environ()


from loguru import logger

from minimatrix.cross_signing import CrossSigningKeys, ensure_device_cross_signed
from minimatrix.matrix_client import MatrixClientHandler

CROSS_SIGNING_SEED_INFO: str = "wachtmeater cross-signing {usage}"
"""HMAC message per key usage for the cross-signing keys — changing it changes the bot's identity."""


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
        # Set by ``stop_sync`` so the resilient ``start_sync`` loop knows a
        # clean shutdown was requested (vs. a recoverable sync error).
        self._stop_requested = False

    async def connect(self) -> None:
        """Authenticate, import old E2EE keys, and perform an initial sync."""
        jwt = cfg.auth.method == "jwt"
        await self._handler.login(
            cfg.auth.method,
            cfg.matrix.password,
            keycloak_url=cfg.auth.keycloak_url if jwt else None,
            keycloak_realm=cfg.auth.keycloak_realm if jwt else None,
            keycloak_client_id=cfg.auth.keycloak_client_id if jwt else None,
            keycloak_client_secret=cfg.auth.keycloak_client_secret if jwt else None,
            jwt_login_type=cfg.auth.jwt_login_type if jwt else None,
            # Cross-signing runs in _cross_sign_device with the bot's own
            # seed_info; minimatrix's default would give it a new identity.
            self_sign_keys=False,
        )
        await self._handler.import_keys_from_old_stores(delete_old=True)
        await self._handler.initial_sync(auto_join=True)
        await self._cross_sign_device()

    async def _cross_sign_device(self) -> None:
        """Sign this device with the account's cross-signing keys (if a secret is configured).

        Failures are only logged: the bot works without it, its messages are
        just flagged as sent from an unverified device.
        """
        secret = cfg.matrix.cross_signing_secret
        client = self._handler.client
        if not secret:
            self.logger.warning(
                f"Cross-Signing AUS (matrix.cross_signing_secret nicht gesetzt): Geraet {client.device_id} "
                "bleibt unsigniert, Clients markieren Bot-Nachrichten als unverifiziert"
            )
            return
        master = CrossSigningKeys.from_secret(secret, CROSS_SIGNING_SEED_INFO).master.public_key
        self.logger.info(f"Cross-Signing AN: {client.user_id} Geraet {client.device_id}, Master-Key {master}")
        try:
            if client.should_upload_keys:
                self.logger.info("Cross-Signing: Geraeteschluessel werden zuerst hochgeladen")
                await client.keys_upload()
            summary = await ensure_device_cross_signed(client, secret, CROSS_SIGNING_SEED_INFO)
            self.logger.info(f"Cross-Signing OK: {summary}")
        except Exception as e:
            self.logger.error(f"Cross-Signing fehlgeschlagen: {e}")

    async def get_or_create_room(
        self,
        *,
        configured_room: str,
        auto_create: bool,
        meater_uuid: str,
        pitmaster_mxid: str,
        persisted_room_id: str | None,
        promote_pitmaster_to_admin: bool = True,
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
            promote_pitmaster_to_admin: Grant the pitmaster power level 100 in
                a freshly created cook room. Only affects rooms created here —
                a rejoined or configured room keeps whatever levels it has.

        Returns:
            A ``RoomSelection`` whose fields are the resolved room IDs
            (or ``None`` when the corresponding source did not yield a
            usable room).
        """
        from nio import JoinResponse, RoomCreateError, RoomCreateResponse

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
                create_kwargs = dict(
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
                power_levels = self._cook_room_power_levels(pitmaster_mxid, promote_pitmaster_to_admin)
                resp = await self._handler.client.room_create(**create_kwargs, power_level_override=power_levels)
                if self._is_creator_in_users_rejection(resp) and power_levels is not None:
                    # Room v12+ (MSC4289): creators hold implicit top power and must not be listed.
                    users = {u: lvl for u, lvl in power_levels["users"].items() if u != self.get_bot_user_id()}
                    resp = await self._handler.client.room_create(
                        **create_kwargs, power_level_override={**power_levels, "users": users}
                    )
                if isinstance(resp, RoomCreateError):
                    MatrixMessagingAdapter.logger.error(
                        f"Cook-Room konnte nicht erstellt werden: {resp.status_code} {resp.message}"
                    )
                if isinstance(resp, RoomCreateResponse):
                    cook_id = resp.room_id
                    if promote_pitmaster_to_admin and pitmaster_mxid:
                        MatrixMessagingAdapter.logger.info(
                            f"Cook-Room {cook_id} erstellt — Pitmaster {pitmaster_mxid} als Admin (PL 100)."
                        )

        # Avoid duplicate when a user accidentally configures the same room
        # as both broadcast and persisted cook room.
        if cook_id is not None and cook_id == broadcast_id:
            cook_id = None

        return RoomSelection(broadcast=broadcast_id, cook=cook_id)

    def _cook_room_power_levels(
        self, pitmaster_mxid: str, promote_pitmaster_to_admin: bool
    ) -> dict[str, dict[str, int]] | None:
        """Build the ``power_level_override`` for a freshly created cook room.

        Returns ``None`` when nothing should be overridden, so the homeserver
        applies its defaults (creator at 100, everyone else at 0).

        The override is merged into the default ``m.room.power_levels``
        content *per top-level key*, so supplying ``users`` REPLACES the
        server's default ``{creator: 100}`` map wholesale rather than adding
        to it.  The bot's own MXID must therefore be restated at 100 — omit it
        and the bot ends up at power level 0 in the room it just created,
        unable to invite, set topics, or ever fix the levels again.

        Args:
            pitmaster_mxid: Pitmaster user ID; empty means nobody to promote.
            promote_pitmaster_to_admin: Whether to grant the pitmaster admin rights.

        Returns:
            A ``{"users": {mxid: level}}`` override, or ``None`` to keep the
            server defaults.
        """
        if not (promote_pitmaster_to_admin and pitmaster_mxid):
            return None
        return {"users": {self.get_bot_user_id(): 100, pitmaster_mxid: 100}}

    @staticmethod
    def _is_creator_in_users_rejection(resp: object) -> bool:
        """Whether *resp* is the homeserver refusing the creator in ``power_levels.users``.

        Args:
            resp: Result of ``room_create``.

        Returns:
            ``True`` for the room-v12+ rejection, ``False`` otherwise.
        """
        from nio import RoomCreateError

        return isinstance(resp, RoomCreateError) and "must not appear in content.users" in (resp.message or "")

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

    async def send_message(self, room_id: str, text: str, *, strict: bool = False, html: str | None = None) -> None:
        """Trust devices in *room_id* and send a text message.

        Args:
            room_id: Target Matrix room ID.
            text: Message body (plain text; the fallback when *html* is set).
            strict: When ``True``, bypass minimatrix's error-swallowing
                ``send_message`` and raise ``MessageSendError`` if the
                homeserver rejects the event (``RoomSendError``) or the send
                raises.  Used for alerts, which must be retried on failure.
            html: Optional ``org.matrix.custom.html`` rendering of *text*,
                sent as ``formatted_body`` (minimatrix cannot send it, so
                this also bypasses its ``send_message``).  Without *strict*
                a failed delivery is only logged.

        Raises:
            MessageSendError: Only with *strict*, when delivery failed.
        """
        await self._handler.trust_devices_in_room(room_id)
        if not strict and html is None:
            await self._handler.send_message(room_id, text)
            return
        content = {"msgtype": "m.text", "body": text}
        if html is not None:
            content |= {"format": "org.matrix.custom.html", "formatted_body": html}
        try:
            resp = await self._handler.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )
        except Exception as e:
            if strict:
                raise MessageSendError(f"Senden an {room_id} fehlgeschlagen: {e}") from e
            logger.error(f"Senden an {room_id} fehlgeschlagen: {e}")
            return
        if isinstance(resp, RoomSendError):
            if strict:
                raise MessageSendError(f"Senden an {room_id} abgelehnt: {resp.message} ({resp.status_code})")
            logger.error(f"Senden an {room_id} abgelehnt: {resp.message} ({resp.status_code})")

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
        """Begin the long-polling sync loop (blocks until stopped).

        Resilient against nio's benign ``LocalProtocolError("No key query
        required.")``: nio's ``sync_forever`` schedules a ``keys_query`` when
        ``should_query_keys`` is set, but the query list can be empty by the
        time the task runs, raising this error and tearing down the whole sync
        loop.  Treating it as fatal previously crashed the watcher (exit 1)
        roughly every time a key query became due, triggering pod restarts.
        Here we swallow it and resume syncing unless a stop was requested.
        """
        self._stop_requested = False
        while not self._stop_requested:
            try:
                await self._handler.sync_forever(timeout=30000)
                return
            except LocalProtocolError as exc:
                if self._stop_requested or "No key query required" not in str(exc):
                    raise
                MatrixMessagingAdapter.logger.debug(f"Ignoring benign nio key-query race, resuming sync: {exc}")

    def stop_sync(self) -> None:
        """Signal the sync loop to stop."""
        self._stop_requested = True
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

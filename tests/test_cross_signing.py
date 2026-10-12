"""Tests for the bot's self cross-signing (``minimatrix.cross_signing`` via the adapter).

The key derivation, signing and upload flow are tested in minimatrix; here
only the wachtmeater side: the bot's key identity and the login hook.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from minimatrix.cross_signing import CrossSigningError, CrossSigningKeys

from wachtmeater import cfg
from wachtmeater import matrix_adapter as ma


def test_seed_info_keeps_the_bots_identity() -> None:
    # Public keys the former libolm-based wachtmeater.cross_signing derived for
    # "s3cret" — a different seed_info would give every bot a new identity.
    keys = CrossSigningKeys.from_secret("s3cret", ma.CROSS_SIGNING_SEED_INFO)
    assert keys.master.public_key == "41n9vvYZGe4CgTAVLPg6Y1QpLKd1LkdJfvMV1xbPMM4"
    assert keys.self_signing.public_key == "xW+rp3ULt4tLCLt73h26Y/aSNfxPEc5E4I8wpQonggY"


class TestAdapterHook:
    """``MatrixMessagingAdapter._cross_sign_device`` never breaks the login."""

    def _adapter(self) -> Any:
        adapter = ma.MatrixMessagingAdapter.__new__(ma.MatrixMessagingAdapter)
        adapter._handler = MagicMock()
        adapter._handler.client.should_upload_keys = False
        return adapter

    def test_without_secret_nothing_happens(self) -> None:
        adapter = self._adapter()
        with (
            patch.object(cfg.matrix, "cross_signing_secret", ""),
            patch.object(ma, "ensure_device_cross_signed", AsyncMock()) as ensure,
        ):
            asyncio.run(adapter._cross_sign_device())
        ensure.assert_not_called()

    def test_errors_are_only_logged(self) -> None:
        adapter = self._adapter()
        adapter._handler.client.should_upload_keys = True
        adapter._handler.client.keys_upload = AsyncMock()
        failing = AsyncMock(side_effect=CrossSigningError("nope"))
        with (
            patch.object(cfg.matrix, "cross_signing_secret", "s3cret"),
            patch.object(ma, "ensure_device_cross_signed", failing),
        ):
            asyncio.run(adapter._cross_sign_device())
        adapter._handler.client.keys_upload.assert_awaited_once()
        failing.assert_awaited_once_with(adapter._handler.client, "s3cret", ma.CROSS_SIGNING_SEED_INFO)

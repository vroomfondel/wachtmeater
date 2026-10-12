"""Tests for the bot's self cross-signing (``minimatrix.cross_signing`` via the adapter).

The key derivation, signing and upload flow are tested in minimatrix; here
only the wachtmeater side: the bot's key identity and the login hook.
"""

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from minimatrix.cross_signing import CrossSigningError, CrossSigningKeys

from wachtmeater import cfg
from wachtmeater import matrix_adapter as ma


def test_seed_info_keeps_the_bots_identity() -> None:
    # Public keys the former libolm-based wachtmeater.cross_signing derived for
    # "s3cret" — a different seed_info would give every bot a new identity.
    keys = CrossSigningKeys.from_secret("s3cret", ma.CROSS_SIGNING_SEED_INFO)
    assert keys.master.public_key == "41n9vvYZGe4CgTAVLPg6Y1QpLKd1LkdJfvMV1xbPMM4"
    assert keys.self_signing.public_key == "xW+rp3ULt4tLCLt73h26Y/aSNfxPEc5E4I8wpQonggY"


@pytest.fixture
def logs() -> Iterator[list[str]]:
    """Collect ``LEVEL message`` lines logged by wachtmeater during the test."""
    lines: list[str] = []
    logger.enable("wachtmeater")
    sink = logger.add(lambda m: lines.append(m.rstrip("\n")), format="{level} {message}", level="DEBUG")
    yield lines
    logger.remove(sink)
    logger.disable("wachtmeater")


class TestAdapterHook:
    """``MatrixMessagingAdapter._cross_sign_device`` never breaks the login."""

    def _adapter(self) -> Any:
        adapter = ma.MatrixMessagingAdapter.__new__(ma.MatrixMessagingAdapter)
        adapter._handler = MagicMock()
        adapter._handler.client.should_upload_keys = False
        return adapter

    def test_without_secret_nothing_happens(self, logs: list[str]) -> None:
        adapter = self._adapter()
        adapter._handler.client.device_id = "DEV1"
        with (
            patch.object(cfg.matrix, "cross_signing_secret", ""),
            patch.object(ma, "ensure_device_cross_signed", AsyncMock()) as ensure,
        ):
            asyncio.run(adapter._cross_sign_device())
        ensure.assert_not_called()
        assert any(l.startswith("WARNING Cross-Signing AUS") and "DEV1" in l for l in logs)

    def test_success_logs_identity_and_result(self, logs: list[str]) -> None:
        adapter = self._adapter()
        adapter._handler.client.device_id = "DEV1"
        ensure = AsyncMock(return_value="Geraet DEV1 bereits signiert")
        with (
            patch.object(cfg.matrix, "cross_signing_secret", "s3cret"),
            patch.object(ma, "ensure_device_cross_signed", ensure),
        ):
            asyncio.run(adapter._cross_sign_device())
        master = CrossSigningKeys.from_secret("s3cret", ma.CROSS_SIGNING_SEED_INFO).master.public_key
        assert any(l.startswith("INFO Cross-Signing AN") and "DEV1" in l and master in l for l in logs)
        assert "INFO Cross-Signing OK: Geraet DEV1 bereits signiert" in logs

    def test_errors_are_only_logged(self, logs: list[str]) -> None:
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
        assert "ERROR Cross-Signing fehlgeschlagen: nope" in logs

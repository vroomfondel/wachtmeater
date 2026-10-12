"""Tests for the bot's self cross-signing (``wachtmeater.cross_signing``)."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from olm.utility import ed25519_verify

from wachtmeater import cfg
from wachtmeater import cross_signing as cs
from wachtmeater import matrix_adapter as ma

USER = "@bot:example.org"
DEVICE = "DEVICEID"
KEYS = cs.CrossSigningKeys.from_secret("s3cret")


def _verify(obj: dict[str, Any], key: Any) -> None:
    """Raise unless *obj* carries a valid signature by *key* (canonical JSON, spec style)."""
    sig = obj["signatures"][USER][f"ed25519:{key.public_key}"]
    stripped = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    ed25519_verify(key.public_key, cs.canonical_json(stripped), sig)


def _device_keys() -> dict[str, Any]:
    return {
        "user_id": USER,
        "device_id": DEVICE,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {f"curve25519:{DEVICE}": "curve", f"ed25519:{DEVICE}": "ed"},
        "signatures": {USER: {f"ed25519:{DEVICE}": "selfsig"}},
        "unsigned": {"device_display_name": "bot"},
    }


class FakeClient:
    """Minimal nio client: records POSTs, answers from a path -> (status, json) table."""

    def __init__(self, answers: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.user_id = USER
        self.device_id = DEVICE
        self.access_token = "tok"
        self.answers = answers
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, path: str, data: str, headers: dict[str, str]) -> Any:
        assert method == "POST" and headers["Authorization"] == "Bearer tok"
        self.posts.append((path, json.loads(data)))
        status, body = self.answers.get(path.rsplit("/v3/", 1)[1], (200, {}))
        resp = MagicMock(status=status)
        resp.json = AsyncMock(return_value=body)
        return resp

    def paths(self) -> list[str]:
        return [p.rsplit("/v3/", 1)[1] for p, _ in self.posts]


def _query(master: str | None = None, device: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {"device_keys": {USER: {DEVICE: device or _device_keys()}}}
    if master:
        body["master_keys"] = {USER: {"user_id": USER, "usage": ["master"], "keys": {f"ed25519:{master}": master}}}
    return 200, body


def test_keys_are_derived_deterministically_and_distinct() -> None:
    again = cs.CrossSigningKeys.from_secret("s3cret")
    other = cs.CrossSigningKeys.from_secret("other")
    pubs = {KEYS.master.public_key, KEYS.self_signing.public_key, KEYS.user_signing.public_key}
    assert len(pubs) == 3
    assert again.master.public_key == KEYS.master.public_key
    assert other.master.public_key != KEYS.master.public_key


def test_canonical_json_is_sorted_and_compact() -> None:
    assert cs.canonical_json({"b": 1, "a": {"d": "ä", "c": [1, 2]}}) == '{"a":{"c":[1,2],"d":"ä"},"b":1}'


def test_sign_json_ignores_signatures_and_unsigned_and_keeps_existing() -> None:
    signed = cs.sign_json(_device_keys(), KEYS.self_signing, USER)
    _verify(signed, KEYS.self_signing)
    assert signed["signatures"][USER][f"ed25519:{DEVICE}"] == "selfsig"
    assert signed["unsigned"] == {"device_display_name": "bot"}


def test_upload_body_signs_sub_keys_with_master() -> None:
    body = cs.device_signing_upload_body(KEYS, USER)
    assert body["master_key"]["keys"] == {f"ed25519:{KEYS.master.public_key}": KEYS.master.public_key}
    assert body["self_signing_key"]["usage"] == ["self_signing"]
    _verify(body["self_signing_key"], KEYS.master)
    _verify(body["user_signing_key"], KEYS.master)


def test_first_run_uploads_keys_and_signs_device() -> None:
    client = FakeClient({"keys/query": _query()})
    summary = asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))
    assert client.paths() == ["keys/query", "keys/device_signing/upload", "keys/signatures/upload"]
    assert summary == f"Cross-Signing-Schluessel hochgeladen, Geraet {DEVICE} signiert"
    signed = client.posts[2][1][USER][DEVICE]
    _verify(signed, KEYS.self_signing)
    assert "unsigned" not in signed


def test_already_set_up_does_nothing() -> None:
    device = cs.sign_json(_device_keys(), KEYS.self_signing, USER)
    client = FakeClient({"keys/query": _query(KEYS.master.public_key, device)})
    summary = asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))
    assert client.paths() == ["keys/query"]
    assert summary == f"Geraet {DEVICE} bereits signiert"


def test_known_master_signs_only_the_new_device() -> None:
    client = FakeClient({"keys/query": _query(KEYS.master.public_key)})
    asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))
    assert client.paths() == ["keys/query", "keys/signatures/upload"]


def test_foreign_master_needs_admin_permission() -> None:
    client = FakeClient({"keys/query": _query("FOREIGN"), "keys/device_signing/upload": (401, {"flows": []})})
    with pytest.raises(cs.CrossSigningError, match="_allow_cross_signing_replacement_without_uia"):
        asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))
    assert "keys/signatures/upload" not in client.paths()


def test_signature_failures_raise() -> None:
    client = FakeClient(
        {
            "keys/query": _query(KEYS.master.public_key),
            "keys/signatures/upload": (200, {"failures": {USER: {DEVICE: {"errcode": "M_INVALID_SIGNATURE"}}}}),
        }
    )
    with pytest.raises(cs.CrossSigningError, match="M_INVALID_SIGNATURE"):
        asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))


def test_unknown_device_raises() -> None:
    client = FakeClient({"keys/query": (200, {"device_keys": {USER: {}}})})
    with pytest.raises(cs.CrossSigningError, match="noch nicht bekannt"):
        asyncio.run(cs.ensure_device_cross_signed(client, "s3cret"))


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
        failing = AsyncMock(side_effect=cs.CrossSigningError("nope"))
        with (
            patch.object(cfg.matrix, "cross_signing_secret", "s3cret"),
            patch.object(ma, "ensure_device_cross_signed", failing),
        ):
            asyncio.run(adapter._cross_sign_device())
        adapter._handler.client.keys_upload.assert_awaited_once()
        failing.assert_awaited_once_with(adapter._handler.client, "s3cret")

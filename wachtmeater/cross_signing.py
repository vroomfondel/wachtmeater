"""Cross-sign the bot's own Matrix device so clients stop flagging its messages.

Element marks every message of the bot with a red "!" ("encrypted by a
device not verified by its owner"): the bot logs in with fresh devices
(operator, one per watcher pod) and nobody vouches for them.  matrix-nio has
no cross-signing support, so this module does the minimum by hand:

1. The three cross-signing keys (master, self-signing, user-signing) are
   derived deterministically from one configured secret
   (``matrix.cross_signing_secret``).  Every pod that shares the secret
   therefore holds the same identity — no key store to distribute.
2. If the server does not know this master key yet, the keys are uploaded
   (``/keys/device_signing/upload``).  Synapse accepts the *first* upload
   without user-interactive auth (MSC3967); replacing existing keys of the
   account needs auth the JWT login cannot provide — then a server admin
   must allow it once (``_allow_cross_signing_replacement_without_uia``).
3. The bot's current device is signed with the self-signing key
   (``/keys/signatures/upload``), unless it already is.

Clients then see the device as verified by its owner.  Keys are signed
over their canonical JSON (sorted keys, no whitespace, without
``signatures`` / ``unsigned``) with ed25519, as the spec requires.
"""

import copy
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from olm.pk import PkSigning
from loguru import logger
from nio import AsyncClient

JsonDict = dict[str, Any]
"""A Matrix JSON object (key/signature payloads)."""

_SEED_INFO = "wachtmeater cross-signing {usage}"
"""HMAC message per key usage — changing it changes the bot's identity."""


class CrossSigningError(Exception):
    """Cross-signing could not be set up (the bot keeps working without it)."""


@dataclass(frozen=True)
class CrossSigningKeys:
    """The account's three cross-signing key pairs.

    Attributes:
        master: Master key — the account's identity.
        self_signing: Signs the account's own devices.
        user_signing: Signs other users' master keys (uploaded, not used).
    """

    master: PkSigning
    self_signing: PkSigning
    user_signing: PkSigning

    @classmethod
    def from_secret(cls, secret: str) -> "CrossSigningKeys":
        """Derive the key pairs from *secret* (HMAC-SHA256 per usage as ed25519 seed).

        Args:
            secret: The configured cross-signing secret (non-empty).

        Returns:
            The derived keys; the same secret always yields the same keys.
        """

        def _key(usage: str) -> PkSigning:
            seed = hmac.new(secret.encode(), _SEED_INFO.format(usage=usage).encode(), hashlib.sha256).digest()
            return PkSigning(seed)

        return cls(master=_key("master"), self_signing=_key("self_signing"), user_signing=_key("user_signing"))


def canonical_json(obj: JsonDict) -> str:
    """Encode *obj* as Matrix canonical JSON (sorted keys, compact, UTF-8 kept).

    Args:
        obj: JSON object.

    Returns:
        The canonical JSON string.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sign_json(obj: JsonDict, key: PkSigning, user_id: str) -> JsonDict:
    """Return a copy of *obj* with *key*'s signature added.

    The signature covers the canonical JSON without ``signatures`` and
    ``unsigned``; existing signatures are kept.

    Args:
        obj: Key object to sign.
        key: Signing key; its public key is the key ID (``ed25519:<pub>``).
        user_id: The signing user.

    Returns:
        The signed copy.
    """
    signed = copy.deepcopy(obj)
    signatures = signed.pop("signatures", {})
    unsigned = signed.pop("unsigned", None)
    signature = key.sign(canonical_json(signed))
    signatures.setdefault(user_id, {})[f"ed25519:{key.public_key}"] = signature
    signed["signatures"] = signatures
    if unsigned is not None:
        signed["unsigned"] = unsigned
    return signed


def _key_object(key: PkSigning, user_id: str, usage: str) -> JsonDict:
    return {"user_id": user_id, "usage": [usage], "keys": {f"ed25519:{key.public_key}": key.public_key}}


def device_signing_upload_body(keys: CrossSigningKeys, user_id: str) -> JsonDict:
    """Build the ``/keys/device_signing/upload`` body (sub-keys signed by the master key).

    Args:
        keys: The account's cross-signing keys.
        user_id: The bot's MXID.

    Returns:
        The request body (without ``auth``).
    """
    return {
        "master_key": _key_object(keys.master, user_id, "master"),
        "self_signing_key": sign_json(_key_object(keys.self_signing, user_id, "self_signing"), keys.master, user_id),
        "user_signing_key": sign_json(_key_object(keys.user_signing, user_id, "user_signing"), keys.master, user_id),
    }


def _master_public(query: JsonDict, user_id: str) -> str | None:
    """Return the server's master public key of *user_id* from a ``/keys/query`` response."""
    master = query.get("master_keys", {}).get(user_id)
    if not master:
        return None
    return next(iter(master.get("keys", {}).values()), None)


def is_device_signed(device_keys: JsonDict, keys: CrossSigningKeys, user_id: str) -> bool:
    """Tell whether *device_keys* already carry the self-signing key's signature.

    Args:
        device_keys: The device's key object from ``/keys/query``.
        keys: The account's cross-signing keys.
        user_id: The bot's MXID.

    Returns:
        ``True`` if the signature is present.
    """
    sigs = device_keys.get("signatures", {}).get(user_id, {})
    return f"ed25519:{keys.self_signing.public_key}" in sigs


async def _request(client: AsyncClient, path: str, body: JsonDict) -> tuple[int, JsonDict]:
    """POST *body* to the client-server API with the client's access token.

    Args:
        client: Logged-in nio client.
        path: API path below the homeserver URL.
        body: JSON body.

    Returns:
        HTTP status and the decoded JSON response.
    """
    resp = await client.send(
        "POST",
        path,
        data=json.dumps(body),
        headers={"Authorization": f"Bearer {client.access_token}", "Content-Type": "application/json"},
    )
    try:
        data: JsonDict = await resp.json()
    except Exception:
        data = {}
    return resp.status, data


async def ensure_device_cross_signed(client: AsyncClient, secret: str) -> str:
    """Make sure the account's cross-signing keys exist and this device is signed.

    Args:
        client: Logged-in nio client whose device keys are uploaded.
        secret: The configured cross-signing secret (non-empty).

    Returns:
        A short German summary of what was done.

    Raises:
        CrossSigningError: If the keys or the signature could not be uploaded.
    """
    user_id, device_id = client.user_id, client.device_id
    keys = CrossSigningKeys.from_secret(secret)

    status, query = await _request(client, "/_matrix/client/v3/keys/query", {"device_keys": {user_id: []}})
    if status != 200:
        raise CrossSigningError(f"keys/query fehlgeschlagen ({status}): {query}")

    done: list[str] = []
    server_master = _master_public(query, user_id)
    if server_master != keys.master.public_key:
        status, resp = await _request(
            client, "/_matrix/client/v3/keys/device_signing/upload", device_signing_upload_body(keys, user_id)
        )
        if status == 401:
            raise CrossSigningError(
                f"{user_id} hat bereits andere Cross-Signing-Schluessel (Master {server_master}); Ersetzen braucht "
                "eine Anmeldung, die der Bot nicht kann. Einmalig als Synapse-Admin erlauben: POST "
                f"/_synapse/admin/v1/users/{user_id}/_allow_cross_signing_replacement_without_uia"
            )
        if status != 200:
            raise CrossSigningError(f"Cross-Signing-Schluessel-Upload fehlgeschlagen ({status}): {resp}")
        done.append("Cross-Signing-Schluessel hochgeladen" if server_master is None else "Cross-Signing ersetzt")

    device = query.get("device_keys", {}).get(user_id, {}).get(device_id)
    if device is None:
        raise CrossSigningError(f"Geraeteschluessel von {device_id} sind auf dem Server noch nicht bekannt")
    if is_device_signed(device, keys, user_id):
        done.append(f"Geraet {device_id} bereits signiert")
    else:
        signed = sign_json(device, keys.self_signing, user_id)
        signed.pop("unsigned", None)
        status, resp = await _request(
            client, "/_matrix/client/v3/keys/signatures/upload", {user_id: {device_id: signed}}
        )
        failures = resp.get("failures") if status == 200 else None
        if status != 200 or failures:
            raise CrossSigningError(f"Geraete-Signatur fehlgeschlagen ({status}): {failures or resp}")
        done.append(f"Geraet {device_id} signiert")
    logger.info(f"Cross-Signing: {', '.join(done)} (Master {keys.master.public_key})")
    return ", ".join(done)

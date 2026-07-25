#!/usr/bin/env python
"""MEATER Cloud WebSocket client — sub-degree cook data without a browser.

The MEATER Cloud share page renders temperatures already rounded to whole
degrees (``.internal-value`` literally contains ``95°``), so DOM scraping can
never recover decimals no matter how it parses.  The page itself is fed by a
WebSocket that carries the underlying fixed-point integers:

    wss://cooks.cloud.meater.com/?cook=<uuid>&token=<jwt>&lang=<lang>

Temperatures arrive as integers with a denominator of 32 — the share page's
own ``DEFAULT_DENOMINATOR``, applied as ``TemperatureToCelsius(t) = t / 32``.
That is a resolution of 1/32 C = 0.03125 C.  Note this is *transport*
resolution, not sensor accuracy: MEATER specifies roughly +/- 0.5-1 C, so the
decimals sharpen trends (stall detection) rather than absolute readings.

The connection token is embedded in the share page HTML as
``window.MEATER.config``, so a plain HTTP GET is enough to obtain it — this
module needs neither Playwright nor a CDP browser.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import TypedDict, cast

import requests
from loguru import logger

from wachtmeater import cfg, read_dot_env_to_environ
from wachtmeater.meater_monitor import CookData, CookNotFoundError, MeaterFetchError

read_dot_env_to_environ()


class _WireCook(TypedDict, total=False):
    """The ``cook`` object of a MEATER Cloud WebSocket frame.

    Temperatures are fixed-point integers (see ``TEMPERATURE_DENOMINATOR``);
    times are in seconds.
    """

    internal: int
    ambient: int
    target: int
    peak: int
    elapsedTime: int
    remainingTime: int
    state: int
    description: str
    cookName: str


class _WireCookSummary(TypedDict, total=False):
    """The ``cookSummary`` object of a MEATER Cloud WebSocket frame."""

    isFinished: bool
    date: int
    peak: int


class _WireFrame(TypedDict, total=False):
    """A decoded MEATER Cloud WebSocket cook frame."""

    cook: _WireCook
    cookSummary: _WireCookSummary
    probeDisconnected: bool


TEMPERATURE_DENOMINATOR: int = 32
"""Fixed-point denominator for all temperatures on the wire.

Mirrors ``DEFAULT_DENOMINATOR`` in the share page's ``cook-monitor.js``,
whose source carries the note: "all temps received in weblink are converted
to DEFAULT_DENOMINATOR".
"""

INVALID_READING: int = -1024
"""Sentinel raw value meaning "no valid reading" (``TEMPS.INVALID_READING``)."""

DEFAULT_WS_BASE: str = "wss://cooks.cloud.meater.com"
"""Fallback WebSocket host if the page HTML does not carry an explicit URL."""

DEFAULT_LANG: str = "en-US"
"""Language parameter; only affects server-side text we do not consume."""


class CloudUnavailableError(MeaterFetchError):
    """The MEATER Cloud share page or its WebSocket was unreachable.

    Represents a transient, our-side-or-network problem — DNS failure, HTTP
    error, a missing token in the HTML, or the WebSocket never delivering a
    cook frame.  Like :class:`~wachtmeater.meater_monitor.CdpUnavailableError`
    this is explicitly NOT evidence that the cook has ended, and callers must
    never escalate it to cook-end detection.
    """


def _redact(text: str) -> str:
    """Strip any ``token=…`` value from *text* before it reaches a log.

    The WebSocket URL carries a bearer token as a query parameter. Today's
    ``websockets`` errors happen not to echo the URI, but that is not a
    guarantee worth betting a credential on — watcher logs are shipped off-box.

    Args:
        text: Arbitrary message that may embed a WebSocket URL.

    Returns:
        The message with any token value replaced by ``<redacted>``.
    """
    return re.sub(r"(token=)[^&\s]+", r"\1<redacted>", text)


def _to_celsius(raw: int | None) -> float | None:
    """Convert a raw fixed-point wire temperature to degrees Celsius.

    Args:
        raw: Raw integer from the WebSocket payload, or ``None``.

    Returns:
        Temperature in Celsius with 1/32 C resolution, or ``None`` if the
        value is missing or the firmware's invalid-reading sentinel.
    """
    if raw is None or raw == INVALID_READING:
        return None
    return raw / TEMPERATURE_DENOMINATOR


def _format_duration(seconds: int | None) -> str | None:
    """Format a second count as ``"2h 26m"`` / ``"45m"``.

    Matches the shape the scraped DOM produced, so downstream display and
    ``_parse_time_str`` round-tripping stay unchanged.

    Args:
        seconds: Duration in seconds, or ``None``.

    Returns:
        Human-readable duration, or ``None`` if *seconds* is ``None``.
    """
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fetch_share_config(url: str, timeout: float) -> tuple[str, str]:
    """Read the WebSocket base URL and auth token from the share page HTML.

    The page bootstraps its own WebSocket from ``window.MEATER.config``, so
    the token is present in the initial HTML response — no JS execution
    required.

    Args:
        url: Full MEATER Cloud cook URL.
        timeout: Per-request timeout in seconds.

    Returns:
        Tuple of ``(ws_base, token)``.

    Raises:
        CookNotFoundError: The cook URL returned HTTP 404/410 — the cook was
            deleted or expired upstream.
        CloudUnavailableError: Any other HTTP failure, or the token could not
            be located in the returned HTML.
    """
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise CloudUnavailableError(f"Share page request failed: {e}") from e

    if resp.status_code in (404, 410):
        raise CookNotFoundError(f"Cook URL returned HTTP {resp.status_code} — cook no longer exists: {url}")
    if not resp.ok:
        raise CloudUnavailableError(f"Share page returned HTTP {resp.status_code}")

    html = resp.text
    token_match = re.search(r'"token"\s*:\s*"([^"]+)"', html)
    if not token_match:
        raise CloudUnavailableError("No WebSocket token found in share page HTML (page layout changed?)")

    base_match = re.search(r'"url"\s*:\s*"(wss?://[^"]+)"', html)
    ws_base = base_match.group(1) if base_match else DEFAULT_WS_BASE
    return ws_base, token_match.group(1)


async def _read_cook_frame(ws_url: str, timeout: float) -> _WireFrame:
    """Connect to the cook WebSocket and return the first usable frame.

    Frames unrelated to cook state (keepalives, connection chatter) are
    skipped until one carrying ``cook`` or ``cookSummary`` arrives or the
    deadline expires.

    Args:
        ws_url: Fully-formed ``wss://`` URL including cook ID and token.
        timeout: Overall deadline in seconds for connect plus first frame.

    Returns:
        The decoded JSON payload of the first cook-bearing frame.

    Raises:
        CloudUnavailableError: Connection failed, or no cook frame arrived
            before the deadline.
    """
    import websockets

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        async with websockets.connect(ws_url, open_timeout=timeout) as ws:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CloudUnavailableError("No cook frame received before timeout")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and ("cook" in payload or "cookSummary" in payload):
                    return cast(_WireFrame, payload)
    except CloudUnavailableError:
        raise
    except asyncio.TimeoutError as e:
        raise CloudUnavailableError("Timed out waiting for a cook frame") from e
    except Exception as e:
        raise CloudUnavailableError(_redact(f"WebSocket connection failed: {e}")) from e


def _derive_status(payload: _WireFrame, internal: float | None, target: float | None) -> str:
    """Map a WebSocket payload to the monitor's status vocabulary.

    Reproduces the share page's own logic rather than inventing new rules,
    because ``"done"`` and ``"finished"`` both terminate a cook downstream:

    * The page renders its finished summary — and sets the ``finished`` CSS
      class the scraper keyed on — when ``cookSummary.isFinished`` is true, or
      when there is no ``cook`` payload and the probe is *not* flagged as
      disconnected.
    * ``"offline"`` mirrors the old "Searching for cook…" / empty-DOM case:
      no usable cook data at all.
    * ``"done"`` keeps its original meaning — internal temperature has reached
      the target — and is deliberately not derived from ``CookState``.

    Args:
        payload: Decoded WebSocket frame.
        internal: Internal temperature in Celsius, if valid.
        target: Target temperature in Celsius, if valid.

    Returns:
        One of ``"cooking"``, ``"done"``, ``"finished"``, ``"offline"``,
        or ``"unknown"``.
    """
    cook: _WireCook = payload.get("cook") or {}
    summary: _WireCookSummary = payload.get("cookSummary") or {}
    probe_disconnected = bool(payload.get("probeDisconnected"))

    if summary.get("isFinished") or (not cook and not probe_disconnected):
        return "finished"
    if not cook:
        return "offline"
    if internal is None and target is None:
        return "offline"
    if internal is not None and target is not None:
        return "done" if internal >= target else "cooking"
    return "cooking"


def _started_at(payload: _WireFrame, elapsed_seconds: int | None) -> str | None:
    """Determine the cook's start timestamp as an ISO string.

    Prefers the authoritative ``cookSummary.date`` (epoch milliseconds) and
    falls back to deriving it from the elapsed time.

    Args:
        payload: Decoded WebSocket frame.
        elapsed_seconds: Elapsed cook time in seconds, if known.

    Returns:
        Local-time ISO 8601 timestamp, or ``None`` if neither source is
        available.
    """
    summary: _WireCookSummary = payload.get("cookSummary") or {}
    date_ms = summary.get("date")
    if date_ms is not None and date_ms > 0:
        return datetime.fromtimestamp(date_ms / 1000).isoformat()
    if elapsed_seconds is not None:
        return (datetime.now() - timedelta(seconds=elapsed_seconds)).isoformat()
    return None


def parse_cook_frame(payload: _WireFrame) -> CookData:
    """Build a :class:`CookData` from a decoded WebSocket frame.

    Split out from the network path so the wire format can be tested against
    recorded payloads without touching MEATER Cloud.

    Args:
        payload: Decoded JSON payload of a cook-bearing frame.

    Returns:
        Parsed cook data with sub-degree temperatures and ``source`` set to
        ``"cloud-ws"``. The ``screenshot`` field is left unset — the caller
        attaches one if it wants a visual.
    """
    cook: _WireCook = payload.get("cook") or {}
    summary: _WireCookSummary = payload.get("cookSummary") or {}

    internal = _to_celsius(cook.get("internal"))
    ambient = _to_celsius(cook.get("ambient"))
    target = _to_celsius(cook.get("target"))
    peak = _to_celsius(cook.get("peak") if cook.get("peak") is not None else summary.get("peak"))

    elapsed_seconds = cook.get("elapsedTime")
    remaining_seconds = cook.get("remainingTime")

    return CookData(
        cook_name=cook.get("description") or cook.get("cookName") or None,
        started_at=_started_at(payload, elapsed_seconds),
        internal_temp_c=internal,
        target_temp_c=target,
        ambient_temp_c=ambient,
        remaining_time=_format_duration(remaining_seconds),
        remaining_minutes=remaining_seconds // 60 if remaining_seconds is not None else None,
        elapsed_time=_format_duration(elapsed_seconds),
        elapsed_minutes=elapsed_seconds // 60 if elapsed_seconds is not None else None,
        status=_derive_status(payload, internal, target),
        peak_temp_c=peak,
        source="cloud-ws",
    )


def fetch_cook_data(url: str, timeout: float | None = None) -> CookData:
    """Fetch cook data directly from the MEATER Cloud WebSocket.

    Obtains the connection token from the share page HTML, opens the
    WebSocket, and parses the first cook-bearing frame. Runs its own event
    loop, so it must be called from a synchronous context (the watcher calls
    it from a worker thread).

    Args:
        url: Full MEATER Cloud cook URL.
        timeout: Per-stage timeout in seconds; defaults to
            ``cfg.monitoring.cloud_ws_timeout``.

    Returns:
        Parsed cook data with sub-degree temperatures.

    Raises:
        CookNotFoundError: The cook no longer exists upstream.
        CloudUnavailableError: Any transient fetch/connect/parse failure.
    """
    effective_timeout = timeout if timeout is not None else cfg.monitoring.cloud_ws_timeout

    ws_base, token = _fetch_share_config(url, effective_timeout)
    cook_id = url.rstrip("/").rsplit("/", 1)[-1]
    ws_url = f"{ws_base}/?cook={cook_id}&token={token}&lang={DEFAULT_LANG}"

    logger.debug(f"Connecting to MEATER Cloud WebSocket for cook {cook_id}")
    payload = asyncio.run(_read_cook_frame(ws_url, effective_timeout))
    data = parse_cook_frame(payload)
    logger.info(
        f"Cloud data: internal={data.internal_temp_c}°C, ambient={data.ambient_temp_c}°C, "
        f"target={data.target_temp_c}°C, status={data.status}"
    )
    return data

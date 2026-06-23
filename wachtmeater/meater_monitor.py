#!/usr/bin/env python
"""MEATER Cook Monitor.

Extracts cooking data (temperatures, status, timing) from MEATER Cloud
share URLs by scraping the rendered page via a remote Chrome DevTools
Protocol (CDP) browser instance using Playwright.

Usage:
    wachtmeater monitor <cook-url>
    wachtmeater monitor https://cooks.cloud.meater.com/cook/b46f2292-...

Output:
    JSON object with internal/ambient/target temperatures, cook status,
    elapsed/remaining time, and battery level.
"""

import base64
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, TypedDict
from urllib.parse import urlparse

import requests

from wachtmeater import cfg, read_dot_env_to_environ

read_dot_env_to_environ()


from loguru import logger


class _RawCookData(TypedDict):
    """Raw dictionary returned by the in-page ``page.evaluate()`` JS call."""

    internal_temp_c: int | None
    target_temp_c: int | None
    ambient_temp_c: int | None
    cook_name: str | None
    title: str
    cook_time_html: str
    cook_time_visible: bool
    cook_time_clickable: bool
    remaining_text: str | None
    resting_time_html: str
    resting_time_visible: bool
    cook_finished: bool
    summary_text: str | None
    summary_peak: int | None
    searching_for_cook: bool


class CookData(NamedTuple):
    """Parsed cooking data from a MEATER Cloud cook page.

    Attributes:
        cook_name: Name or label of the cook session.
        started_at: Timestamp when the cook started.
        internal_temp_c: Internal (meat) temperature in Celsius.
        target_temp_c: Target temperature in Celsius.
        ambient_temp_c: Ambient (smoker/grill) temperature in Celsius.
        remaining_time: Human-readable remaining time or ``"Estimating"``.
        remaining_minutes: Remaining time in minutes.
        elapsed_time: Human-readable elapsed time.
        elapsed_minutes: Elapsed time in minutes.
        status: Cook status — ``"cooking"``, ``"done"``, ``"finished"``,
            ``"offline"`` (station lost cloud connectivity), or ``"unknown"``.
        battery: MEATER probe battery percentage.
        peak_temp_c: Peak internal temperature reported by the MEATER summary.
        screenshot: Filesystem path to a screenshot of the cook page.
    """

    cook_name: str | None = None
    started_at: str | None = None
    internal_temp_c: int | None = None
    target_temp_c: int | None = None
    ambient_temp_c: int | None = None
    remaining_time: str | None = None
    remaining_minutes: int | None = None
    elapsed_time: str | None = None
    elapsed_minutes: int | None = None
    status: str = "unknown"
    battery: int | None = None
    peak_temp_c: int | None = None
    screenshot: Path | str | None = None


class MeaterFetchError(Exception):
    """Base class for failures while fetching MEATER cook data."""


class CdpUnavailableError(MeaterFetchError):
    """The CDP browser endpoint was unreachable, hung, or timed out.

    Represents an infrastructure problem on *our* side — the CDP endpoint
    being down, the WebSocket handshake timing out, or the navigation
    stalling.  This is explicitly NOT evidence that the cook has ended;
    callers must treat it as a transient outage and never escalate it to
    cook-end detection.
    """


class CookNotFoundError(MeaterFetchError):
    """The MEATER cook URL / cook ID no longer exists.

    The share page responded with a not-found status (HTTP 4xx), meaning
    the cook was deleted or expired upstream.  Unlike
    :class:`CdpUnavailableError`, this is legitimate evidence that the
    cook has ended.
    """


CDP_ENDPOINT: str = cfg.browser.cdp_url
SCREENSHOT_DIR: Path = Path(cfg.browser.screenshot_dir or str((Path(__file__).resolve().parent / "data").resolve()))
SCREENSHOT_DIR = SCREENSHOT_DIR.resolve()


def _parse_time_str(time_str: str) -> int | None:
    """Parse a human-readable time string into total minutes.

    Supports formats like ``"2h 26m"``, ``"3h"``, ``"45m"``,
    ``"01:23:45"`` (HH:MM:SS), and ``"23:45"`` (MM:SS).

    Args:
        time_str: The time string to parse.

    Returns:
        Total minutes as an integer, or ``None`` if the format is
        not recognised.
    """
    # Try "Xh Ym" format (e.g. "2h 26m")
    hm_match = re.match(r"(\d+)h\s*(\d+)m", time_str)
    if hm_match:
        return int(hm_match.group(1)) * 60 + int(hm_match.group(2))
    # Try "Xh" only (e.g. "3h")
    h_match = re.match(r"(\d+)h\s*$", time_str)
    if h_match:
        return int(h_match.group(1)) * 60
    # Try "Xm" only (e.g. "45m")
    m_match = re.match(r"(\d+)m\s*$", time_str)
    if m_match:
        return int(m_match.group(1))
    # Try colon-separated HH:MM:SS or MM:SS
    parts = time_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return None


def _cdp_ping(ws_url: str, timeout: float = 3.0) -> bool:
    """Best-effort liveness check of a single CDP target via a raw WebSocket.

    Opens the target's debugger WebSocket and issues ``Runtime.evaluate``.
    A hung/zombie renderer still completes the WS upgrade but never answers
    CDP commands, so a missing reply within ``timeout`` means the tab is dead.

    Uses only the stdlib (no extra ws dependency). Returns ``True`` if the
    target replied, ``False`` on any error/timeout.
    """
    try:
        u = urlparse(ws_url)
        host = u.hostname or "localhost"
        port = u.port or 80
        path = u.path or "/"
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            s.sendall(handshake.encode())
            hdr = b""
            while b"\r\n\r\n" not in hdr:
                chunk = s.recv(1)
                if not chunk:
                    return False
                hdr += chunk
            if b" 101 " not in hdr.split(b"\r\n", 1)[0]:
                return False
            msg = json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "1", "returnByValue": True},
                },
                separators=(",", ":"),
            ).encode()
            # Client->server frames must be masked (RFC 6455); payload < 126 bytes.
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
            frame = bytes([0x81, 0x80 | len(msg)]) + mask + masked
            s.sendall(frame)
            return b'"id":1' in s.recv(4096)
    except Exception:
        return False


def _reap_unresponsive_targets(cdp_endpoint: str, cdp_host: str) -> None:
    """Close hung/zombie page tabs in a SHARED Chrome before connecting.

    Playwright's ``connect_over_cdp`` attaches to and initialises *every* open
    page; a single unresponsive renderer makes the whole connect hang until
    timeout. The Chrome instance is shared (e.g. wachtmeater + clawdbot), so we
    only close tabs that fail a liveness ping — healthy tabs are left alone.

    Entirely best-effort: any failure is logged and swallowed so it can never
    break the monitoring flow.
    """
    try:
        targets = requests.get(f"{cdp_endpoint}/json/list", timeout=10).json()
    except Exception as e:
        logger.warning(f"Tab reaper: could not list CDP targets: {e}")
        return
    for t in targets:
        if t.get("type") != "page":
            continue
        ws = t.get("webSocketDebuggerUrl", "")
        if not ws:
            continue
        ws = ws.replace("ws://localhost:9222", f"ws://{cdp_host}")
        if _cdp_ping(ws):
            continue
        tid = t.get("id")
        try:
            requests.get(f"{cdp_endpoint}/json/close/{tid}", timeout=10)
            logger.warning(f"Tab reaper: closed unresponsive tab {tid} ({t.get('title')!r} {t.get('url', '')[:60]})")
        except Exception as e:
            logger.warning(f"Tab reaper: failed to close tab {tid}: {e}")


def extract_via_browser(url: str) -> CookData:
    """Scrape the rendered MEATER cook page via a remote CDP browser.

    Connects to an existing Chrome instance over CDP (configured via
    ``BROWSER_CDP_URL`` env var), navigates to the cook URL, waits for
    the page to render, and extracts data via DOM querySelector calls.

    Args:
        url: Full MEATER Cloud cook URL to scrape.

    Returns:
        A ``CookData`` named tuple with the parsed cook data and
        the screenshot path.

    Raises:
        Exception: If the CDP WebSocket URL cannot be retrieved or the
            browser connection fails.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    # First, get the WebSocket URL from the CDP endpoint.  A failure here
    # means our CDP infrastructure is unreachable — a transient, our-side
    # problem, never a sign that the cook itself has ended.
    try:
        logger.info(f"Fetching CDP WebSocket URL from {CDP_ENDPOINT}")
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=10)
        resp.raise_for_status()
        ws_url: str = resp.json().get("webSocketDebuggerUrl")
        cdp_host = CDP_ENDPOINT.replace("http://", "").replace("https://", "")
        ws_url = ws_url.replace("ws://localhost:9222", f"ws://{cdp_host}")
        logger.debug(f"CDP WebSocket URL: {ws_url}")
    except Exception as e:
        raise CdpUnavailableError(f"Failed to get WebSocket URL: {e}") from e

    # Defend against a hung tab in the shared Chrome wedging connect_over_cdp:
    # close any unresponsive tabs first, leaving healthy (foreign) tabs intact.
    _reap_unresponsive_targets(CDP_ENDPOINT, cdp_host)

    with sync_playwright() as p:
        logger.info("Connecting to browser via CDP...")
        # Bounded timeout: if a fresh zombie appears between reaping and
        # connecting, fail fast (60s) instead of blocking the default 180s.
        # A connect timeout/failure is a CDP-side outage, not cook-end.
        try:
            browser = p.chromium.connect_over_cdp(ws_url, timeout=60000)
        except Exception as e:
            raise CdpUnavailableError(f"CDP connect failed: {e}") from e
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        try:
            logger.info(f"Navigating to {url}")
            # Capture the navigation response so we can tell a genuine
            # "cook no longer exists" (HTTP 4xx) apart from a navigation
            # timeout (our-side network/CDP stall).
            try:
                response = page.goto(url, timeout=30000)
            except PlaywrightTimeoutError as e:
                raise CdpUnavailableError(f"Navigation to cook URL timed out: {e}") from e
            if response is not None and response.status in (404, 410):
                raise CookNotFoundError(f"Cook URL returned HTTP {response.status} — cook no longer exists: {url}")
            logger.debug("Waiting 5s for page content to load...")
            page.wait_for_timeout(5000)  # Wait for content to load

            meater_uuid: str = url.split("/")[-1]

            # Save a screenshot for debugging / visual reference
            screenshot_dir: Path = SCREENSHOT_DIR
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = screenshot_dir / f"meater-screenshot-{meater_uuid}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info(f"Screenshot saved: {screenshot_path}")

            # Extract data via DOM selectors — first read (default/remaining mode)
            logger.debug("Extracting cook data from DOM...")
            data: _RawCookData = page.evaluate("""() => {
                const text = (sel) => document.querySelector(sel)?.textContent?.trim() || null;
                const tempVal = (sel) => {
                    const t = text(sel);
                    return t ? parseInt(t.replace('°', '')) : null;
                };
                const cookTime = document.getElementById('cook-time');
                const restingTime = document.getElementById('resting-time');
                return {
                    internal_temp_c: tempVal('.internal-value'),
                    target_temp_c: tempVal('.target-value'),
                    ambient_temp_c: tempVal('.ambient-value'),
                    cook_name: text('#cook-description'),
                    title: document.title,
                    cook_time_html: cookTime?.innerHTML || '',
                    cook_time_visible: cookTime ? cookTime.style.display !== 'none' : false,
                    cook_time_clickable: !!cookTime,
                    remaining_text: text('#cook-time > span.remaining'),
                    resting_time_html: restingTime?.innerHTML || '',
                    resting_time_visible: restingTime ? restingTime.style.display !== 'none' : false,
                    cook_finished: document.getElementById('cook')?.classList.contains('finished') || false,
                    summary_text: document.querySelector('#summary p')?.textContent?.trim() || null,
                    summary_peak: (() => {
                        const h3 = document.querySelector('#summary h3');
                        if (!h3) return null;
                        const match = h3.textContent.match(/Peak\\s*:\\s*(\\d+)/);
                        return match ? parseInt(match[1]) : null;
                    })(),
                    searching_for_cook: /searching for cook/i.test(document.body.innerText || ''),
                };
            }""")

            # Click #cook-time to toggle to the other mode and read again
            toggled_cook_time_html = ""
            if data.get("cook_time_clickable"):
                try:
                    logger.debug("Toggling cook-time display...")
                    page.click("#cook-time")
                    page.wait_for_timeout(300)
                    toggled_cook_time_html = page.evaluate(
                        """() => document.getElementById('cook-time')?.innerHTML || ''"""
                    )
                    # Restore original state
                    page.click("#cook-time")
                except Exception:
                    pass

            # Parse cook time HTML — assign each reading to remaining or elapsed
            logger.debug("Parsing time and status data...")
            elapsed_time = None
            elapsed_minutes = None
            remaining_time = None
            remaining_minutes = None
            started_at = None

            for html in [data.get("cook_time_html", ""), toggled_cook_time_html]:
                if not html or not data.get("cook_time_visible"):
                    continue
                if "Estimating" in html:
                    remaining_time = "Estimating"
                elif "<br>" in html:
                    time_part = html.split("<br>")[0].strip()
                    if "elapsed" in html.lower():
                        elapsed_time = time_part
                        elapsed_minutes = _parse_time_str(time_part)
                    elif "remaining" in html.lower():
                        remaining_time = time_part
                        remaining_minutes = _parse_time_str(time_part)

            # Parse remaining time from dedicated span.remaining element
            raw_remaining = data.get("remaining_text")
            if raw_remaining:
                if "Estimating" in raw_remaining:
                    remaining_time = "Estimating"
                else:
                    remaining_time = raw_remaining
                    remaining_minutes = _parse_time_str(raw_remaining)

            # Parse started_at from document.title
            # Format: "Beef Brisket | Cook started Saturday, March 7, 2026 at 3:12 AM | MEATER Cloud"
            title = data.get("title", "")
            for segment in title.split("|"):
                segment = segment.strip()
                if segment.startswith("Cook started "):
                    date_str = segment[len("Cook started ") :]
                    try:
                        started_at = datetime.strptime(date_str, "%A, %B %d, %Y at %I:%M %p").isoformat()
                    except ValueError:
                        pass
                    break

            # Determine status
            internal = data.get("internal_temp_c")
            target = data.get("target_temp_c")
            cook_finished = data.get("cook_finished", False)
            searching_for_cook = data.get("searching_for_cook", False)
            no_cook_data = (
                internal is None
                and target is None
                and data.get("ambient_temp_c") is None
                and data.get("cook_name") is None
            )

            if cook_finished:
                status = "finished"
            elif internal is not None and target is not None:
                status = "done" if internal >= target else "cooking"
            elif searching_for_cook or no_cook_data:
                # Station lost connectivity to MEATER Cloud — share page is
                # stuck on a "Searching for cook…" spinner with empty DOM.
                status = "offline"
            else:
                status = "unknown"

            # Persist raw DOM artefacts on suspected outages so the heuristic
            # can be refined against real outage pages later.
            if status == "offline":
                try:
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    html_path = screenshot_dir / f"meater-offline-{meater_uuid}-{ts}.html"
                    text_path = screenshot_dir / f"meater-offline-{meater_uuid}-{ts}.txt"
                    html_path.write_text(page.content(), encoding="utf-8")
                    body_text = page.evaluate("() => document.body.innerText || ''")
                    text_path.write_text(body_text, encoding="utf-8")
                    logger.info(f"Offline artefacts saved: {html_path.name}, {text_path.name}")
                except Exception as e:
                    logger.warning(f"Failed to save offline artefacts: {e}")

            peak_temp_c = data.get("summary_peak")

            logger.info(
                f"Cook data: internal={internal}°C, ambient={data.get('ambient_temp_c')}°C, "
                f"target={target}°C, status={status}, cook_finished={cook_finished}"
            )
            return CookData(
                cook_name=data.get("cook_name"),
                started_at=started_at,
                internal_temp_c=internal,
                target_temp_c=target,
                ambient_temp_c=data.get("ambient_temp_c"),
                remaining_time=remaining_time,
                remaining_minutes=remaining_minutes,
                elapsed_time=elapsed_time,
                elapsed_minutes=elapsed_minutes,
                status=status,
                peak_temp_c=peak_temp_c,
                screenshot=str(screenshot_path),
            )
        finally:
            browser.close()

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

import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, TypedDict

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
            or ``"unknown"``.
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
    from playwright.sync_api import sync_playwright

    # First, get the WebSocket URL from the CDP endpoint
    try:
        logger.info(f"Fetching CDP WebSocket URL from {CDP_ENDPOINT}")
        resp = requests.get(f"{CDP_ENDPOINT}/json/version", timeout=10)
        resp.raise_for_status()
        ws_url: str = resp.json().get("webSocketDebuggerUrl")
        cdp_host = CDP_ENDPOINT.replace("http://", "").replace("https://", "")
        ws_url = ws_url.replace("ws://localhost:9222", f"ws://{cdp_host}")
        logger.debug(f"CDP WebSocket URL: {ws_url}")
    except Exception as e:
        raise Exception(f"Failed to get WebSocket URL: {e}")

    with sync_playwright() as p:
        logger.info("Connecting to browser via CDP...")
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        try:
            logger.info(f"Navigating to {url}")
            page.goto(url, timeout=30000)
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

            if cook_finished:
                status = "finished"
            elif internal is not None and target is not None:
                status = "done" if internal >= target else "cooking"
            else:
                status = "unknown"

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

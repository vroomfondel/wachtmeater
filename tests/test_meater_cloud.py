"""Tests for wachtmeater/meater_cloud.py and the hybrid fetch path.

The recorded payloads mirror real frames captured from the MEATER Cloud
WebSocket, so the fixed-point decoding and status mapping are pinned against
the actual wire format rather than an invented one.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from wachtmeater import cfg
from wachtmeater.meater_monitor import CookData, CookNotFoundError

mc = pytest.importorskip("wachtmeater.meater_cloud")
mm = pytest.importorskip("wachtmeater.meater_monitor")


def _frame(**cook_overrides: Any) -> dict[str, Any]:
    """Build a cook frame based on a real recorded payload."""
    cook = {
        "seq": 3,
        "probeNum": 1,
        "clipNumber": 0,
        "peak": 3042,
        "internal": 3042,
        "ambient": 3498,
        "target": 3136,
        "elapsedTime": 49249,
        "remainingTime": 7752,
        "state": 2,
        "isCustomCook": True,
        "cookName": "",
        "cutID": 15,
        "defaultTemperatureScale": "C",
        "description": "Beef Brisket",
    }
    cook.update(cook_overrides)
    return {
        "cook": cook,
        "cookSummary": {"isFinished": False, "date": 1784930209000, "peak": 3040},
        "probeDisconnected": False,
    }


class TestToCelsius:
    """Tests for _to_celsius — the 1/32 C fixed-point decoding."""

    def test_decodes_denominator_32(self) -> None:
        assert mc._to_celsius(3042) == 95.0625

    def test_round_target_decodes_exactly(self) -> None:
        # Targets are set as whole degrees; an exact result confirms there is
        # no offset in the encoding.
        assert mc._to_celsius(3136) == 98.0

    def test_invalid_reading_is_none(self) -> None:
        assert mc._to_celsius(mc.INVALID_READING) is None

    def test_none_is_none(self) -> None:
        assert mc._to_celsius(None) is None


class TestRedact:
    """Tests for _redact — keeps the bearer token out of logs."""

    def test_removes_token_value(self) -> None:
        msg = "failed: wss://cooks.cloud.meater.com/?cook=abc&token=eyJhbGci.secret&lang=en-US"
        out = mc._redact(msg)
        assert "eyJhbGci.secret" not in out
        assert "<redacted>" in out
        assert "cook=abc" in out

    def test_leaves_clean_messages_alone(self) -> None:
        assert mc._redact("plain failure") == "plain failure"


class TestFormatDuration:
    """Tests for _format_duration."""

    def test_hours_and_minutes(self) -> None:
        assert mc._format_duration(7752) == "2h 9m"

    def test_minutes_only(self) -> None:
        assert mc._format_duration(2700) == "45m"

    def test_none(self) -> None:
        assert mc._format_duration(None) is None

    def test_negative_clamped(self) -> None:
        assert mc._format_duration(-5) == "0m"


class TestDeriveStatus:
    """Tests for _derive_status — mirrors the share page's own rules."""

    def test_is_finished_wins(self) -> None:
        payload = _frame()
        payload["cookSummary"]["isFinished"] = True
        assert mc._derive_status(payload, 50.0, 96.0) == "finished"

    def test_no_cook_and_probe_connected_is_finished(self) -> None:
        # Page rule: (!data.cook && !data.probeDisconnected) renders the summary.
        payload = {"cookSummary": {"isFinished": False}, "probeDisconnected": False}
        assert mc._derive_status(payload, None, None) == "finished"

    def test_no_cook_but_probe_disconnected_is_offline(self) -> None:
        payload = {"cookSummary": {"isFinished": False}, "probeDisconnected": True}
        assert mc._derive_status(payload, None, None) == "offline"

    def test_target_reached_is_done(self) -> None:
        assert mc._derive_status(_frame(), 98.5, 98.0) == "done"

    def test_below_target_is_cooking(self) -> None:
        assert mc._derive_status(_frame(), 95.0625, 98.0) == "cooking"

    def test_no_usable_temps_is_offline(self) -> None:
        assert mc._derive_status(_frame(), None, None) == "offline"


class TestParseCookFrame:
    """Tests for parse_cook_frame against a recorded payload."""

    def test_temperatures_are_sub_degree(self) -> None:
        cook = mc.parse_cook_frame(_frame())
        assert cook.internal_temp_c == 95.0625
        assert cook.ambient_temp_c == 109.3125
        assert cook.target_temp_c == 98.0

    def test_metadata(self) -> None:
        cook = mc.parse_cook_frame(_frame())
        assert cook.cook_name == "Beef Brisket"
        assert cook.status == "cooking"
        assert cook.source == "cloud-ws"
        assert cook.remaining_time == "2h 9m"
        assert cook.elapsed_time == "13h 40m"
        assert cook.remaining_minutes == 129
        assert cook.started_at is not None

    def test_invalid_internal_becomes_none(self) -> None:
        cook = mc.parse_cook_frame(_frame(internal=mc.INVALID_READING))
        assert cook.internal_temp_c is None

    def test_no_screenshot_attached(self) -> None:
        # The WebSocket path carries no image; the caller attaches one.
        assert mc.parse_cook_frame(_frame()).screenshot is None


class TestFetchShareConfig:
    """Tests for _fetch_share_config — token extraction and error mapping."""

    def _resp(self, status: int, text: str = "") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.ok = 200 <= status < 300
        resp.text = text
        return resp

    @patch("wachtmeater.meater_cloud.requests.get")
    def test_extracts_token_and_base(self, mock_get: MagicMock) -> None:
        html = 'window.MEATER.config = {"url":"wss://cooks.cloud.meater.com","token":"abc.def.ghi"};'
        mock_get.return_value = self._resp(200, html)
        base, token = mc._fetch_share_config("https://x/cook/u", 5.0)
        assert base == "wss://cooks.cloud.meater.com"
        assert token == "abc.def.ghi"

    @patch("wachtmeater.meater_cloud.requests.get")
    def test_falls_back_to_default_base(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._resp(200, '{"token":"t"}')
        base, _ = mc._fetch_share_config("https://x/cook/u", 5.0)
        assert base == mc.DEFAULT_WS_BASE

    @patch("wachtmeater.meater_cloud.requests.get")
    def test_404_is_cook_not_found(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._resp(404)
        with pytest.raises(CookNotFoundError):
            mc._fetch_share_config("https://x/cook/u", 5.0)

    @patch("wachtmeater.meater_cloud.requests.get")
    def test_500_is_transient(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._resp(500)
        with pytest.raises(mc.CloudUnavailableError):
            mc._fetch_share_config("https://x/cook/u", 5.0)

    @patch("wachtmeater.meater_cloud.requests.get")
    def test_missing_token_is_transient(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._resp(200, "<html>no config here</html>")
        with pytest.raises(mc.CloudUnavailableError):
            mc._fetch_share_config("https://x/cook/u", 5.0)


class TestExtractCookDataHybrid:
    """Tests for the hybrid fetch path in meater_monitor.extract_cook_data."""

    URL = "https://cooks.cloud.meater.com/cook/abc-123"

    @pytest.fixture(autouse=True)
    def _cfg(self) -> Any:
        """Run with the WebSocket preferred and screenshots off by default."""
        with (
            patch.object(cfg.monitoring, "cloud_ws_enabled", True),
            patch.object(cfg.browser, "screenshot_enabled", False),
        ):
            yield

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_prefers_websocket(self, mock_ws: MagicMock, mock_browser: MagicMock) -> None:
        mock_ws.return_value = CookData(internal_temp_c=95.0625, source="cloud-ws")
        cook = mm.extract_cook_data(self.URL)
        assert cook.internal_temp_c == 95.0625
        assert cook.source == "cloud-ws"
        mock_browser.assert_not_called()

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_cook_not_found_propagates_without_browser(self, mock_ws: MagicMock, mock_browser: MagicMock) -> None:
        # Both paths read the same share URL, so a 404 is authoritative and
        # must not be retried through the browser.
        mock_ws.side_effect = CookNotFoundError("gone")
        with pytest.raises(CookNotFoundError):
            mm.extract_cook_data(self.URL)
        mock_browser.assert_not_called()

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_transient_failure_falls_back_to_browser(self, mock_ws: MagicMock, mock_browser: MagicMock) -> None:
        mock_ws.side_effect = mc.CloudUnavailableError("cloud down")
        mock_browser.return_value = CookData(internal_temp_c=95.0, source="browser")
        cook = mm.extract_cook_data(self.URL)
        assert cook.source == "browser"
        mock_browser.assert_called_once_with(self.URL)

    @patch("wachtmeater.meater_monitor.extract_via_browser")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_disabled_websocket_uses_browser(self, mock_ws: MagicMock, mock_browser: MagicMock) -> None:
        mock_browser.return_value = CookData(source="browser")
        with patch.object(cfg.monitoring, "cloud_ws_enabled", False):
            mm.extract_cook_data(self.URL)
        mock_ws.assert_not_called()
        mock_browser.assert_called_once()

    @patch("wachtmeater.meater_monitor.capture_screenshot")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_screenshot_attached_when_enabled(self, mock_ws: MagicMock, mock_shot: MagicMock) -> None:
        mock_ws.return_value = CookData(internal_temp_c=95.0625, source="cloud-ws")
        mock_shot.return_value = "/data/shot.png"
        with patch.object(cfg.browser, "screenshot_enabled", True):
            cook = mm.extract_cook_data(self.URL)
        assert cook.screenshot == "/data/shot.png"
        assert cook.internal_temp_c == 95.0625

    @patch("wachtmeater.meater_monitor.capture_screenshot")
    @patch("wachtmeater.meater_cloud.fetch_cook_data")
    def test_screenshot_failure_keeps_reading(self, mock_ws: MagicMock, mock_shot: MagicMock) -> None:
        # A dead CDP endpoint must cost the image, never the temperature.
        mock_ws.return_value = CookData(internal_temp_c=95.0625, source="cloud-ws")
        mock_shot.side_effect = mm.CdpUnavailableError("cdp down")
        with patch.object(cfg.browser, "screenshot_enabled", True):
            cook = mm.extract_cook_data(self.URL)
        assert cook.internal_temp_c == 95.0625
        assert cook.screenshot is None

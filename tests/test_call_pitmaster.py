"""Tests for wachtmeater/call_pitmaster.py."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from wachtmeater.config import SipConfig, WachtmeaterConfig
from wachtmeater.call_pitmaster import call_pitmaster


def _make_cfg(**sip_overrides: Any) -> WachtmeaterConfig:
    """Build a WachtmeaterConfig with SipConfig defaults, applying overrides."""
    sip_kwargs: dict[str, Any] = {
        "operator_url": "http://sip-operator.test.svc.cluster.local/call",
        "recording_dir": "/data/recordings",
        "dest": "+15550100999",
        "server": "sip.example.com",
        "port": 5060,
        "transport": "tls",
        "srtp": "mandatory",
        "user": "100",
        "password": "t0ps3cret",
        "call_pre_delay": 1.0,
        "call_post_delay": 1.0,
        "call_inter_delay": 2.1,
        "call_repeat_announcement": 2,
        "call_transcribe": True,
        "call_wait_for_silence": 2.0,
        "call_verbose": True,
    }
    sip_kwargs.update(sip_overrides)
    cfg = WachtmeaterConfig(sip=SipConfig(**sip_kwargs))
    return cfg


# -- call_pitmaster ----------------------------------------------------------


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg())
def test_payload_structure(mock_post: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_post.return_value = mock_resp

    call_pitmaster("test alert")

    _, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert payload["text"] == "test alert"
    assert payload["dest"] == "+15550100999"
    assert "sip_server" in payload
    assert payload["repeat"] == 2
    assert payload["transcribe"] is True


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg(operator_url="http://custom:8080/call"))
def test_env_url_override(mock_post: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_post.return_value = mock_resp

    call_pitmaster("hi")

    args, _ = mock_post.call_args
    assert args[0] == "http://custom:8080/call"


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg(port=9999))
def test_env_sip_port(mock_post: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_post.return_value = mock_resp

    call_pitmaster("hi")

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["sip_port"] == 9999


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg())
def test_recording_path(mock_post: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_post.return_value = mock_resp

    call_pitmaster("hi")

    _, kwargs = mock_post.call_args
    rec = kwargs["json"]["record"]
    assert rec.endswith(".wav")
    assert "recording_" in rec


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg())
def test_http_error_propagates(mock_post: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    mock_post.return_value = mock_resp

    with pytest.raises(requests.HTTPError):
        call_pitmaster("boom")


@patch("wachtmeater.call_pitmaster.requests.post")
@patch("wachtmeater.call_pitmaster.cfg", _make_cfg(call_transcribe=False))
def test_bool_false_transcribe(mock_post: MagicMock) -> None:
    """Verify that bool(False) from config is actually False (not the old bool('False')→True bug)."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_post.return_value = mock_resp

    call_pitmaster("hi")

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["transcribe"] is False

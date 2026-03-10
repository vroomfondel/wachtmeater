"""Trigger a SIP phone call via sipstuff-operator.

Python replacement for ``call-pitmaster.sh``.  Use as a library
(``from wachtmeater.call_pitmaster import call_pitmaster``) or via
the CLI (``wachtmeater call "text to speak"``).
"""

import os
from datetime import datetime
import requests

from wachtmeater import cfg
from loguru import logger

SipOperatorResponse = dict[str, str | int | float | bool | None]


def call_pitmaster(text: str) -> SipOperatorResponse:
    """Place a SIP call with TTS via sipstuff-operator.

    The ``operator_url`` supports ``http://user:pass@host/path`` basic-auth
    syntax (handled natively by *requests*).

    Args:
        text: The text to speak during the call.

    Returns:
        Parsed JSON response from sipstuff-operator.

    Raises:
        requests.RequestException: On HTTP / connection errors.
    """
    sip = cfg.sip
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    assert sip.dest, "SIP_DEST must be configured"

    logger.info(f"Calling {sip.dest} via {sip.operator_url}")
    logger.debug(f"SIP: {sip.server}:{sip.port} ({sip.transport}), user={sip.user}")
    logger.debug(f"TTS text: {text!r}")

    payload = {
        "dest": sip.dest,
        "text": text,
        "sip_server": sip.server,
        "sip_port": sip.port,
        "sip_transport": sip.transport,
        "sip_srtp": sip.srtp,
        "sip_user": sip.user,
        "sip_password": sip.password,
        "pre_delay": sip.call_pre_delay,
        "post_delay": sip.call_post_delay,
        "inter_delay": sip.call_inter_delay,
        "repeat": sip.call_repeat_announcement,
        "record": os.environ.get("SIP_CALL_RECORD_FILENAME", f"{sip.recording_dir}/recording_{timestamp}.wav"),
        "transcribe": sip.call_transcribe,
        "wait_for_silence": sip.call_wait_for_silence,
        "verbose": sip.call_verbose,
    }

    logger.debug(f"Recording to {payload['record']}")
    logger.debug(f"POST {sip.operator_url}")  # supports basic-auth in URL
    resp = requests.post(sip.operator_url, json=payload, timeout=120)
    resp.raise_for_status()
    data: SipOperatorResponse = resp.json()
    logger.info(f"Call completed (HTTP {resp.status_code})")
    return data

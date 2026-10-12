"""Alert delivery: Matrix message plus optional SIP call to the pitmaster.

``deliver_once`` retries an alert message until it was sent and marks the
alert as done only then; the call is placed once.  Other modules call
``alerting.call_pitmaster(...)`` via the module, so tests can patch it in one
place.
"""

from typing import Protocol

from loguru import logger

from wachtmeater import cfg
from wachtmeater.watcher_state import WatcherState


class SyncSender(Protocol):
    """Synchronous sender callable: ``(text, image_path_or_None, *, html=None) -> None``."""

    def __call__(self, text: str, image_path: str | None, /, *, html: str | None = None) -> None:
        """Send *text* (plain body; *html* as optional ``formatted_body``) and an optional image."""
        ...


def call_pitmaster(message: str) -> bool:
    """Trigger a phone call to Pitmaster with the given alert message.

    Args:
        message: Alert text to deliver via call_pitmaster.

    Returns:
        ``True`` if the call succeeded, ``False`` otherwise.
    """
    from wachtmeater.call_pitmaster import call_pitmaster

    try:
        result = call_pitmaster(message)
        logger.info(f"Called Pitmaster ({cfg.sip.dest}): {message}")
        logger.debug(f"Response: {result}")
        return True
    except Exception as e:
        logger.error(f"Failed to call: {e}")
        return False


def send_alert(send: SyncSender | None, text: str, image_path: str | None = None, *, html: str | None = None) -> bool:
    """Post an alert message to Matrix and report whether it was delivered.

    Args:
        send: Sync sender, or ``None`` when no messaging backend is attached
            (then there is nothing to deliver and the alert counts as sent).
        text: Alert message body.
        image_path: Optional image to attach.
        html: Optional HTML rendering of *text* (``formatted_body``).

    Returns:
        ``True`` if the message was delivered (or there is no backend),
        ``False`` if sending raised — the caller retries on the next check.
    """
    if send is None:
        return True
    try:
        send(text, image_path, html=html)
        return True
    except Exception as e:
        logger.error(f"Matrix: Alarm-Nachricht nicht zugestellt ({e}) — neuer Versuch beim naechsten Check: {text!r}")
        return False


def deliver_once(
    state: WatcherState,
    key: str,
    text: str,
    send: SyncSender | None,
    call_text: str | None = None,
    image_path: str | None = None,
) -> bool:
    """Deliver a one-shot alert: Matrix message plus at most one SIP call.

    The caller sets its "already fired" flag only when this returns ``True``,
    so an undelivered message is retried on the next check.  The SIP call is
    placed once per alert episode — *key* is remembered in
    ``state.alert_calls_sent`` until the message got through, so retries do
    not ring the phone again.

    Args:
        state: Watcher state (``alert_calls_sent`` is updated).
        key: Alert identifier, e.g. ``"wrap"``.
        text: Matrix message body.
        send: Sync sender (may be ``None``).
        call_text: Text for the SIP call, or ``None`` for message-only alerts.
        image_path: Optional image to attach to the message.

    Returns:
        ``True`` once the message is delivered.
    """
    delivered = send_alert(send, text, image_path)
    if call_text is not None and key not in state.alert_calls_sent:
        call_pitmaster(call_text)
        state.alert_calls_sent.append(key)
    if delivered:
        clear_pending_call(state, key)
    return delivered


def clear_pending_call(state: WatcherState, key: str) -> None:
    """Forget that the SIP call for alert *key* was already placed.

    Args:
        state: Watcher state.
        key: Alert identifier.
    """
    state.alert_calls_sent = [k for k in state.alert_calls_sent if k != key]

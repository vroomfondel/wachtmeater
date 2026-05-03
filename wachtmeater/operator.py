"""Wachtmeater Operator — Matrix-driven controller for cook watchers.

Long-running pod that listens in a configured Matrix room
(``MATRIX_OPERATOR_LISTENING_ROOM``) for ``operator …`` commands and
spawns/destroys/lists ``meater-watcher-*`` Kubernetes Jobs in the
``meater`` namespace.

Recognised commands:

* ``operator new <MEATER_URL>``     — start a watcher job for *URL*.
* ``operator delete <spec>``        — delete a watcher; *spec* may be a
  full MEATER URL, a cook UUID, the 8-char short suffix, or a 1-based
  list index from a recent ``operator list``.
* ``operator list``                 — list active watcher jobs.
* ``operator status``               — operator's own health summary.
* ``operator help`` / ``hilfe``     — show this list.

Only the MXID configured as ``cfg.matrix.pitmaster`` may issue commands.
The operator runs under the same Matrix user as the watcher pods but
uses a separate crypto-store path
(``cfg.matrix.operator_crypto_store_path``) so the SQLite-backed nio
session does not race with watchers.  At startup the operator
auto-trusts every device of its own MXID via
``trust_devices_for_user`` so it can decrypt watcher messages in shared
rooms without manual verification.
"""

import asyncio
import re
import signal
import sys
from datetime import datetime, timedelta, timezone

from kubernetes import client
from kubernetes.client import ApiException
from loguru import logger

from wachtmeater import cfg, read_dot_env_to_environ
from wachtmeater.create_meater_watcher_job import (
    NAMESPACE,
    _load_kube_config,
    _short_uuid,
    create_resources,
    delete_resources,
)
from wachtmeater.messaging import MessagingBackend

read_dot_env_to_environ()


HELP_TEXT: str = """\
Wachtmeater Operator Befehle:

operator new <MEATER_URL>      - Neuen Watcher-Job starten
operator delete <spec>         - Watcher loeschen (URL, UUID, short, oder Listen-Index)
operator list                  - Aktive Watcher auflisten
operator status                - Operator-Health
operator help / hilfe          - Diese Hilfe anzeigen\
"""


class OperatorState:
    """In-memory state for the running operator process.

    Attributes:
        last_list: Job names from the most recent ``operator list``,
            so that a numeric index in ``operator delete <N>`` resolves
            back to a job.
        started_at: UTC timestamp the event loop entered.
    """

    def __init__(self) -> None:
        self.last_list: list[str] = []
        self.started_at: datetime = datetime.now(timezone.utc)


def _list_watcher_jobs() -> list[tuple[str, str, str]]:
    """Return ``(job_name, meater_url, status)`` for each active watcher job.

    The MEATER URL is recovered from the job's ``wachtmeater/meater-url``
    annotation (set by ``create_resources``).  The status is a one-word
    summary (``Active``, ``Succeeded``, ``Failed``, or ``Pending``).

    Returns:
        List of ``(job_name, meater_url, status)`` tuples sorted by name.
    """
    batch_v1 = client.BatchV1Api()
    resp = batch_v1.list_namespaced_job(NAMESPACE, label_selector=None)
    rows: list[tuple[str, str, str]] = []
    for job in resp.items:
        if not job.metadata.name.startswith("meater-watcher-"):
            continue
        annotations = job.metadata.annotations or {}
        url = annotations.get("wachtmeater/meater-url", "<unknown>")
        st = job.status
        if st and st.active:
            status = "Active"
        elif st and st.succeeded:
            status = "Succeeded"
        elif st and st.failed:
            status = "Failed"
        else:
            status = "Pending"
        rows.append((job.metadata.name, url, status))
    rows.sort(key=lambda r: r[0])
    return rows


def _resolve_delete_spec(spec: str, state: OperatorState) -> str | None:
    """Resolve a user-supplied delete spec into a MEATER URL.

    Accepts (in order of preference):

    1. A full MEATER URL (contains ``meater.com/cook/``).
    2. A 1-based numeric index into ``state.last_list``.
    3. A cook UUID (any string with ``-`` or that starts a job suffix).
    4. The 8-char short suffix used in job names.

    Args:
        spec: The token after ``operator delete``.
        state: Operator state, used for numeric-index lookups.

    Returns:
        The resolved MEATER URL, or ``None`` if no match was found.
    """
    spec = spec.strip()
    if "meater.com/cook/" in spec:
        return spec

    if spec.isdigit():
        idx = int(spec)
        if 1 <= idx <= len(state.last_list):
            target_job = state.last_list[idx - 1]
            for job_name, url, _status in _list_watcher_jobs():
                if job_name == target_job:
                    return url
        return None

    # UUID or short suffix — match against current jobs by short suffix.
    candidate_short = spec.replace("-", "")[:8].lower()
    for _job_name, url, _status in _list_watcher_jobs():
        if _short_uuid(url) == candidate_short:
            return url
    return None


def handle_operator_command(body: str, sender_mxid: str, state: OperatorState) -> str | None:
    """Parse a Matrix message and return a response string or sentinel.

    Args:
        body: Raw message text from the Matrix room.
        sender_mxid: Full MXID of the sender (for the pitmaster ACL).
        state: Mutable operator state; updated for index-based delete.

    Returns:
        A response string for the user, a sentinel (``"__op_new__\\n<url>"``,
        ``"__op_delete__\\n<url>"``, ``"__op_list__"``, ``"__op_status__"``),
        or ``None`` if the message is not an operator command.
    """
    raw = body.strip()
    if not re.match(r"^\s*operator\b", raw, re.IGNORECASE) and raw.lower() not in ("help", "hilfe", "?"):
        return None

    # Pitmaster ACL — applies to every recognised operator-prefixed command.
    allowed = cfg.matrix.pitmaster
    if not allowed:
        logger.warning("MATRIX_PITMASTER not configured — operator commands disabled")
        return "Operator-Befehle sind deaktiviert (MATRIX_PITMASTER nicht gesetzt)."
    if sender_mxid != allowed:
        logger.info(f"Operator-Befehl von {sender_mxid} ignoriert (nicht Pitmaster {allowed})")
        return None

    text = raw.lower()

    if text in ("help", "hilfe", "?", "operator help", "operator hilfe", "operator ?"):
        return HELP_TEXT

    m = re.match(r"^operator\s+new\s+(\S+)\s*$", raw, re.IGNORECASE)
    if m:
        url = m.group(1)
        if "meater.com/cook/" not in url:
            return f"Fehler: '{url}' sieht nicht wie eine MEATER-URL aus."
        return f"__op_new__\n{url}"

    m = re.match(r"^operator\s+delete\s+(\S+)\s*$", raw, re.IGNORECASE)
    if m:
        spec = m.group(1)
        return f"__op_delete__\n{spec}"

    if text == "operator list":
        return "__op_list__"

    if text == "operator status":
        return "__op_status__"

    return f"Unbekannter Operator-Befehl. {HELP_TEXT}"


def _format_list(rows: list[tuple[str, str, str]]) -> str:
    """Render ``operator list`` rows as a numbered text table."""
    if not rows:
        return "Keine aktiven Watcher-Jobs."
    lines = [f"{len(rows)} Watcher-Job(s):"]
    for i, (job, url, status) in enumerate(rows, start=1):
        short = job.removeprefix("meater-watcher-")
        lines.append(f"  [{i}] {short}  {status}  {url}")
    return "\n".join(lines)


def _format_uptime(delta: timedelta) -> str:
    """Render a ``timedelta`` as a compact ``2d 3h 15m 7s`` string.

    Drops leading zero units; always shows seconds so very short uptimes
    (``58s``) remain readable.
    """
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _format_status(state: OperatorState) -> str:
    """Render ``operator status`` summary."""
    uptime = _format_uptime(datetime.now(timezone.utc) - state.started_at)
    try:
        rows = _list_watcher_jobs()
        api_ok = True
    except ApiException:
        rows = []
        api_ok = False
    return (
        f"Wachtmeater Operator\n"
        f"Uptime: {uptime}\n"
        f"K8s API: {'OK' if api_ok else 'FEHLER'}\n"
        f"Aktive Watcher: {sum(1 for _, _, s in rows if s == 'Active')}/{len(rows)}\n"
        f"Listening room: {cfg.matrix.operator_listening_room}"
    )


async def event_loop() -> None:
    """Run the operator's Matrix listener until interrupted.

    Loads kube config, connects to Matrix, joins the operator listening
    room, auto-trusts the bot's own devices, registers the message
    callback, and blocks on the long-poll sync loop.
    """
    if not cfg.matrix.operator_listening_room:
        logger.error("MATRIX_OPERATOR_LISTENING_ROOM is empty — refusing to start")
        sys.exit(1)
    if not cfg.matrix.pitmaster:
        logger.warning("MATRIX_PITMASTER not configured — operator will reject all commands")

    _load_kube_config()
    op_state = OperatorState()

    from wachtmeater.matrix_adapter import MatrixMessagingAdapter

    messaging: MessagingBackend = MatrixMessagingAdapter(
        crypto_store_path=cfg.matrix.operator_crypto_store_path,
    )
    await messaging.connect()

    # Resolve and join the listening room (alias or ID).
    selection = await messaging.get_or_create_room(
        configured_room=cfg.matrix.operator_listening_room,
        auto_create=False,
        meater_uuid="",
        pitmaster_mxid=cfg.matrix.pitmaster,
        persisted_room_id=None,
    )
    if not selection.broadcast:
        logger.error(f"Konnte operator-Room {cfg.matrix.operator_listening_room!r} nicht joinen")
        sys.exit(1)
    operator_room = selection.broadcast
    logger.info(f"Operator listening in {operator_room}")

    bot_mxid = messaging.get_bot_user_id()
    logger.info(f"Operator: eingeloggt als {bot_mxid}")

    # Auto-trust our own devices so watcher messages decrypt without manual
    # verification.  Falls back gracefully if the backend doesn't support it.
    handler = getattr(messaging, "_handler", None)
    if handler is not None and hasattr(handler, "trust_devices_for_user"):
        try:
            await handler.trust_devices_for_user(bot_mxid)
            logger.info(f"Operator: vertraut allen Devices von {bot_mxid}")
        except Exception as e:
            logger.warning(f"trust_devices_for_user fehlgeschlagen: {e}")

    stop_event = asyncio.Event()

    async def on_message(incoming_room_id: str, sender: str, body: str, room_display_name: str) -> None:
        """Handle an incoming Matrix room message in the operator room."""
        if sender == bot_mxid:
            return
        if incoming_room_id != operator_room:
            return  # only react in the configured listening room

        logger.info(f"Operator [{room_display_name}] <{sender}>: {body}")

        response = handle_operator_command(body, sender, op_state)
        if response is None:
            return

        if response.startswith("__op_new__\n"):
            url = response.removeprefix("__op_new__\n")
            await messaging.send_message(operator_room, f"Starte Watcher fuer {url} ...")
            try:
                await asyncio.to_thread(create_resources, url)
                await messaging.send_message(operator_room, f"Watcher gestartet: {url}")
            except Exception as e:
                logger.exception("create_resources fehlgeschlagen")
                await messaging.send_message(operator_room, f"Fehler beim Anlegen: {e}")
            return

        if response.startswith("__op_delete__\n"):
            spec = response.removeprefix("__op_delete__\n")
            resolved = _resolve_delete_spec(spec, op_state)
            if resolved is None:
                await messaging.send_message(
                    operator_room,
                    f"Konnte '{spec}' nicht aufloesen (URL, UUID, short, oder Listen-Index nach 'operator list').",
                )
                return
            await messaging.send_message(operator_room, f"Loesche Watcher fuer {resolved} ...")
            try:
                await asyncio.to_thread(delete_resources, resolved)
                await messaging.send_message(operator_room, f"Watcher geloescht: {resolved}")
            except Exception as e:
                logger.exception("delete_resources fehlgeschlagen")
                await messaging.send_message(operator_room, f"Fehler beim Loeschen: {e}")
            return

        if response == "__op_list__":
            try:
                rows = await asyncio.to_thread(_list_watcher_jobs)
            except ApiException as e:
                await messaging.send_message(operator_room, f"K8s-API-Fehler: {e}")
                return
            op_state.last_list = [job for job, _url, _status in rows]
            await messaging.send_message(operator_room, _format_list(rows))
            return

        if response == "__op_status__":
            await messaging.send_message(operator_room, _format_status(op_state))
            return

        # Plain string response (help text, error)
        await messaging.send_message(operator_room, response)

    messaging.register_message_callback(on_message)

    # Greeting
    try:
        await messaging.send_message(
            operator_room,
            f"Wachtmeater Operator gestartet.\n\n{HELP_TEXT}",
        )
    except Exception as e:
        logger.warning(f"Startup-Gruss fehlgeschlagen: {e}")

    sync_task = asyncio.create_task(messaging.start_sync())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {sync_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    messaging.stop_sync()
    await messaging.close()


def main() -> None:
    """Entry point for ``wachtmeater operator``."""
    loop = asyncio.new_event_loop()

    def _handle_signal() -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    logger.info("Starting operator event loop...")
    try:
        loop.run_until_complete(event_loop())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        logger.info("Operator event loop stopped.")

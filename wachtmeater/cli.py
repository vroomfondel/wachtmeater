"""Unified CLI for wachtmeater — MEATER BBQ monitoring suite.

Provides sub-commands for continuous monitoring, one-shot status checks,
SIP phone-call alerts, Matrix messaging, and Kubernetes job management.

Example:
    Start the persistent watcher event loop::

        $ wachtmeater watcher

    Start the watcher but skip the initial SIP test call::

        $ wachtmeater watcher --skip-startup-test-call

    Run a single-shot MEATER status check (explicit URL)::

        $ wachtmeater monitor https://app.meater.com/cooks/abc123

    Run a single-shot check using the ``MEATER_URL`` env var::

        $ export MEATER_URL=https://app.meater.com/cooks/abc123
        $ wachtmeater monitor

    Trigger a SIP phone call to the pitmaster::

        $ wachtmeater call "Brisket is done!"

    Send a text message to all configured Matrix rooms::

        $ wachtmeater send-matrix "Internal temp reached 96 °C"

    Send a message with a screenshot to a specific room::

        $ wachtmeater send-matrix "Status update" --image /tmp/screenshot.png --room '!abcdef:matrix.org'

    Deploy a MEATER watcher Kubernetes job::

        $ wachtmeater deploy --meater-url https://app.meater.com/cooks/abc123

    Delete the Kubernetes resources for a finished cook::

        $ wachtmeater deploy --meater-url https://app.meater.com/cooks/abc123 --delete
"""

import argparse
import os
import sys

from wachtmeater import configure_logging, glogger, print_banner, read_dot_env_to_environ


from loguru import logger


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser for wachtmeater.

    Returns:
        An ``argparse.ArgumentParser`` with sub-commands for ``watcher``,
        ``monitor``, ``call``, ``send-matrix``, and ``deploy``.
    """
    parser = argparse.ArgumentParser(
        prog="wachtmeater",
        description="MEATER BBQ probe monitor with Matrix alerts and SIP call notifications.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # watcher
    sp_w = sub.add_parser("watcher", help="Run persistent watcher event loop with Matrix listener")
    sp_w.add_argument(
        "--skip-startup-test-call",
        action="store_true",
        help="Skip the startup test call on startup",
    )

    # monitor
    sp_m = sub.add_parser("monitor", help="Single-shot MEATER status check via CDP")
    sp_m.add_argument(
        "cook_url",
        nargs="?",
        default=None,
        help="MEATER Cloud cook URL (default: $MEATER_URL)",
    )

    # call
    sp_c = sub.add_parser("call", help="Trigger a SIP phone call via sipstuff-operator")
    sp_c.add_argument("text", nargs="+", help="Text to speak")

    # send-matrix
    sp_sm = sub.add_parser("send-matrix", help="Send a message (and optional screenshot) to Matrix rooms")
    sp_sm.add_argument("status_text", help="Status text to send")
    sp_sm.add_argument("--image", help="Path to screenshot image")
    sp_sm.add_argument("--room", help="Send only to this room ID")

    # deploy
    sp_d = sub.add_parser("deploy", help="Deploy/delete MEATER watcher K8s Job")
    sp_d.add_argument("--meater-url", required=True, help="MEATER cook URL")
    sp_d.add_argument("--delete", action="store_true", help="Delete resources for this cook")

    return parser


def main() -> None:
    """Entry point for the wachtmeater CLI."""
    os.environ.setdefault("LOGURU_LEVEL", "DEBUG")
    configure_logging()
    glogger.enable("wachtmeater")

    read_dot_env_to_environ()
    print_banner()

    parser = build_parser()
    args = parser.parse_args()
    logger.debug(f"CLI command: {args.command}")

    if args.command == "watcher":
        import asyncio
        import signal

        from wachtmeater.meater_watcher import WatcherError, event_loop

        def _handle_signal() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()

        loop = asyncio.new_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_signal)
        logger.info("Starting watcher event loop...")
        try:
            loop.run_until_complete(event_loop(args.skip_startup_test_call))
        except asyncio.CancelledError:
            pass
        except WatcherError as exc:
            logger.error(f"Watcher error: {exc}")
            sys.exit(1)
        finally:
            loop.close()
            logger.info("Watcher event loop stopped.")

    elif args.command == "monitor":
        import json

        from wachtmeater.meater_monitor import extract_via_browser

        url = args.cook_url or os.environ.get("MEATER_URL")
        if not url:
            logger.error("No cook URL provided (pass as argument or set MEATER_URL)")
            sys.exit(1)
        logger.info("Running single-shot MEATER monitor check...")
        cook = extract_via_browser(url)
        output = cook._asdict()
        output["url"] = url
        output["source"] = "playwright/browser"
        print(json.dumps(output, indent=2, ensure_ascii=False))

    elif args.command == "call":
        import json

        import requests

        from wachtmeater.call_pitmaster import call_pitmaster

        text = " ".join(args.text)
        logger.info(f"Triggering SIP call: {text!r}")
        try:
            result = call_pitmaster(text)
            logger.info(json.dumps(result, indent=2))
        except requests.RequestException as exc:
            logger.error(f"SIP call failed: {exc}")
            sys.exit(1)

    elif args.command == "send-matrix":
        import asyncio

        from wachtmeater.matrix_adapter import MatrixMessagingAdapter

        logger.info("Sending to Matrix...")
        adapter = MatrixMessagingAdapter()
        asyncio.run(adapter.send_one(args.status_text, args.image, args.room))

    elif args.command == "deploy":
        from wachtmeater.create_meater_watcher_job import create_resources, delete_resources

        logger.info(f"{'Deleting' if args.delete else 'Creating'} K8s resources for {args.meater_url}...")
        if args.delete:
            delete_resources(args.meater_url)
        else:
            create_resources(args.meater_url)


if __name__ == "__main__":
    main()

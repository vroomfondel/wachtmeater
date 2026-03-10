"""Tests for wachtmeater/cli.py — parser and exit-code behaviour."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from wachtmeater.cli import build_parser, main


def test_watcher_defaults() -> None:
    args = build_parser().parse_args(["watcher"])
    assert args.command == "watcher"
    assert args.skip_startup_test_call is False


def test_watcher_skip_startup_test_call() -> None:
    args = build_parser().parse_args(["watcher", "--skip-startup-test-call"])
    assert args.skip_startup_test_call is True


def test_monitor_no_url() -> None:
    args = build_parser().parse_args(["monitor"])
    assert args.command == "monitor"
    assert args.cook_url is None


def test_monitor_with_url() -> None:
    url = "https://cooks.cloud.meater.com/cook/abc123"
    args = build_parser().parse_args(["monitor", url])
    assert args.cook_url == url


def test_call_text() -> None:
    args = build_parser().parse_args(["call", "hello", "world"])
    assert args.command == "call"
    assert args.text == ["hello", "world"]


def test_deploy_meater_url() -> None:
    url = "https://cooks.cloud.meater.com/cook/abc123"
    args = build_parser().parse_args(["deploy", "--meater-url", url])
    assert args.command == "deploy"
    assert args.meater_url == url
    assert args.delete is False


def test_deploy_delete() -> None:
    url = "https://cooks.cloud.meater.com/cook/abc123"
    args = build_parser().parse_args(["deploy", "--meater-url", url, "--delete"])
    assert args.delete is True
    assert args.meater_url == url


def test_no_subcommand_raises() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ============================================================================
# Exit-code behaviour for watcher subcommand
# ============================================================================


class TestWatcherExitCodes:
    """Verify that WatcherError → exit 1, clean return → exit 0."""

    @staticmethod
    def _make_mock_meater_watcher(event_loop_fn: object, error_cls: type | None = None) -> types.ModuleType:
        """Build a fake ``wachtmeater.meater_watcher`` module."""
        mod = types.ModuleType("wachtmeater.meater_watcher")
        mod.event_loop = event_loop_fn  # type: ignore[attr-defined]
        if error_cls is None:
            error_cls = type("WatcherError", (Exception,), {})
        mod.WatcherError = error_cls  # type: ignore[attr-defined]
        return mod

    def test_watcher_error_exits_1(self) -> None:
        WatcherError = type("WatcherError", (Exception,), {})

        async def _boom(skip: bool) -> None:
            raise WatcherError("fatal")

        fake_mod = self._make_mock_meater_watcher(_boom, WatcherError)

        with (
            patch("sys.argv", ["wachtmeater", "watcher"]),
            patch.dict(sys.modules, {"wachtmeater.meater_watcher": fake_mod}),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1

    def test_clean_return_exits_0(self) -> None:
        async def _ok(skip: bool) -> None:
            return

        fake_mod = self._make_mock_meater_watcher(_ok)

        with (
            patch("sys.argv", ["wachtmeater", "watcher"]),
            patch.dict(sys.modules, {"wachtmeater.meater_watcher": fake_mod}),
        ):
            # Should NOT raise SystemExit — clean return means exit 0
            main()

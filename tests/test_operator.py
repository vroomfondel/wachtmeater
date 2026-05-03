"""Tests for wachtmeater/operator.py.

Requires kubernetes (imported transitively via create_meater_watcher_job).
"""

from unittest.mock import MagicMock, patch

import pytest

op = pytest.importorskip("wachtmeater.operator")

from wachtmeater.operator import OperatorState  # noqa: E402  (must follow importorskip)

PITMASTER = "@pitmaster:matrix.example.com"
OTHER_USER = "@stranger:matrix.example.com"


@pytest.fixture
def state() -> OperatorState:
    return OperatorState()


@pytest.fixture(autouse=True)
def _set_pitmaster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: pitmaster ACL is configured."""
    monkeypatch.setattr(op.cfg.matrix, "pitmaster", PITMASTER)


# ============================================================================
# handle_operator_command — ACL
# ============================================================================


class TestACL:
    """Tests for the pitmaster MXID gate."""

    def test_non_pitmaster_ignored(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator list", OTHER_USER, state) is None

    def test_pitmaster_passes(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator list", PITMASTER, state) == "__op_list__"

    def test_pitmaster_unset_disables_commands(
        self,
        state: OperatorState,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(op.cfg.matrix, "pitmaster", "")
        result = op.handle_operator_command("operator list", PITMASTER, state)
        assert result is not None
        assert "deaktiviert" in result.lower()

    def test_non_operator_message_passes_through(self, state: OperatorState) -> None:
        # Plain chatter must return None — both for stranger and pitmaster.
        assert op.handle_operator_command("hallo welt", OTHER_USER, state) is None
        assert op.handle_operator_command("hallo welt", PITMASTER, state) is None


# ============================================================================
# handle_operator_command — command parsing
# ============================================================================


class TestCommandParsing:
    """Tests for parsing the recognised operator commands."""

    URL = "https://cooks.cloud.meater.com/cook/abc-123-def"

    def test_help(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator help", PITMASTER, state) == op.HELP_TEXT

    def test_hilfe(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator hilfe", PITMASTER, state) == op.HELP_TEXT

    def test_bare_help_passes(self, state: OperatorState) -> None:
        # Plain "help" without "operator" prefix should be treated as operator help
        # only when sender is pitmaster (ACL is on the operator-prefixed branch).
        # We accept it here because the fall-through allows the help text.
        assert op.handle_operator_command("help", PITMASTER, state) == op.HELP_TEXT

    def test_new_returns_sentinel(self, state: OperatorState) -> None:
        result = op.handle_operator_command(f"operator new {self.URL}", PITMASTER, state)
        assert result == f"__op_new__\n{self.URL}"

    def test_new_rejects_non_meater_url(self, state: OperatorState) -> None:
        result = op.handle_operator_command("operator new https://example.com/foo", PITMASTER, state)
        assert result is not None
        assert "MEATER" in result

    def test_new_case_insensitive_keyword(self, state: OperatorState) -> None:
        result = op.handle_operator_command(f"OPERATOR NEW {self.URL}", PITMASTER, state)
        assert result == f"__op_new__\n{self.URL}"

    def test_delete_returns_sentinel(self, state: OperatorState) -> None:
        result = op.handle_operator_command("operator delete abc-123", PITMASTER, state)
        assert result == "__op_delete__\nabc-123"

    def test_list_returns_sentinel(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator list", PITMASTER, state) == "__op_list__"

    def test_status_returns_sentinel(self, state: OperatorState) -> None:
        assert op.handle_operator_command("operator status", PITMASTER, state) == "__op_status__"

    def test_unknown_operator_command(self, state: OperatorState) -> None:
        result = op.handle_operator_command("operator dance", PITMASTER, state)
        assert result is not None
        assert "Unbekannter" in result


# ============================================================================
# _resolve_delete_spec
# ============================================================================


class TestResolveDeleteSpec:
    """Tests for _resolve_delete_spec(spec, state)."""

    URL_A = "https://cooks.cloud.meater.com/cook/aaaaaaaa-1111-2222-3333-444444444444"
    URL_B = "https://cooks.cloud.meater.com/cook/bbbbbbbb-5555-6666-7777-888888888888"

    def test_full_url_passes_through(self, state: OperatorState) -> None:
        assert op._resolve_delete_spec(self.URL_A, state) == self.URL_A

    @patch.object(op, "_list_watcher_jobs")
    def test_short_suffix_resolves(self, mock_list: MagicMock, state: OperatorState) -> None:
        mock_list.return_value = [
            ("meater-watcher-aaaaaaaa", self.URL_A, "Active"),
            ("meater-watcher-bbbbbbbb", self.URL_B, "Active"),
        ]
        assert op._resolve_delete_spec("aaaaaaaa", state) == self.URL_A
        assert op._resolve_delete_spec("bbbbbbbb", state) == self.URL_B

    @patch.object(op, "_list_watcher_jobs")
    def test_uuid_resolves_via_short(self, mock_list: MagicMock, state: OperatorState) -> None:
        mock_list.return_value = [
            ("meater-watcher-aaaaaaaa", self.URL_A, "Active"),
        ]
        # Full UUID with dashes — _short_uuid strips dashes and takes first 8 chars
        assert op._resolve_delete_spec("aaaaaaaa-1111-2222-3333-444444444444", state) == self.URL_A

    @patch.object(op, "_list_watcher_jobs")
    def test_index_resolves_from_last_list(
        self,
        mock_list: MagicMock,
        state: OperatorState,
    ) -> None:
        # Simulate a prior `operator list` having populated state.last_list.
        state.last_list = ["meater-watcher-aaaaaaaa", "meater-watcher-bbbbbbbb"]
        mock_list.return_value = [
            ("meater-watcher-aaaaaaaa", self.URL_A, "Active"),
            ("meater-watcher-bbbbbbbb", self.URL_B, "Active"),
        ]
        assert op._resolve_delete_spec("1", state) == self.URL_A
        assert op._resolve_delete_spec("2", state) == self.URL_B

    def test_index_out_of_range(self, state: OperatorState) -> None:
        state.last_list = ["meater-watcher-aaaaaaaa"]
        assert op._resolve_delete_spec("5", state) is None

    @patch.object(op, "_list_watcher_jobs", return_value=[])
    def test_unknown_short_returns_none(self, _mock: MagicMock, state: OperatorState) -> None:
        assert op._resolve_delete_spec("ffffffff", state) is None


# ============================================================================
# _format_list
# ============================================================================


class TestFormatList:
    def test_empty(self) -> None:
        assert "Keine" in op._format_list([])

    def test_numbered(self) -> None:
        rows = [
            ("meater-watcher-aaaaaaaa", "https://cooks.cloud.meater.com/cook/aaa", "Active"),
            ("meater-watcher-bbbbbbbb", "https://cooks.cloud.meater.com/cook/bbb", "Failed"),
        ]
        out = op._format_list(rows)
        assert "[1]" in out
        assert "[2]" in out
        assert "aaaaaaaa" in out
        assert "Active" in out
        assert "Failed" in out


# ============================================================================
# _format_status
# ============================================================================


class TestFormatStatus:
    @patch.object(op, "_list_watcher_jobs")
    def test_smoke(self, mock_list: MagicMock, state: OperatorState) -> None:
        mock_list.return_value = [
            ("meater-watcher-aaaaaaaa", "https://cooks.cloud.meater.com/cook/aaa", "Active"),
            ("meater-watcher-bbbbbbbb", "https://cooks.cloud.meater.com/cook/bbb", "Succeeded"),
        ]
        out = op._format_status(state)
        assert "Operator" in out
        assert "Aktive Watcher: 1/2" in out

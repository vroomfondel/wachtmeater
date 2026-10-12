"""Tests for the help table (plain text + HTML rendering)."""

from wachtmeater import commands, operator
from wachtmeater.help_table import HelpTable, html_with_help

TABLE = HelpTable(
    "Befehle:",
    (
        ("status", "Status abrufen"),
        ("enable pull <temp>", "Rausnehmen-Alarm " + "sehr lange Beschreibung " * 6),
    ),
)


def test_text_aligns_commands_and_wraps_under_the_description_column() -> None:
    lines = TABLE.text.splitlines()
    assert lines[:2] == ["Befehle:", ""]
    assert lines[2] == "status             - Status abrufen"
    assert lines[3].startswith("enable pull <temp> - Rausnehmen-Alarm sehr lange")
    assert all(len(line) <= 120 for line in lines)
    # continuation lines start exactly below the description
    assert lines[4].startswith(" " * 21) and lines[4][21] != " "


def test_html_is_an_escaped_table() -> None:
    html = TABLE.html
    assert html.startswith("<p><strong>Befehle:</strong></p><table><thead>")
    assert "<tr><td><code>enable pull &lt;temp&gt;</code></td><td>Rausnehmen-Alarm sehr" in html
    assert html.endswith("</tbody></table>")
    assert "<temp>" not in html


def test_html_with_help_keeps_surrounding_text() -> None:
    msg = f"Unbekannt <x>.\n\n{TABLE.text}\nEnde"
    assert html_with_help(msg, TABLE) == f"Unbekannt &lt;x&gt;.<br><br>{TABLE.html}<br>Ende"


def test_html_with_help_none_without_help() -> None:
    assert html_with_help("Pause beendet.", TABLE) is None


def test_watcher_and_operator_help_have_one_row_per_command() -> None:
    assert commands.HELP_TEXT == commands.HELP.text
    assert html_with_help(commands.HELP_TEXT, commands.HELP) == commands.HELP.html
    assert commands.HELP.html.count("<tr>") == len(commands.HELP.rows) + 1  # + header row
    assert ("operator list", "Aktive Watcher auflisten") in operator.HELP.rows

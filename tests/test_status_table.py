"""Tests for status messages as plain text and HTML tables."""

from wachtmeater.status_table import StatusMessage, StatusRow

STALL = StatusRow(
    "Stall",
    "AN (< 4 C/h)",
    ("Anstieg ueber 30 Min.", "bereits ausgeloest"),
    plain="Stall: AN (Alarm bei < 4 C/h Anstieg ueber 30 Min.) [bereits ausgeloest]",
)
MSG = StatusMessage(
    "MEATER Status: <Brisket>",
    ((StatusRow("Internal (Fleisch)", "70.0 C"),), (STALL, StatusRow("Time", "12:00"))),
)


def test_text_keeps_one_line_per_row_and_separator() -> None:
    assert MSG.text == (
        "MEATER Status: <Brisket>\n"
        "Internal (Fleisch): 70.0 C\n"
        "---\n"
        "Stall: AN (Alarm bei < 4 C/h Anstieg ueber 30 Min.) [bereits ausgeloest]\n"
        "Time: 12:00"
    )


def test_html_has_heading_and_one_table_per_section() -> None:
    html = MSG.html
    assert html.startswith("<p><strong>MEATER Status: &lt;Brisket&gt;</strong></p><table><tbody>")
    assert html.count("<table>") == 2
    assert "<tr><td><strong>Internal (Fleisch)</strong></td><td>70.0 C</td></tr>" in html


def test_notes_are_short_lines_below_the_value() -> None:
    assert STALL.html == (
        "<tr><td><strong>Stall</strong></td>"
        "<td>AN (&lt; 4 C/h)<br><em>Anstieg ueber 30 Min.</em><br><em>bereits ausgeloest</em></td></tr>"
    )


def test_url_becomes_short_link() -> None:
    row = StatusRow("URL", "https://cooks.cloud.meater.com/cook/x?a=1&b=2")
    assert row.text == "URL: https://cooks.cloud.meater.com/cook/x?a=1&b=2"
    assert '<a href="https://cooks.cloud.meater.com/cook/x?a=1&amp;b=2">MEATER Cloud</a>' in row.html


def test_empty_sections_are_skipped() -> None:
    msg = StatusMessage("T", ((StatusRow("A", "1"),), ()))
    assert msg.text == "T\nA: 1"
    assert msg.html.count("<table>") == 1

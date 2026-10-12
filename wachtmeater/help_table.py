"""Command help as a two-column table, rendered as plain text and as HTML.

Matrix clients wrap the plain-text help at arbitrary points, which breaks the
column layout.  The help is therefore kept as ``(command, description)`` rows
and sent twice in one event: the plain text as ``body`` (fallback,
notifications) and an HTML ``<table>`` as ``formatted_body``, which Element
renders with proper columns.
"""

import html
import textwrap
from dataclasses import dataclass

HelpRow = tuple[str, str]
"""One help entry: ``(command syntax, description)``."""

TEXT_WIDTH: int = 120
"""Line width of the plain-text help (descriptions wrap below their column)."""


@dataclass(frozen=True)
class HelpTable:
    """A titled list of commands with their descriptions.

    Attributes:
        title: Heading line, e.g. ``"MEATER Watcher Befehle:"``.
        rows: The commands, in display order.
    """

    title: str
    rows: tuple[HelpRow, ...]

    @property
    def text(self) -> str:
        """Plain-text rendering: command column padded, descriptions wrapped beneath their column."""
        width = max(len(cmd) for cmd, _ in self.rows) + 1
        indent = " " * (width + 2)
        lines = [self.title, ""]
        for cmd, desc in self.rows:
            lines.append(
                textwrap.fill(
                    desc,
                    width=TEXT_WIDTH,
                    initial_indent=f"{cmd:<{width}}- ",
                    subsequent_indent=indent,
                    break_on_hyphens=False,
                )
            )
        return "\n".join(lines)

    @property
    def html(self) -> str:
        """HTML rendering as a ``<table>`` (subset allowed in Matrix ``formatted_body``)."""
        body = "".join(
            f"<tr><td><code>{html.escape(cmd)}</code></td><td>{html.escape(desc)}</td></tr>" for cmd, desc in self.rows
        )
        return (
            f"<p><strong>{html.escape(self.title)}</strong></p>"
            f"<table><thead><tr><th>Befehl</th><th>Beschreibung</th></tr></thead><tbody>{body}</tbody></table>"
        )


def html_with_help(message: str, table: HelpTable) -> str | None:
    """Build the HTML form of a message that embeds the plain-text help.

    The help text inside *message* is replaced by the HTML table; the text
    around it is escaped, with line breaks kept.

    Args:
        message: Outgoing message text.
        table: The help table whose ``text`` may be embedded in *message*.

    Returns:
        The HTML ``formatted_body``, or ``None`` if *message* does not
        contain the help.
    """
    before, found, after = message.partition(table.text)
    if not found:
        return None
    return _text_html(before) + table.html + _text_html(after)


def _text_html(text: str) -> str:
    """Escape plain text for HTML, keeping line breaks."""
    return html.escape(text).replace("\n", "<br>")

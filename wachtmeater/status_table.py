"""Multi-line status messages (status, eta, offline) as plain text and HTML.

Matrix clients render the plain text with arbitrary line wraps, and some
(FluffyChat) do not wrap table cells at all, so one long cell pushes the
whole value column out of view.  A message is therefore kept as rows with a
short label, a short value and optional notes: the HTML ``formatted_body``
shows each row as a two-column table row with the notes as extra short lines
under the value; the plain-text ``body`` keeps the classic one-line form.
"""

import html
from dataclasses import dataclass

SEPARATOR: str = "---"
"""Plain-text line between two sections (one HTML table each)."""


@dataclass(frozen=True)
class StatusRow:
    """One status entry.

    Attributes:
        label: Short label (left column), e.g. ``"Stall"``.
        value: Short value (right column), e.g. ``"AN (< 4 C/h)"``.
        notes: Details shown as separate short lines below the value (HTML only).
        plain: Plain-text form when it is not just ``"label: value"`` (may
            span several lines).
    """

    label: str
    value: str
    notes: tuple[str, ...] = ()
    plain: str | None = None

    @property
    def text(self) -> str:
        """Plain-text rendering of the row."""
        return self.plain if self.plain is not None else f"{self.label}: {self.value}"

    @property
    def html(self) -> str:
        """HTML table row: bold label, value (a URL becomes a link) plus one line per note."""
        cell = html.escape(self.value)
        if self.value.startswith(("http://", "https://")):
            cell = f'<a href="{html.escape(self.value, quote=True)}">MEATER Cloud</a>'
        cell += "".join(f"<br><em>{html.escape(note)}</em>" for note in self.notes)
        return f"<tr><td><strong>{html.escape(self.label)}</strong></td><td>{cell}</td></tr>"


@dataclass(frozen=True)
class StatusMessage:
    """A titled status message made of one or more row sections.

    Attributes:
        title: Heading line, e.g. ``"MEATER Status: Brisket"``.
        sections: Row groups, separated by ``SEPARATOR`` in plain text.
    """

    title: str
    sections: tuple[tuple[StatusRow, ...], ...]

    @property
    def text(self) -> str:
        """Plain-text rendering: title, then one line per row, sections split by ``SEPARATOR``."""
        blocks = ["\n".join(row.text for row in rows) for rows in self.sections if rows]
        return "\n".join([self.title, f"\n{SEPARATOR}\n".join(blocks)])

    @property
    def html(self) -> str:
        """HTML rendering: bold title and one ``<table>`` per section."""
        tables = "".join(
            f"<table><tbody>{''.join(row.html for row in rows)}</tbody></table>" for rows in self.sections if rows
        )
        return f"<p><strong>{html.escape(self.title)}</strong></p>{tables}"

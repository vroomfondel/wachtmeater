"""Read watcher readings from a Wachtmeater Element chat export.

Special use for after-the-fact analysis (e.g. ``wachtmeater eta``): the
watcher's status messages in an exported Matrix room are parsed back into
``(epoch, core, ambient)`` readings for ``wachtmeater.core_forecast``.
The live watcher does not need this — it uses its own samples.
"""

import json
import re
import zipfile
from pathlib import Path

from wachtmeater.core_forecast import Reading

_STATUS_RE = re.compile(r"Internal \(Fleisch\): ([\d.]+) C\s+(?:\| )?Ambient \(Rauch\): ([\d.]+) C")


def readings_from_element_export(path: str | Path) -> list[Reading]:
    """Extract core/ambient readings from a Wachtmeater Element chat export.

    Args:
        path: The export ZIP or its ``export.json``.

    Returns:
        Readings from the watcher's status messages, oldest first.

    Raises:
        ValueError: If the export contains no status messages.
    """
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith("export.json"))
            data = json.loads(z.read(name))
    else:
        data = json.loads(path.read_text())

    readings: list[Reading] = []
    for event in data.get("messages", []):
        body = event.get("content", {}).get("body", "")
        m = _STATUS_RE.search(body)
        if m:
            readings.append((event["origin_server_ts"] / 1000, float(m[1]), float(m[2])))
    if not readings:
        raise ValueError("Keine Statusnachrichten im Export gefunden.")
    readings.sort(key=lambda r: r[0])
    return readings

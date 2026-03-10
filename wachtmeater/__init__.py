"""wachtmeater — MEATER BBQ monitoring suite with Matrix alerts and SIP calls.

Provides shared logging configuration, environment loading, and a startup
banner used by all wachtmeater sub-commands.
"""

__version__ = "0.0.1"

import json
import logging
import os
import sys
import tomllib
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger as glogger

from .config import SECTION_REGISTRY, WachtmeaterConfig

__all__ = ["cfg", "configure_logging", "glogger", "print_banner", "read_dot_env_to_environ"]

cfg: WachtmeaterConfig = WachtmeaterConfig.from_environ()


class _InterceptHandler(logging.Handler):
    """Route standard ``logging`` records into loguru.

    Installed on the root logger so that libraries using the stdlib
    ``logging`` module (e.g. ``matrix-nio``) have their output formatted
    and coloured consistently by loguru instead of printing raw to stderr.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        """Forward a stdlib log record to loguru."""
        # Map stdlib level to loguru level name
        try:
            level: str | int = glogger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk the stack to find the frame depth outside of the logging module
        # so that loguru reports the correct caller location.
        current: types.FrameType | None = logging.currentframe()
        depth = 0
        while current is not None:
            if current.f_code.co_filename != logging.__file__:
                depth += 1
                if depth > 1:
                    break
            current = current.f_back

        glogger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


from tabulate import tabulate

glogger.disable(__name__)


def _loguru_skiplog_filter(record: dict[str, Any]) -> bool:
    """Filter function to hide records with ``extra['skiplog']`` set.

    Args:
        record: A loguru log record dict.

    Returns:
        ``False`` when the record carries ``extra['skiplog'] == True``,
        suppressing it from the sink; ``True`` otherwise.
    """
    return not record.get("extra", {}).get("skiplog", False)


LOGURU_FORMAT: str = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


def configure_logging(
    loguru_filter: Callable[[dict[str, Any]], bool] = _loguru_skiplog_filter,
) -> None:
    """Configure a default ``loguru`` sink with a convenient format and filter.

    Removes all existing sinks, then adds a single ``sys.stderr`` sink
    using ``LOGURU_FORMAT`` and the given *loguru_filter*.  The log level
    is read from the ``LOGURU_LEVEL`` environment variable (defaulting to
    ``DEBUG``).

    Args:
        loguru_filter: A callable accepting a loguru record dict and
            returning ``True`` to keep or ``False`` to suppress the
            record.  Defaults to ``_loguru_skiplog_filter``.
    """
    os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")
    glogger.remove()
    glogger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=LOGURU_FORMAT, filter=loguru_filter)  # type: ignore[arg-type]
    glogger.configure(extra={"classname": "None", "skiplog": False})

    # Intercept stdlib logging (e.g. matrix-nio) → loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING, force=True)


def print_banner() -> None:
    """Log a startup banner with version, build time, and project URLs.

    Renders a ``tabulate`` mixed-grid table with a Unicode box-drawing
    title row and emits it via loguru in raw mode.
    """
    startup_rows = [
        ["version", __version__],
        ["buildtime", os.environ.get("BUILDTIME", "n/a")],
        ["github", "https://github.com/vroomfondel/wachtmeater"],
        ["Docker Hub", "https://hub.docker.com/r/xomoxcc/wachtmeater"],
    ]
    table_str = tabulate(startup_rows, tablefmt="mixed_grid")
    lines = table_str.split("\n")
    table_width = len(lines[0])
    title = "wachtmeater starting up"
    title_border = "\u250d" + "\u2501" * (table_width - 2) + "\u2511"
    title_row = "\u2502 " + title.center(table_width - 4) + " \u2502"
    separator = lines[0].replace("\u250d", "\u251d").replace("\u2511", "\u2525").replace("\u252f", "\u253f")

    glogger.opt(raw=True).info(
        "\n{}\n", title_border + "\n" + title_row + "\n" + separator + "\n" + "\n".join(lines[1:])
    )


def _load_flat_env_file(env_file: Path) -> None:
    """Parse a flat ``KEY=VALUE`` file into ``os.environ`` via ``setdefault``.

    Blank lines, comment lines (starting with ``#``), and lines without
    an ``=`` are silently skipped.  Surrounding quotes on values are
    stripped.

    Args:
        env_file: Path to the flat environment file.
    """
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _load_toml_config_file(toml_file: Path) -> None:
    """Parse a TOML config file, mapping section keys to env vars via field metadata.

    Known sections (registered in ``SECTION_REGISTRY``) have their TOML
    keys mapped to environment variable names through dataclass field
    metadata.  Unknown sections fall back to uppercasing the key as the
    env var name.

    Args:
        toml_file: Path to the TOML configuration file.
    """
    from dataclasses import fields as dc_fields

    data = tomllib.loads(toml_file.read_text())
    for section_name, section_dict in data.items():
        if not isinstance(section_dict, dict):
            continue
        config_cls = SECTION_REGISTRY.get(section_name)
        if config_cls is None:
            # Unknown section: treat key as env var name (uppercase)
            for key, value in section_dict.items():
                os.environ.setdefault(
                    key.upper(),
                    json.dumps(value) if isinstance(value, list) else str(value),
                )
            continue
        # Known section: map TOML key -> env var via field metadata
        field_map = {f.name: f.metadata["env"] for f in dc_fields(config_cls) if "env" in f.metadata}
        for toml_key, value in section_dict.items():
            env_var = field_map.get(toml_key)
            if env_var:
                os.environ.setdefault(
                    env_var,
                    json.dumps(value) if isinstance(value, list) else str(value),
                )


def read_dot_env_to_environ() -> None:
    """Load config from TOML or flat .env files into os.environ.

    Searches in the package directory and the current working directory.
    Existing environment variables are **not** overridden (uses
    ``os.environ.setdefault``).

    After loading, constructs the typed ``cfg`` global from ``os.environ``.
    """
    global cfg

    cf: str | None = os.environ.get("CONFIG")

    if cf and Path(cf).is_file():
        if Path(cf).suffix == ".toml":
            _load_toml_config_file(Path(cf))
        else:
            _load_flat_env_file(Path(cf))
    else:
        pkg = Path(__file__).resolve().parent
        cwd = Path.cwd()
        _env_files: list[Path] = [
            pkg / "wachtmeater.toml",
            cwd / "wachtmeater.toml",
            pkg / ".env",
            cwd / ".env",
            pkg / ".local.env",
            cwd / ".local.env",
            pkg / "wachtmeater.local.toml",
            cwd / "wachtmeater.local.toml",
        ]

        for _env_file in _env_files:
            if _env_file.is_file():
                glogger.debug(f"Found config file: {_env_file}")
                if _env_file.suffix == ".toml":
                    _load_toml_config_file(_env_file)
                else:
                    _load_flat_env_file(_env_file)

    cfg = WachtmeaterConfig.from_environ()

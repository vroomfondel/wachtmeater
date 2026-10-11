"""Wall-clock helpers shared by the watcher modules.

``now`` is called as ``timeutil.now()`` everywhere, so tests can freeze the
clock by patching it in one place.
"""

import time
from datetime import datetime, timedelta


def now() -> float:
    """Return the current wall-clock time as epoch seconds.

    Wall clock (not monotonic) because samples and the pause deadline are
    persisted across watcher restarts.  Patched in tests to replay a cook.

    Returns:
        Seconds since the epoch.
    """
    return time.time()


def clock(ts: float, now: float) -> str:
    """Format an epoch as ``HH:MM``, with the date when it is not today.

    Args:
        ts: Epoch seconds to format.
        now: Current epoch seconds (defines "today").

    Returns:
        Local wall-clock string.
    """
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M" if dt.date() == datetime.fromtimestamp(now).date() else "%d.%m. %H:%M")


def last_clock_time(hour: int, minute: int, now: float) -> float:
    """Return the most recent local wall-clock time ``hour:minute`` not after *now*.

    Args:
        hour: Hour of day (0-23).
        minute: Minute (0-59).
        now: Current epoch seconds.

    Returns:
        Epoch seconds of today's ``hour:minute``, or yesterday's when today's
        lies in the future.
    """
    now_dt = datetime.fromtimestamp(now)
    dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt > now_dt:
        dt -= timedelta(days=1)
    return dt.timestamp()

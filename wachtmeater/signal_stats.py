"""Statistics on the watcher's timestamped ``[epoch, value]`` samples.

Trend window and least-squares slope (stall rate, linear ETA), robust noise
estimate (Theil-Sen + MAD) and the Hampel-style disturbance detection
(lid open, wrap, probe re-seat, flare-up), plus sample retention.
"""

import statistics

from wachtmeater.watcher_settings import (
    FORECAST_WINDOW_MINUTES,
    STALL_DISTURBANCE_AMBIENT_DROP,
    STALL_DISTURBANCE_AMBIENT_RISE,
    STALL_DISTURBANCE_CORE_DROP,
    TEMPDOWN_WARMUP_CORE_TEMP,
)
from wachtmeater.watcher_state import WatcherState

# A trend window counts as covered when its samples span at least this share
# of the window; samples up to WINDOW_SLACK beyond the window are accepted so
# check jitter does not drop the oldest reading.
WINDOW_MIN_COVERAGE: float = 0.9
WINDOW_SLACK: float = 1.1
# Whole cook is kept so a backdated ``wrapped at`` still finds its readings.
SAMPLE_RETENTION_SECONDS: int = 48 * 3600
# Hampel identifier: a disturbance must exceed HAMPEL_K robust standard
# deviations of the reading noise (MAD estimate), at least the configured
# STALL_DISTURBANCE_* floor.  1.4826 makes the MAD consistent with sigma for
# normally distributed noise.
HAMPEL_K: float = 3.0
MAD_TO_SIGMA: float = 1.4826


def trend_window(
    samples: list[list[float]],
    now: float,
    window_minutes: float,
    ignore_before: float | None = None,
) -> list[list[float]] | None:
    """Select the ``[ts, value]`` samples covering the last *window_minutes*.

    Args:
        samples: Timestamped readings, oldest first.
        now: Current epoch seconds.
        window_minutes: Window length.
        ignore_before: Samples at or before this epoch are skipped.

    Returns:
        The samples in the window, or ``None`` if they do not span at least
        ``WINDOW_MIN_COVERAGE`` of it (too little data, e.g. after a restart,
        an outage or a disturbance).
    """
    window = window_minutes * 60
    pts = [
        p for p in samples if p[0] >= now - window * WINDOW_SLACK and (ignore_before is None or p[0] > ignore_before)
    ]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < window * WINDOW_MIN_COVERAGE:
        return None
    return pts


def slope_per_hour(pts: list[list[float]]) -> float:
    """Least-squares slope of ``[ts, value]`` samples in units per hour.

    A regression instead of newest-minus-oldest keeps single noisy readings
    from dominating.

    Args:
        pts: At least two samples with distinct timestamps.

    Returns:
        Slope in value units per hour.
    """
    n = len(pts)
    mean_t = sum(p[0] for p in pts) / n
    mean_v = sum(p[1] for p in pts) / n
    num = sum((p[0] - mean_t) * (p[1] - mean_v) for p in pts)
    den = sum((p[0] - mean_t) ** 2 for p in pts)
    return num / den * 3600 if den else 0.0


def noise_sigma(samples: list[list[float]]) -> float:
    """Robust estimate of the per-reading noise of ``[ts, value]`` samples.

    The readings are detrended with a Theil-Sen line (slope = median of all
    pairwise slopes), so a steady rise (warm-up, end phase) is not mistaken
    for noise; the spread of the residuals is then measured by their MAD.
    Both are medians, so a single lid-open dip or probe re-seat in the window
    barely moves the estimate (the Hampel identifier).

    Args:
        samples: Timestamped readings, oldest first.

    Returns:
        Estimated standard deviation of one reading in C (``0.0`` with fewer
        than three readings).
    """
    if len(samples) < 3:
        return 0.0
    slopes = [(q[1] - p[1]) / (q[0] - p[0]) for i, p in enumerate(samples) for q in samples[i + 1 :] if q[0] != p[0]]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(p[1] - slope * p[0] for p in samples)
    residuals = [p[1] - (intercept + slope * p[0]) for p in samples]
    return MAD_TO_SIGMA * statistics.median(abs(r) for r in residuals)


def detect_disturbance(
    state: WatcherState,
    now: float,
    current_internal: float | None,
    current_ambient: float | None,
) -> str | None:
    """Detect a lid-open / wrap / probe-reseat / flare-up event from the new reading.

    Must run before the current reading is appended to the samples.

    Each check uses a Hampel-style threshold: the configured
    ``STALL_DISTURBANCE_*`` value, raised to ``HAMPEL_K`` times the noise of
    the last ``FORECAST_WINDOW_MINUTES`` (``noise_sigma``) when the signal is
    noisier than that — a jumpy pit then does not restart the trend on every
    swing.

    Args:
        state: Watcher state holding the previous samples.
        now: Current epoch seconds.
        current_internal: Current core temperature.
        current_ambient: Current ambient temperature.

    Returns:
        A short German reason, or ``None`` if the reading looks undisturbed.
    """
    since = now - FORECAST_WINDOW_MINUTES * 60 * WINDOW_SLACK
    if current_ambient is not None and state.ambient_samples:
        samples = [p for p in state.ambient_samples if p[0] >= since] or state.ambient_samples[-1:]
        window = [p[1] for p in samples]
        noise = HAMPEL_K * noise_sigma(samples)
        # Median, not mean: an earlier dip in the window must not pull the level down.
        level = statistics.median(window)
        drop = max(STALL_DISTURBANCE_AMBIENT_DROP, noise)
        if level - current_ambient >= drop:
            return f"Ambient {current_ambient:.1f} C liegt {level - current_ambient:.1f} C unter dem Median ({level:.1f} C)"
        # Rise above the window maximum, not the median: recovering from a lid-open dip must not count.
        peak = max(window)
        warming_up = current_internal is None or current_internal < TEMPDOWN_WARMUP_CORE_TEMP
        rise = max(STALL_DISTURBANCE_AMBIENT_RISE, noise)
        if not warming_up and current_ambient - peak >= rise:
            return f"Ambient-Spitze {current_ambient:.1f} C liegt {current_ambient - peak:.1f} C ueber dem Hoechstwert ({peak:.1f} C)"
    if current_internal is not None and state.internal_samples:
        samples = [p for p in state.internal_samples if p[0] >= since]
        prev_internal = state.internal_samples[-1][1]
        if prev_internal - current_internal >= max(STALL_DISTURBANCE_CORE_DROP, HAMPEL_K * noise_sigma(samples)):
            return f"Kerntemperatur-Sprung {prev_internal:.1f} -> {current_internal:.1f} C"
    return None


def append_sample(
    samples: list[list[float]],
    now: float,
    value: float | None,
) -> list[list[float]]:
    """Append a reading and drop samples older than the retention period.

    Args:
        samples: Existing ``[ts, value]`` samples.
        now: Current epoch seconds.
        value: New reading (``None`` is not recorded).

    Returns:
        The pruned sample list including the new reading.
    """
    if value is not None:
        samples = [*samples, [now, value]]
    cutoff = now - SAMPLE_RETENTION_SECONDS
    return [p for p in samples if p[0] >= cutoff]

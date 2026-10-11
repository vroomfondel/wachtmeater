"""End-phase core temperature forecast: when does the core reach the target?

Implements the model from the Obsidian note "Kerntemperatur-Prognose
(Endphase)" (derived from brisket cook 15f2e6f5, 10./11.10.2026).

Late in the cook only the slowest heat-conduction mode is left, so the core
follows Newton's law of heating towards an effective surface temperature
``A``::

    T(t) = A - (A - T0) * exp(-c * t)

and the remaining time from the current reading ``T_now`` to the target
``T_Z`` is::

    dt = ln((A - T_now) / (A - T_Z)) / c

``c`` is fitted from the data.  ``A`` is the effective outside temperature:
its upper bound is the *real* smoker (pit) temperature — not the MEATER
ambient reading, which hangs above the meat and is cooled by it.  In foil
with juice the surface is capped near 100 C by boiling.

Variants reported (as in the note):

* linear trend — the earliest possible arrival (the real curve flattens),
* fixed ``A`` = pit temperature — fastest plausible exponential variant,
* fixed ``A`` at 104 / 102 / 100 C,
* free ``A`` (bounded by the pit temperature) with its standard error.

Only fit the *end phase* (after stall and wrap, once the rise is steady); a
window starting right after the wrap fits badly.

Fitting
-------
The fits use ``scipy.optimize.least_squares`` (the note's script used
``curve_fit``, which wraps the same solver):

* ``c`` sits in the exponent, so the fit is *nonlinear* least squares — there
  is no closed-form solution like for the linear trend.  Log-linearising
  (``ln(A - T)`` against ``t``) would be closed-form, but it weights the
  residuals differently (errors near ``A`` get blown up) and breaks as soon
  as a reading reaches ``A``.
* ``T0`` (the curve's start value) is fitted too, as in the standard
  asymptotic regression model (R's ``SSasymp``), instead of being pinned to
  the first reading: one noisy first reading would otherwise shift the
  whole curve (a -1 C glitch moved the ETA by up to ~80 min).
* **Robust loss (Huber).**  A plain least-squares fit lets a single bad
  reading (probe touched, lid open, sensor glitch) pull the whole curve,
  because its error counts squared.  The Huber loss counts errors up to a
  threshold squared and beyond it only linearly, so outliers lose most of
  their pull.  The threshold follows the standard recipe: a first
  least-squares fit gives the residuals, their MAD gives a robust noise
  ``sigma``, and the threshold is ``1.345 * sigma`` (95 % efficiency on
  clean, normally distributed data), at least ``F_SCALE_MIN``.  On the
  cook from 11.10.2026 a -1 C glitch in the first reading moves the ETA by
  0-3 min instead of 19-29 min.
* The free fit (``A``, ``T0``, ``c``) is bounded: ``target + 0.1 <= A <= pit``.
  It is started from a few ``A`` values and the best result is kept, so a
  local minimum near one bound does not win.  The standard error of ``A``
  comes from the Jacobian at the solution, as ``curve_fit`` computes its
  covariance.

With ``loss="linear"`` the fits reproduce ``curve_fit`` with ``A``, ``T0`` and
``c`` free (``tests/test_core_forecast.py``).  The note's script pinned ``T0``
to the first reading and therefore shows slightly different numbers.

The module only works on generic ``(epoch, core, ambient)`` readings — the
watcher feeds it its own samples (end phase from the ``wrapped`` command on,
pit temperature via ``set pit``).  Reading an Element chat export is a
separate special use, see ``wachtmeater.element_export`` and
``wachtmeater eta``.

Usage::

    readings = [(epoch_s, core_c, ambient_c), ...]
    print(format_report(forecast(readings, target=96.5, pit=110.0)))
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import OptimizeResult, least_squares

Reading = tuple[float, float, float | None]
"""One status reading: ``(epoch_seconds, core_c, ambient_c_or_None)``."""

DEFAULT_PIT_TEMP: float = 110.0
DEFAULT_TARGET_TEMP: float = 96.5
DEFAULT_FIXED_AMBIENTS: tuple[float, ...] = (104.0, 102.0, 100.0)

_C_BOUNDS_FIXED: tuple[float, float] = (1e-6, 10.0)
"""Search range for ``c`` [1/h] with a fixed ``A`` (unbounded in the note's script)."""
_C_BOUNDS_FREE: tuple[float, float] = (1e-3, 5.0)
"""Bounds for ``c`` [1/h] in the free fit (as in the note's script)."""
_FREE_A_MARGIN: float = 0.1
"""Free ``A`` must stay at least this far above the target (else no arrival)."""

Loss = Literal["linear", "huber"]
"""``least_squares`` loss: ``"linear"`` = plain least squares, ``"huber"`` = robust."""
FIT_LOSS: Loss = "huber"
"""Default loss of the exponential fits (see the module docstring)."""
HUBER_K: float = 1.345
"""Huber threshold in units of the robust noise sigma (standard tuning constant)."""
MAD_TO_SIGMA: float = 1.4826
"""Makes the MAD consistent with sigma for normally distributed noise."""
F_SCALE_MIN: float = 0.05
"""Lower bound of the Huber threshold in C.  The MEATER reports 0.1 C steps;
on a near-perfect fit the MAD would otherwise shrink to ~0 and every reading
would count as an outlier."""
_FREE_A_STARTS: tuple[float, ...] = (0.1, 0.5, 0.9)
"""Start values of the free fit as fractions of the ``A`` range."""

MAX_ETA_HOURS: float = 24.0
"""ETAs further out than this are shown as "nicht absehbar".  A (nearly) flat
window gives a tiny ``c`` or slope and an ETA years ahead — e.g. the core
standing at 93 C for an hour on 02.05. — which is meaningless."""

DEFAULT_SEARCH_STEP_S: float = 600.0
"""Step between candidate fit starts for ``find_endphase_start``."""
DEFAULT_MIN_SPAN_S: float = 3600.0
"""Minimum data span after a candidate start."""
DEFAULT_STABLE_RMS: float = 0.1
"""Free-fit RMS [C] up to which the end phase counts as steady (good fits on
11.10.2026: 0.07; windows starting too early: 0.24-0.48)."""
DEFAULT_BOUND_EPS: float = 0.2
"""Free ``A`` within this distance [C] above its lower bound counts as pinned
(at least; see ``is_stable``)."""
DEFAULT_STDERR_MAX: float = 5.0
"""Largest standard error of the free ``A`` [C] for which ``A`` counts as
determined by the data (11.10.2026, 08:01-10:25: ±3.9 with ``T0`` fitted —
±1.8 when ``T0`` was pinned to the first reading; a 1-hour window right after
acceptance can be ±13 and more)."""
DEFAULT_SEARCH_MAX_POINTS: int = 40
"""Readings per candidate fit during the search (thinned, keeps it cheap)."""
DEFAULT_MAX_CANDIDATES: int = 36
"""Upper bound on candidate starts per search; longer windows get a coarser step."""


@dataclass(frozen=True)
class LinearFit:
    """Linear trend — earliest possible arrival.

    Attributes:
        rate: Slope in C per hour.
        eta_hours: Hours from the last reading until the regression line hits
            the target (``None`` without a positive slope).
    """

    rate: float
    eta_hours: float | None


@dataclass(frozen=True)
class ExpFit:
    """Exponential end-phase fit ``T(t) = A - (A - T0) * exp(-c t)``.

    Attributes:
        ambient: Effective surface temperature ``A`` in C.
        c: Rate constant in 1/h.
        rms: Root-mean-square residual in C.
        eta_hours: Hours from the last reading until the target is reached
            (``None`` if ``A`` does not exceed the target or the current core).
        ambient_stderr: Standard error of ``A`` (free fit only).
        fixed: ``True`` when ``A`` was given, ``False`` for the free fit.
    """

    ambient: float
    c: float
    rms: float
    eta_hours: float | None
    ambient_stderr: float | None = None
    fixed: bool = True


@dataclass(frozen=True)
class Forecast:
    """All forecast variants for one fit window.

    Attributes:
        start: Epoch seconds of the first reading in the window.
        now: Epoch seconds of the last reading (ETAs are relative to it).
        core_start: Core temperature at the start of the window.
        core_now: Current core temperature.
        ambient_mean: Mean MEATER ambient over the window (informational —
            not a valid ``A``), ``None`` without ambient readings.
        pit: Real smoker temperature used as upper bound for ``A``.
        target: Target core temperature.
        points: Number of readings in the window.
        linear: Linear trend (earliest arrival).
        fixed: Exponential fits with fixed ``A`` (pit first).
        free: Free-``A`` fit, ``None`` if it failed.
    """

    start: float
    now: float
    core_start: float
    core_now: float
    ambient_mean: float | None
    pit: float
    target: float
    points: int
    linear: LinearFit
    fixed: list[ExpFit] = field(default_factory=list)
    free: ExpFit | None = None

    def eta(self, hours: float | None) -> datetime | None:
        """Convert an ETA offset in hours to a local wall-clock time.

        Args:
            hours: Offset from the last reading.

        Returns:
            The arrival time, or ``None`` if *hours* is ``None``.
        """
        if hours is None:
            return None
        return datetime.fromtimestamp(self.now) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


FloatArray = NDArray[np.float64]


def _model(t: FloatArray, ambient: float, core0: float, c: float) -> FloatArray:
    """Evaluate the end-phase model ``T(t) = A - (A - T0) * exp(-c * t)``.

    The core approaches the effective surface temperature ``A``
    exponentially; the gap ``A - T`` shrinks by the factor ``e`` every
    ``1/c`` hours.  At ``t = 0`` the model returns exactly ``T0``; for large
    ``t`` it tends to ``A`` without ever reaching it.

    Args:
        t: Times in **hours** since the start of the fit window (the first
            reading), not epoch seconds.
        ambient: ``A`` — effective surface temperature in C that the core
            heads towards.  Not the MEATER ambient reading: at most the real
            smoker (pit) temperature, in foil with juice ~100 C (boiling).
        core0: ``T0`` — model core temperature in C at ``t = 0`` (start of
            the window).  Fitted like ``c``, not pinned to the first reading.
        c: Rate constant in **1/h** — how fast the core closes the gap to
            ``A``.  Ideally ``~ pi^2 * alpha / D^2`` for a slab of thickness
            ``D``, in reality smaller (wrap, air-to-meat heat transfer,
            remaining evaporation).  On 11.10.2026 about 0.09–0.16 /h
            depending on ``A``.

    Returns:
        Modelled core temperatures in C at times *t*.
    """
    return ambient - (ambient - core0) * np.exp(-c * t)


def eta_hours(ambient: float, c: float, core_now: float, target: float) -> float | None:
    """Remaining hours until the exponential model reaches *target*.

    Args:
        ambient: Effective surface temperature ``A``.
        c: Rate constant in 1/h.
        core_now: Current core temperature.
        target: Target core temperature.

    Returns:
        ``ln((A - T_now) / (A - T_Z)) / c``; ``0.0`` if the target is already
        reached; ``None`` if ``A`` does not exceed the target (never arrives).
    """
    if core_now >= target:
        return 0.0
    if ambient <= target or c <= 0:
        return None
    return math.log((ambient - core_now) / (ambient - target)) / c


def fit_linear(t_h: Sequence[float], core: Sequence[float], target: float) -> LinearFit:
    """Least-squares line through the readings — the earliest arrival.

    Args:
        t_h: Hours since the first reading.
        core: Core temperatures.
        target: Target core temperature.

    Returns:
        Slope and hours from the last reading until the line hits *target*.
    """
    n = len(t_h)
    mean_t = sum(t_h) / n
    mean_k = sum(core) / n
    den = sum((t - mean_t) ** 2 for t in t_h)
    rate = sum((t - mean_t) * (k - mean_k) for t, k in zip(t_h, core)) / den if den else 0.0
    if rate <= 0:
        return LinearFit(rate=rate, eta_hours=None)
    intercept = mean_k - rate * mean_t
    return LinearFit(rate=rate, eta_hours=(target - intercept) / rate - t_h[-1])


def _c_guess(t: FloatArray, core: FloatArray, ambient: float, bounds: tuple[float, float]) -> float:
    """Start value for ``c`` from the first and last reading.

    Solves the model through both end points; falls back to 0.1 /h when the
    data do not allow it (flat or falling core, a reading at or above ``A``).
    The result is clipped strictly inside *bounds*, as ``least_squares``
    requires.
    """
    lo, hi = bounds
    c = 0.1
    if t[-1] > 0 and core[-1] > core[0] and ambient > core[-1]:
        c = math.log((ambient - core[0]) / (ambient - core[-1])) / float(t[-1])
    return min(max(c, lo * 1.01), hi * 0.99)


def _robust_f_scale(residuals: FloatArray) -> float:
    """Huber threshold from the residuals of a plain least-squares fit.

    ``HUBER_K`` robust standard deviations (MAD estimate), at least
    ``F_SCALE_MIN``.
    """
    sigma = MAD_TO_SIGMA * float(np.median(np.abs(residuals)))
    return max(HUBER_K * sigma, F_SCALE_MIN)


def _solve(
    residuals: Callable[[FloatArray], FloatArray],
    starts: Sequence[Sequence[float]],
    lower: Sequence[float],
    upper: Sequence[float],
    loss: Loss,
) -> OptimizeResult:
    """Run ``least_squares`` from each start and keep the lowest cost.

    With ``loss="huber"`` a plain least-squares pass comes first: its
    residuals set the Huber threshold (``_robust_f_scale``), and the robust
    pass starts from its solution.

    Args:
        residuals: ``params -> model - measured``.
        starts: Start parameter vectors (strictly inside the bounds).
        lower: Lower bounds per parameter.
        upper: Upper bounds per parameter.
        loss: ``"linear"`` or ``"huber"``.

    Returns:
        The ``least_squares`` result of the final pass.
    """
    best: OptimizeResult | None = None
    for x0 in starts:
        res = least_squares(residuals, x0, bounds=(lower, upper), method="trf")
        if best is None or res.cost < best.cost:
            best = res
    assert best is not None
    if loss == "linear":
        return best
    return least_squares(
        residuals, best.x, bounds=(lower, upper), method="trf", loss=loss, f_scale=_robust_f_scale(best.fun)
    )


def fit_fixed_ambient(
    t_h: Sequence[float], core: Sequence[float], ambient: float, target: float, loss: Loss = FIT_LOSS
) -> ExpFit:
    """Fit ``T0`` and ``c`` with ``A`` held fixed.

    Args:
        t_h: Hours since the first reading.
        core: Core temperatures.
        ambient: Fixed effective surface temperature ``A``.
        target: Target core temperature.
        loss: ``"huber"`` (robust, default) or ``"linear"``.

    Returns:
        The fit with RMS and ETA from the last reading.
    """
    t = np.asarray(t_h, dtype=float)
    k = np.asarray(core, dtype=float)

    def _res(p: FloatArray) -> FloatArray:
        r: FloatArray = _model(t, ambient, p[0], p[1]) - k
        return r

    x0 = [float(k[0]), _c_guess(t, k, ambient, _C_BOUNDS_FIXED)]
    res = _solve(_res, [x0], [-np.inf, _C_BOUNDS_FIXED[0]], [np.inf, _C_BOUNDS_FIXED[1]], loss)
    c = float(res.x[1])
    rms = math.sqrt(float(np.mean(_res(res.x) ** 2)))
    return ExpFit(ambient=ambient, c=c, rms=rms, eta_hours=eta_hours(ambient, c, core[-1], target))


def fit_free_ambient(
    t_h: Sequence[float], core: Sequence[float], target: float, pit: float, loss: Loss = FIT_LOSS
) -> ExpFit | None:
    """Fit ``A``, ``T0`` and ``c`` with ``target + 0.1 <= A <= pit``.

    Args:
        t_h: Hours since the first reading.
        core: Core temperatures.
        target: Target core temperature (lower bound for ``A``).
        pit: Real smoker temperature (upper bound for ``A``).
        loss: ``"huber"`` (robust, default) or ``"linear"``.

    Returns:
        The free fit, or ``None`` if it is undetermined (too few points or
        a pit temperature not above the target).
    """
    n = len(t_h)
    lo = target + _FREE_A_MARGIN
    if n < 3 or pit <= lo:
        return None
    t = np.asarray(t_h, dtype=float)
    k = np.asarray(core, dtype=float)

    def _res(p: FloatArray) -> FloatArray:
        r: FloatArray = _model(t, p[0], p[1], p[2]) - k
        return r

    starts = []
    for frac in _FREE_A_STARTS:
        a0 = lo + frac * (pit - lo)
        starts.append([a0, float(k[0]), _c_guess(t, k, a0, _C_BOUNDS_FREE)])
    res = _solve(_res, starts, [lo, -np.inf, _C_BOUNDS_FREE[0]], [pit, np.inf, _C_BOUNDS_FREE[1]], loss)
    ambient, c = float(res.x[0]), float(res.x[2])
    return ExpFit(
        ambient=ambient,
        c=c,
        rms=math.sqrt(float(np.mean(_res(res.x) ** 2))),
        eta_hours=eta_hours(ambient, c, core[-1], target),
        ambient_stderr=_ambient_stderr(res, n),
        fixed=False,
    )


def _ambient_stderr(res: OptimizeResult, n: int) -> float | None:
    """Standard error of the fitted ``A`` (the "±3.9" in ``A=102.5±3.9``).

    Question: how precisely do the data pin down ``A``?  If ``A`` could move
    a lot while the curve still fits the readings about as well, ``A`` is
    uncertain.  Computed exactly as ``scipy.optimize.curve_fit`` computes its
    covariance:

    * The **Jacobian** ``J`` holds, for each reading, how strongly the model
      value reacts to a small change of each parameter (``A``, ``T0``,
      ``c``).  ``least_squares`` returns it at the solution (with a robust
      loss already weighted, so outliers count less here too).
    * ``cov = s^2 * (J^T J)^-1`` with ``s^2 = 2 * cost / (n - 3)``, the
      residual variance (``n - 3``: three fitted parameters).
    * The standard error of ``A`` is the square root of ``cov[0][0]``.

    It gets large when the parameters can stand in for each other (a higher
    ``A`` with a smaller ``c`` gives almost the same curve); more curvature in
    the data (a longer end phase) separates them.  It is a linear
    approximation that ignores the bounds: with ``A`` at a bound the value is
    only a rough indication.

    Args:
        res: ``least_squares`` result of the free fit.
        n: Number of readings.

    Returns:
        Standard error of ``A`` in C, or ``None`` with too few readings or a
        singular ``J^T J`` (parameters not separable).
    """
    if n < 4:
        return None
    jtj = res.jac.T @ res.jac
    try:
        cov = np.linalg.inv(jtj) * (2 * res.cost / (n - 3))
    except np.linalg.LinAlgError:
        return None
    var_a = float(cov[0, 0])
    if not math.isfinite(var_a) or var_a < 0:
        return None
    return math.sqrt(var_a)


# ---------------------------------------------------------------------------
# Forecast over readings
# ---------------------------------------------------------------------------


def thin(readings: Sequence[Reading], max_points: int) -> list[Reading]:
    """Evenly thin readings to at most *max_points*, keeping first and last.

    Args:
        readings: Readings, oldest first.
        max_points: Upper bound (>= 2).

    Returns:
        The thinned readings.
    """
    n = len(readings)
    if n <= max_points:
        return list(readings)
    return [readings[round(i * (n - 1) / (max_points - 1))] for i in range(max_points)]


def is_stable(
    fc: Forecast,
    rms_max: float = DEFAULT_STABLE_RMS,
    bound_eps: float = DEFAULT_BOUND_EPS,
    stderr_max: float = DEFAULT_STDERR_MAX,
) -> bool:
    """Tell whether a forecast's window lies in the steady end phase.

    All of these must hold for the free fit:

    1. It follows the readings closely: ``rms <= rms_max``.  Windows that
       still contain the post-wrap acceleration fit with RMS 0.2-0.5.
    2. ``A`` is determined by the data: standard error ``<= stderr_max``.  A
       short window has too little curvature, and ``A`` can then move by
       ±10 C or more without the fit getting worse.
    3. ``A`` is not pinned to its lower bound (just above the target): it
       must lie at least ``max(bound_eps, stderr)`` above it, i.e. clearly
       more than its own uncertainty.  An ``A`` at the lower bound means the
       optimum wanted to go lower — the window is not steady end phase, and
       its ETA (far in the future) is an artefact of the bound.

    ``A`` at the upper bound (pit) is fine: that is a physical limit, not a
    failure.

    Args:
        fc: Forecast of one candidate window.
        rms_max: Maximum RMS of the free fit in C.
        bound_eps: Minimum distance in C above the lower bound.
        stderr_max: Maximum standard error of ``A`` in C.

    Returns:
        ``True`` if the window is usable.
    """
    free = fc.free
    if free is None or free.rms > rms_max:
        return False
    if free.ambient_stderr is None or free.ambient_stderr > stderr_max:
        return False
    return free.ambient - (fc.target + _FREE_A_MARGIN) >= max(bound_eps, free.ambient_stderr)


def find_endphase_start(
    readings: Sequence[Reading],
    target: float,
    pit: float,
    *,
    step_s: float = DEFAULT_SEARCH_STEP_S,
    min_span_s: float = DEFAULT_MIN_SPAN_S,
    rms_max: float = DEFAULT_STABLE_RMS,
    bound_eps: float = DEFAULT_BOUND_EPS,
    stderr_max: float = DEFAULT_STDERR_MAX,
    max_points: int = DEFAULT_SEARCH_MAX_POINTS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> float | None:
    """Find the earliest fit start that lies in the steady end phase.

    Right after the wrap the core still accelerates and the model fits
    badly; how long that lasts depends on the cook.  Instead of a fixed
    delay, candidate starts are tried from the first reading on in steps of
    *step_s*; the first whose window (start .. last reading) passes
    ``is_stable`` wins.  Earliest is best: more curve determines ``A``
    better.

    On 11.10.2026 (wrap ~05:30, data to 10:25) this finds ~08:00: windows
    from 06:39-07:40 fit with RMS 0.08-0.5 and/or ``A`` pinned at the
    target.

    Args:
        readings: Readings from the earliest allowed start on, oldest first.
        target: Target core temperature.
        pit: Real smoker temperature (upper bound for ``A``).
        step_s: Step between candidate starts in seconds.
        min_span_s: Minimum data span after a candidate start.
        rms_max: See ``is_stable``.
        bound_eps: See ``is_stable``.
        stderr_max: See ``is_stable``.
        max_points: Readings per candidate fit (thinned).
        max_candidates: At most this many candidate starts; with a longer
            window the step grows accordingly (bounds the cost: one candidate
            fit is ~0.01-0.03 s).

    Returns:
        Epoch of the accepted start, or ``None`` if no window is steady yet.
    """
    if len(readings) < 3:
        return None
    last = readings[-1][0]
    start = readings[0][0]
    step_s = max(step_s, (last - start - min_span_s) / max_candidates)
    while last - start >= min_span_s:
        window = [r for r in readings if r[0] >= start]
        if len(window) >= 3:
            fc = forecast(thin(window, max_points), target=target, pit=pit, fixed_ambients=())
            if is_stable(fc, rms_max, bound_eps, stderr_max):
                return window[0][0]
        start += step_s
    return None


def select_since(readings: Sequence[Reading], since: str) -> list[Reading]:
    """Keep readings from wall-clock time ``HH:MM`` on (the end-phase window).

    The time refers to the day of the last reading; if that lies after the
    last reading, the previous day is meant.

    Args:
        readings: Readings, oldest first.
        since: Start time as ``"HH:MM"``.

    Returns:
        The readings at or after that time.
    """
    cutoff = _clock_cutoff(readings, since)
    return [r for r in readings if r[0] >= cutoff]


def select_until(readings: Sequence[Reading], until: str) -> list[Reading]:
    """Keep readings up to wall-clock time ``HH:MM`` (inclusive minute).

    For after-the-fact analysis: "what would the forecast have said at
    07:52?" — and to cut off the rest phase after the pull, which must not be
    fitted.  The day is resolved like in ``select_since``.

    Args:
        readings: Readings, oldest first.
        until: End time as ``"HH:MM"``.

    Returns:
        The readings up to and including that minute.
    """
    cutoff = _clock_cutoff(readings, until) + 60
    return [r for r in readings if r[0] < cutoff]


def _clock_cutoff(readings: Sequence[Reading], hhmm: str) -> float:
    """Resolve ``HH:MM`` to an epoch on the day of the last reading.

    If that time lies after the last reading, the previous day is meant.

    Args:
        readings: Readings, oldest first (non-empty).
        hhmm: Wall-clock time as ``"HH:MM"``.

    Returns:
        Epoch seconds of that minute.
    """
    hh, mm = map(int, hhmm.split(":"))
    last = datetime.fromtimestamp(readings[-1][0])
    at = last.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if at > last:
        at -= timedelta(days=1)
    return at.timestamp()


def forecast(
    readings: Sequence[Reading],
    target: float = DEFAULT_TARGET_TEMP,
    pit: float = DEFAULT_PIT_TEMP,
    fixed_ambients: Sequence[float] = DEFAULT_FIXED_AMBIENTS,
    loss: Loss = FIT_LOSS,
) -> Forecast:
    """Compute all forecast variants over the given end-phase readings.

    Args:
        readings: End-phase readings, oldest first (filter with
            ``select_since`` beforehand).
        target: Target core temperature.
        pit: Real smoker temperature — first fixed ``A`` and upper bound of
            the free fit.
        fixed_ambients: Additional fixed ``A`` values; those not above the
            target are skipped.
        loss: Loss of the exponential fits (``"huber"`` robust, default, or
            ``"linear"`` plain least squares).

    Returns:
        The forecast.

    Raises:
        ValueError: With fewer than two readings.
    """
    if len(readings) < 2:
        raise ValueError("Mindestens zwei Messpunkte noetig.")
    start = readings[0][0]
    t_h = [(r[0] - start) / 3600 for r in readings]
    core = [r[1] for r in readings]
    ambients = [r[2] for r in readings if r[2] is not None]

    fixed: list[ExpFit] = []
    for ambient in (pit, *fixed_ambients):
        if ambient > target and all(f.ambient != ambient for f in fixed):
            fixed.append(fit_fixed_ambient(t_h, core, ambient, target, loss))

    return Forecast(
        start=start,
        now=readings[-1][0],
        core_start=core[0],
        core_now=core[-1],
        ambient_mean=sum(ambients) / len(ambients) if ambients else None,
        pit=pit,
        target=target,
        points=len(readings),
        linear=fit_linear(t_h, core, target),
        fixed=fixed,
        free=fit_free_ambient(t_h, core, target, pit, loss),
    )


def format_report(fc: Forecast) -> str:
    """Render the forecast like the note's script output.

    Args:
        fc: The forecast.

    Returns:
        Multi-line German report.
    """

    def _hm(hours: float | None) -> str:
        if hours is None:
            return "nie (A <= Ziel)"
        if hours > MAX_ETA_HOURS:
            return "nicht absehbar (Kern steigt kaum)"
        eta = fc.eta(hours)
        return eta.strftime("%H:%M") if eta else "nie (A <= Ziel)"

    ambient = f"MEATER-Ambient Ø {fc.ambient_mean:.1f} °C, " if fc.ambient_mean is not None else ""
    lines = [
        f"Fit {datetime.fromtimestamp(fc.start):%H:%M}–{datetime.fromtimestamp(fc.now):%H:%M} "
        f"({fc.points} Punkte): Kern {fc.core_start:.1f}→{fc.core_now:.1f} °C, "
        f"{ambient}Pit {fc.pit:.0f} °C, Ziel {fc.target} °C",
        f"linear      : {fc.linear.rate:.2f} °C/h  -> "
        f"{_hm(fc.linear.eta_hours) if fc.linear.eta_hours is not None else 'nicht absehbar (kein Anstieg)'}"
        f"  (früheste Ankunft)",
    ]
    for f in fc.fixed:
        tag = "  <- A = Pit (schnellste Exp.-Variante)" if f.ambient == fc.pit else ""
        lines.append(f"A={f.ambient:5.1f} °C : c={f.c:.3f}/h rms={f.rms:.3f} -> {_hm(f.eta_hours)}{tag}")
    if fc.free is not None:
        err = f"±{fc.free.ambient_stderr:.1f}" if fc.free.ambient_stderr is not None else ""
        lines.append(f"A frei (≤Pit): A={fc.free.ambient:.1f}{err} °C c={fc.free.c:.3f}/h -> {_hm(fc.free.eta_hours)}")
    else:
        lines.append("A frei (≤Pit): nicht bestimmbar")
    return "\n".join(lines)

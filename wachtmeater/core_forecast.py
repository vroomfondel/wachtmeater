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
The note's script uses ``scipy.optimize.curve_fit``.  This module is pure
Python on purpose — numpy/scipy would be two heavy new dependencies in the
watcher image just for one small fit — so it needs its own least-squares
solver:

* ``c`` sits in the exponent, so the fit is *nonlinear* least squares — there
  is no closed-form solution like for the linear trend.
* Log-linearising (``ln(A - T)`` against ``t``, then a straight line) would be
  closed-form, but it weights the residuals differently (errors near ``A``
  get blown up) and gives a different ``c`` than the note's ``curve_fit``.
  It also breaks as soon as a reading reaches ``A``.
* With ``A`` fixed only one parameter is left, ``c``, in a known range.  For
  that, a 1-D search on ``SSE(c)`` is the simplest robust solver — see
  ``_golden_min`` for why golden section.
* The free fit (``A`` and ``c``) is reduced to 1-D searches too: for each
  candidate ``A`` the best ``c`` is found (the "profile"), and the profile is
  minimised over ``A`` in ``[target + 0.1, pit]``.  This handles the bounds
  directly and finds the same optimum as ``curve_fit`` with bounds.  The
  standard error of ``A`` comes from the Jacobian, exactly as ``curve_fit``
  computes its covariance.

Checked against the note: on the cook from 11.10.2026 (08:01–10:25) every
value matches the scipy output to the printed precision
(``tests/test_core_forecast.py``).

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
DEFAULT_STDERR_MAX: float = 3.0
"""Largest standard error of the free ``A`` [C] for which ``A`` counts as
determined by the data (11.10.2026, 08:01-10:25: ±1.8; a 1-hour window right
after acceptance can be ±13)."""
DEFAULT_SEARCH_MAX_POINTS: int = 40
"""Readings per candidate fit during the search (thinned, keeps it cheap)."""
DEFAULT_MAX_CANDIDATES: int = 36
"""Upper bound on candidate starts per search; longer windows get a coarser step."""
"""Free ``A`` must stay at least this far above the target (else no arrival)."""


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


def _golden_min(f: Callable[[float], float], lo: float, hi: float, iterations: int = 120) -> float:
    """Minimise a unimodal function on ``[lo, hi]`` by golden-section search.

    This is the solver behind all exponential fits (see the module docstring
    for why we need our own instead of ``scipy.optimize.curve_fit``).

    Why golden section and not Newton / Gauss-Newton / Levenberg-Marquardt:

    * **No derivatives and no start value.**  It only evaluates ``f``.
      Gradient methods need ``dSSE/dc`` and a good initial guess (``curve_fit``
      needs ``p0``) and can diverge or run out of bounds on flat data.
    * **Bounds are built in.**  The search never leaves ``[lo, hi]``, so
      ``c > 0`` and ``target < A <= pit`` hold without extra checks.
    * **Guaranteed convergence.**  Each step shrinks the interval by a fixed
      factor of 0.618, so 120 steps give a precision of ~1e-25 of the range,
      far below the sensor resolution.  There are no tolerance or step-size
      settings to tune.
    * **Cheap enough.**  One evaluation of ``SSE`` is ``O(n)``.  With the
      profile over ``A`` that is ~120 x 120 x n operations: measured ~0.2 s
      for the at most 120 points the watcher fits (it thins its samples),
      ~0.03 s for 15 points.  That runs in the check's worker thread
      every few minutes, so it is no problem.

    "Golden": the two inner points are placed at the golden ratio, so after
    each step one of them can be reused.  Each step therefore costs only one
    new function evaluation instead of two.

    Requirement: ``f`` must be unimodal on ``[lo, hi]`` (exactly one minimum).
    That holds for ``SSE(c)`` of the monotone, saturating model on rising
    data, and in practice for the profile over ``A``.  For a function with
    several minima the search would only find one of them.

    Args:
        f: Function to minimise.
        lo: Lower bound.
        hi: Upper bound.
        iterations: Number of interval reductions (0.618^120 ~ 1e-25).

    Returns:
        The argument of the minimum.
    """
    inv_phi = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    x1 = b - inv_phi * (b - a)
    x2 = a + inv_phi * (b - a)
    f1, f2 = f(x1), f(x2)
    for _ in range(iterations):
        if f1 <= f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - inv_phi * (b - a)
            f1 = f(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + inv_phi * (b - a)
            f2 = f(x2)
    return (a + b) / 2


def _model(t: float, ambient: float, core0: float, c: float) -> float:
    """Evaluate the end-phase model ``T(t) = A - (A - T0) * exp(-c * t)``.

    The core approaches the effective surface temperature ``A``
    exponentially; the gap ``A - T`` shrinks by the factor ``e`` every
    ``1/c`` hours.  At ``t = 0`` the model returns exactly ``T0``; for large
    ``t`` it tends to ``A`` without ever reaching it.

    Args:
        t: Time in **hours** since the start of the fit window (the first
            reading), not epoch seconds.
        ambient: ``A`` — effective surface temperature in C that the core
            heads towards.  Not the MEATER ambient reading: at most the real
            smoker (pit) temperature, in foil with juice ~100 C (boiling).
        core0: ``T0`` — core temperature in C at ``t = 0``, i.e. the first
            reading of the window.  Held fixed (not fitted), as in the
            note's script.
        c: Rate constant in **1/h** — how fast the core closes the gap to
            ``A``.  Fitted from the data; ideally ``~ pi^2 * alpha / D^2``
            for a slab of thickness ``D``, in reality smaller (wrap,
            air-to-meat heat transfer, remaining evaporation).  On
            11.10.2026 about 0.09–0.18 /h depending on ``A``.

    Returns:
        Modelled core temperature in C at time *t*.
    """
    return ambient - (ambient - core0) * math.exp(-c * t)


def _sse(t_h: Sequence[float], core: Sequence[float], ambient: float, c: float) -> float:
    """Sum of squared residuals of the exponential model over all readings.

    This is the quantity the fit minimises.  ``T0`` is the first reading.

    Args:
        t_h: Times of the readings in hours since the first reading.
        core: Measured core temperatures in C, same order as *t_h*.
        ambient: Effective surface temperature ``A`` in C (see ``_model``).
        c: Rate constant in 1/h (see ``_model``).

    Returns:
        ``sum((T_model(t) - T_measured)^2)`` in C^2.
    """
    core0 = core[0]
    return sum((_model(t, ambient, core0, c) - k) ** 2 for t, k in zip(t_h, core))


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


def _best_c(t_h: Sequence[float], core: Sequence[float], ambient: float, bounds: tuple[float, float]) -> float:
    """Least-squares ``c`` for a fixed ``A``."""
    return _golden_min(lambda c: _sse(t_h, core, ambient, c), *bounds)


def fit_fixed_ambient(t_h: Sequence[float], core: Sequence[float], ambient: float, target: float) -> ExpFit:
    """Fit ``c`` with ``A`` held fixed.

    Args:
        t_h: Hours since the first reading.
        core: Core temperatures.
        ambient: Fixed effective surface temperature ``A``.
        target: Target core temperature.

    Returns:
        The fit with RMS and ETA from the last reading.
    """
    c = _best_c(t_h, core, ambient, _C_BOUNDS_FIXED)
    rms = math.sqrt(_sse(t_h, core, ambient, c) / len(t_h))
    return ExpFit(ambient=ambient, c=c, rms=rms, eta_hours=eta_hours(ambient, c, core[-1], target))


def fit_free_ambient(t_h: Sequence[float], core: Sequence[float], target: float, pit: float) -> ExpFit | None:
    """Fit ``A`` and ``c`` with ``target + 0.1 <= A <= pit``.

    The profile ``min_c SSE(A, c)`` is minimised over ``A``.  The standard
    error of ``A`` comes from ``_ambient_stderr`` (Jacobian, as in
    ``curve_fit``).

    Args:
        t_h: Hours since the first reading.
        core: Core temperatures.
        target: Target core temperature (lower bound for ``A``).
        pit: Real smoker temperature (upper bound for ``A``).

    Returns:
        The free fit, or ``None`` if it is undetermined (too few points or
        a pit temperature not above the target).
    """
    n = len(t_h)
    lo = target + _FREE_A_MARGIN
    if n < 3 or pit <= lo:
        return None

    def _profile(ambient: float) -> float:
        return _sse(t_h, core, ambient, _best_c(t_h, core, ambient, _C_BOUNDS_FREE))

    ambient = _golden_min(_profile, lo, pit)
    c = _best_c(t_h, core, ambient, _C_BOUNDS_FREE)
    sse = _sse(t_h, core, ambient, c)

    return ExpFit(
        ambient=ambient,
        c=c,
        rms=math.sqrt(sse / n),
        eta_hours=eta_hours(ambient, c, core[-1], target),
        ambient_stderr=_ambient_stderr(t_h, core[0], ambient, c, sse),
        fixed=False,
    )


def _ambient_stderr(t_h: Sequence[float], core0: float, ambient: float, c: float, sse: float) -> float | None:
    """Standard error of the fitted ``A`` (the "±1.8" in ``A=98.5±1.8``).

    Question: how precisely do the data pin down ``A``?  If ``A`` could move
    a lot while the curve still fits the readings about as well, ``A`` is
    uncertain.  This is the same estimate ``scipy.optimize.curve_fit``
    returns as its covariance matrix.

    **What is the Jacobian?**  For each reading it holds how strongly the
    model value reacts to a small change of each parameter — the partial
    derivatives at the fitted optimum.  With ``n`` readings and two
    parameters (``A``, ``c``) it is an ``n x 2`` table::

        J[i] = ( dT(t_i)/dA ,  dT(t_i)/dc )

    For ``T(t) = A - (A - T0) * exp(-c t)`` (with ``e = exp(-c t)``):

    * ``dT/dA = 1 - e`` — at ``t = 0`` raising ``A`` changes nothing (the
      curve starts at ``T0``), late in the window it shifts the curve almost
      one-to-one.  So readings late in the window carry the information
      about ``A``.
    * ``dT/dc = (A - T0) * t * e`` — how much faster/slower approach
      changes the value; largest in the middle of the curve.

    **Steps:**

    1. ``J^T J`` (2x2): for each parameter pair, sum the products of the
       derivatives over all readings::

           [ sum(dA*dA)  sum(dA*dc) ]     = [ jaa  jac ]
           [ sum(dA*dc)  sum(dc*dc) ]       [ jac  jcc ]

       Large diagonal = the data react strongly to this parameter (it is well
       determined).  Large off-diagonal = ``A`` and ``c`` can partly stand in
       for each other (a higher ``A`` with smaller ``c`` gives a similar
       curve) — exactly the trade-off the note describes.
    2. Residual variance ``s^2 = SSE / (n - 2)``: the scatter of the readings
       around the curve; ``n - 2`` because two parameters were fitted.
    3. Covariance ``cov = s^2 * (J^T J)^-1``.  The inverse of a 2x2 matrix
       is ``1/det * [[jcc, -jac], [-jac, jaa]]`` with
       ``det = jaa*jcc - jac^2``.  The variance of ``A`` is the top-left
       entry: ``s^2 * jcc / det``.
    4. The standard error is its square root.

    Intuition: ``det`` is small when the two derivative columns are almost
    proportional — ``A`` and ``c`` can hardly be told apart — so the error of
    ``A`` gets large.  More curvature in the data (a longer end phase) makes
    them distinguishable and the error shrinks.

    This is a linear approximation around the optimum.  It assumes
    independent errors of equal size and ignores the bounds: if ``A`` sits
    at a bound (target or pit) the value is only a rough indication.

    Args:
        t_h: Times of the readings in hours since the first reading.
        core0: ``T0``, the first reading (fixed in the model).
        ambient: Fitted ``A`` in C.
        c: Fitted rate constant in 1/h.
        sse: Sum of squared residuals at the optimum.

    Returns:
        Standard error of ``A`` in C, or ``None`` if ``A`` and ``c`` cannot
        be separated (``det <= 0``) or there are too few readings.
    """
    n = len(t_h)
    if n < 3:
        return None
    # Step 1: J^T J from the partial derivatives of every reading
    jaa = jac = jcc = 0.0
    for t in t_h:
        e = math.exp(-c * t)
        d_a = 1 - e  # dT/dA
        d_c = (ambient - core0) * t * e  # dT/dc
        jaa += d_a * d_a
        jac += d_a * d_c
        jcc += d_c * d_c
    det = jaa * jcc - jac * jac
    if det <= 0:
        return None
    # Steps 2–4: s^2 = SSE/(n-2); var(A) = s^2 * [(J^T J)^-1]_AA = s^2 * jcc/det
    return math.sqrt(sse / (n - 2) * jcc / det)


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
    hh, mm = map(int, since.split(":"))
    last = datetime.fromtimestamp(readings[-1][0])
    start = last.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start > last:
        start -= timedelta(days=1)
    cutoff = start.timestamp()
    return [r for r in readings if r[0] >= cutoff]


def forecast(
    readings: Sequence[Reading],
    target: float = DEFAULT_TARGET_TEMP,
    pit: float = DEFAULT_PIT_TEMP,
    fixed_ambients: Sequence[float] = DEFAULT_FIXED_AMBIENTS,
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
            fixed.append(fit_fixed_ambient(t_h, core, ambient, target))

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
        free=fit_free_ambient(t_h, core, target, pit),
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

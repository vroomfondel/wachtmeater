# End-phase core temperature forecast

*Deutsche Fassung: [kerntemperatur-prognose.md](kerntemperatur-prognose.md)*

When will the core reach the pull temperature? The watcher answers this with three complementary forecasts:

| Forecast                    | Where                            | What it tells you                                                                                            |
|-----------------------------|----------------------------------|--------------------------------------------------------------------------------------------------------------|
| **MEATER remaining time**   | line `Remaining:`                | Estimate from the MEATER Cloud. If it is missing or 0, the line says `unbekannt` (unknown).                  |
| **Linear watcher forecast** | line `Watcher-Prognose bis …`    | Trend over the last 60 minutes. It gives the **earliest possible arrival**, because the real curve flattens. |
| **End-phase forecast**      | block `Endphasen-Prognose bis …` | Exponential fit after the wrap, physically grounded. It gives a range from "earliest" to "likely".           |

This note describes the end-phase forecast: model, fit, adaptive choice of the fit window, usage and limitations. It was derived from the brisket cook of 10/11 Oct 2026 (MEATER cook `15f2e6f5`). The code lives in [`wachtmeater/core_forecast.py`](../wachtmeater/core_forecast.py), the watcher integration in `wachtmeater/meater_watcher.py` (`_endphase_lines`).

The watcher's messages are in German. Status lines and CLI output below are quoted verbatim.

---

## 1. Physical background

- **Cooking time depends on thickness, not weight.** Heat penetrates to a depth of about $L \approx \sqrt{\alpha \cdot t}$, so $t \approx L^2/\alpha$. Double the thickness takes about four times as long. For meat, the thermal diffusivity is $\alpha \approx 1.3\text{–}1.4 \cdot 10^{-7}\ \text{m}^2/\text{s}$ (Pit Physics).
- **The stall is evaporative cooling.** Greg Blonder showed this experimentally (AmazingRibs). Wrapping largely stops the evaporation, after which the core rises again.
- **There is no ready-made formula for the whole core curve.** Pit Physics states explicitly that you have to fit measured data. That is exactly what the watcher does, but only for the end phase, where a simple model holds.

## 2. Model: why the end phase is exponential

Heat conduction in the meat follows $\partial T/\partial t = \alpha \nabla^2 T$. The boundary condition is an approximately constant effective surface temperature $A$. The solution is a sum of modes that decay at different rates. Late in the cook only the **slowest mode** is left, and for the core:

$$T(t) = A - (A - T_0)\, e^{-c\,t}$$

This is Newton's law of heating. For a slab of thickness $D$ one would ideally get $c \approx \pi^2 \alpha / D^2$. In reality $c$ is smaller, because foil or paper, the heat transfer from air into the meat and remaining evaporation slow things down further. That is why **$c$ is fitted from the data** rather than calculated.

The remaining time from the current value $T_{now}$ to the target $T_Z$ is then:

$$\Delta t = \frac{1}{c}\,\ln\!\frac{A - T_{now}}{A - T_Z}$$

**Consequences:**

- The curve is **not linear**; its slope keeps decreasing. A linear extrapolation therefore gives the earliest arrival.
- The result depends strongly on $A$. If $A$ is only slightly above $T_Z$, the denominator gets small and the remaining time very long. This is where the "second plateau" just below 95 °C at low smoker temperatures comes from.

### What is $A$?

| Situation                  | effective $A$                                                                                                                   |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| Upper bound, always        | **real smoker temperature (pit)**. Not the MEATER ambient reading: that sensor hangs above the meat and is cooled by it.        |
| Aluminium foil with juice  | The surface is capped at **~100 °C** by boiling juice (open foil, 1 atm).                                                       |
| Butcher paper              | Some evaporation continues, so $A$ stays below the smoker temperature.                                                          |
| Unwrapped, after the stall | below the smoker temperature, because evaporation still slows things down                                                       |
| Turning up the smoker      | $A$ rises. This is the most effective lever on the remaining time, but in foil only while the surface is not capped at ~100 °C. |

During the 11 Oct cook the MEATER ambient showed ~101–106 °C (mean 103.8 °C), while the smoker was actually at a constant **110 °C**. That is why the watcher uses the smoker temperature as the upper bound (`set pit`, default 110 °C) and not the ambient reading.

## 3. What the watcher calculates

Over the fit window the watcher calculates three variants:

| Variant                                      | Meaning                                              |
|----------------------------------------------|------------------------------------------------------|
| **A = pit** (fixed)                          | Fastest physically plausible exponential arrival     |
| **A = 100 °C** (fixed)                       | Foil with boiling juice                              |
| **A free** (between target + 0.1 °C and pit) | The $A$ that fits the data best, with standard error |

`wachtmeater eta` additionally prints A = 104 and 102 °C and the linear trend.

### Fitting without numpy and scipy

The original note used `scipy.optimize.curve_fit`. In the watcher image, numpy and scipy would be two heavy new dependencies for one small fit. So `core_forecast` is pure Python:

- **Why no closed form:** $c$ is in the exponent, so the fit is *nonlinear* least squares. You could take the logarithm ($\ln(A - T)$ against $t$) and fit a straight line. But that weights the residuals differently, because errors near $A$ get blown up. It then gives a different $c$ than `curve_fit`.
- **Fixed $A$:** One parameter is left, $c$, in a known range. A one-dimensional **golden-section search** on the sum of squared errors $SSE(c)$ finds it. It needs neither derivatives nor a start value, stays within the bounds and is guaranteed to converge. Each step shrinks the interval by a factor of 0.618.
- **Free $A$:** For each candidate $A$ the best $c$ is determined (the "profile"). The profile is then minimised over $A \in [T_Z + 0.1;\ \text{pit}]$, again by golden section. The bounds are thus built in, and the optimum is the same as `curve_fit` with `bounds`.
- **Uncertainty of $A$** (the "±1.8"): It comes from the Jacobian. For each reading it holds how strongly the model value reacts to a small change of $A$ or $c$:
  - $\partial T/\partial A = 1 - e^{-ct}$: 0 at the start of the window, almost 1 at the end. So mainly the late readings carry information about $A$.
  - $\partial T/\partial c = (A - T_0)\,t\,e^{-ct}$: largest in the middle of the curve.

  These derivatives give $\text{cov} = s^2 (J^\top J)^{-1}$ with $s^2 = SSE/(n-2)$, the same formula `curve_fit` uses. The uncertainty gets large when $A$ and $c$ can stand in for each other: a higher $A$ with a smaller $c$ gives almost the same curve.
- **Runtime:** about 0.2 s for 120 points and 0.03 s for 15 points. The watcher thins its samples to at most 120 points.

The docstrings in `core_forecast.py` explain every calculation step in detail.

## 4. Adaptive fit window

According to the original note, **only the end phase** may be fitted, i.e. after the stall and the wrap, once the rise is steady. Right after the wrap the core still accelerates and the model fits badly. How long this transition lasts depends on the cook. So the watcher does not use a fixed delay; it searches for the fit start anew on every check (`find_endphase_start`):

1. Candidates start **60 minutes after `wrapped`** (`endphase_settle_minutes`) and are spaced **10 minutes** apart (`endphase_search_step_minutes`). Each candidate must be followed by at least **60 minutes of data** (`endphase_min_minutes`).
2. The **earliest** candidate whose free fit is stable wins (`is_stable`). Early is good, because more curve determines $A$ better. Stable means:
   - **RMS ≤ 0.1 °C** (`endphase_stable_rms`). Windows containing the post-wrap transition were at 0.2–0.5 °C.
   - **Standard error of $A$ ≤ 3 °C.** A short window has too little curvature, and $A$ is then arbitrary (±10 °C and more).
   - **$A$ lies at least one standard error above the lower bound.** If $A$ sticks to the target, the optimum actually wanted to go lower. The window is then not yet the end phase, and an ETA far in the future is an artefact of the bound.

   $A$ at the upper bound (pit), on the other hand, is fine: that is a physical limit.
3. The search runs **anew on every check**. With more data an earlier start can become acceptable: on the export it moved from 08:12 to 08:01. At most 36 candidates are tried per search.
4. A **disturbance or pause** (lid open, probe re-seated, `pause`) moves the earliest start later. So the dip is never fitted.

As long as no stable window has been found, the watcher shows a **preliminary A=pit estimate** over the last 60 minutes. It is clearly marked and should be read as the earliest exponential arrival.

An example of why a fixed delay is not enough, from the 11 Oct cook (wrap ~05:30, data until 10:25):

| Fit from | Minutes after wrap | RMS A=pit | free A                    |
|----------|--------------------|-----------|---------------------------|
| 06:39    | ~70                | 0.505     | 96.6 (at the lower bound) |
| 07:20    | ~110               | 0.214     | 96.6 (at the lower bound) |
| 07:40    | ~130               | 0.150     | 96.6 (at the lower bound) |
| 08:00    | ~150               | 0.091     | **98.5 ± 1.8**            |

## 5. Using it in the watcher

| Command           | Effect                                                                                                                                         |
|-------------------|------------------------------------------------------------------------------------------------------------------------------------------------|
| `wrapped [<min>]` | Like `pause` (mutes TempDown, stall and ambient range, then resets max ambient). Marks the wrap as done and **starts the end-phase forecast**. |
| `set pit <C>`     | Real smoker temperature as the upper bound for $A$ (config default: 110 °C)                                                                    |
| `reset pit`       | Back to the config default                                                                                                                     |
| `reset wrap`      | Re-arms the wrap reminder. Also clears the wrap time.                                                                                          |

The status line shows `Smoker-Temp (Pit): 110 C (Standard) [gewrappt 05:24]`. The forecast block goes through three phases.

After the wrap, not enough data yet:

```
Endphasen-Prognose: sammelt Daten (Fit fruehestens ab 06:24, erste Prognose fruehestens ~07:33)
```

Enough data, but no stable fit yet:

```
Endphasen-Prognose: noch kein stabiler Fit seit 06:24 (Anstieg noch nicht gleichmaessig)
  vorlaeufig A=Pit 110 C (frueheste, letzte 60 Min.): ~14:08
```

Stable fit found:

```
Endphasen-Prognose bis 96.0 C (Fit 07:53-10:13, 15 Punkte, Pit 110 C):
  A=Pit 110 C (frueheste): ~14:06
  A=100 C (Folie, Saft kocht): ~15:50
  A frei 98.5±1.8 C (RMS 0.07): ~16:48
```

The target is the pull override (`enable pull`), otherwise the MEATER target.

### Configuration (`[monitoring]`)

| Key                            | Env                            | Default | Meaning                                               |
|--------------------------------|--------------------------------|---------|-------------------------------------------------------|
| `pit_temp`                     | `PIT_TEMP`                     | `110.0` | Real smoker temperature, upper bound for $A$          |
| `endphase_settle_minutes`      | `ENDPHASE_SETTLE_MINUTES`      | `60`    | Earliest fit start after `wrapped` (lower bound only) |
| `endphase_min_minutes`         | `ENDPHASE_MIN_MINUTES`         | `60`    | Minimum data span after a fit start                   |
| `endphase_search_step_minutes` | `ENDPHASE_SEARCH_STEP_MINUTES` | `10`    | Step of the start search                              |
| `endphase_stable_rms`          | `ENDPHASE_STABLE_RMS`          | `0.1`   | Maximum RMS for the end phase to count as steady      |
| `forecast_window_minutes`      | `FORECAST_WINDOW_MINUTES`      | `60`    | Window of the linear watcher forecast                 |

## 6. Special use: analysing an Element chat export

The module itself only works on generic readings `(epoch, core, ambient)`. For after-the-fact analysis, [`wachtmeater/element_export.py`](../wachtmeater/element_export.py) reads the status messages from an Element export (ZIP or `export.json`):

```bash
# fixed window as in the original note
wachtmeater eta "Element - Wachtmeater 15f2e6f5 - Chat Export - ….zip" --pit 110 --target 96.5 --since 08:00
# search the end-phase start adaptively (from --since or the first reading on)
wachtmeater eta "….zip" --since 06:30 --auto
```

Output for the 11 Oct 2026 cook. It is identical to the scipy output in the original note:

```
Fit 08:01–10:25 (15 Punkte): Kern 84.9→89.8 °C, MEATER-Ambient Ø 103.8 °C, Pit 110 °C, Ziel 96.5 °C
linear      : 1.99 °C/h  -> 13:45  (früheste Ankunft)
A=110.0 °C : c=0.092/h rms=0.091 -> 14:49  <- A = Pit (schnellste Exp.-Variante)
A=104.0 °C : c=0.124/h rms=0.078 -> 15:34
A=102.0 °C : c=0.141/h rms=0.073 -> 16:05
A=100.0 °C : c=0.162/h rms=0.069 -> 17:01
A frei (≤Pit): A=98.5±1.8 °C c=0.183/h -> 18:26
```

## 7. Validation

- `tests/test_core_forecast.py` checks $c$, RMS, ETA and the free fit (A, standard error) against this table, using the 15 exact readings from the export.
- `tests/test_cook_replay.py` replays the whole cook through the watcher in 10-minute steps: `wrapped` at 05:24, the forecast phases, the adaptive start at ~08:00, and the effect of `set pit`.

## 8. Interpretation and limitations

- **A range, not a point.** For the 11 Oct cook the result was: earliest ~14:50 (A = pit), likely around 16:00–16:30, with a risk of an outlier towards 18:00 if $A$ really is ~98 °C. MEATER estimated ~15:30 at 10:25.
- **An $A$ close to the target reacts strongly to small changes.** ±1.8 °C in $A$ means several hours of difference in the ETA. The RMS differences between the variants (0.07 vs. 0.09 °C) are close to the sensor resolution. That is a hint, not proof.
- **The stable fit comes late**, about two hours into the end phase. Only the curvature of the later curve separates the variants. Until then the preliminary A=pit line is the best statement, and it is a lower bound.
- **The thresholds are based on a single cook** (RMS 0.1 °C, standard error 3 °C). They are configurable or documented in `core_forecast.py` and should be reviewed after more cooks.
- **The target temperature is only a guide.** The brisket is done when the probe slides in like butter. Start checking at ~93 °C.
- **In a hurry?** Set the smoker to 125–135 °C after the wrap; Pit Physics recommends 275 °F. In foil this only raises $A$ while the surface is not capped at ~100 °C. Butcher paper or a foil boat benefit more.

## Sources

- [Pit Physics – The Science of Competition Brisket](https://www.pit-physics.com/brisket/): $L \approx \sqrt{\alpha t}$, values for $\alpha$, stall = evaporation, boat/wrap at 165–175 °F, then 275 °F
- [AmazingRibs – Understanding and beating the barbecue stall](https://amazingribs.com/more-technique-and-science/more-cooking-science/understanding-and-beating-barbecue-stall/): Blonder's evaporation experiment
- [AmazingRibs – Texas Crutch](https://amazingribs.com/more-technique-and-science/more-cooking-science/texas-crutch/): wrapping in foil or butcher paper
- [AmazingRibs – Texas Brisket (PDF)](https://amazingribs.com/books/Texas-Brisket.pdf): cooking time depends mainly on thickness and average smoker temperature
- [BU Today – Everything You Know about Grilling Is Wrong](https://bu.edu/today/2016/everything-you-know-about-grilling-is-wrong): interview with Greg Blonder
- Measured data: Wachtmeater chat export of cook `15f2e6f5` from 10/11 Oct 2026 (Element), smoker actually at a constant 110 °C
- Derivation: private Obsidian note "Kerntemperatur-Prognose (Endphase)", from which this note and `core_forecast.py` originate

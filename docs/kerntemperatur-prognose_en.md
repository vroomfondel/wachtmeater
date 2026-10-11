# End-phase core temperature forecast

*Deutsche Fassung: [kerntemperatur-prognose.md](kerntemperatur-prognose.md)*

When will the core reach the pull temperature? The watcher answers this with three complementary forecasts:

| Forecast                    | Where                            | What it tells you                                                                                            |
|-----------------------------|----------------------------------|--------------------------------------------------------------------------------------------------------------|
| **MEATER remaining time**   | line `Remaining:`                | Estimate from the MEATER Cloud. If it is missing or 0, the line says `unbekannt` (unknown).                  |
| **Linear watcher forecast** | line `Watcher-Prognose bis …`    | Trend over the last 60 minutes. It gives the **earliest possible arrival**, because the real curve flattens. |
| **End-phase forecast**      | block `Endphasen-Prognose bis …` | Exponential fit after the wrap, physically grounded. It gives a range from "earliest" to "likely".           |

This note describes the end-phase forecast: model, fit, fit window, usage and limitations. It was derived from the brisket cook of 10/11 Oct 2026 (MEATER cook `15f2e6f5`). The code lives in [`wachtmeater/core_forecast.py`](../wachtmeater/core_forecast.py), the watcher integration in `wachtmeater/status_text.py` (`endphase_lines`).

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

The watcher calculates **one** variant: $A$ fixed to the smoker temperature (**A = pit**, `set pit`, default 110 °C). That is the fastest physically plausible exponential arrival. Only $c$ is fitted.

`wachtmeater eta` (CLI) still prints several variants: A = pit, A = 104/102/100 °C (foil with boiling juice), A free (between target + 0.1 °C and pit, with standard error) and the linear trend.

### Fitting with scipy and a robust loss (Huber)

The fits use `scipy.optimize.least_squares`; the original note used `curve_fit`, which wraps the same solver.

- **Nonlinear:** $c$ is in the exponent, so the fit is *nonlinear* least squares. Taking the logarithm ($\ln(A - T)$ against $t$) would have a closed form, but it weights the residuals differently (errors near $A$ get blown up) and fails as soon as a reading reaches $A$.
- **$T_0$ is fitted too**, as in the standard asymptotic regression model (R: `SSasymp`), instead of being anchored at the first reading. Otherwise one noisy first reading shifts the whole curve.
- **Robust loss (Huber):** In a plain fit every error counts squared, so a single bad reading (probe touched, lid open, sensor glitch) pulls the whole curve. The Huber loss counts errors up to a threshold squared and beyond it only linearly. The threshold follows the standard recipe: a first plain fit gives the residuals, their MAD a robust spread $\sigma$, and the threshold is $1.345\,\sigma$ (at least 0.05 °C because of the 0.1 °C sensor resolution).
- **Effect on 11 Oct:** −1 °C in the first reading moved the ETA by 56 minutes (A = pit) or 82 minutes (A = 100 °C) with an anchored $T_0$, by 19 or 29 minutes with a fitted $T_0$, and by 0 or 2 minutes with Huber. On clean data Huber differs from the plain fit by at most 2 minutes; in the 26/27 Sep backtest by a few minutes at most.
- **Bounds:** The free fit keeps $T_Z + 0.1 \le A \le \text{pit}$. It starts from several $A$ values so that no local minimum at a bound wins.
- **Uncertainty of $A$** (the "±4.5"): It comes from the Jacobian at the optimum, i.e. how strongly the model value reacts to small changes of $A$, $T_0$ and $c$. $\text{cov} = s^2 (J^\top J)^{-1}$ with $s^2 = 2\,\text{cost}/(n-3)$, exactly as in `curve_fit`. It gets large when the parameters can stand in for each other: a higher $A$ with a smaller $c$ gives almost the same curve. With $T_0$ as a third parameter, $A$ is less certain than with an anchored $T_0$ (±4.5 instead of ±1.8 °C on 11 Oct); the stability threshold is therefore 5 °C instead of 3 °C.
- **Runtime:** about 1 ms for a fixed-$A$ fit over 120 points, 12 ms for all variants over 15 points, around 0.05 s for the adaptive search of the end-phase start.

The docstrings in `core_forecast.py` explain every calculation step in detail.

## 4. Fit window

According to the original note, **only the end phase** may be fitted, i.e. after the stall and the wrap. The watcher starts the fit at a **fixed time**: `endphase_settle_minutes` (default 60) after `wrapped`.

- $c$ and the start value $T_0$ are fitted (`core_forecast.fit_fixed_ambient`), with $A$ = pit.
- The ETA is computed from the **current** reading: $\Delta t = \ln\frac{A - T_{now}}{A - T_Z}/c$. The measured value partly corrects the model error of a fixed $A$; from the curve value the fixed variants were 8–17 minutes worse in the 26/27 Sep backtest.
- **Outlier in the last reading:** If it is clearly off the curve (Hampel: more than $3\sigma$ of the residuals, at least 0.3 °C), the forecast starts from the curve value, and a second line shows the ETA from the measured value for comparison. −1 °C in the last reading then moves the ETA by 6–12 instead of 32–42 minutes. In the backtest the hint never triggered.
- The first forecast appears once the window holds at least `endphase_min_minutes` (60) of data.
- **Disturbances or pauses** (`trend_ignore_before`) do not move the fit start. They only affect the linear trend and the stall rate. `trend ab HH:MM` restarts their evaluation on demand.

ETAs more than 24 hours ahead are shown as "nicht absehbar (Kern steigt kaum)" (not foreseeable, core barely rising). If the core stays at the same value for an hour, the fit would otherwise give an ETA years in the future. If pit <= target, it says "nie (Pit <= Ziel)" (never).

**Why no adaptive window any more:** in the brisket cook `15f2e6f5` on 11 Oct 2026 a fire flare-up around 11:47–12:08 (ambient up to 113.9 °C, core +5 °C/h, then flat) made every candidate window fail RMS ≤ 0.1 (0.26–0.43 °C). Windows after the bump had a standard error of $A$ of ±125 to ±500 °C. The adaptive search never found a stable window and only fell back to preliminary estimates. With A = pit = 110 °C fixed and the fit from 06:30 (wrap 05:30 + 60 min), the forecast was ~14:38 (data until 13:35, core 94.9 °C, $c$ ≈ 0.108/h).

The adaptive search (`find_endphase_start`) still exists for `wachtmeater eta --auto` (section 6).

## 5. Using it in the watcher

| Command            | Effect                                                                                                                                                                       |
|--------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `wrapped [<min>]`  | Marks the wrap as done and **starts the end-phase forecast**. With `<min>` it also pauses like `pause` (mutes TempDown, stall and ambient range, then resets max ambient). |
| `wrapped at HH:MM` | Reports the wrap afterwards: like `wrapped`, but anchored at the last `HH:MM`, without a pause. Replaces an earlier wrap and drops a trend restart set at/after the previous wrap time. |
| `set pit <C>`      | $A$ of the end-phase forecast, i.e. the real smoker temperature (config default: 110 °C)                                                                                     |
| `reset pit`        | Back to the config default                                                                                                                                                   |
| `trend ab HH:MM`   | Evaluate the linear trend and stall rate only from `HH:MM` on (e.g. after a fire flare-up)                                                                                   |
| `trend reset`      | Undo the trend restart                                                                                                                                                       |
| `reset wrap`       | Re-arms the wrap reminder. Also clears the wrap time.                                                                                                                        |

The status line shows `Smoker-Temp (Pit): 110 C (Standard) [gewrappt 05:24]`. After `wrapped` the watcher confirms, e.g.: `Wrap als erledigt markiert (05:30). Endphasen-Prognose fittet ab 06:30 mit A = Pit 110 C (aendern mit 'set pit <C>'), Prognose fruehestens nach 60 Min. Daten.`

The forecast block has two states. After the wrap, not enough data yet:

```
Endphasen-Prognose: sammelt Daten (Fit ab 06:24, erste Prognose fruehestens ~07:33)
```

Enough data:

```
Endphasen-Prognose bis 96.0 C (A=Pit 110 C, Fit 06:33-10:13, 23 Punkte, RMS 0.33 C): ~13:40
```

Edge cases: `...: nie (Pit <= Ziel)` and `...: nicht absehbar (Kern steigt kaum)` (ETA > 24 h).

The target is the pull override (`enable pull`), otherwise the MEATER target.

### Configuration (`[monitoring]`)

| Key                            | Env                            | Default | Meaning                                               |
|--------------------------------|--------------------------------|---------|-------------------------------------------------------|
| `pit_temp`                     | `PIT_TEMP`                     | `110.0` | $A$ of the end-phase forecast (real smoker temperature) |
| `endphase_settle_minutes`      | `ENDPHASE_SETTLE_MINUTES`      | `60`    | Fixed fit start after `wrapped`                          |
| `endphase_min_minutes`         | `ENDPHASE_MIN_MINUTES`         | `60`    | Minimum data span before the first forecast              |
| `forecast_window_minutes`      | `FORECAST_WINDOW_MINUTES`      | `60`    | Window of the linear watcher forecast                 |

## 6. Special use: analysing an Element chat export

The module itself only works on generic readings `(epoch, core, ambient)`. For after-the-fact analysis, [`wachtmeater/element_export.py`](../wachtmeater/element_export.py) reads the status messages from an Element export (ZIP or `export.json`):

```bash
# fixed window as in the original note
wachtmeater eta "Element - Wachtmeater 15f2e6f5 - Chat Export - ….zip" --pit 110 --target 96.5 --since 08:00
# search the end-phase start adaptively (from --since or the first reading on)
wachtmeater eta "….zip" --since 06:30 --auto
# forecast as of an earlier time: only readings up to --until (also cuts off the rest phase)
wachtmeater eta "Element - Wachtmeater e62a2cc9 - ….zip" --since 05:46 --until 07:52 --auto --target 97 --pit 120
```

Output for the 11 Oct 2026 cook (Huber). Without the robust loss (`loss="linear"`) it is identical to `scipy.optimize.curve_fit` with $A$, $T_0$ and $c$ free and differs by at most one minute (free A = 102.5 ± 3.9 °C → 16:05). The original note anchored $T_0$ at the first reading and therefore got different values (free A = 98.5 ± 1.8 °C → 18:26):

```
Fit 08:01–10:25 (15 Punkte): Kern 84.9→89.8 °C, MEATER-Ambient Ø 103.8 °C, Pit 110 °C, Ziel 96.5 °C
linear      : 1.99 °C/h  -> 13:45  (früheste Ankunft)
A=110.0 °C : c=0.088/h rms=0.062 -> 14:59  <- A = Pit (schnellste Exp.-Variante)
A=104.0 °C : c=0.121/h rms=0.058 -> 15:43
A=102.0 °C : c=0.137/h rms=0.058 -> 16:13
A=100.0 °C : c=0.159/h rms=0.059 -> 17:08
A frei (≤Pit): A=102.3±4.5 °C c=0.135/h -> 16:07
```

**Reading the output** (it is in German):

| Term                                    | Meaning                                                                                                                        |
|-----------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|
| `Fit 08:01–10:25 (15 Punkte)`           | fit window and number of readings (*Punkte* = points)                                                                          |
| `Kern 84.9→89.8 °C`                     | core temperature at the start and end of the window                                                                            |
| `MEATER-Ambient Ø`                      | mean MEATER ambient — for information only, not a valid $A$                                                                    |
| `Ziel`                                  | target core temperature                                                                                                        |
| `linear … (früheste Ankunft)`           | linear trend, the earliest arrival                                                                                             |
| `A=… : c=…/h rms=… -> 14:59`            | exponential fit with fixed $A$: rate constant $c$, RMS of the residuals in °C, ETA (wall-clock time of the last reading's day) |
| `<- A = Pit (schnellste Exp.-Variante)` | marks the variant with $A$ = pit, the fastest exponential one                                                                  |
| `A frei (≤Pit): A=102.3±4.5 °C`        | free fit, $A$ bounded by the pit temperature, with standard error                                                              |
| `nie (A <= Ziel)`                       | never: $A$ does not exceed the target                                                                                          |
| `nicht absehbar (…)`                    | not foreseeable: ETA more than 24 h ahead (core barely rising)                                                                 |

**Options and edge cases:**

- `--auto` from 06:30 finds the start 08:01 by itself and prints exactly the output above.
- If `--auto` finds no stable window, the command logs an error and exits with code 1. This always happens with whole-degree exports (see section 7).
- `--until HH:MM` uses only readings up to that minute. On a finished export this shows what the forecast would have said at an earlier time. It also cuts off the rest phase after the pull, which would otherwise be fitted too. As with `--since`, the day of the last reading applies; if the time lies after it, the previous day is meant.
- Without `--pit` the config value `pit_temp` applies (default 110 °C). `--target` defaults to 96.5 °C.
- The export must contain the watcher's status messages (`Internal (Fleisch): … Ambient (Rauch): …`). Other messages are ignored.

**Caveat:** This export ends at 10:25 with the core at 89.8 °C. When this cook actually reached the target is not recorded in it. The table therefore proves that the calculation matches scipy, not that the forecast is right. For the accuracy of the forecast, see the backtest in section 7.

**Example with a known arrival:** cook of 26/27 Sep, wrap at 04:46, target 97 °C actually reached at **09:25**. The smoker temperature is unknown; the MEATER ambient read 113–126 °C after the wrap, hence `--pit 120`. As of 07:52:

```
Fit 06:30–07:52 (9 Punkte): Kern 86.1→94.7 °C, MEATER-Ambient Ø 116.3 °C, Pit 120 °C, Ziel 97.0 °C
linear      : 6.16 °C/h  -> 08:08  (früheste Ankunft)
A=120.0 °C : c=0.217/h rms=0.388 -> 08:19  <- A = Pit (schnellste Exp.-Variante)
A=104.0 °C : c=0.494/h rms=0.243 -> 08:27
A=102.0 °C : c=0.587/h rms=0.195 -> 08:31
A=100.0 °C : c=0.722/h rms=0.129 -> 08:40
A frei (≤Pit): A=98.0±0.4 °C c=0.935/h -> 09:09
```

The free fit is 16 minutes early, the fixed variants 45–66 minutes. With `--until 08:23` the free fit predicts 09:45 (20 minutes late).

## 7. Validation

- `tests/test_core_forecast.py` checks $c$, RMS, ETA and the free fit (A, standard error) against this table, using the 15 exact readings from the export.
- `tests/test_cook_replay.py` replays the whole cook through the watcher in 10-minute steps: `wrapped` at 05:24, the forecast states, the fixed fit start (fit from 06:33, wrap 05:24), and the effect of `set pit`.

### Analysed cooks

Besides the cook of 10/11 Oct, five more Element exports were analysed:

| Export     | Date      | Resolution    | Course                                              | Target reached                        | Use                                       |
|------------|-----------|---------------|-----------------------------------------------------|---------------------------------------|-------------------------------------------|
| `15f2e6f5` | 10/11 Oct | 0.1 °C        | wrap ~05:30; export ends 10:25 at 89.8 °C           | unknown                               | derivation, tests; free A = 102.3 ± 4.5 °C |
| `e62a2cc9` | 26/27 Sep | 0.1 °C        | stall 01:51–04:46, wrap 04:46                       | 95 °C 08:03, 96 °C 08:44, 97 °C 09:25 | backtest; free A = 97.4–98.1 °C           |
| `8d401460` | 25 Jul    | whole degrees | stall 03:22–07:00, wrap probably ~06:30             | 95 °C 13:08, 96 °C 14:10              | backtest                                  |
| `62bebe00` | 23 Jun    | whole degrees | data gap 01:12–07:29                                | 95 °C 13:06, 96 °C 13:27              | backtest                                  |
| `f35f7682` | 2 May     | whole degrees | watcher started at 78 °C; core at 93 °C 15:45–16:57 | 93 °C 15:45, 94 °C 17:08              | backtest                                  |
| `758731c2` | 27 Sep    | 0.1 °C        | rest phase only (75.9 → 69 °C)                      | –                                     | ignored                                   |

The whole-degree exports predate the WebSocket (DOM scraping). The smoker temperature of the older cooks is not known. The calculations used 110, 120 and 130 °C; this changes little, because the freely fitted A lies far below.

### Backtest

Based on the exports from 2 May, 23 Jun, 25 Jul and 26/27 Sep 2026. The backtest describes the earlier, adaptive watcher version (A = 100 °C and free A now exist only in the CLI). For every end-phase check, the predictions of each method were recomputed and compared with the actual arrival at the target. Error in minutes, median, negative = too early.

Only the 26/27 Sep cook has decimals and a known arrival:

| Method          | < 1 h before | 1–2 h   | 2–3 h   |
|-----------------|--------------|---------|---------|
| MEATER          | +6           | −42     | −89     |
| linear          | −20          | −61     | −103    |
| A = pit         | −20          | −56     | −90     |
| **A = 100 °C**  | **−12**      | **−33** | **−48** |
| A free (stable) | −4           | −20     | –       |

The other three cooks only have whole degrees, because they predate the WebSocket. Rounding alone produces an RMS of about 0.29 °C there. A stable fit is therefore never reached, not even with a relaxed threshold, and $A$ cannot be determined from such data. All methods, MEATER included, scatter by about ±1.5 hours more than 2 hours out.

Errors for the three whole-degree cooks (median in minutes, pit 120 °C; A = pit and A = 100 °C over the last 60 minutes, like the preliminary display of the earlier watcher version):

| Method     | < 1 h before | 1–2 h | 2–3 h | > 3 h |
|------------|--------------|-------|-------|-------|
| MEATER     | +66          | +33   | −9    | −81   |
| linear     | +14          | −4    | −53   | −100  |
| A = pit    | +11          | +3    | −46   | −69   |
| A = 100 °C | +20          | +30   | −7    | −2    |

Some medians look good, but the spread is large: more than 3 hours out, single values are off by up to ±3 hours. Shortly before the target, MEATER is clearly too pessimistic on these cooks (up to +140 minutes).

### Alerts on the earlier cooks

All cooks were also replayed through the watcher's current alert logic, without the commands sent manually back then:

| Cook       | Stall alert | actual stall | TempDown without warm-up guard                             | with warm-up guard (from core 40 °C) |
|------------|-------------|--------------|------------------------------------------------------------|--------------------------------------|
| `15f2e6f5` | 03:24       | 02:30–05:30  | 2 (wrap 05:37/05:48)                                       | 2 (wrap; 0 with `pause`)             |
| `e62a2cc9` | 02:42       | 01:51–04:46  | ~18 (ambient spike of 117 °C during warm-up, then ~105 °C) | 1 (wrap 04:46)                       |
| `8d401460` | 03:53       | 03:22–07:00  | 4 (107 °C at start)                                        | 0                                    |

- Stall detection hits all three real stalls, also with whole degrees. The wrap does not cause a false stall alert.
- For `62bebe00` and `f35f7682` it reports a "stall" at 79–80 °C after the wrap: the rise was slow there (2–3 °C/h), and whole degrees make the rate coarse. In that case the message says "Bereits gewrappt" (already wrapped) instead of "Jetzt wrappen?" (wrap now?).
- Back then the false TempDown alerts during warm-up were worked around with `reset ambient`. The warm-up guard (`tempdown_warmup_core_temp`, default 40 °C) removes them. A wrap is covered by `pause` or `wrapped <min>`.

## 8. Interpretation and limitations

- **A range, not a point.** For the 11 Oct cook the result was: earliest ~14:50 (A = pit), likely around 16:00–16:30, with a risk of an outlier towards 18:00 if $A$ really is ~98 °C. MEATER estimated ~15:30 at 10:25.
- **An $A$ close to the target reacts strongly to small changes.** A few °C in $A$ mean several hours of difference in the ETA (anchored $T_0$ gave 98.5 °C → 18:26, fitted $T_0$ 102.3 °C → 16:07). The RMS differences between the variants (0.058 vs. 0.062 °C) are close to the sensor resolution. That is a hint, not proof.
- **The watcher shows only A = pit**, i.e. the earliest exponential arrival. A free $A$ needs about two hours of end phase before the curvature separates the variants (see `wachtmeater eta`). A = 100 °C was the best early estimate in the backtest.
- **With whole degrees (browser fallback) the end-phase forecast does not work.** This applies to fitting $A$ freely; the watcher still calculates with fixed A = pit.
- **The thresholds are based on few cooks** (RMS 0.1 °C, standard error 5 °C), only one of them with decimals and a known arrival. They are configurable or documented in `core_forecast.py` and should be reviewed after more cooks.
- **The target temperature is only a guide.** The brisket is done when the probe slides in like butter. Start checking at ~93 °C.
- **In a hurry?** Set the smoker to 125–135 °C after the wrap; Pit Physics recommends 275 °F. In foil this only raises $A$ while the surface is not capped at ~100 °C. Butcher paper or a foil boat benefit more.

## Sources

- [Pit Physics – The Science of Competition Brisket](https://www.pit-physics.com/brisket/): $L \approx \sqrt{\alpha t}$, values for $\alpha$, stall = evaporation, boat/wrap at 165–175 °F, then 275 °F
- [AmazingRibs – Understanding and beating the barbecue stall](https://amazingribs.com/more-technique-and-science/more-cooking-science/understanding-and-beating-barbecue-stall/): Blonder's evaporation experiment
- [AmazingRibs – Texas Crutch](https://amazingribs.com/more-technique-and-science/more-cooking-science/texas-crutch/): wrapping in foil or butcher paper
- [AmazingRibs – Texas Brisket (PDF)](https://amazingribs.com/books/Texas-Brisket.pdf): cooking time depends mainly on thickness and average smoker temperature
- [BU Today – Everything You Know about Grilling Is Wrong](https://bu.edu/today/2016/everything-you-know-about-grilling-is-wrong): interview with Greg Blonder
- Measured data: Wachtmeater chat export of cook `15f2e6f5` from 10/11 Oct 2026 (Element), smoker actually at a constant 110 °C
- Backtest data: Wachtmeater chat exports `e62a2cc9` (26/27 Sep 2026), `8d401460` (25 Jul 2026), `62bebe00` (23 Jun 2026), `f35f7682` (2 May 2026); `758731c2` (27 Sep 2026, rest phase only) not used
- Derivation: private Obsidian note "Kerntemperatur-Prognose (Endphase)", from which this note and `core_forecast.py` originate

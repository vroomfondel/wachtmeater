# Kerntemperatur-Prognose in der Endphase

*English version: [kerntemperatur-prognose_en.md](kerntemperatur-prognose_en.md)*

Wann erreicht der Kern die Pull-Temperatur? Der Watcher beantwortet das mit drei Prognosen, die sich ergänzen:

| Prognose                     | Wo                               | Was sie aussagt                                                                                                      |
|------------------------------|----------------------------------|----------------------------------------------------------------------------------------------------------------------|
| **MEATER-Restzeit**          | Zeile `Remaining:`               | Schätzung der MEATER Cloud. Fehlt sie oder ist sie 0, steht dort `unbekannt`.                                        |
| **Lineare Watcher-Prognose** | Zeile `Watcher-Prognose bis …`   | Trend der letzten 60 Minuten. Gibt die **früheste mögliche Ankunft** an, weil die echte Kurve abflacht.              |
| **Endphasen-Prognose**       | Block `Endphasen-Prognose bis …` | Exponentieller Fit nach dem Wrap, physikalisch begründet. Gibt eine Spanne von „frühestens“ bis „wahrscheinlich“ an. |

Diese Notiz beschreibt die Endphasen-Prognose: Modell, Fit, Fit-Fenster, Bedienung und Grenzen. Hergeleitet wurde sie am Brisket-Cook vom 10./11.10.2026 (MEATER-Cook `15f2e6f5`). Der Code steht in [`wachtmeater/core_forecast.py`](../wachtmeater/core_forecast.py), die Einbindung in den Watcher in `wachtmeater/status_text.py` (`endphase_lines`).

---

## 1. Physikalischer Hintergrund

- **Die Garzeit hängt an der Dicke, nicht am Gewicht.** Die Wärme dringt etwa $L \approx \sqrt{\alpha \cdot t}$ tief ein, also gilt $t \approx L^2/\alpha$. Doppelte Dicke heißt etwa viermal so lange. Für Fleisch liegt die Temperaturleitfähigkeit bei $\alpha \approx 1{,}3\text{–}1{,}4 \cdot 10^{-7}\ \text{m}^2/\text{s}$ (Pit Physics).
- **Der Stall ist Verdunstungskühlung.** Greg Blonder hat das experimentell gezeigt (AmazingRibs). Wrappen stoppt die Verdunstung weitgehend, danach steigt der Kern wieder.
- **Eine fertige Formel für den gesamten Kernverlauf gibt es nicht.** Pit Physics sagt ausdrücklich, dass man dafür Messdaten fitten muss. Genau das macht der Watcher, aber nur für die Endphase, in der ein einfaches Modell trägt.

## 2. Modell: Warum die Endphase exponentiell verläuft

Die Wärmeleitung im Fleisch folgt $\partial T/\partial t = \alpha \nabla^2 T$. Als Randbedingung dient eine annähernd konstante wirksame Oberflächentemperatur $A$. Die Lösung ist eine Summe von Moden, die unterschiedlich schnell abklingen. Spät im Cook bleibt nur noch die **langsamste Mode**, und für den Kern gilt:

$$T(t) = A - (A - T_0)\, e^{-c\,t}$$

Das ist das Newton'sche Aufwärmgesetz. Für eine Platte der Dicke $D$ wäre idealisiert $c \approx \pi^2 \alpha / D^2$. Real ist $c$ kleiner, weil Folie bzw. Paper, der Wärmeübergang von der Luft ins Fleisch und Restverdunstung zusätzlich bremsen. Deshalb wird **$c$ aus den Daten gefittet** und nicht ausgerechnet.

Die Restzeit vom aktuellen Wert $T_{now}$ bis zum Ziel $T_Z$ ist dann:

$$\Delta t = \frac{1}{c}\,\ln\!\frac{A - T_{now}}{A - T_Z}$$

**Folgerungen:**

- Die Kurve ist **nicht linear**, ihre Steigung nimmt ständig ab. Eine lineare Hochrechnung liefert deshalb die früheste Ankunft.
- Das Ergebnis hängt stark von $A$ ab. Liegt $A$ nur knapp über $T_Z$, wird der Nenner klein und die Restzeit sehr lang. Daher kommt das „zweite Plateau“ kurz vor 95 °C bei niedriger Smokertemperatur.

### Was ist $A$?

| Situation                 | wirksames $A$                                                                                                                          |
|---------------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| Obergrenze, immer         | **echte Smokertemperatur (Pit)**. Nicht der MEATER-Ambient-Wert: Der Sensor hängt über dem Fleisch und wird von ihm gekühlt.           |
| Alufolie mit Saft         | Die Oberfläche ist durch kochenden Saft bei **~100 °C** gedeckelt (offene Folie, 1 atm).                                               |
| Butcher Paper             | Es verdunstet weiter etwas, $A$ liegt also unter der Smokertemperatur.                                                                 |
| Unwrapped, nach dem Stall | unter der Smokertemperatur, weil die Verdunstung noch bremst                                                                           |
| Smoker höher drehen       | $A$ steigt. Das ist der wirksamste Hebel für die Restzeit, bei Folie aber nur, solange die Oberfläche nicht bei ~100 °C gedeckelt ist. |

Beim Cook vom 11.10. zeigte der MEATER-Ambient ~101–106 °C (Ø 103,8 °C), der Smoker lag aber konstant bei **110 °C**. Deshalb nimmt der Watcher die Smokertemperatur als Obergrenze (`set pit`, Default 110 °C) und nicht den Ambient-Wert.

## 3. Was der Watcher berechnet

Der Watcher rechnet **eine** Variante: $A$ fest auf der Smokertemperatur (**A = Pit**, `set pit`, Default 110 °C). Das ist die schnellste physikalisch plausible exponentielle Ankunft. Nur $c$ wird gefittet.

`wachtmeater eta` (CLI) gibt weiterhin mehrere Varianten aus: A = Pit, A = 104/102/100 °C (Folie mit kochendem Saft), A frei (zwischen Ziel + 0,1 °C und Pit, mit Standardfehler) und den linearen Trend.

### Fit mit scipy und robustem Verlust (Huber)

Gefittet wird mit `scipy.optimize.least_squares`; die Original-Notiz nutzte `curve_fit`, das intern denselben Löser verwendet.

- **Nichtlinear:** $c$ steht im Exponenten, der Fit ist also *nichtlineare* kleinste Quadrate. Logarithmieren ($\ln(A - T)$ gegen $t$) wäre geschlossen lösbar, gewichtet die Abweichungen aber anders (Fehler nahe $A$ werden aufgeblasen) und scheitert, sobald ein Messwert $A$ erreicht.
- **$T_0$ wird mitgefittet** wie im Standardmodell der asymptotischen Regression (R: `SSasymp`), statt am ersten Messwert verankert zu werden. Sonst verschiebt ein einziger verrauschter erster Punkt die ganze Kurve.
- **Robuster Verlust (Huber):** Beim normalen Fit zählt jeder Fehler im Quadrat, ein einzelner falscher Messwert (Fühler berührt, Deckel auf, Sensor-Glitch) zieht deshalb die ganze Kurve. Die Huber-Verlustfunktion zählt Fehler bis zu einer Schwelle quadratisch, darüber nur linear. Die Schwelle folgt dem Standardrezept: Ein erster normaler Fit liefert die Residuen, deren MAD eine robuste Streuung $\sigma$, die Schwelle ist $1{,}345\,\sigma$ (mindestens 0,05 °C wegen der Sensorauflösung von 0,1 °C).
- **Wirkung am 11.10.:** −1 °C im ersten Messpunkt verschob die ETA mit verankertem $T_0$ um 56 Minuten (A = Pit) bzw. 82 Minuten (A = 100 °C), mit gefittetem $T_0$ um 19 bzw. 29 Minuten, mit Huber um 0 bzw. 2 Minuten. Auf sauberen Daten weicht Huber höchstens 2 Minuten vom normalen Fit ab, im Backtest vom 26./27.09. höchstens wenige Minuten.
- **Grenzen:** Der freie Fit hält $T_Z + 0{,}1 \le A \le \text{Pit}$ ein. Er startet von mehreren $A$-Werten aus, damit kein lokales Minimum an einer Grenze gewinnt.
- **Unsicherheit von $A$** (das „±4,5“): Sie kommt aus der Jacobi-Matrix im Optimum, also daraus, wie stark der Modellwert auf kleine Änderungen von $A$, $T_0$ und $c$ reagiert. $\text{cov} = s^2 (J^\top J)^{-1}$ mit $s^2 = 2\,\text{cost}/(n-3)$, genau wie bei `curve_fit`. Die Unsicherheit wird groß, wenn sich die Parameter gegenseitig ersetzen können: Ein höheres $A$ mit kleinerem $c$ ergibt fast dieselbe Kurve. Mit $T_0$ als drittem Parameter ist $A$ unsicherer als mit verankertem $T_0$ (±4,5 statt ±1,8 °C am 11.10.); die Stabilitätsschwelle liegt deshalb bei 5 °C statt 3 °C.
- **Laufzeit:** etwa 1 ms für einen Fit mit festem $A$ über 120 Punkte, 12 ms für alle Varianten über 15 Punkte, rund 0,05 s für die adaptive Suche des Endphasen-Starts.

Die Docstrings in `core_forecast.py` erklären jeden Rechenschritt im Detail.

## 4. Fit-Fenster

Laut Original-Notiz darf **nur die Endphase** gefittet werden, also nach Stall und Wrap. Der Watcher startet den Fit zu einem **festen Zeitpunkt**: `endphase_settle_minutes` (Default 60) nach `wrapped`.

- Gefittet werden $c$ und der Startwert $T_0$ (`core_forecast.fit_fixed_ambient`), mit $A$ = Pit.
- Die ETA wird vom **aktuellen** Messwert gerechnet: $\Delta t = \ln\frac{A - T_{now}}{A - T_Z}/c$.
- Die erste Prognose kommt, sobald das Fenster mindestens `endphase_min_minutes` (60) Daten enthält.
- **Störungen oder Pausen** (`trend_ignore_before`) verschieben den Fit-Start nicht. Sie wirken nur auf den linearen Trend und die Stall-Rate. Mit `trend ab HH:MM` lässt sich deren Auswertung gezielt neu ansetzen.

ETAs, die mehr als 24 Stunden in der Zukunft liegen, zeigt der Watcher als „nicht absehbar (Kern steigt kaum)“. Steht der Kern eine Stunde lang auf demselben Wert, liefert der Fit sonst eine ETA Jahre in der Zukunft. Ist Pit <= Ziel, steht dort „nie (Pit <= Ziel)“.

**Warum kein adaptives Fenster mehr:** Beim Brisket-Cook `15f2e6f5` am 11.10.2026 gab es gegen ~11:47–12:08 ein Aufflammen des Feuers (Ambient bis 113,9 °C, Kern +5 °C/h, danach flach). Damit scheiterte jedes Kandidaten-Fenster an RMS ≤ 0,1 (0,26–0,43 °C). Fenster nach dem Ausschlag hatten einen Standardfehler von $A$ von ±125 bis ±500 °C. Die adaptive Suche fand nie ein stabiles Fenster und fiel nur auf vorläufige Schätzungen zurück. Mit festem A = Pit = 110 °C und Fit ab 06:30 (Wrap 05:30 + 60 Min.) lag die Prognose bei ~14:38 (Daten bis 13:35, Kern 94,9 °C, $c$ ≈ 0,108/h).

Die adaptive Suche (`find_endphase_start`) gibt es weiterhin für `wachtmeater eta --auto` (Abschnitt 6).

## 5. Bedienung im Watcher

| Befehl             | Wirkung                                                                                                                                                                          |
|--------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `wrapped [<min>]`  | Markiert den Wrap als erledigt und **startet die Endphasen-Prognose**. Mit `<min>` zusätzlich wie `pause` (TempDown, Stall und Ambient-Bereich stumm, danach Max-Ambient-Reset). |
| `wrapped at HH:MM` | Wrap nachtragen: wie `wrapped`, aber auf das letzte `HH:MM` rückdatiert, ohne Pause. Ersetzt einen früheren Wrap und verwirft einen Trend-Neustart, der zum vorigen Wrap-Zeitpunkt oder später gesetzt wurde. Alias: `gewrappt um HH:MM`. |
| `set pit <C>`      | $A$ der Endphasen-Prognose, also die echte Smokertemperatur (Default aus der Config: 110 °C)                                                                                     |
| `reset pit`        | Zurück auf den Config-Default                                                                                                                                                    |
| `trend ab HH:MM`   | Linearen Trend und Stall-Rate erst ab `HH:MM` auswerten (z. B. nach einem Aufflammen des Feuers)                                                                                 |
| `trend reset`      | Trend-Neustart zurücknehmen                                                                                                                                                      |
| `reset wrap`       | Wrap-Reminder neu scharf schalten. Löscht auch den Wrap-Zeitpunkt.                                                                                                               |

Die Statuszeile zeigt `Smoker-Temp (Pit): 110 C (Standard) [gewrappt 05:24]`. Nach `wrapped` bestätigt der Watcher z. B.: `Wrap als erledigt markiert (05:30). Endphasen-Prognose fittet ab 06:30 mit A = Pit 110 C (aendern mit 'set pit <C>'), Prognose fruehestens nach 60 Min. Daten.`

Der Prognose-Block hat zwei Zustände. Nach dem Wrap, noch zu wenig Daten:

```
Endphasen-Prognose: sammelt Daten (Fit ab 06:24, erste Prognose fruehestens ~07:33)
```

Genug Daten:

```
Endphasen-Prognose bis 96.0 C (A=Pit 110 C, Fit 06:33-10:13, 23 Punkte, RMS 0.33 C): ~13:40
```

Sonderfälle: `...: nie (Pit <= Ziel)` und `...: nicht absehbar (Kern steigt kaum)` (ETA > 24 h).

Als Ziel gilt der Pull-Override (`enable pull`), sonst das MEATER-Ziel.

### Konfiguration (`[monitoring]`)

| Schlüssel                      | Env                            | Default | Bedeutung                                               |
|--------------------------------|--------------------------------|---------|---------------------------------------------------------|
| `pit_temp`                     | `PIT_TEMP`                     | `110.0` | $A$ der Endphasen-Prognose (echte Smokertemperatur)     |
| `endphase_settle_minutes`      | `ENDPHASE_SETTLE_MINUTES`      | `60`    | Fester Fit-Start nach `wrapped`                         |
| `endphase_min_minutes`         | `ENDPHASE_MIN_MINUTES`         | `60`    | Mindest-Datenspanne vor der ersten Prognose             |
| `forecast_window_minutes`      | `FORECAST_WINDOW_MINUTES`      | `60`    | Fenster der linearen Watcher-Prognose                   |

## 6. Sondernutzung: Auswertung eines Element-Chat-Exports

Das Modul selbst arbeitet nur mit generischen Messpunkten `(epoch, kern, ambient)`. Für eine nachträgliche Auswertung liest [`wachtmeater/element_export.py`](../wachtmeater/element_export.py) die Statusnachrichten aus einem Element-Export (ZIP oder `export.json`):

```bash
# festes Fenster wie in der Original-Notiz
wachtmeater eta "Element - Wachtmeater 15f2e6f5 - Chat Export - ….zip" --pit 110 --target 96.5 --since 08:00
# Start der Endphase adaptiv suchen (ab --since bzw. ab dem ersten Messpunkt)
wachtmeater eta "….zip" --since 06:30 --auto
# Stand zu einem früheren Zeitpunkt: nur Messpunkte bis --until (schneidet auch die Ruhephase ab)
wachtmeater eta "Element - Wachtmeater e62a2cc9 - ….zip" --since 05:46 --until 07:52 --auto --target 97 --pit 120
```

Ausgabe für den Cook vom 11.10.2026 (Huber). Ohne robusten Verlust (`loss="linear"`) ist sie identisch zu `scipy.optimize.curve_fit` mit $A$, $T_0$ und $c$ frei und weicht höchstens eine Minute ab (A frei = 102,5 ± 3,9 °C → 16:05). Die Original-Notiz hatte $T_0$ am ersten Punkt verankert und kam deshalb auf andere Werte (A frei = 98,5 ± 1,8 °C → 18:26):

```
Fit 08:01–10:25 (15 Punkte): Kern 84.9→89.8 °C, MEATER-Ambient Ø 103.8 °C, Pit 110 °C, Ziel 96.5 °C
linear      : 1.99 °C/h  -> 13:45  (früheste Ankunft)
A=110.0 °C : c=0.088/h rms=0.062 -> 14:59  <- A = Pit (schnellste Exp.-Variante)
A=104.0 °C : c=0.121/h rms=0.058 -> 15:43
A=102.0 °C : c=0.137/h rms=0.058 -> 16:13
A=100.0 °C : c=0.159/h rms=0.059 -> 17:08
A frei (≤Pit): A=102.3±4.5 °C c=0.135/h -> 16:07
```

**So liest man die Ausgabe:**

| Teil                                    | Bedeutung                                                                                          |
|-----------------------------------------|----------------------------------------------------------------------------------------------------|
| `Fit 08:01–10:25 (15 Punkte)`           | Fit-Fenster und Zahl der Messpunkte                                                                |
| `Kern 84.9→89.8 °C`                     | Kerntemperatur am Anfang und Ende des Fensters                                                     |
| `MEATER-Ambient Ø`                      | mittlerer MEATER-Ambient — nur zur Info, kein gültiges $A$                                         |
| `linear … (früheste Ankunft)`           | linearer Trend, die früheste Ankunft                                                               |
| `A=… : c=…/h rms=… -> 14:59`            | Exponentieller Fit mit festem $A$: Ratenkonstante $c$, RMS der Abweichungen in °C, ETA als Uhrzeit |
| `<- A = Pit (schnellste Exp.-Variante)` | markiert die Variante mit $A$ = Pit                                                                |
| `A frei (≤Pit): A=102.3±4.5 °C`        | freier Fit, $A$ durch die Smokertemperatur begrenzt, mit Standardfehler                            |
| `nie (A <= Ziel)`                       | $A$ liegt nicht über dem Ziel, die Kurve erreicht es nie                                           |
| `nicht absehbar (…)`                    | ETA mehr als 24 Stunden in der Zukunft, der Kern steigt kaum                                       |

**Optionen und Sonderfälle:**

- `--auto` findet von 06:30 aus selbst den Start 08:01 und gibt genau die Ausgabe oben aus.
- Findet `--auto` kein stabiles Fenster, meldet der Befehl einen Fehler und endet mit Exit-Code 1. Das passiert bei Exporten mit ganzen Grad immer (siehe Abschnitt 7).
- `--until HH:MM` nimmt nur Messpunkte bis zu dieser Minute. So lässt sich an einem fertigen Export nachrechnen, was die Prognose zu einem früheren Zeitpunkt gesagt hätte. Außerdem schneidet es die Ruhephase nach dem Pull ab, die sonst mitgefittet würde. Wie bei `--since` gilt der Tag der letzten Messung; liegt die Uhrzeit danach, ist der Vortag gemeint.
- Ohne `--pit` gilt der Config-Wert `pit_temp` (Default 110 °C). `--target` steht per Default auf 96,5 °C.
- Der Export muss die Statusnachrichten des Watchers enthalten (`Internal (Fleisch): … Ambient (Rauch): …`). Andere Nachrichten werden ignoriert.

**Einschränkung:** Dieser Export endet um 10:25 bei 89,8 °C Kern. Wann der Cook das Ziel tatsächlich erreicht hat, steht nicht darin. Die Tabelle belegt also, dass die Rechnung mit scipy übereinstimmt, nicht dass die Prognose stimmt. Zur Treffsicherheit siehe den Backtest in Abschnitt 7.

**Beispiel mit bekannter Ankunft:** Cook vom 26./27.09., Wrap um 04:46, Ziel 97 °C tatsächlich um **09:25** erreicht. Die Smokertemperatur ist unbekannt; der MEATER-Ambient lag nach dem Wrap bei 113–126 °C, daher `--pit 120`. Stand 07:52:

```
Fit 06:30–07:52 (9 Punkte): Kern 86.1→94.7 °C, MEATER-Ambient Ø 116.3 °C, Pit 120 °C, Ziel 97.0 °C
linear      : 6.16 °C/h  -> 08:08  (früheste Ankunft)
A=120.0 °C : c=0.217/h rms=0.388 -> 08:19  <- A = Pit (schnellste Exp.-Variante)
A=104.0 °C : c=0.494/h rms=0.243 -> 08:27
A=102.0 °C : c=0.587/h rms=0.195 -> 08:31
A=100.0 °C : c=0.722/h rms=0.129 -> 08:40
A frei (≤Pit): A=98.0±0.4 °C c=0.935/h -> 09:09
```

Der freie Fit liegt 16 Minuten zu früh, alle festen Varianten 45–66 Minuten. Mit `--until 08:23` sagt der freie Fit 09:45 voraus (20 Minuten zu spät).

## 7. Validierung

- `tests/test_core_forecast.py` prüft $c$, RMS, ETA und den freien Fit (A, Standardfehler) gegen diese Tabelle. Grundlage sind die 15 exakten Messpunkte aus dem Export.
- `tests/test_cook_replay.py` spielt den ganzen Cook mit 10-Minuten-Schritten durch den Watcher: `wrapped` um 05:24, Prognose-Zustände, fester Fit-Start (Fit ab 06:33, Wrap 05:24), Einfluss von `set pit`.

### Ausgewertete Cooks

Neben dem Cook vom 10./11.10. wurden fünf weitere Element-Exporte ausgewertet:

| Export     | Datum      | Auflösung  | Verlauf                                                      | Ziel erreicht                         | Verwendung                                  |
|------------|------------|------------|--------------------------------------------------------------|---------------------------------------|---------------------------------------------|
| `15f2e6f5` | 10./11.10. | 0,1 °C     | Wrap ~05:30; Export endet 10:25 bei 89,8 °C                  | unbekannt                             | Herleitung, Tests; freies A = 102,3 ± 4,5 °C |
| `e62a2cc9` | 26./27.09. | 0,1 °C     | Stall 01:51–04:46, Wrap 04:46                                | 95 °C 08:03, 96 °C 08:44, 97 °C 09:25 | Backtest; freies A = 97,4–98,1 °C           |
| `8d401460` | 25.07.     | ganze Grad | Stall 03:22–07:00, Wrap vermutlich ~06:30                    | 95 °C 13:08, 96 °C 14:10              | Backtest                                    |
| `62bebe00` | 23.06.     | ganze Grad | Datenlücke 01:12–07:29                                       | 95 °C 13:06, 96 °C 13:27              | Backtest                                    |
| `f35f7682` | 02.05.     | ganze Grad | Watcher erst bei 78 °C gestartet; Kern 15:45–16:57 bei 93 °C | 93 °C 15:45, 94 °C 17:08              | Backtest                                    |
| `758731c2` | 27.09.     | 0,1 °C     | nur Abkühlphase (75,9 → 69 °C)                               | –                                     | ignoriert                                   |

Die Exporte mit ganzen Grad stammen aus der Zeit vor dem WebSocket (DOM-Scraping). Die Smokertemperatur ist bei den älteren Cooks nicht bekannt. Gerechnet wurde mit 110, 120 und 130 °C; das ändert wenig, weil das frei gefittete A weit darunter liegt.

### Backtest

Grundlage sind die Exporte vom 02.05., 23.06., 25.07. und 26./27.09.2026. Der Backtest beschreibt die frühere, adaptive Watcher-Version (A = 100 °C und freies A gibt es heute nur noch im CLI). Zu jedem Check der Endphase wurde nachgerechnet, was die Verfahren vorhergesagt hätten. Verglichen wurde mit der tatsächlichen Ankunft am Ziel. Fehler in Minuten, Median, negativ = zu früh.

Nur der Cook vom 26./27.09. hat Nachkommastellen und eine bekannte Ankunft:

| Verfahren       | < 1 h vorher | 1–2 h   | 2–3 h   |
|-----------------|--------------|---------|---------|
| MEATER          | +6           | −42     | −89     |
| linear          | −20          | −61     | −103    |
| A = Pit         | −20          | −56     | −90     |
| **A = 100 °C**  | **−12**      | **−33** | **−48** |
| A frei (stabil) | −4           | −20     | –       |

Die übrigen drei Cooks liefern nur ganze Grad, weil sie aus der Zeit vor dem WebSocket stammen. Allein das Runden erzeugt dort einen RMS von etwa 0,29 °C. Ein stabiler Fit ergibt sich deshalb nie, auch nicht mit gelockerter Schwelle. Bei solchen Daten ist $A$ nicht bestimmbar. Alle Verfahren, auch MEATER, streuen dort bei mehr als 2 Stunden Abstand um etwa ±1,5 Stunden.

Fehler bei den drei Cooks mit ganzen Grad (Median in Minuten, Pit 120 °C; A = Pit und A = 100 °C über die letzten 60 Minuten, wie die vorläufige Anzeige der früheren Watcher-Version):

| Verfahren  | < 1 h vorher | 1–2 h | 2–3 h | > 3 h |
|------------|--------------|-------|-------|-------|
| MEATER     | +66          | +33   | −9    | −81   |
| linear     | +14          | −4    | −53   | −100  |
| A = Pit    | +11          | +3    | −46   | −69   |
| A = 100 °C | +20          | +30   | −7    | −2    |

Die Mediane sehen teils gut aus, die Spannweiten sind aber groß: Bei mehr als 3 Stunden Abstand liegen einzelne Werte bis zu ±3 Stunden daneben. MEATER ist kurz vor dem Ziel bei diesen Cooks deutlich zu pessimistisch (bis zu +140 Minuten).

### Alarme an den früheren Cooks

Alle Cooks wurden außerdem durch die aktuelle Alarmlogik des Watchers gespielt, ohne die damals manuell geschickten Befehle:

| Cook       | Stall-Alarm | tatsächlicher Stall | TempDown ohne Anheiz-Schutz                             | mit Anheiz-Schutz (ab Kern 40 °C) |
|------------|-------------|---------------------|---------------------------------------------------------|-----------------------------------|
| `15f2e6f5` | 03:24       | 02:30–05:30         | 2 (Wrap 05:37/05:48)                                    | 2 (Wrap; mit `pause` 0)           |
| `e62a2cc9` | 02:42       | 01:51–04:46         | ~18 (Ambient-Spitze 117 °C beim Anheizen, dann ~105 °C) | 1 (Wrap 04:46)                    |
| `8d401460` | 03:53       | 03:22–07:00         | 4 (107 °C beim Start)                                   | 0                                 |

- Die Stall-Erkennung trifft alle drei echten Stalls, auch bei ganzen Grad. Der Wrap löst keinen Stall-Fehlalarm aus.
- Bei `62bebe00` und `f35f7682` meldet sie nach dem Wrap bei 79–80 °C einen „Stall“: Der Anstieg war dort langsam (2–3 °C/h), und ganze Grad machen die Rate grob. Die Nachricht sagt in diesem Fall „Bereits gewrappt“ statt „Jetzt wrappen?“.
- Die TempDown-Fehlalarme beim Anheizen hattest du damals per `reset ambient` umgangen. Der Anheiz-Schutz (`tempdown_warmup_core_temp`, Default 40 °C) beseitigt sie. Einen Wrap fängt `pause` bzw. `wrapped <min>` ab.

## 8. Interpretation und Grenzen

- **Spanne statt Punkt.** Beim Cook vom 11.10. ergab sich: frühestens ~14:50 (A = Pit), wahrscheinlich gegen 16:00–16:30, mit Ausreißerrisiko Richtung 18 Uhr, falls $A$ wirklich bei ~98 °C liegt. MEATER schätzte um 10:25 ~15:30.
- **Ein $A$ nahe dem Ziel reagiert stark auf kleine Änderungen.** Wenige °C bei $A$ heißen mehrere Stunden Unterschied in der ETA (mit verankertem $T_0$ ergab sich 98,5 °C → 18:26, mit gefittetem 102,3 °C → 16:07). Die RMS-Unterschiede zwischen den Varianten (0,058 vs. 0,062 °C) liegen nahe an der Sensorauflösung. Das ist ein Hinweis, kein Beweis.
- **Der Watcher zeigt nur A = Pit** und damit die früheste exponentielle Ankunft. Ein freies $A$ braucht etwa zwei Stunden Endphase, bis die Krümmung die Varianten trennt (siehe `wachtmeater eta`). A = 100 °C war im Backtest die beste frühe Schätzung.
- **Bei ganzen Grad (Browser-Fallback) trägt die Endphasen-Prognose nicht.** Das gilt für die freie Anpassung von $A$; mit festem A = Pit rechnet der Watcher trotzdem.
- **Die Schwellen beruhen auf wenigen Cooks** (RMS 0,1 °C, Standardfehler 5 °C), davon nur einer mit Nachkommastellen und bekannter Ankunft. Sie sind konfigurierbar bzw. in `core_forecast.py` dokumentiert und sollten nach weiteren Cooks überprüft werden.
- **Die Zieltemperatur ist nur ein Richtwert.** Fertig ist das Brisket, wenn der Fühler wie in Butter gleitet. Ab ~93 °C prüfen.
- **Es eilt?** Den Smoker nach dem Wrap auf 125–135 °C stellen; Pit Physics empfiehlt 275 °F. Bei Folie hebt das $A$ nur, solange die Oberfläche nicht bei ~100 °C gedeckelt ist. Butcher Paper oder ein Folienboot profitieren stärker.

## Quellen

- [Pit Physics – The Science of Competition Brisket](https://www.pit-physics.com/brisket/): $L \approx \sqrt{\alpha t}$, $\alpha$-Werte, Stall = Verdunstung, Boot/Wrap bei 165–175 °F, danach 275 °F
- [AmazingRibs – Understanding and beating the barbecue stall](https://amazingribs.com/more-technique-and-science/more-cooking-science/understanding-and-beating-barbecue-stall/): Blonders Verdunstungsexperiment
- [AmazingRibs – Texas Crutch](https://amazingribs.com/more-technique-and-science/more-cooking-science/texas-crutch/): Wrappen mit Folie bzw. Butcher Paper
- [AmazingRibs – Texas Brisket (PDF)](https://amazingribs.com/books/Texas-Brisket.pdf): Die Garzeit hängt vor allem an Dicke und durchschnittlicher Smokertemperatur
- [BU Today – Everything You Know about Grilling Is Wrong](https://bu.edu/today/2016/everything-you-know-about-grilling-is-wrong): Interview mit Greg Blonder
- Messdaten: Wachtmeater-Chat-Export des Cooks `15f2e6f5` vom 10./11.10.2026 (Element), Smoker real konstant 110 °C
- Backtest-Daten: Wachtmeater-Chat-Exporte `e62a2cc9` (26./27.09.2026), `8d401460` (25.07.2026), `62bebe00` (23.06.2026), `f35f7682` (02.05.2026); `758731c2` (27.09.2026, nur Abkühlphase) nicht verwendet
- Herleitung: private Obsidian-Notiz „Kerntemperatur-Prognose (Endphase)“, aus der diese Notiz und `core_forecast.py` hervorgegangen sind

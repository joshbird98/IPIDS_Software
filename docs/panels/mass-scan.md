# Mass Scan

The **Mass Scan** panel sweeps the analyser magnet current, measures the beam current on the Faraday cup at each step and draws the result as a mass spectrum. It also finds and names the peaks, measures the mass resolution, and holds the mass calibration that the HMI uses to set the magnet for a given mass.

![The Mass Scan panel: sweep settings on the left, the spectrum in the middle, the mass calibration on the right](../images/panel-mass-scan.png)

The panel has three parts side by side: the settings on the left, the plot in the middle and the analysis on the right. The **Setup** and **Analysis** buttons above the plot hide and show the two side parts. Drag a divider to share out the width; a divider you have moved stays where you put it.

## Before you scan

For a scan, or a first tune of the beam:

- insert the Faraday cup and bias it at more than 50 V, on the [Faraday Cup & SMU](faraday-cup-smu.md) panel;
- retract the aperture ladder fully (the largest opening): it is set by hand;
- set the object (pre-magnet) and image (post-magnet) slits; 10 mm each is typical for a first tune;
- switch the analyser magnet on, on the [Beamline Optics](beamline-optics.md) panel.

Sweep a range where you expect a peak. Where a peak sits depends on the beam energy (the extraction plus the target (cathode) voltage), so look at earlier scans and logs made at the same energy. The whole procedure is in [Selecting a mass and tuning the beam](../procedures/tuning-the-beam.md).

## How a scan runs

The scan runs in the sequencer service (`service_sequencer`), not in the HMI. The panel only starts it, stops it and draws its points. Closing the HMI does not stop a scan, and an HMI that is opened again shows it (the sequencer keeps the pass in progress and the last finished pass).

While it runs, the scan holds a control lease on the analyser magnet and on the Faraday cup SMU. The [Automation bar](automation-bar.md) shows **mass scan holds: magnet, smu**, and the magnet and SMU controls in the other panels are greyed out, with the reason in their tooltip.

The sequencer runs a scan like this:

1. It sets the SMU's integration time to **SMU NPLC** and waits 0.5 s.
2. At each point from **Start** to **Stop** it:
    - sets the magnet current;
    - waits until the measured magnet current is within **HW Tol** of the setpoint and the SMU reports its range settled, for at most 5 s (after 5 s it measures anyway);
    - waits **Wait Time**;
    - takes **Samples** readings, **Interval** apart. If the SMU changes range part way, that point's readings start again;
    - records the averages of the magnet current and the beam current, and the target and extraction voltages. With 10 or more samples, the first and last 10 % of the readings are left out of the average.
3. With **Continuous Loop** ticked, it sets the magnet back to **Start**, waits 2 s and starts the next pass.
4. When the scan ends, whether it completed, was aborted or failed, it puts the SMU back to 1 NPLC (fast readings for the other panels). The magnet stays at the last current the scan set.

A scan fails, with the reason on the status line, if the magnet refuses a setpoint, if the SMU does not accept the integration time, or if the magnet or Faraday cup readings stop updating (not live at the end of the 5 s settling time, or for 5 s while sampling). A failed scan also raises a medium-priority alarm on the [Alarms](alarms.md) panel, which clears when the next scan starts.

Points appear on the plot as they are measured. When a pass finishes, the sequencer saves it (with **Auto-Export CSV on Complete** ticked), and the panel fetches the whole pass and analyses it. A pass stopped part way stays on the plot but is never saved, neither by the sequencer nor with **Save trace**.

> [!NOTE]
> If you take control with the Automation bar during a scan, the scan stops and cannot put the SMU back to 1 NPLC, because it no longer holds the SMU: the beam-current readings stay at the scan's integration time until a scan next ends. Press **ABORT** instead to stop a scan and let it put the SMU back.

## Settings

| Control | What it does |
|---|---|
| **Start:**, **Stop:** | Magnet current at the first and the last point. They are limited to the magnet setpoint's range in the tag registry, and their tooltip gives the magnet's hard cap. **Stop** may be below **Start**: the scan then runs downwards. |
| **Step:** | Current step between points, 0.001 to 10 A. |
| **HW Tol:** | How close the measured magnet current must come to each setpoint before the point is measured, 0.001 to 1 A. |
| **Wait Time:** | Extra settling time at each point once the magnet is within tolerance, 0 to 5 s. |
| **Samples:** | Beam-current readings averaged at each point, 1 to 1000. |
| **Interval:** | Time between readings, 0.01 to 1 s. It is never shorter than one SMU integration plus 50 ms. |
| **SMU NPLC:** | The SMU's integration time during the scan, in power-line cycles (1 NPLC is 20 ms), 0.001 to 100. A longer integration gives quieter readings and a slower scan. |
| **Continuous Loop** | Repeats the sweep until you press **ABORT**. The previous pass stays on the plot as a faint dashed trace. |
| **Auto-Export CSV on Complete** | The sequencer saves every finished pass as a CSV file, even with the HMI closed. Ticked by default. |
| Base export path and **...** | The folder and the start of the file name for saved traces; **...** chooses them with a file dialog. Each file is named `base_YYYYmmdd_HHMMSS.csv`. The default base is `data/exports/Mass_Scans/MassScan`. |
| **Est. Scan Time** | An estimate of one pass from the settings. It leaves out the time the magnet takes to settle at each point, so a pass usually takes longer. |
| **Remaining** | During a scan, the same estimate for the rest of the pass. |
| **START PRECISION SWEEP** | Asks the sequencer to start a scan with these settings. Greyed out while a scan runs, and when a scan could not start (see below). |
| **ABORT** | Stops the scan. Available only while a scan runs. |
| **Status:** | What the scan is doing (for example **scanning (pass 2)**), how it ended, or why the sequencer refused a start or a stop. |

The settings are locked while a scan runs.

### When START is greyed out

The tooltip of **START PRECISION SWEEP** gives the reason:

| Tooltip | What to do |
|---|---|
| **Disabled: The sequencer service is not running (see the Service Manager panel).** | Start or restart `service_sequencer` on the [Service Manager](service-manager.md) panel. |
| **Disabled: Magnet service not running (no live data).** or **Disabled: SMU service not running (no live data).** | The magnet or SMU service is not publishing. Check the Service Manager panel. |
| **Disabled: Magnet:** or **Disabled: SMU:** followed by a reason | That service would refuse an operator command now, for example because the safety relay is off or another sequence holds it. |
| **Disabled: Magnet PSU must be enabled.** | Switch the magnet on in the [Beamline Optics](beamline-optics.md) panel. |

The sequencer checks the settings again when it starts a scan. It refuses a start or stop current outside the magnet's range or past its hard cap, equal start and stop currents, a step that is not above 0 A, more than 100 000 points, and an NPLC outside 0.001 to 100. The reason appears on the status line.

## The plot

The horizontal axis is the mass in AMU, worked out from the measured magnet current with the [mass calibration](#mass-calibration). The vertical axis is the beam current, labelled in pA, nA, µA, mA or A as needed.

| What you see | Meaning |
|---|---|
| Blue line with dots | The pass on the plot (the pass in progress, the last finished pass or a loaded trace). |
| Red stretches | Negative beam current. |
| Faint dashed line | The previous pass of a looping scan. |
| Triangles | Peaks found by **Auto-detect peaks**. |
| Red crosses | The beam current 0.5 AMU either side of each peak (its tails). |
| Green stars | Calibration points. |
| Labels above peaks | The isotope or molecule a peak was matched to, and its mass. Blue: one of the expected elements. Amber: something else. |

| Control | What it does |
|---|---|
| **Setup**, **Analysis** | Hide or show the settings and the analysis. |
| **Log Y** | Logarithmic current axis. It is a symmetric log: negative currents have their own logarithmic region below zero, and the axis is linear within about 1 pA of zero. |
| **Clear** | Clears the plot, the previous pass and the peaks. **Save trace** is then greyed out until the next finished pass or a loaded trace. |
| Mouse wheel, drag | Zoom and pan. The current axis follows the data in the visible mass range. |
| Double-click | When no scan is running: sets the magnet to the mass under the pointer, using the calibration and the beam energy recorded at the trace's last point (30 keV if there is none). |
| **Ctrl** and left click | Adds a calibration point at the nearest peak (see [Mass calibration](#mass-calibration)). |

A double-click sends the magnet setpoint at once, without a second click. It goes as an ordinary operator command, so the magnet service refuses it if a sequence holds the magnet or the magnet is locked out. It also logs a purple user marker, **Manual Magnet Jump**, with the current.

## Mass calibration

The panel converts magnet current to mass with:

```
AMU = k (I + I_off)² / E
```

where I is the magnet current in A and E the beam energy in eV: the target voltage plus the extraction voltage recorded with each point, for singly charged ions. A point with no recorded voltages is taken as 30 keV.

| Control | What it does |
|---|---|
| **Beam energy E:** (**Mass calibration**) | The beam energy recorded with the trace: the average, with ± half its spread if it changed during the trace. **Unknown** when none was recorded. |
| **Constant k:**, **Offset I_off:** (**Mass calibration**) | The calibration constants. A typed value redraws the mass axis when you press Enter or move on; a stepped one, at once. |
| Table under **Multi-point fit** (**Magnet (A)**, **True AMU**) | The calibration points. |
| **Clear** (under **Multi-point fit**) | Removes every calibration point. The **Clear** above the plot leaves them. |
| **Calculate fit** | Fits the calibration to the points and redraws the mass axis. |

To calibrate:

1. **Ctrl** and left click a peak whose mass you know. The panel takes the highest point within five points either side of the click and asks for its true mass (**Add Calibration Peak**, 1 to 300 AMU).
2. Repeat for other known peaks. Points may come from several traces: they stay until you press **Clear** under **Multi-point fit**, and each keeps its own beam energy.
3. Press **Calculate fit**. With one point only k is fitted and I_off is kept (**Fit: 1 point (I_off fixed)**). With two or more, k and I_off are both fitted (**Fit R² =** and the number of points). Points that make no sense, for example masses that fall as the current rises, give a **Fit Error**.

> [!IMPORTANT]
> The calibration is not kept when the HMI closes. It starts at k = 1391.9 and I_off = 0.223 every time. After a fit, the panel writes k and I_off into the trace file you last opened with **Load trace...** (columns `Calib_k` and `Calib_I_off`), and opening that file again brings them back.

**CALCULATE & SET AMPS** on the [Beamline Optics](beamline-optics.md) panel uses this calibration, with the live beam energy, to set the magnet for a mass. The marker it logs says where the calibration came from: fitted (with the time and R²), entered by hand, from a trace file, or the start-up values.

> [!TIP]
> The magnet has hysteresis. A calibration from a scan with rising current holds when the magnet current is approached from below, or after a degauss.

## Peaks and isotopes

| Control | What it does |
|---|---|
| **Expected elements...** | Opens the periodic table (**Select Expected Elements**). Click the elements you expect in the beam, then **OK**. Peaks are matched against their isotopes and compounds first. |
| **Max peaks:** | The most peaks to keep, 1 to 100. |
| **Sensitivity:** | The smallest peak that counts, as a percentage of the trace's range of current (the peak's prominence). |
| **Auto-detect peaks** | Finds the peaks now, and again whenever a pass finishes or a setting that affects them changes. Press it again (**Stop auto-detect**) to stop and clear them. |
| Peak table | One row per peak: **Mass (AMU)**, **Match** and **Tail ±0.5**. |
| **Avg Mass Resolution (M/ΔM):** | The average of M/ΔM over the peaks found, where ΔM is each peak's full width at half its height. **No peaks detected.** in red when there are none. |

Peak finding needs at least 10 points and the `scipy` package. It ignores negative currents and keeps the most prominent peaks, favouring those close to a whole mass.

A peak within 0.25 AMU of a whole mass is matched against the isotopes and molecular ions at that mass. Up to two candidates made of the expected elements (molecules of up to four atoms) are shown; failing those, the likeliest other isotope or molecule (up to two atoms). Common additions are included: +H, +H₂, +O, +O₂ and +N. Peaks further from a whole mass, or with no candidate, are not named. On the plot, a named peak within 0.6 AMU of a larger named peak gets no label of its own.

The isotope list is `isotope_db.json`, next to the panel's code, built with `python src/utils/build_element_database.py` (which needs the `mendeleev` package). The lab PC has it. Without that file the panel uses a short built-in list (H, He, C, N, O, F, Ne, Si, P, S, Cl, Ar and Ge), and choosing any other element makes no difference.

**Tail ±0.5** is the larger of the beam currents 0.5 AMU either side of the peak, in parts per million of the peak current: how much of this peak reaches its neighbours. It reads **< 1 ppm** below 1 ppm and **Overlapped** above 500 000 ppm. Its colour runs from green (up to 100 ppm) through amber (1000 ppm) to red (10 000 ppm and above).

## Loading and saving traces

| Control | What it does |
|---|---|
| **Data Source:** | Where the trace on the plot came from: **Live Scan**, **Saved as** and the file name (the tooltip gives the full path), **Recent Scan (Unsaved)**, **Loaded** and the file name, or **None**. |
| **Load trace...** | Opens a trace saved as CSV and puts it on the plot. If the file carries a calibration, the panel uses it. Starting a new scan replaces the loaded trace. |
| **Save trace** | Saves the last finished pass, or the loaded trace, as a new file `base_YYYYmmdd_HHMMSS.csv` from the base export path. Greyed out until there is one. |

A trace file has one row per point, with the columns `Timestamp` (when the file was written), `Requested_Magnet_A`, `Measured_Magnet_A`, `Measured_Beam_A`, `Target_kV` and `Extraction_kV`, and after a fit `Calib_k` and `Calib_I_off`. **Load trace...** needs at least `Measured_Magnet_A` and `Measured_Beam_A`. Without the two voltage columns it warns that the masses may use the default energy, and uses 30 keV. A loaded trace saved again keeps its measured current as the requested current and its whole beam energy under `Target_kV`.

## See also

- [Selecting a mass and tuning the beam](../procedures/tuning-the-beam.md)
- [Beamline Optics](beamline-optics.md): switching the magnet on, degauss and **CALCULATE & SET AMPS**
- [Faraday Cup & SMU](faraday-cup-smu.md)
- [The Automation bar](automation-bar.md): control leases and taking control
- [Optimiser](optimiser.md)
- [How the controls behave](../getting-started/controls-and-colours.md)
- [Services and messaging](../how-it-works/services.md)

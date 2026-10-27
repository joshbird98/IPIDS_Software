# The source turbo's load guard

The turbo service watches the source turbo pump for an overload: the pump working harder than normal at full speed, as it does when the valve between it and its backing pump is shut. It warns within minutes, and if the load doesn't fall back it shuts the source down while the source still has hours in hand to cool. The turbo itself is never stopped.

## Why it is there

On 26 October 2026 the valve behind the source turbo was closed by mistake at 10:22, during a target rod change. The turbo ran on at full speed for 13 hours with nothing to show for it on the source vacuum, while gas built up behind it:

| Time | Current | Bearing | Converter | |
|---|---|---|---|---|
| overnight | 0.8 A | 43 °C | 28 °C | normal |
| 10:23 | 1.2 A | 43 °C | 28 °C | the valve shut: +0.4 A at once |
| 12:15 | 1.5 A | 46 °C | 31 °C | |
| 15:45 | 2.0 A | 50 °C | 35 °C | |
| 22:00 | 4.0 A | 59 °C | 45 °C | |
| 22:37 | 4.3 A | 61 °C | 47 °C | the pump's own warning |
| 23:19 | 6.2 A | 65 °C | 53 °C | |
| 23:20 | | | | the pump failed; the source went up to about 1 mbar |

The pump's own warning came 43 minutes before it failed: too late, as the source stays hot inside for a couple of hours after a shutdown, and the caesium and everything else oxidises if air gets in while it is hot. The current told the story from the start.

## What it watches

The pump's motor current, bearing temperature and converter temperature, each smoothed over 60 s (a gas burst lasts seconds: the rod change drew 2.3 A for a second), and the pump's own warning and error bits. It judges them only once the pump has been at full speed (98 % or more) for 10 minutes, since a run-up draws more; if the pump is already at full speed when the turbo service starts, it judges at once.

- **Overloaded**: the current 0.3 A or more above normal (0.8 A).
- **Running hot**: the bearing or the converter 5 °C or more above normal (43 °C and 28 °C).

## What it does

The warning comes 2 minutes into an overload; the later stages are timed from the warning.

| Stage | When | What happens |
|---|---|---|
| **Warning** | overloaded for 2 min; or running hot, or the pump's own warning, for 2 min | Alarm `turbo.source.load_warning` (priority 2): "Source turbo working harder than normal: the source will be shut down at 10:45 (in 20 min) unless its current falls back. Check the valve between it and its backing pump, and the backing pump." The **Vacuum System** panel shows **LOAD HIGH: SOURCE SHUTDOWN AT 10:45**. |
| **Critical** | still overloaded 10 min after the warning; or at once, if the pump's own warning comes on while it is overloaded | Alarm `turbo.source.load_critical` (priority 1), with the time of the shutdown. The panel shows **OVERLOADED: SOURCE SHUTDOWN AT 10:45**. |
| **Tripped** | 20 min after the warning (or 10 min after the pump's own warning, if that is sooner), unless the current is falling back; at once if the overloaded pump reports an error or drops below 93 % speed | Alarm `turbo.source.load_trip` (priority 1). The PLC service sends **INITIATE SHUTDOWN** to the PLC and switches on the caesium oven's forced cooling. The panel shows **PROTECTION TRIPPED: SOURCE SHUT DOWN**. |

**Falling back** means the current's excess over normal has dropped by at least 30 % from its peak in the last 10 minutes. Opening the valve again does that within a minute; the temperatures follow much more slowly, so they don't decide it. While the current is falling back the shutdown waits, and it comes once the current stops falling while still overloaded.

Each stage has its own alarm: the warning's clears when the critical one comes. The alarms' text is the guard's own, with the readings and the time of the shutdown. Each change of stage is also in the event log.

When the load has been back to normal for about 5 minutes (`clear_after_s`), the alarms clear by themselves. If the current comes back but the pump is still running hot, a critical alarm goes back to a warning, and the event log says `Source turbo current back to normal (...): no source shutdown`.

Running hot without an overload, or the pump's own warning on its own, only warns: a warm room or warm cooling water raises the temperatures too, and the pump's warnings cover other things (its supply voltage, a cold room). Shutting the source down needs the current: the load itself.

## The source shutdown

The trip is acted on by the PLC service:

- **INITIATE SHUTDOWN**, if the safety relay is on and no shutdown sequence is running: the PLC's operator shutdown (**HOLD HOT PHASE**, then **FILAMENT RAMP DOWN**; see [The PLC program](plc.md#shutdown-sequences)). It is sent again every 30 s until the PLC's sequence runs. If a sequence is already running, it is left to run. If the relay is already off, there is nothing to shut down.
- **Forced cooling** of the caesium oven (`ion_beam.source.cesium.cmd_force_cooling`).
- **It can't be aborted.** Until that sequence has finished, the PLC service refuses **ABORT SHUTDOWN**, a withdrawal of the shutdown request and switching the forced cooling off: "the source turbo protection started this shutdown: it can't be aborted". This holds even if the guard is reset meanwhile.
- **The source stays off.** Until the trip is reset, the PLC service refuses **ENABLE SAFETY RELAY** and switching on the extraction, target, filament, thermionic and caesium oven supplies: "the source turbo protection has tripped: the source stays off until RESET FAULTS, once the turbo's load is back to normal".

The System Health panel greys out **ABORT SHUTDOWN** and **ENABLE SAFETY RELAY**, with the reason in their tooltips, and its shutdown chip ends **- SOURCE TURBO PROTECTION**. The Interlocks panel's safety relay group says why too.

The turbo is left running: it is stopped only by hand, at the pump, by trained staff. A shut backing valve can't be opened from the software either. Later hardware to open that valve and flood the source with argon in an emergency would act on the same trip.

## What to do

1. On a warning, check the valve between the source turbo and its backing pump, and the backing pump. If the valve was shut, open it: the current falls back within a minute or so, there is no shutdown, and the alarms clear after 5 minutes back to normal.
2. If the shutdown has started, let it run; it can't be aborted. The caesium oven is being cooled.
3. Once the cause is found and the turbo's current is back to normal (or the pump has stopped), press **RESET FAULTS** on the [System Health](../panels/system-health.md) panel. The turbo service clears the trip; while the turbo is still overloaded it refuses, with the readings: "the source turbo is still overloaded (...): its protection stays tripped until its load is back to normal". The temperatures don't hold the reset: they take longer to come down, and running hot then only warns.
4. Then start the source as usual, once its shutdown has finished.

## On 26 October 2026

Replayed through the guard, the day's telemetry gives:

| Time | |
|---|---|
| 10:22:50 | the valve shut |
| 10:25 | **Warning**: 1.2 A, normally 0.8 A |
| 10:35 | **Critical**: still overloaded, the source to be shut down at 10:45 |
| 10:45 | **Tripped**: the source shut down and the caesium oven cooled |
| 22:37 | (the pump's own warning) |
| 23:20 | (the pump failed) |

The source would have been shut down 12 h 35 min before the pump failed. Nothing else in the day set it off: not the rod change's pressure burst, nor the night at full speed.

## Settings

The settings are in `config/turbo_guard.json`, which the config editor checks (see [Configuration files and the config editor](../engineering/configuration.md#the-source-turbos-load-guard)). The turbo service reads them when it starts, and logs them: `Load guard: normal 0.8 A, ...`.

| Setting | Default | What it is |
|---|---|---|
| `enabled` | true | false switches the guard off. |
| `shut_down_source` | true | false: the stages and alarms as usual, but no shutdown and no refusals. Use it to try new settings out. |
| `normal_current_a` | 0.8 | The current at full speed with the backing line open. |
| `normal_bearing_c`, `normal_converter_c` | 43, 28 | The temperatures likewise, in °C. |
| `current_margin_a` | 0.3 | Overloaded: the current this far above normal. |
| `temp_margin_c` | 5 | Running hot: the bearing or the converter this far above normal. |
| `warn_after_s` | 120 | The warning, after this long. |
| `critical_after_s` | 600 | The critical alarm, this long after the warning. |
| `trip_after_s` | 1200 | The trip, this long after the warning, unless the current is falling back. |
| `pump_warning_trip_s` | 600 | With the pump's own warning while overloaded: the trip this long after it, if that is sooner. |
| `falling_fraction`, `falling_window_s` | 0.3, 600 | Falling back: the excess down by this fraction from its peak in this many seconds. |
| `clear_after_s` | 300 | Back to normal for this long: the alarms clear. |
| `at_speed_pct`, `settle_s` | 98, 600 | Judged only at this much of full speed, for this long. |
| `smooth_s` | 60 | The readings' median over this long. |

> [!IMPORTANT]
> The normal values are the pump's on 26 October 2026. Set them again after a change of pump, of its cooling or of its backing line, from the data viewer (the current and temperatures at full speed overnight). Too low, and the guard can shut the source down for nothing; too high, and it acts later.

> [!NOTE]
> Anything else that raises the source turbo's backing pressure for more than 20 minutes trips it too: a backing pump failing, a leak in the backing line, or another pump's roughing through a shared backing line, if there is one. The current falls back as such a pump-down goes on, which holds the shutdown off.

## Where it lives

- `src/core/turbo_guard.py`: the guard itself, with the 26 October numbers.
- `service_source_turbo`: reads the pump, runs the guard and publishes its stage as `ion_beam.source.turbo_pump.stat_load_warning`, `stat_load_critical` and `stat_load_trip` (logged, so they can be plotted in the data viewer), `rb_guard_stage` (0 to 3) and the text and shutdown time for the alarms and the panel. A trip, and an overload's clock, are saved in `data/turbo_guard_state.json`, so a restart of the turbo service doesn't lose them.
- `service_plc`: acts on `stat_load_trip`, and publishes `ion_beam.system.turbo_protection` (1 while it holds the source down).
- In the simulator, the fault `turbo_backing_closed` shuts the simulated pump's backing valve: its current goes up 0.4 A at once and 0.1 A a minute after that, the guard warns after 2 minutes and trips after 22, and the pump fails at 6 A, after 48 minutes. See [The simulator](simulator.md).

## See also

- [Vacuum System](../panels/vacuum.md)
- [System Health](../panels/system-health.md)
- [Alarm handling](alarms.md)
- [The PLC program](plc.md)
- [Services and messaging](services.md)

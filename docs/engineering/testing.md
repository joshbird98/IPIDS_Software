# Tests

The automated tests: how to run them, why they are safe to run on the lab PC, what GitHub runs on every push, the tests that run the simulated system, and the help tests and screenshots.

## Running the tests

Install the tools once:

```
pip install -r requirements-dev.txt
```

That installs everything in `requirements.txt` plus pytest and pytest-timeout. Then, from the repository's top folder:

| Command | Runs |
|---|---|
| `python -m pytest -m "not slow"` | the quick tests, about a minute |
| `python -m pytest -m slow` | only the slow tests, which start the whole simulated system |
| `python -m pytest` | everything |
| `python -m pytest tests/test_help.py` | the help tests (below) |

The tests are in `tests/`, and `pytest.ini` sets them up: each test may take at most 120 s, and the tests that start the simulated service stack are marked `slow`. Set `SINDIE_TEST_SHOTS` to a folder to have the HMI test save a screenshot of each stage.

> [!NOTE]
> `requirements.txt` is saved in UTF-16 (it starts with a byte-order mark), unlike the other text files. pip reads it as it is; edit it with an editor that keeps the encoding.

## Safe to run on the lab PC

The tests keep away from the real system and its data. `tests/conftest.py` sets, before anything else is loaded:

| Variable | Set to |
|---|---|
| `SINDIE_PORT_OFFSET` | 20000: every ZeroMQ port moves up by 20000 |
| `SINDIE_DATA_DIR`, `SINDIE_LOG_DIR` | folders in a new temporary folder, `sindie_tests_...` |
| `SINDIE_SETTINGS_DIR` | a folder in the same temporary folder, so test windows don't save their layout over the operator's |
| `SINDIE_RECIPES_DIR` | a copy of `config/recipes/` in the temporary folder |
| `SINDIE_REGISTRY` | a tag registry built fresh from the config files in the checkout |
| `QT_QPA_PLATFORM` | `offscreen`: no windows appear |

The service manager the slow tests start only clears up leftover processes of its own instance, so it can't stop the real services.

> [!WARNING]
> `conftest.py` sets each variable only if it isn't set already. Run the tests from a shell where none of them is set to the real system's values, such as a `SINDIE_DATA_DIR` that points at the machine's data folder.

## What the tests cover

| Area | Files |
|---|---|
| Messaging and leases | `test_commands.py`, `test_leases.py`, `test_device_service.py`, `test_relay_watch.py`, `test_telemetry_cache.py` |
| Device services, against simulated devices | `test_service_plc.py`, `test_service_vacuum.py`, `test_service_turbo.py`, `test_service_spellman.py`, `test_service_magnet.py`, `test_service_steerer.py`, `test_service_smu.py`, `test_magnet_driver.py` |
| Service manager | `test_restart_policy.py`, `test_manager_purge.py` |
| Alarms | `test_alarms.py`, `test_service_alarms.py`, `test_alarm_panel.py` |
| Sequencer: recipes, mass scans, optimiser | `test_sequencer_recipe.py`, `test_sequencer_mass_scan.py`, `test_sequencer_optimiser.py`, `test_sequencer_port.py`, `test_service_sequencer.py`, `test_sequencer_panels.py`, `test_recipe_builder.py`, `test_recipe_check.py`, `test_recipe_routines.py`, `test_recipe_files.py` |
| Configuration | `test_config_store.py`, `test_config_editor.py`, `test_hard_caps.py`, `test_tolerance.py` |
| Logging and data | `test_logger_journal.py`, `test_data_bounds.py`, `test_export_unreadable.py` |
| HMI | `test_setpoint_box.py`, `test_hmi_focus.py`, `test_wheel_guard.py`, `test_source_widget.py`, `test_theme.py`, `test_gui_settings.py`, `test_mass_calibration.py`, `test_mass_scan_analysis.py`, `test_mass_scan_axis.py` |
| Data viewer | `test_viewer_colours.py`, `test_viewer_profiles.py`, `test_event_overlay.py`, `test_viewer_markers.py` |
| Simulator | `test_sim_plc_model.py`, `test_sim_registry.py`, `test_sim_flag.py`, `test_sim_stack.py`, `test_hmi_sim.py` |
| Help | `test_help.py` |

Most files start with a few lines saying what they check.

## Tests on the simulated system

The slow tests start the real service manager with simulated devices, in the test's own data folder (`tests/sim_stack.py`), and talk to it as the HMI would:

- `test_sim_stack.py`: every service comes up; commands are answered through the real services; the safety relay powers the Spellman supply; refusals give their reasons; the command audit trail reaches `events.db`; tolerance events; the restart back-off after a crash; stopping and starting a service; the logger's crash journal; stopping the manager stops everything.
- `test_hmi_sim.py`: the HMI, offscreen, against the simulated stack: only fresh telemetry is shown, command feedback, the two-click confirmation, unknown values when a service stops, recipe failures with their reasons, recipe Stop, the tolerance colouring.
- `test_viewer_markers.py`: the data viewer's event markers reach `events.db` and the exports.

Three quick tests cover the simulator itself: `test_sim_plc_model.py` runs the simulated PLC's model on a fake clock, `test_sim_registry.py` checks that `run_simulation.py` builds its registry from the config files, and `test_sim_flag.py` checks that `--sim` on the HMI, the data viewer or the service manager also moves it to the simulator's ports, unless a port offset is already set. See [The simulator](../how-it-works/simulator.md).

## On GitHub

GitHub Actions runs the tests on every push and pull request, and on demand (`.github/workflows/tests.yml`), on Linux with Python 3.13:

1. **quick tests**: `python -m pytest -m "not slow"`, within 15 minutes;
2. **simulated full stack + HMI**: `python -m pytest -m slow`, within 25 minutes, only if the quick tests passed.

It checks out only `src`, `tests`, `config`, `plc` and `docs`, leaving out the example data. A newer push to the same branch cancels a run still going. A failing test is also shown on the run's summary page, and if the second job fails, the services' logs are kept with the run for 7 days (`service-logs`).

## The help tests

`tests/test_help.py` checks the help pages in `docs/` and the help window:

- every page is in the contents on [the home page](../README.md), and every entry in the contents exists;
- every link and section link leads to a page and a heading that exist;
- every screenshot exists and has a description;
- nested lists are indented by four spaces, which the help window needs;
- the pages render: call-outs, tables, code blocks, scaled screenshots; a page is drawn again after a theme switch, and still shows (plainer) without the `Markdown` package; every page opens in the help window; search finds pages; a page can be saved as PDF;
- every HMI panel has a page, and **F1** finds it; the HMI's **Help** menu; **About SINDIE** finds the software's version without git.

Run them after changing any page: `python -m pytest tests/test_help.py`. The rules for writing pages are in [Writing help pages](writing-help-pages.md).

## Screenshots for the help

The pictures in `docs/images/` are made by a script, so they can be taken again whenever the HMI changes:

```
python src/utils/make_help_screenshots.py
```

It takes about four minutes. It starts the simulated services on their own ports (offset 34000) in a temporary data folder, so it is safe on the lab PC next to the running system. It opens the HMI and the data viewer offscreen on a 1536 × 864 screen and brings the simulated machine up as an operator would: gate valve, safety relay, extraction and the target (cathode), einzel, caesium oven, filament and thermionic, the magnet on silicon-28 and the beam on the Faraday cup. It runs a mass scan, then saves each panel, the whole HMI and the data viewer (both with numbered markers, explained in [A tour of the HMI](../getting-started/screen-tour.md) and [Using the data viewer](../viewer/data-viewer.md)), and a few dialogs.

| Option | Effect |
|---|---|
| `--theme light` | take them in the light theme (dark by default) |
| `--out <folder>` | save them elsewhere |
| `--port-offset N` | another port offset, if 34000 is in use |

Run it again after changing what a panel looks like, then check the pages that show it.

## See also

- [The simulator](../how-it-works/simulator.md)
- [Writing help pages](writing-help-pages.md)
- [Configuration files and the config editor](configuration.md)

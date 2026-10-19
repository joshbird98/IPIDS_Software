"""run_simulation.py builds the simulator's tag registry from the config files at every start.

In October 2026 the simulator ran on a config/system_tags.json from before the einzel's current
limits were added: the HMI never sent one, so the einzel took 10 kV, stayed at 0 V and latched an
arc."""
import json
import os

import run_simulation

CURRENT = "ion_beam.source.einzel.sp_requested_current"
VOLTAGE = "ion_beam.source.einzel.sp_requested_voltage"


def test_the_simulator_registry_matches_the_configs_and_keeps_hand_made_labels(tmp_path):
    with open(os.environ["SINDIE_REGISTRY"]) as f:          # built fresh for the tests (conftest.py)
        old = json.load(f)
    old[CURRENT]["min_val"] = old[CURRENT]["max_val"] = None  # as before the limits were in mpd_config.json
    old[VOLTAGE]["description"] = "Einzel lens (hand-labelled)"
    seed = tmp_path / "config_system_tags.json"
    seed.write_text(json.dumps(old))

    path = run_simulation.prepare_registry(str(tmp_path / "data_sim"), seed=str(seed))

    assert path == str(tmp_path / "data_sim" / "system_tags.json")
    with open(path) as f:
        built = json.load(f)
    assert (built[CURRENT]["min_val"], built[CURRENT]["max_val"]) == (0.0, 330.0)
    assert built[VOLTAGE]["description"] == "Einzel lens (hand-labelled)"
    assert json.loads(seed.read_text())[CURRENT]["max_val"] is None      # the machine's own file untouched


def test_without_a_machine_registry_it_is_built_from_scratch(tmp_path):
    data = tmp_path / "data_sim"
    data.mkdir()
    (data / "system_tags.json").write_text("{}")                          # a leftover from an earlier run
    path = run_simulation.prepare_registry(str(data), seed=str(tmp_path / "missing.json"))
    with open(path) as f:
        built = json.load(f)
    assert built[CURRENT]["max_val"] == 330.0 and len(built) > 100


def test_the_builders_warnings_are_shown(tmp_path, monkeypatch, capsys):
    """A missing PLC interface file gives a registry without PLC tags (every PLC setpoint refused):
    the simulator says so instead of starting quietly."""
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if str(p).endswith("PLC_PC_Interface.scl")
                        else real_exists(p))
    run_simulation.prepare_registry(str(tmp_path / "data_sim"), seed=str(tmp_path / "missing.json"))
    out = capsys.readouterr().out
    assert "Tag registry: Warning: PLC SCL file not found" in out

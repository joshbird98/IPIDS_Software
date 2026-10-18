"""Recipe builder: step icons, colours, and the parameters a new ROUTINE_CALL step starts with."""

COLOR_HEADER = "background-color: #2c3e50; color: white; padding: 4px; font-weight: bold;"

STEP_ICONS = {
    "ACTION": "⚡", "WAIT_TIME": "⏱️", "WAIT_TELEMETRY": "📡",
    "USER_PROMPT": "🙋", "SUB_RECIPE": "🔗", "ROUTINE_CALL": "⚙️",
    "VERIFIED_ACTION": "🛡️", "PARALLEL": "🔀"
}

# --- Routine Definitions ---
ROUTINE_DEFAULTS = {
    # The Mass Scan panel's scan (src/sequencer/mass_scan.py), then the magnet set on the strongest peak
    "mass_scan": {
        "start_a": 20.0,
        "stop_a": 30.0,
        "step_a": 0.05,
        "samples": 5,
        "nplc": 1.0,
        "goto_peak": True
    },
    "safety_relay_init": {
        "gv_cmd_open": "ion_beam.source.chamber.cmd_open_gv",
        "gv_stat_open": "ion_beam.source.chamber.stat_gv_open",
        "gv_open_perm": "ion_beam.source.chamber.stat_gv_open_permissive",
        "cmd_virtual": "ion_beam.system.cmd_enable_safety",
        "rb_relay": "ion_beam.facilities.safety_relay_active",
        "timeout_sec": 6.0,
        "gv_timeout_sec": 15.0,
        "stabilization_sec": 15.0
    },
    "hv_conditioning": {
        "cmd_tag": "ion_beam.source.extraction.sp_requested_voltage",
        "rb_tag": "ion_beam.source.extraction.rb_voltage",
        "max_val": 30.0,
        "step_size": 1.0,
        "hold_time_sec": 60.0,
        "pressure_tag": "ion_beam.source.vacuum_gauge_1.rb_pressure",
        "pressure_max": 5e-5,
        "pressure_recover": 1e-5,
        # A 1.0-while-arcing flag; the extraction supply has none in the registry (the PLC quenches
        # its arcs itself), so none by default - a Spellman supply's stat_arc_exceeded works here
        "arc_mode_tag": "",
        "backoff_step": 2.0,
        "max_arcs": 5,
        "arc_cooldown_sec": 5.0
    },
    "psu_control": {
            "cmd_enable": "ion_beam.source.filament.cmd_enable",
            "stat_enabled": "ion_beam.source.filament.stat_enabled",
            "sp_tag": "ion_beam.source.filament.sp_requested_current",
            "rb_tag": "ion_beam.source.filament.rb_current",
            "target_val": 5.0,
            "tolerance": 0.2,
            "timeout_sec": 45.0,
            "wait_for_readback": 1.0,
            "requires_relay": 1.0,
            "rb_relay": "ion_beam.facilities.safety_relay_active"
        }
}

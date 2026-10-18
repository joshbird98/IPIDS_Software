"""Where each Spellman supply named in config/mpd_config.json sits in the tag tree.

The device names are fixed: the Spellman service routes commands and telemetry by them, the
registry builder names the tags after them, and the PLC is sent their status flags. A supply
whose name isn't here has no place in the tag tree, so the config checks refuse it.
"""
LOCATIONS = {
    "source_einzel": "ion_beam.source.einzel",
    "beamline_einzel": "ion_beam.beamline.einzel",
    "neutral_trap_pos": "ion_beam.beamline.neutral_trap_pos",
    "neutral_trap_neg": "ion_beam.beamline.neutral_trap_neg",
}

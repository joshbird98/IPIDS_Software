"""Force the magnet back to manual (HMI) control mode, e.g. after a crashed mass scan
left it locked in automatic mode."""
try:
    from testing.cmd_helper import send
except ImportError:
    from cmd_helper import send

if __name__ == "__main__":
    send("ion_beam.beamline.magnet.cmd_ctrl_mode", 0, subsystem="magnet")

"""Force the cesium oven duty cycle. Prints the service's answer (ok / rejected + reason / ...)."""
try:
    from testing.cmd_helper import send
except ImportError:
    from cmd_helper import send

if __name__ == "__main__":
    send("ion_beam.source.cesium.testing_cmd_force_duty_cycle", 15.0)

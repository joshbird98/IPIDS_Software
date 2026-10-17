"""Enable safety relay. Prints the service's answer (ok / rejected + reason / ...)."""
try:
    from testing.cmd_helper import send
except ImportError:
    from cmd_helper import send

if __name__ == "__main__":
    send("ion_beam.system.cmd_enable_safety", True)

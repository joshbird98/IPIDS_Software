"""Start the source turbo pump. Prints the service's answer (ok / rejected + reason / ...)."""
try:
    from testing.cmd_helper import send
except ImportError:
    from cmd_helper import send

if __name__ == "__main__":
    send("ion_beam.source.turbo_pump.cmd_enable", True)

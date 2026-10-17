"""Close source gate valve. Prints the service's answer (ok / rejected + reason / ...)."""
try:
    from testing.cmd_helper import send
except ImportError:
    from cmd_helper import send

if __name__ == "__main__":
    send("ion_beam.source.chamber.cmd_open_gv", False)

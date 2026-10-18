"""X-steerer magnet PSU: the same service as the analyser magnet (service_magnet_psu.py),
run for the steerer's PSU, tags and config (steerer_magnet_config.json)."""
import os
import sys

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.service_magnet_psu import MagnetPsuService, main  # noqa: E402


def SteererMagnetMicroservice():
    return MagnetPsuService("steerer")


if __name__ == "__main__":
    main("steerer")

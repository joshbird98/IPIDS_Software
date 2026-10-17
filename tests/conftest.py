"""Test configuration.

Every test process runs with its ZeroMQ ports shifted (IPIDS_PORT_OFFSET) and its
data/log folders redirected to a temporary directory, so the suite can run on
the lab PC while the real system is running without touching it.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ipids_tests_")
os.environ.setdefault("IPIDS_PORT_OFFSET", "20000")
os.environ.setdefault("IPIDS_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("IPIDS_LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# A tag registry built fresh from the config files in this checkout (not whatever
# config/system_tags.json happens to be on this machine), so limits match the config.
if not os.environ.get("IPIDS_REGISTRY", "").strip():
    import contextlib  # noqa: E402
    import io  # noqa: E402

    os.environ["IPIDS_REGISTRY"] = os.path.join(_TMP, "system_tags.json")
    from src.utils.build_registry import build_system_registry  # noqa: E402

    with contextlib.redirect_stdout(io.StringIO()):
        build_system_registry(os.environ["IPIDS_REGISTRY"])

import socket  # noqa: E402

import pytest  # noqa: E402


def free_tcp_endpoint() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"tcp://127.0.0.1:{port}"


@pytest.fixture
def endpoint():
    return free_tcp_endpoint()


class FakeEvents:
    """Stands in for EventHelper: records what would have gone to events.db."""

    def __init__(self):
        self.events = []
        self.commands = []

    def log_general(self, message, severity="INFO", event_type="GENERAL", metadata=None, timestamp_override=None):
        self.events.append({"message": message, "severity": severity, "type": event_type, "metadata": metadata or {}})

    def log_command(self, message, ok=True, metadata=None):
        self.commands.append({"message": message, "ok": ok, "metadata": metadata or {}})

    def log_fault(self, fault_key, active, metadata=None):
        self.events.append({"message": fault_key, "type": "FAULT", "active": active})

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


@pytest.fixture
def fake_events():
    return FakeEvents()

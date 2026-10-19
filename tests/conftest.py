"""Test configuration.

Every test process runs with its ZeroMQ ports shifted (SINDIE_PORT_OFFSET) and its
data/log folders (and saved window layouts) redirected to a temporary directory, so the
suite can run on the lab PC while the real system is running without touching it.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="sindie_tests_")
os.environ.setdefault("SINDIE_PORT_OFFSET", "20000")
os.environ.setdefault("SINDIE_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("SINDIE_LOG_DIR", os.path.join(_TMP, "logs"))
# Window layouts the test windows save (and restore) stay here, not in the user's settings
os.environ.setdefault("SINDIE_SETTINGS_DIR", os.path.join(_TMP, "settings"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Recipes the tests write go to a copy of config/recipes, never the real folder
if not os.environ.get("SINDIE_RECIPES_DIR", "").strip():
    import shutil  # noqa: E402

    os.environ["SINDIE_RECIPES_DIR"] = os.path.join(_TMP, "recipes")
    shutil.copytree(os.path.join(ROOT, "config", "recipes"), os.environ["SINDIE_RECIPES_DIR"], dirs_exist_ok=True)

# A tag registry built fresh from the config files in this checkout (not whatever
# config/system_tags.json happens to be on this machine), so limits match the config.
if not os.environ.get("SINDIE_REGISTRY", "").strip():
    import contextlib  # noqa: E402
    import io  # noqa: E402

    os.environ["SINDIE_REGISTRY"] = os.path.join(_TMP, "system_tags.json")
    from src.utils.build_registry import build_system_registry  # noqa: E402

    with contextlib.redirect_stdout(io.StringIO()):
        build_system_registry(os.environ["SINDIE_REGISTRY"])

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


def _gh_escape(text: str, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def pytest_runtest_logreport(report):
    """On GitHub Actions, also report each failure as an annotation, so it shows on the
    run's summary page (and through the API) without opening the full log."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not report.failed:
        return
    lines = [line for line in str(report.longrepr).splitlines() if line.strip()]
    title = f"{report.nodeid} ({report.when})"
    sys.__stdout__.write(f"\n::error title={_gh_escape(title, prop=True)}::{_gh_escape(chr(10).join(lines[-20:]))}\n")
    sys.__stdout__.flush()


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

    def log_user_marker(self, marker_ts, text, color):
        self.events.append({"message": text, "severity": "INFO", "type": "USER_MARKER", "metadata": {"color": color}})

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


@pytest.fixture
def fake_events():
    return FakeEvents()

"""src/utils/network_probe.py, without the network: the report, the test notification, the folder
check, and that it needs nothing but the standard library (it is meant to run on the lab PC with
any Python)."""
import ast
import datetime
import os
import sys

from src.utils import network_probe as probe


def test_standard_library_only():
    with open(probe.__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_the_backup_folder_is_the_compactors():
    from src.services.service_data_compactor import ONEDRIVE_PATH
    assert dict((path, label) for label, path in probe.FOLDERS)[ONEDRIVE_PATH].startswith(
        "The data compactor's backup folder")


def test_the_report_names_no_pc_and_says_what_was_reached():
    results = [{"name": "ntfy", "host": "ntfy.sh", "port": 443, "kind": "https", "dns": "ok (1.2.3.4, 3 ms)",
                "https": "ok (HTTP 200, 80 ms)"},
               {"name": "SMTP", "host": "smtp.office365.com", "port": 587, "kind": "smtp",
                "dns": "ok (5.6.7.8, 2 ms)", "tcp": "FAILED: timed out (10000 ms)"}]
    shares = [("The research share", r"\\surrey.ac.uk\research\ibcfac2", "no answer within 20 s")]
    text = probe.report(results, shares, "", datetime.datetime(2026, 10, 22, 9, 30))
    assert "[ok] ntfy: ntfy.sh:443" in text and "[--] SMTP: smtp.office365.com:587" in text
    assert "The research share (\\\\surrey.ac.uk\\research\\ibcfac2): no answer within 20 s" in text
    assert "1 of 2 services reached" in text
    import socket
    assert socket.gethostname() not in text and "addresses" not in text


def test_the_test_notification_names_no_pc(monkeypatch):
    sent = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(probe.urllib.request, "urlopen", lambda req, **k: sent.append(req) or Answer())
    assert probe.send_test("sindie-test-abc") == "sent (HTTP 200)"
    import socket
    body = sent[0].data.decode()
    assert body.startswith("SINDIE network test at ") and socket.gethostname() not in body
    assert sent[0].full_url == "https://ntfy.sh/sindie-test-abc"


def test_a_folder_is_only_listed(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert probe.check_folder(str(tmp_path)).startswith("readable: 1 entries")
    assert probe.check_folder(os.path.join(str(tmp_path), "missing")).startswith("FAILED")
    assert os.listdir(tmp_path) == ["a.txt"]            # nothing written

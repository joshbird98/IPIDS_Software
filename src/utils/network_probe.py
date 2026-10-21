"""What the lab PC's network lets through, for choosing how SINDIE could send alerts, show its state
remotely and back up its data. Run it on the lab PC and send the report:

    python src/utils/network_probe.py                read-only checks (about 30 s)
    python src/utils/network_probe.py --send-test    also sends one test notification through ntfy.sh,
                                                     to a random topic it prints (open it on a phone
                                                     first to see it arrive)

Only the standard library: it runs with any Python 3.8+. Every check goes out from the PC (nothing
listens for connections), certificates are always verified (a failure is reported, as it can mean the
network inspects HTTPS), and nothing is written anywhere but the report. The report is printed and
saved as SystemLogs/network_probe_<date>.txt.

For each service: does its name resolve (DNS), does a connection to it open (TCP), and does HTTPS
work (TLS and an HTTP answer, any status code: an answer is what matters), and how long each took.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TIMEOUT = 10.0

# (what it would be for, host, port, kind): kind https = GET /, tcp = connection only, smtp = its greeting
CHECKS = [
    ("Push alerts: ntfy (phone notifications, free)", "ntfy.sh", 443, "https"),
    ("Push alerts: Pushover", "api.pushover.net", 443, "https"),
    ("Push alerts: Telegram bot", "api.telegram.org", 443, "https"),
    ("Dead-man's switch: healthchecks.io pings", "hc-ping.com", 443, "https"),
    ("Teams workflow webhooks (Power Automate), an example host", "prod-00.westus.logic.azure.com", 443, "https"),
    ("Microsoft sign-in (OneDrive, Teams, Graph)", "login.microsoftonline.com", 443, "https"),
    ("Microsoft Graph (OneDrive / SharePoint upload)", "graph.microsoft.com", 443, "https"),
    ("E-mail: Office 365 SMTP (STARTTLS)", "smtp.office365.com", 587, "smtp"),
    ("E-mail: Gmail SMTP (STARTTLS)", "smtp.gmail.com", 587, "smtp"),
    ("GitLab (status pushed to a repository)", "gitlab.com", 443, "https"),
    ("GitHub", "github.com", 443, "https"),
    ("GitHub API", "api.github.com", 443, "https"),
    ("Cloudflare Tunnel (read-only remote page), port 7844", "region1.v2.argotunnel.com", 7844, "tcp"),
    ("Cloudflare (Workers / Pages)", "www.cloudflare.com", 443, "https"),
    ("Tailscale coordination (private remote access)", "controlplane.tailscale.com", 443, "https"),
    ("Tailscale relay (DERP)", "derp1.tailscale.com", 443, "https"),
    ("Google Gemini (recipe drafting)", "generativelanguage.googleapis.com", 443, "https"),
    ("Python packages (pip)", "pypi.org", 443, "https"),
    ("Python packages (downloads)", "files.pythonhosted.org", 443, "https"),
    ("Backblaze B2 (off-site backups, restic)", "api.backblazeb2.com", 443, "https"),
]
# (label, folder): read-only checks. The second is service_data_compactor.ONEDRIVE_PATH, written out
# so that this runs with the standard library alone (tests/test_network_probe.py checks they match)
FOLDERS = [("The research share", r"\\surrey.ac.uk\research\ibcfac2"),
           ("The data compactor's backup folder (OneDrive)",
            r"C:\Users\100kV\Josh\OneDrive\OneDrive - University of Surrey\IPIDS_Project\IPIDS_Data_Backup")]


def _ms(t0: float) -> str:
    return f"{(time.monotonic() - t0) * 1000:.0f} ms"


def check(name: str, host: str, port: int, kind: str) -> dict:
    out = {"name": name, "host": host, "port": port, "kind": kind}
    t0 = time.monotonic()
    proxy = urllib.request.getproxies().get("https")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out["dns"] = f"ok ({infos[0][4][0]}, {_ms(t0)})"
    except OSError as e:
        out["dns"] = f"FAILED: {e}"
        if not (proxy and kind == "https"):     # (through a proxy, the proxy looks the name up)
            return out
    if kind == "https":
        t0 = time.monotonic()
        req = urllib.request.Request(f"https://{host}/", method="GET",
                                     headers={"User-Agent": "SINDIE-network-probe/1"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
                out["https"] = f"ok (HTTP {r.status}, {_ms(t0)})"
        except urllib.error.HTTPError as e:              # an answer: the service was reached
            out["https"] = f"ok (HTTP {e.code}, {_ms(t0)})"
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                out["https"] = (f"CERTIFICATE NOT TRUSTED ({reason.verify_message}): the network may be "
                                f"inspecting HTTPS")
            else:
                out["https"] = f"FAILED: {reason} ({_ms(t0)})"
        except (OSError, ssl.SSLError) as e:
            out["https"] = f"FAILED: {e} ({_ms(t0)})"
        if proxy:
            out["https"] += f" via proxy {proxy}"
        return out
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as s:
            out["tcp"] = f"ok ({_ms(t0)})"
            if kind == "smtp":
                s.settimeout(TIMEOUT)
                banner = s.recv(200).decode("ascii", "replace").strip()
                out["smtp"] = f"greeting: {banner[:60]}" if banner else "no greeting"
    except OSError as e:
        out["tcp"] = f"FAILED: {e} ({_ms(t0)})"
    return out


def check_folder(path: str, timeout: float = 20.0) -> str:
    """Whether a folder (a network share) can be listed, without waiting on a share that doesn't
    answer (the listing runs in a thread the program doesn't wait for). Nothing is written."""
    result = {}

    def listing():
        try:
            result["n"] = len(os.listdir(path))
        except OSError as e:
            result["error"] = e

    t0 = time.monotonic()
    worker = threading.Thread(target=listing, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return f"no answer within {timeout:.0f} s"
    if "error" in result:
        return f"FAILED: {result['error']}"
    return f"readable: {result['n']} entries ({_ms(t0)}); write access not tested"


def send_test(topic: str) -> str:
    """One notification through ntfy.sh to `topic` (only with --send-test)."""
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", method="POST",
                                 data=f"SINDIE network test at {datetime.datetime.now():%H:%M:%S}".encode(),
                                 headers={"Title": "SINDIE network test", "User-Agent": "SINDIE-network-probe/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            return f"sent (HTTP {r.status})"
    except (urllib.error.URLError, OSError) as e:
        return f"FAILED: {getattr(e, 'reason', e)}"


def report(results, shares, sent, started) -> str:
    lines = [f"SINDIE network probe, {started:%Y-%m-%d %H:%M}", f"PC: {platform.platform()}, "
             f"Python {platform.python_version()}"]
    proxies = urllib.request.getproxies()
    shown = {k: v for k, v in proxies.items() if k in ("http", "https")}
    if "no" in proxies:
        shown["no proxy for"] = f"{len(proxies['no'].split(','))} names"
    lines.append("Proxy settings: " + (json.dumps(shown) if shown else "none (direct connections)"))
    lines.append("")
    worked = 0
    for r in results:
        steps = [f"DNS {r['dns']}"] + [f"{k.upper()} {r[k]}" for k in ("tcp", "smtp", "https") if k in r]
        last = steps[-1].split(" ", 1)[1]          # (the last step decides: through a proxy, DNS needn't work)
        ok = not last.startswith(("FAILED", "CERTIFICATE", "no "))
        worked += ok
        lines.append(f"[{'ok' if ok else '--'}] {r['name']}: {r['host']}:{r['port']}")
        lines += [f"       {s}" for s in steps]
    lines.append("")
    for label, path, result in shares:
        lines.append(f"{label} ({path}): {result}")
    if sent:
        lines.append(f"Test notification: {sent}")
    lines.append("")
    lines.append(f"{worked} of {len(results)} services reached. (A Teams workflow's own webhook address, once one "
                 "is made, is worth checking too: it may be on another host.)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--send-test", action="store_true",
                    help="also send one test notification through ntfy.sh (to a random topic, printed first)")
    args = ap.parse_args()
    started = datetime.datetime.now()
    sent = ""
    if args.send_test:
        topic = "sindie-test-" + secrets.token_hex(6)
        print(f"Test notification topic: https://ntfy.sh/{topic}  (open it in a browser or the ntfy app now)")
        time.sleep(5)
    print(f"Checking {len(CHECKS)} services and {len(FOLDERS)} folders (about 30 s)...")
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda c: check(*c), CHECKS))
    shares = [(label, p, check_folder(p)) for label, p in FOLDERS]
    if args.send_test:
        sent = f"{send_test(topic)} to https://ntfy.sh/{topic}"
    text = report(results, shares, sent, started)
    print()
    print(text)
    folder = os.path.join(ROOT, "SystemLogs")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"network_probe_{started:%Y%m%d_%H%M}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\nSaved as {path}")
    except OSError as e:
        print(f"\n(Not saved: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

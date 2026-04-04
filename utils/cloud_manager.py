import time
import threading
import json
import requests
import numpy as np

# --- CONFIGURATION (Or pass these into __init__) ---
# Ideally, move these to a config file, but for now we keep them here
GIST_ID = "9de20220c7cd1e3c359c22b4775faa46"
TOKEN = "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"

class NumpyEncoder(json.JSONEncoder):
    """ Helper to handle NumPy types in JSON """

    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return obj.item()
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)


class CloudManager:
    def __init__(self, target_interval=10.0):
        self.upload_target_interval = target_interval

        # State Tracking
        self.next_allowed_upload_time = 0
        self.upload_consecutive_failures = 0
        self.current_thread = None

    def tick(self, snapshot_data):
        """
        Called by PLC_Interface when logging locally.
        snapshot_data: The dictionary of tag values { "Voltage": 5.5, ... }
        """
        # 1. Check Thread Status
        if self.current_thread is not None and self.current_thread.is_alive():
            return

            # 2. Check Timing / Backoff
        if time.time() < self.next_allowed_upload_time:
            return

        # 3. START BACKGROUND THREAD
        # We pass the data into the thread so it doesn't change mid-upload
        self.current_thread = threading.Thread(target=self._upload_worker, args=(snapshot_data,))
        self.current_thread.daemon = True
        self.current_thread.start()

    def _upload_worker(self, snapshot):
        """
        Runs in background. Handles the specific Gist logic + Rate Limiting.
        """
        try:
            # --- 1. PREPARE PAYLOAD (Your Logic) ---
            final_payload = {
                "timestamp": time.time(),
                "data": snapshot
            }

            # Serialize once to ensure it's valid before network opening
            json_content = json.dumps(final_payload, cls=NumpyEncoder)

            payload = {"files": {"status.json": {"content": json_content}}}
            headers = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}

            # --- 2. SEND REQUEST ---
            response = requests.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                json=payload,
                headers=headers,
                timeout=10
            )

            # --- 3. HANDLE RATE LIMITS (Your Logic) ---
            cooldown = self.upload_target_interval

            # A. Check "Retry-After" header
            if "Retry-After" in response.headers:
                wait_time = int(response.headers["Retry-After"])
                cooldown = max(cooldown, wait_time + 2)

            # B. Check "X-RateLimit" headers
            remaining = int(response.headers.get("X-RateLimit-Remaining", 5000))
            reset_time = float(response.headers.get("X-RateLimit-Reset", 0))

            if remaining < 50:
                time_until_reset = max(0.0, reset_time - time.time())
                adaptive_slowdown = (time_until_reset / max(1, remaining)) + 10.0
                cooldown = max(cooldown, adaptive_slowdown)

            # --- 4. SUCCESS / FAILURE HANDLING ---
            if response.status_code == 200:
                self.upload_consecutive_failures = 0
                # print(f"✅ Cloud OK. Next in {cooldown:.1f}s")

            elif response.status_code in [403, 429]:
                # Hard Backoff for abuse detection
                self.upload_consecutive_failures += 1
                penalty = 60 * self.upload_consecutive_failures
                cooldown = max(cooldown, penalty)
                print(f"❌ Cloud Rate Limited. Backing off {cooldown}s")

            else:
                print(f"❌ Cloud Upload Error: {response.status_code}")
                self.upload_consecutive_failures += 1
                cooldown = 30  # Default penalty for generic error

            # Set the next allowed time
            self.next_allowed_upload_time = time.time() + cooldown

        except Exception as e:
            print(f"❌ Cloud Connection Error: {e}")
            self.upload_consecutive_failures += 1
            self.next_allowed_upload_time = time.time() + 60
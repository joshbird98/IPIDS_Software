import streamlit as st
import requests
import time
import json
import datetime
import pandas as pd

# --- CONFIGURATION ---
# The raw link to your status.json file
RAW_URL = "https://gist.githubusercontent.com/joshbird98/9de20220c7cd1e3c359c22b4775faa46/raw/status.json"

st.set_page_config(
    page_title="Ion Source Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- CSS STYLING ---
st.markdown("""
    <style>
        /* 1. The Card Container */
        .stMetric {
            background-color: #1E1E1E; /* Slightly lighter dark grey */
            border: 1px solid #333333; /* Subtle border */
            padding: 15px 20px;
            border-radius: 10px;        /* Rounded corners */
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3); /* Pop-out shadow */
        }

        /* 2. The Label (Small text at the top) */
        [data-testid="stMetricLabel"] {
            color: #B0B0B0 !important; /* Light Grey (Readable) */
            font-weight: 500;
            font-size: 0.9rem;
        }

        /* 3. The Value (Big number in the middle) */
        [data-testid="stMetricValue"] {
            color: #FFFFFF !important; /* Pure White */
            font-family: 'Roboto Mono', monospace; /* Tech/Sci-fi font look */
        }

        /* 4. Center alignment fix */
        div[data-testid="column"] {
            text-align: center;
        }

        /* 5. Reduce whitespace at top */
        .block-container {
            padding-top: 2rem;
        }
    </style>
""", unsafe_allow_html=True)


# --- DATA FETCHING ---
def get_raw_data():
    """Fetches the JSON directly from the raw GitHub URL"""
    try:
        # Cache buster to force GitHub to give us the fresh file
        cache_buster = f"?t={int(time.time())}"
        final_url = RAW_URL + cache_buster

        response = requests.get(final_url, timeout=5)
        if response.status_code == 200:
            try:
                snapshot = response.json()
            except json.JSONDecodeError:
                return {"error": "File is not valid JSON"}, None

            raw_ts = snapshot.get('timestamp')

            # Debugging: If no timestamp, return None immediately
            if raw_ts is None:
                return snapshot, None

            # Attempt Parsing
            try:
                # 1. Try Float (Unix Timestamp)
                ts_val = float(raw_ts)
            except (ValueError, TypeError):
                # 2. Try ISO String (e.g. 2026-01-17T...)
                try:
                    # Clean up "Z" if present
                    clean_ts = str(raw_ts).replace("Z", "")
                    # Use standard library (lighter than pandas)
                    dt = datetime.datetime.fromisoformat(clean_ts)
                    ts_val = dt.timestamp()
                except Exception as e:
                    # If parsing fails, we still return the data, just no time
                    print(f"Timestamp parsing failed: {e}")
                    ts_val = None

            return snapshot, ts_val

    except Exception:
        pass
    return None, None


def get_val(data, path, default=0):
    """Helper to safely extract nested keys"""
    if not data: return default
    return data.get(path, default)


# --- MAIN UI ---

# 1. Header & Controls
col_title, col_btn = st.columns([6, 1])
with col_title:
    st.title("⚡ IPIDS Remote Dashboard")
with col_btn:
    if st.button('🔄 Refresh'):
        st.rerun()

# --- 2. INTELLIGENT DATA FETCHING ---

# Initialize memory (Session State) to store the best data we've seen so far
if "best_snapshot" not in st.session_state:
    st.session_state["best_snapshot"] = None
if "best_ts" not in st.session_state:
    st.session_state["best_ts"] = 0.0

# Fetch fresh candidate from the web
fresh_snapshot, fresh_ts = get_raw_data()

# DECISION ENGINE:
# 1. If we got new data AND it is newer/same as what we have -> Update
if fresh_snapshot and fresh_ts:
    if fresh_ts >= st.session_state["best_ts"]:
        st.session_state["best_snapshot"] = fresh_snapshot
        st.session_state["best_ts"] = fresh_ts
    else:
        # This is the fix!
        # We fetched data, but it was OLDER than what we already have.
        # This is a "Ghost" packet from an out-of-sync GitHub server.
        # We silently ignore it.
        pass

# 2. If fetch failed entirely (None), we just stick with what we have in memory.

# Set the variables to be used by the rest of the script
raw_snapshot = st.session_state["best_snapshot"]
msg_timestamp = st.session_state["best_ts"]

# --- 3. CONNECTION LOGIC ---

# If we have absolutely nothing in memory (First run and failed), show error
if raw_snapshot is None:
    st.warning("📡 Connecting to GitHub...")
    st.info(f"Target: {RAW_URL}")
    time.sleep(2)
    st.rerun()

# Extract 'data' dict from our "Best Known" snapshot
data = raw_snapshot.get("data", {})

# Extract 'data' dict
data = raw_snapshot.get("data", {})

# Calculate "Freshness"
age_seconds = None
status_color = "grey"
status_msg = "UNKNOWN STATE"

if msg_timestamp:
    age_seconds = time.time() - msg_timestamp

    if age_seconds > 300:  # 5 Minutes = Definitely Offline
        status_color = "red"
        status_msg = "OFFLINE / STALE"
    elif age_seconds > 130:  # 80 Seconds = Missed a beat (since we upload every 60s)
        status_color = "orange"
        status_msg = "SLOW CONNECTION"
    else:
        status_color = "green"
        status_msg = "ONLINE"
else:
    # Timestamp failed to parse
    status_color = "red"
    status_msg = "DATA INVALID"

# 4. Status Banner
fault_active = get_val(data, "system.general.systemFault", False)
state_code = get_val(data, "system.ionSource.general.status", 0)
state_map = {0: "OFF", 1: "STARTING", 2: "RUNNING", 99: "FAULT"}
sys_state = state_map.get(state_code, "UNKNOWN")

# Banner Container
with st.container():
    c1, c2, c3 = st.columns([1, 2, 1])
    c1.metric("System State", sys_state)

    # Logic: Fault overrides everything. Then Connection Status.
    if fault_active:
        c2.error(f"⚠️ SYSTEM FAULT ACTIVE ({status_msg})")
    elif status_color == "green":
        c2.success(f"🟢 SYSTEM NORMAL ({status_msg})")
    elif status_color == "orange":
        c2.warning(f"🟠 {status_msg} ({int(age_seconds)}s ago)")
    else:
        # Red / Grey
        c2.error(f"🔴 {status_msg}")

    # Timestamp Display
    if msg_timestamp:
        pretty_time = datetime.datetime.fromtimestamp(msg_timestamp).strftime('%H:%M:%S')
    else:
        pretty_time = "Unknown"
    c3.metric("Last Update", pretty_time)

st.divider()

# --- KEY METRICS LAYOUT ---

# ROW 1: The "Big Three"
st.subheader("🚀 Primary Parameters")
r1c1, r1c2, r1c3, r1c4 = st.columns(4)

coolant = get_val(data, "system.general.coolantStatus", False)
r1c1.metric("Coolant Flow", "OK" if coolant else "LOW", delta_color="normal" if coolant else "inverse")

p_source = get_val(data, "system.vacuumSystem.gauges.source.readback_mB", 0)
r1c2.metric("Source Pressure", f"{p_source:.2e} mbar")

cup_current = get_val(data, "beamline.drop_in_cup.measured_current_A", 0)
if cup_current == 0:
    cup_current = get_val(data, "beamline.straight_thru_cup.measured_current_A", 0)
    label = "Beam Current (Straight)"
else:
    label = "Beam Current (Cup)"
r1c3.metric(label, f"{cup_current * 1e6:.1f} µA")

v_mag = get_val(data, "beamline.magnet.readbackA", 0)
r1c4.metric("Magnet Current", f"{v_mag:.2f} A")

# ROW 2: Ion Source Internals
st.subheader("⚛️ Ion Source Control")
r2c1, r2c2, r2c3, r2c4 = st.columns(4)

fil_a = get_val(data, "system.ionSource.ioniser.filament.readbackA", 0)
r2c1.metric("Filament", f"{fil_a:.2f} A")

ion_w = get_val(data, "system.ionSource.ioniser.readbackW", 0)
r2c2.metric("Ioniser Power", f"{ion_w:.1f} W")

ext_v = get_val(data, "system.ionSource.extraction.readbackV", 0)
r2c3.metric("Extraction", f"{ext_v:.1f} V")

cs_temp = get_val(data, "system.ionSource.cesium.readbackC", 0)
r2c4.metric("Cesium Temp", f"{cs_temp:.1f} °C")

# ROW 3: Vacuum
st.subheader("💨 Vacuum & Cooling")
r3c1, r3c2, r3c3, r3c4 = st.columns(4)

turbo_spd = get_val(data, "system.vacuumSystem.pumps.turbo.source_1.speed", 0)
r3c1.metric("Turbo Speed", f"{turbo_spd:.0f} Hz")

coolant = get_val(data, "system.general.coolantStatus", False)
r3c2.metric("Coolant Flow", "OK" if coolant else "LOW", delta_color="normal" if coolant else "inverse")

gate_val = get_val(data, "system.vacuumSystem.valves.gate.open", False)
r3c3.metric("Gate Valve", "OPEN" if gate_val else "CLOSED")

stage_z = get_val(data, "endstation.stage.motion.readback_z_mm", 0)
r3c4.metric("Stage Z", f"{stage_z:.1f} mm")

# --- RAW DATA (DEBUGGING) ---
st.divider()
with st.expander("🛠️ View Raw Telemetry Data (Debug)"):
    # Show the timestamp raw so we can see why it failed if it did
    st.write(f"**Raw Timestamp Received:** `{raw_snapshot.get('timestamp')}`")

    filtered_data = {k: v for k, v in data.items() if "faultArray" not in k and "messageBuffer" not in k}
    df = pd.DataFrame(list(filtered_data.items()), columns=["Tag", "Value"])
    st.dataframe(df, use_container_width=True)

# Auto-reload logic
time.sleep(1)
st.empty()
# Only auto-refresh if connection is reasonably healthy or just starting
if age_seconds is None or age_seconds < 600:
    time.sleep(2)
    st.rerun()

import re
import snap7
import threading

from PyQt5.QtWidgets import QLabel
from PyQt5.QtCore import QObject, QThread, pyqtSignal
import struct

from utils.history_manager import RingBufferHistory
from utils.fault_manager import FaultManager
from utils.logger import UnifiedDailyLogger
from utils.message_manager import MessageManager
from utils.cloud_manager import CloudManager

from hmi_config import *

from snap7.util import get_bool, get_int, get_dint, get_dword, get_real, get_string, get_uint, get_time

# ---- type dispatch table ----
TYPE_READERS = {
    "BOOL": lambda data, offset: get_bool(data, int(offset), int(round((offset - int(offset)) * 10))),
    "INT": lambda data, offset: get_int(data, int(offset)),
    "UINT": lambda data, offset: get_uint(data, int(offset)),
    "DINT": lambda data, offset: get_dint(data, int(offset)),
    "UDINT": lambda data, offset: get_uint(data, int(offset)),
    "TIME": lambda data, offset: get_time(data, int(offset)),
    "DWORD": lambda data, offset: get_dword(data, int(offset)),
    "REAL": lambda data, offset: get_real(data, int(offset)),
    "STRING": lambda data, offset: get_string(data, int(offset)),  # adjust max length
}

# Ion Source Modes
SRC_POWERED_OFF = 0
SRC_OFF = 1
SRC_CONDITIONING = 2
SRC_RUNNING = 3
SRC_PAUSING = 4
SRC_PAUSED = 5
SRC_TEST = 6

class PLCWorker(QObject):
    # Signals to communicate with the GUI
    data_updated = pyqtSignal()
    connection_status = pyqtSignal(bool)

    def __init__(self, plc_interface):
        super().__init__()
        self.plc = plc_interface
        self.running = True

    def run(self):
        while self.running:
            # Run the full cycle (Read -> History -> Faults)
            success = self.plc.process_cycle()
            self.connection_status.emit(success)
            if success:
                self.data_updated.emit()

    def stop(self):
        self.running = False

class PLC_Interface:
    def __init__(self, splash=None):
        self.splash = splash

        def update_status(message):
            if self.splash:
                self.splash.showMessage(message, Qt.AlignBottom | Qt.AlignCenter, QColor("white"))
                QApplication.processEvents()  # Force redraw immediately
            else:
                print(message)  # Fallback if no splash

        self.lock = threading.RLock()

        self.hmi_db_num = HMI_DB_NUM
        self.messages_db_num = MESSAGES_DB_NUM
        self.hmi_db_size = 0

        # ============= Connections and status to physical PLC =============
        self.plc_ip = PLC_IP_ADDRESS
        self.rack = PLC_RACK
        self.slot = PLC_SLOT
        self.client = None
        self.connected = False
        self.plc_running = False
        self.plc_stopped = False
        self.connectionDebugMessageSent = False
        QApplication.processEvents()
        update_status(f"Connecting to PLC ({self.plc_ip})...")
        if self.make_connection():
            update_status("Connected.")
        else:
            update_status("Failed to connect...")

        # ================= Dictionaries - save having to load strings each time =================
        self.messages_description_dict = {}
        if self.fillDictionaries():
            update_status("Retrieved message dictionary from PLC...")
        else:
            update_status("Failed to retrieve message dictionary from PLC...")

        # ================= Tags - create tag dictionary, for easy interaction =================
        self.tags = self.createTags()
        QApplication.processEvents()
        if self.readAllTagValues():
            update_status("Retrieved PLC tags...")
        else:
            update_status("Failed to retrieve PLC tags")

        # self.printTags()
        QApplication.processEvents()
        # ================= Centralised utilities ============
        self.history = RingBufferHistory(self)
        self.fault_manager = FaultManager(self, fault_array="system.general.faultArray")
        self.logger = UnifiedDailyLogger(self)
        self.message_manager = MessageManager(self)
        self.last_history_update = 0
        self.last_cloud_update = 0
        self.log_interval = LOG_INTERVAL  # Seconds
        QApplication.processEvents()
        self.cloud_manager = CloudManager(target_interval=10.0)

        # ================= Debug messages, from the GUI ===================
        self.gui_messages= [f"Program start - welcome."]

    def process_cycle(self):
        # 1. Perform the blocking read (~30ms)
        if not self.readAllTagValues(): return False
        current_time = time.time()

        self.history.update(current_time, self.tags)
        is_high_speed = self.logger.is_high_speed_requested
        current_interval = 0.1 if is_high_speed else self.log_interval

        if (current_time - self.last_history_update) >= current_interval:
            self.history.update(current_time, self.tags)
            self.last_history_update = current_time
            self.logger.log_snapshot(None)

        new_faults = self.fault_manager.check_new_faults()
        if new_faults:
            ts_array, data_arrays = self.history.get_snapshot()
            self.logger.log_high_speed_burst(None, None)

        if (current_time - self.last_cloud_update) >= self.log_interval:
            if self.connected:
                snapshot = {k: v['value'] for k, v in self.tags.items()}
                self.cloud_manager.tick(snapshot)
            self.last_cloud_update = current_time

        self.message_manager.update()

        return True

    def make_connection(self):
        with self.lock:
            # Snap7 connection
            self.client = snap7.client.Client()
            self.connected = True
            self.plc_running = True
            self.plc_stopped = False
            try:
                self.client.connect(self.plc_ip, self.rack, self.slot)
                (f"Successfully connected with PLC, using IP: {self.plc_ip}")
                self.connectionDebugMessageSent = False
            except RuntimeError:
                self.connected = False
                if not self.connectionDebugMessageSent:
                    print(f"Failed to connect with PLC, using IP: {self.plc_ip}")
                    self.connectionDebugMessageSent = True
            except snap7.Snap7Exception as e:
                self.plc_running = False
                self.plc_stopped = True
            return self.connected

    def disconnect(self):
        with self.lock:
            if self.connected:
                self.client.disconnect()
                self.connected = False
                self.plc_running = None
                self.plc_stopped = None

    def fillDictionaries(self):
        if not self.connected:
            self.make_connection()
        if self.connected:
            try:
                self.messages_description_dict = self.fillDictionary(self.messages_db_num, MESSAGES_DB_LENGTH)
                return True

            except RuntimeError as e:
                self.connected = False
                print(f"Failed to communicate with PLC - {e}")
                return False
            except Exception as e:
                if '0x00830000' in str(e) or 'STOP' in str(e):
                    self.plc_running = False
                    self.plc_stopped = True
                    print("PLC is stopped - cannot read data")
                else:
                    print(f"Possible PLC fault, failed to communicate - {e}")
                return False
        else:
            return False

    def fillDictionary(self, db_num, length):
        myDictionary = {}
        db_data = self.client.db_read(db_num, 0, length * 256)
        for i in range(length):
            start_index = i * 256
            end_index = start_index + 256

            # Slice the current string's 256-byte data from the main buffer
            string_block = db_data[start_index:end_index]

            # Get the actual length from the second byte (index 1)
            actual_length = string_block[1]
            if actual_length > 0:
                # The string data starts at index 2. We read 'actual_length' bytes from there.
                string_data = string_block[2: 2 + actual_length].decode('utf-8')
            else:
                string_data = "Message code unused"

            # Add the parsed string to the dictionary with the index as the key
            myDictionary[i] = string_data

        return myDictionary

    def createTags(self):
        """
        Parse a TIA Portal DB export file (the .db file text form).
        Builds a dictionary of tags with offset, type, writable, and a placeholder value.
        Handles nested structs, including those with special characters (e.g. "source-beamline").
        """
        tags = {}
        tag_order = []  # Maintain parse order to map offsets later
        stack = []  # Track nested struct names
        offsets = []

        with open(HMI_DB_DOCUMENT_ADDRESS, "r", encoding="utf-8") as f:
            lines = f.readlines()

        in_struct = False
        in_offsets = False

        # --- COMPILED REGEX ---
        # The pattern ((?:"[^"]+")|(?:\w+)) matches either:
        # 1. "Something-With-Hyphens" (Quoted)
        # 2. standardName (Alphanumeric)

        # Matches: name : Array[x..y] of Bool
        re_array_bool = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*Array\[(\d+)\.\.(\d+)\]\s*of\s*Bool', re.IGNORECASE)

        # Matches: name : Array[x..y] of Int
        re_array_int = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*Array\[(\d+)\.\.(\d+)\]\s*of\s*Int', re.IGNORECASE)

        # Matches: name : Type  OR  name : Struct
        re_std = re.compile(r'((?:"[^"]+")|(?:\w+))(?:.*):\s*(\w+)')

        for line in lines:
            line = line.strip()

            # --- Structural Markers ---
            if line.startswith("STRUCT"):
                in_struct = True
                continue

            if line.startswith("END_STRUCT;"):
                if stack:
                    stack.pop()
                continue

            if line.startswith("END_DATA_BLOCK"):
                in_offsets = True
                continue

            # --- Offsets Section ---
            if in_offsets:
                try:
                    # TIA Portal exports offsets like "18.4", we need float
                    offsets.append(float(line))
                except ValueError:
                    pass
                continue

            # --- Tag Definition Section ---
            if in_struct and ":" in line and not line.startswith("TITLE"):

                # Run Regex Checks
                array_match_bool = re_array_bool.search(line)
                array_match_int = re_array_int.search(line)
                std_match = re_std.match(line)

                # 1. ARRAY OF BOOL
                if array_match_bool:
                    name_raw, start_idx, end_idx = array_match_bool.groups()
                    # Remove quotes if present: "source-beamline" -> source-beamline
                    name = name_raw.replace('"', '')

                    start_idx, end_idx = int(start_idx), int(end_idx)
                    writable = "ExternalWritable := 'False'" not in line

                    for i in range(start_idx, end_idx + 1):
                        full_name = ".".join(stack + [f"{name}[{i}]"])
                        tags[full_name] = {
                            "offset": None,
                            "type": "BOOL",
                            "writable": writable,
                            "value": None,
                            "widget": None,
                        }
                        tag_order.append(full_name)

                # 2. ARRAY OF INT
                elif array_match_int:
                    name_raw, start_idx, end_idx = array_match_int.groups()
                    name = name_raw.replace('"', '')

                    start_idx, end_idx = int(start_idx), int(end_idx)
                    writable = "ExternalWritable := 'False'" not in line

                    for i in range(start_idx, end_idx + 1):
                        full_name = ".".join(stack + [f"{name}[{i}]"])
                        tags[full_name] = {
                            "offset": None,
                            "type": "INT",
                            "writable": writable,
                            "value": None,
                            "widget": None,
                        }
                        tag_order.append(full_name)

                # 3. STANDARD TAG or STRUCT DEF
                elif std_match:
                    name_raw, dtype = std_match.groups()
                    name = name_raw.replace('"', '')
                    dtype = dtype.upper()

                    if dtype == "STRUCT":
                        # Pushing to stack ensures 'source-beamline' is treated as a folder
                        stack.append(name)
                    else:
                        writable = "ExternalWritable := 'False'" not in line
                        full_name = ".".join(stack + [name])

                        tags[full_name] = {
                            "offset": None,
                            "type": dtype,
                            "writable": writable,
                            "value": None,
                            "widget": None,
                        }
                        tag_order.append(full_name)

        # --- Assign Offsets ---
        # Filter out duplicate offsets (keep order)
        if offsets:
            clean_offsets = [offsets[0]]
            for i in range(1, len(offsets)):
                if offsets[i] != offsets[i - 1]:
                    clean_offsets.append(offsets[i])

            # Map offsets to tags
            for i, tag_name in enumerate(tag_order):
                if i < len(clean_offsets):
                    tags[tag_name]["offset"] = clean_offsets[i]

            # --- Calculate Final DB Size ---
            # Look at the very last tag to determine the total length required
            if tag_order:
                last_tag_name = tag_order[-1]
                last_offset = tags[last_tag_name]["offset"]
                final_dtype = tags[last_tag_name]["type"]

                # Start with the offset of the last item
                self.hmi_db_size = int(last_offset)

                # Add bytes based on the type of the last item
                if final_dtype == "BOOL":
                    self.hmi_db_size += 1
                elif final_dtype in ["INT", "UINT", "WORD"]:
                    self.hmi_db_size += 2
                elif final_dtype in ["DINT", "UDINT", "REAL", "TIME", "DWORD"]:
                    self.hmi_db_size += 4
                elif final_dtype == "STRING":
                    self.hmi_db_size += 256
                else:
                    print(f"Size calculation not implemented for {final_dtype}, defaulting to +0")
                    self.hmi_db_size += 0
        else:
            self.hmi_db_size = 0
            print("Warning: No offsets found in DB file.")

        #for tag in tags:
            #print(
                #f"{tag}, {tags[tag]['offset']}, {tags[tag]['type']}, {tags[tag]['writable']}, {tags[tag]['value']}, {tags[tag]['widget']}")

        return tags

    def readAllTagValues(self):
        with self.lock:
            if not self.connected:
                self.make_connection()
            if self.connected:
                try:
                    # --- TIMER START ---
                    t_end = 0
                    t_start = time.perf_counter()

                    # 1. Read the raw bytes from the PLC
                    # Ensure hmi_db_size covers the full array (calculated in your parsing step)
                    data = self.client.db_read(self.hmi_db_num, 0, self.hmi_db_size)
                    t_network = time.perf_counter()  # Timestamp after network read

                    # 2. Iterate over every tag in your dictionary
                    for tag_name, meta in self.tags.items():
                        offset = meta.get("offset")
                        dtype = meta.get("type", "").upper()

                        # Safety check: Ignore tags that didn't get an offset assigned
                        if offset is None:
                            continue

                        if dtype in TYPE_READERS:
                            try:
                                # 3. Parse the value using the dispatch table
                                meta["value"] = TYPE_READERS[dtype](data, offset)
                            except Exception as e:
                                meta["value"] = None
                                print(f"Warning: failed to parse {tag_name} ({dtype} @ {offset}): {e}")
                        else:
                            # Handle unknown types or Struct headers that shouldn't be read directly
                            pass


                    # --- TIMER END ---
                    t_end = time.perf_counter()
                    # Calculate durations in milliseconds
                    network_ms = (t_network - t_start) * 1000
                    parse_ms = (t_end - t_network) * 1000
                    total_ms = (t_end - t_start) * 1000

                    #print(f"Total: {total_ms:.2f}ms | Network: {network_ms:.2f}ms | Parse: {parse_ms:.2f}ms")
                    #self.printTags()
                    return True

                except RuntimeError as e:
                    self.connected = False
                    print(f"Failed to communicate with PLC - {e}")
                    return False
                except Exception as e:
                    if '0x00830000' in str(e) or 'STOP' in str(e):
                        self.plc_running = False
                        self.plc_stopped = True
                        print("PLC is stopped - cannot read data")
                    else:
                        print(f"Possible PLC fault, failed to communicate - {e}")
                    return False
            else:
                return False
            return True
        return False

    def printTags(self):
        for tag in self.tags:
            print(f"{tag}: {self.tags[tag]['value']}")

    def writeTag(self, tag, new_value):
        with self.lock:
            #print(f"Writing {new_value} to {tag}")
            self.plc_running = True
            self.plc_stopped = False

            if tag not in self.tags:
                print(f"Warning - Tag '{tag}' not found")
                return False

            meta = self.tags[tag]
            if not meta["writable"]:
                print(f"Warning - Tag '{tag}' is not writable")
                return False

            byte_offset = int(meta["offset"])
            bit_offset = int(round((meta["offset"] % 1) * 10))
            dtype = meta["type"]

            try:
                if dtype == "BOOL":
                    data = bytearray(1)
                    snap7.util.set_bool(data, 0, bit_offset, bool(new_value))
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "INT":
                    new_value = int(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(2)
                    snap7.util.set_int(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "UINT":
                    new_value = int(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(2)
                    snap7.util.set_uint(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "DINT":
                    new_value = int(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(4)
                    snap7.util.set_dint(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "UDINT":
                    new_value = int(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(4)
                    snap7.util.set_udint(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "TIME":
                    new_value = int(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(4)
                    snap7.util.set_udint(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "REAL":
                    new_value = float(new_value)
                    #if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                        #print(f"Requested setpoint value {new_value} is out of range.")
                        #return False
                    data = bytearray(4)
                    snap7.util.set_real(data, 0, new_value)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                elif dtype == "STRING":
                    new_value = str(new_value)
                    data = bytearray(256)
                    snap7.util.set_string(data, 0, str(new_value), 254)
                    self.client.db_write(self.hmi_db_num, byte_offset, data)
                    return True

                else:
                    print(f"Write not implemented for {dtype}")
                    return False

            except ValueError as e:
                print(f"Warning - Failed to write tag '{tag}', due to incorrect data type: {e}")
                return False
            except Exception as e:
                print(f"PLC write failed - {e}")
                self.plc_running = False
                self.plc_stopped = True
                return False

    def get_all_tags_snapshot(self):
        """Returns a simple dictionary of { 'TagName': Value } for all known tags."""
        snapshot = {}

        # Example (if using the dictionary structure from your snippet):
        if hasattr(self, 'tags'):
            for name, meta in self.tags.items():
                snapshot[name] = meta.get('value', 0)

        return snapshot

    def read_mass_scan_data(self, arrayLen=5000):
        """
        Reads the full (up to 5000-points) array from DB35.
        Input: Length of points to retrieve, default 5000
        Returns: A list of tuples [(mag1, curr1), (mag2, curr2), ...]
        """
        DB_NUMBER = 35
        STRUCT_SIZE = 8     # 2x Real (4 bytes each)
        TOTAL_BYTES = arrayLen * STRUCT_SIZE

        with self.lock:
            try:
                print(f"Starting bulk read of {TOTAL_BYTES} bytes from DB{DB_NUMBER}...")
                raw_data = self.client.db_read(DB_NUMBER, 0, TOTAL_BYTES)

                if len(raw_data) != TOTAL_BYTES:
                    print("Error - incomplete read from Scan DB")
                    return None

                results = []

                # Pre-calculate offset loop to avoid range overhead
                # We step through the bytearray 8 bytes at a time
                for i in range(0, TOTAL_BYTES, STRUCT_SIZE):
                    # Slice the 8 bytes for this struct
                    chunk = raw_data[i: i + STRUCT_SIZE]

                    # Unpack 2 floats (Big Endian)
                    mag, curr = struct.unpack('>ff', chunk)
                    results.append((mag, curr))

                print(f"Bulk Read Complete. {len(results)} points parsed.")
                return results

            except Exception as e:
                print(f"Mass Scan Read Failed: {e}")
                return None

    def reset_scan_flag(self):
        # Assuming 'scan_complete' is in your main hmiDB and mapped in self.tags
        self.writeTag("beamline.massScan.feedback.resultsReady", False)

    def shutdown(self):
        """
        Cleanly closes connections and saves data before exit.
        """
        #print("System shutting down")
        if hasattr(self, 'logger'):
            try:
                ts, snap = self.history.get_snapshot()
                self.logger.log_high_speed_burst(ts, snap)
                self.logger.close()
            except Exception as e:
                print(f"Error - {e}")
                pass

        # 3. Disconnect PLC (Optional but good practice)
        if self.client.get_connected():
            self.client.disconnect()

        #print("✅ Shutdown Complete.")

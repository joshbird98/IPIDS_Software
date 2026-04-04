import re
import snap7
from hmi_config import *

from snap7.util import get_bool, get_int, get_dword, get_real, get_string

# ---- type dispatch table ----
TYPE_READERS = {
    "BOOL": lambda data, offset: get_bool(data, int(offset), int(round((offset - int(offset)) * 10))),
    "INT": lambda data, offset: get_int(data, int(offset)),
    "DINT": lambda data, offset: get_int(data, int(offset)),   # snap7 doesn't distinguish
    "DWORD": lambda data, offset: get_dword(data, int(offset)),
    "REAL": lambda data, offset: get_real(data, int(offset)),
    "STRING": lambda data, offset: get_string(data, int(offset)),  # adjust max length
}

class PLC_Interface:
    def __init__(self):

        self.hmi_db_num = HMI_DB_NUM
        self.fault_db_num = FAULT_DB_NUM
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
        self.make_connection()

        # ================= Dictionaries - save having to load strings each time =================
        self.fault_description_dict = {}
        self.messages_description_dict = {}
        self.fillDictionaries()

        # ================= Tags - create tag dictionary, for easy interaction =================
        self.tags = self.createTags()
        self.readTagValues()
        # self.printTags()

    def make_connection(self):
        # Snap7 connection
        self.client = snap7.client.Client()
        self.connected = True
        self.plc_running = True
        self.plc_stopped = False
        try:
            self.client.connect(self.plc_ip, self.rack, self.slot)
            print(f"Successfully connected with PLC, using IP: {self.plc_ip}")
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

    def fillDictionaries(self):
        if not self.connected:
            self.make_connection()
        if self.connected:
            try:
                self.fault_description_dict = self.fillDictionary(self.fault_db_num, FAULT_DB_LENGTH)
                self.messages_description_dict = self.fillDictionary(self.messages_db_num, MESSAGES_DB_LENGTH)

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

    def createTags(self):    # Produce the DB file by exporting the hmi.db file, and then appending the list of tag offset numbers as multiple lines at the end
        """
        Parse a TIA Portal DB export file (the .db file text form).
        Builds a dictionary of tags with offset, type, writable, and a placeholder value.

        Returns:
            tags (dict): {
                "system.ionSource.ioniser.setpointW": {
                    "offset": 278.0,
                    "type": "REAL",
                    "writable": True,
                    "value": None,
                    "widget": None,
                    "minValue": None,
                    "maxValue": None,
                },
                ...
            }
        """
        tags = {}
        tag_order = []  # maintain parse order for mapping offsets later
        stack = []  # track nested struct names
        offsets = []

        with open(HMI_DB_DOCUMENT_ADDRESS, "r", encoding="utf-8") as f:
            lines = f.readlines()

        in_struct = False
        in_offsets = False

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

                # 1. Check for ARRAY OF BOOL
                # Matches: "faultArray { ExternalWritable... } : Array[0..100] of Bool;"
                # We use re.IGNORECASE to handle "Array" or "ARRAY"
                array_match = re.search(r"(\w+)(?:.*):\s*Array\[(\d+)\.\.(\d+)\]\s*of\s*Bool", line, re.IGNORECASE)

                # 2. Check for STANDARD tags (including Struct definitions)
                # Matches: "errorPLC ... : Bool;" or "system : Struct"
                std_match = re.match(r"(\w+)(?:.*):\s*(\w+)", line)

                if array_match:
                    name, start_idx, end_idx = array_match.groups()
                    start_idx, end_idx = int(start_idx), int(end_idx)

                    # Note: We do NOT add the array name itself to 'tags'.
                    # We only add the children, because your offset deduplication
                    # will merge the Array offset and the Element[0] offset.

                    writable = "ExternalWritable := 'False'" not in line

                    # Expand the array into individual tags
                    for i in range(start_idx, end_idx + 1):
                        full_name = ".".join(stack + [f"{name}[{i}]"])

                        tags[full_name] = {
                            "offset": None,  # Will be filled later
                            "type": "BOOL",
                            "writable": writable,
                            "value": None,
                            "widget": None,
                            "minValue": None,
                            "maxValue": None,
                        }
                        tag_order.append(full_name)

                elif std_match:
                    name, dtype = std_match.groups()
                    dtype = dtype.upper()

                    if dtype == "STRUCT":
                        # Push to stack but DO NOT add to tags/tag_order.
                        # Deduplication collapses the Struct offset into its first member's offset.
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
                            "minValue": None,
                            "maxValue": None,
                        }
                        tag_order.append(full_name)

        # --- assign offsets from collected list ---
        offsets = [offsets[0]] + [x for i, x in enumerate(offsets[1:], start=1) if x != offsets[i-1]]

        for i, tag_name in enumerate(tag_order):
            if i < len(offsets):
                tags[tag_name]["offset"] = offsets[i]

        for tag_name in tags:
            if "Min" in tag_name:
                tags[tag_name.replace("Min", "")]["minValue"] = tags[tag_name]["value"]
            if "Max" in tag_name:
                tags[tag_name.replace("Max", "")]["maxValue"] = tags[tag_name]["value"]

        self.hmi_db_size = int(offsets[-1])

        last_tag = list(tags.keys())[-1]
        final_dtype = (tags[last_tag]["type"])

        if final_dtype == "BOOL":
            self.hmi_db_size += 1
        elif final_dtype == "INT":
            self.hmi_db_size += 2
        elif final_dtype == "REAL":
            self.hmi_db_size += 4
        elif final_dtype == "STRING":
            self.hmi_db_size += 256
        elif final_dtype == "DWORD":
            self.hmi_db_size += 4
        else:
            print(f"Read not implemented for {final_dtype}")
            self.hmi_db_size += 0

        #for tag in tags:
            #print(f"{tag}, {tags[tag]['offset']}, {tags[tag]['type']}, {tags[tag]['writable']}, {tags[tag]['value']}, {tags[tag]['widget']}")

        return tags

    def readTagValues(self):
        if not self.connected:
            self.make_connection()
        if self.connected:
            try:
                # --- TIMER START ---
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

                        # 4. Post-Processing: Update Min/Max limits dynamically
                # This allows the PLC to change limits on the fly, and the HMI to adapt
                for tag_name, meta in self.tags.items():
                    val = meta["value"]
                    if val is not None:
                        if tag_name.endswith("Min"):
                            target_tag = tag_name[:-3]  # Remove 'Min'
                            if target_tag in self.tags:
                                self.tags[target_tag]["minValue"] = val
                        elif tag_name.endswith("Max"):
                            target_tag = tag_name[:-3]  # Remove 'Max'
                            if target_tag in self.tags:
                                self.tags[target_tag]["maxValue"] = val
                            # --- TIMER END ---
                            t_end = time.perf_counter()

                            # Calculate durations in milliseconds
                network_ms = (t_network - t_start) * 1000
                parse_ms = (t_end - t_network) * 1000
                total_ms = (t_end - t_start) * 1000

                print(f"Total: {total_ms:.2f}ms | Network: {network_ms:.2f}ms | Parse: {parse_ms:.2f}ms")
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

    def printTags(self):
        for tag in self.tags:
            print(f"{tag}: {self.tags[tag]['value']}")

    def writeTag(self, tag, new_value):

        print(f"Writing {new_value} to {tag}")
        self.running = True
        self.stopped = False

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
                if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                    print(f"Requested setpoint value {new_value} is out of range.")
                    return False
                data = bytearray(2)
                snap7.util.set_int(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "REAL":
                new_value = float(new_value)
                if (new_value < meta["minValue"]) or (new_value > meta["maxValue"]):
                    print(f"Requested setpoint value {new_value} is out of range.")
                    return False
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

plc_interface = PLC_Interface()
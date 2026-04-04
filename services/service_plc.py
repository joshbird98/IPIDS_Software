import time
import zmq
import json
import snap7
import re

from network_config import ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA
from hmi_config import PLC_IP_ADDRESS, PLC_RACK, PLC_SLOT, HMI_DB_NUM, HMI_DB_DOCUMENT_ADDRESS
from utils.snap7_types import TYPE_READERS

class PLCMicroservice:
    def __init__(self):
        # 1. Initialize ZMQ Context & Sockets
        self.context = zmq.Context()

        # PUB Socket for broadcasting state
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(ZMQ_PORT_PLC_PUB)

        # PULL Socket for receiving write commands
        self.cmd_socket = self.context.socket(zmq.PULL)
        self.cmd_socket.bind(ZMQ_PORT_PLC_CMD)

        # Configure Poller to check for incoming commands without blocking
        self.poller = zmq.Poller()
        self.poller.register(self.cmd_socket, zmq.POLLIN)

        # 2. PLC State
        self.client = snap7.client.Client()
        self.connected = False
        self.hmi_db_num = HMI_DB_NUM
        self.hmi_db_size = 0
        self.tags = {}

        # 3. Initialization
        self._load_tags()
        self._connect_plc()

    def _connect_plc(self):
        try:
            self.client.connect(PLC_IP_ADDRESS, PLC_RACK, PLC_SLOT)
            self.connected = True
            print(f"[PLC Service] Connected to {PLC_IP_ADDRESS}")
        except Exception as e:
            self.connected = False
            print(f"[PLC Service] Connection failed: {e}")

    def _load_tags(self):
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

    def read_and_publish(self):
        """Perform 30ms blocking read and ZMQ publish."""
        if not self.connected:
            self._connect_plc()
            if not self.connected:
                return

        try:
            # 1. Network Read
            data = self.client.db_read(self.hmi_db_num, 0, self.hmi_db_size)

            snapshot = {}
            # 2. Parse Memory Block
            for tag_name, meta in self.tags.items():
                offset = meta.get("offset")
                dtype = meta.get("type", "").upper()

                if offset is not None and dtype in TYPE_READERS:
                    val = TYPE_READERS[dtype](data, offset)
                    meta["value"] = val
                    snapshot[tag_name] = val

            # 3. ZMQ Publish (Payload must be bytes)
            payload = json.dumps(snapshot).encode('utf-8')
            self.pub_socket.send_multipart([TOPIC_PLC_DATA, payload])

        except Exception as e:
            print(f"[PLC Service] Read/Publish Error: {e}")
            self.connected = False

    def process_commands(self):
        """Check ZMQ PULL socket for incoming write requests."""
        try:
            # Poll with 0 timeout (non-blocking)
            socks = dict(self.poller.poll(0))

            if self.cmd_socket in socks and socks[self.cmd_socket] == zmq.POLLIN:
                message = self.cmd_socket.recv_json()
                tag = message.get("tag")
                value = message.get("value")

                if tag and value is not None:
                    self._write_tag(tag, value)

        except Exception as e:
            print(f"[PLC Service] Command Error: {e}")

    def _write_tag(self, tag, new_value):
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
                data = bytearray(2)
                snap7.util.set_int(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "UINT":
                new_value = int(new_value)
                data = bytearray(2)
                snap7.util.set_uint(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "DINT":
                new_value = int(new_value)
                data = bytearray(4)
                snap7.util.set_dint(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "UDINT":
                new_value = int(new_value)
                data = bytearray(4)
                snap7.util.set_udint(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "TIME":
                new_value = int(new_value)
                data = bytearray(4)
                snap7.util.set_udint(data, 0, new_value)
                self.client.db_write(self.hmi_db_num, byte_offset, data)
                return True

            elif dtype == "REAL":
                new_value = float(new_value)
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
            print(f"[PLC Service] Warning - Failed to write tag '{tag}', due to incorrect data type: {e}")
            return False
        except Exception as e:
            print(f"[PLC Service] Warning - Failed to write tag '{tag}', due to: {e}")
            self.connected = False  # Changed from legacy flags to simply flagging disconnection
            return False

    def run(self):
        """Main 10Hz loop."""
        print("[PLC Service] Daemon started. Running at 10Hz...")
        while True:
            cycle_start = time.perf_counter()

            self.process_commands()
            self.read_and_publish()

            # Maintain 10Hz (100ms cycle target)
            elapsed = time.perf_counter() - cycle_start
            sleep_time = max(0, 0.1 - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    service = PLCMicroservice()
    service.run()
class FaultManager:
    def __init__(self, plc_interface, fault_array="system.general.faultArray"):
        self.plc = plc_interface
        self.fault_array = fault_array
        self.array_size = 100

        # Internal State
        self.previous_fault_indices = set()  # Use a set for faster lookups
        self.current_fault_indices = set()

        # Public State (The GUI reads this)
        self.active_messages = []
        self.fault_count = 0

    def check_new_faults(self):
        """
        Scans for faults.
        1. Updates self.active_messages (for the GUI).
        2. Returns a list of ONLY NEW faults (for the Logger).
        """
        self.current_fault_indices = set()
        current_messages = []

        # 1. Scan the Tags
        for i in range(self.array_size):
            tag_name = f"{self.fault_array}[{i}]"

            # Read from PLC cache
            val = self.plc.tags.get(tag_name, {}).get('value')

            if val is True:
                self.current_fault_indices.add(i)
                # Look up text description
                msg = self.plc.messages_description_dict.get(i + 100, f"Unknown Fault {i}")
                current_messages.append(msg)

        # 2. Update Public State (GUI sees this immediately)
        self.active_messages = current_messages
        self.fault_count = len(self.active_messages)

        # 3. Detect Rising Edge (New Faults Only)
        # Find indices present now that weren't there last time
        new_indices = self.current_fault_indices - self.previous_fault_indices

        # Update history for next loop
        self.previous_fault_indices = self.current_fault_indices

        # 4. Return new triggers for the Logger
        if new_indices:
            new_fault_list = []
            for i in new_indices:
                new_fault_list.append(self.plc.messages_description_dict.get(i + 100, f"Unknown Fault {i}"))
            return new_fault_list  # LIST NOT EMPTY -> Trigger Logger

        return None  # No NEW faults (even if faults are currently active)
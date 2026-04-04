import datetime


class MessageManager:
    def __init__(self, plc_interface, buffer_tag_base="system.general.messageBuffer",
                 index_tag="system.general.bufferIndex", bufferSize=30):
        self.plc = plc_interface
        self.buffer_base = buffer_tag_base
        self.index_tag = index_tag
        self.bufferSize = bufferSize

        # Public History (The GUI reads this)
        # format: [{"time": "10:00:01", "msg": "Pump Started"}, ...]
        self.message_history = []
        self.max_history = 500  # Keep last 500 messages in RAM

        # Initialize local pointer (sync with PLC so we don't replay old stuff)
        initial_idx = self.plc.tags.get(self.index_tag, {}).get('value')
        self.local_read_index = initial_idx if initial_idx is not None else 0

    def update(self):
        """
        Checks for new messages, appends them to history, and returns new ones.
        """
        new_items = []
        current_time_str = datetime.datetime.now().strftime("%H:%M:%S")

        # 1. FETCH GUI MESSAGES (From your manual buttons)
        if self.plc.gui_messages:
            for msg in self.plc.gui_messages:
                new_items.append({"time": current_time_str, "msg": msg})
            self.plc.gui_messages = []  # Clear queue

        # 2. FETCH PLC BUFFER MESSAGES
        plc_write_index = self.plc.tags.get(self.index_tag, {}).get('value')

        if plc_write_index is not None:
            # Catch up: Loop until our Read Index matches the PLC Write Index
            while self.local_read_index != plc_write_index:

                # Read the specific message ID at the current pointer
                tag_name = f"{self.buffer_base}[{self.local_read_index}]"
                msg_id = self.plc.tags.get(tag_name, {}).get('value')

                if msg_id is not None and msg_id in self.plc.messages_description_dict:
                    text = self.plc.messages_description_dict[msg_id]
                    # Only add if it's not "0" (No Message) if your PLC uses 0 as empty
                    if msg_id != 0:
                        new_items.append({"time": current_time_str, "msg": text})

                # Move pointer forward & Wrap around
                self.local_read_index += 1
                if self.local_read_index >= self.bufferSize:
                    self.local_read_index = 0

        # 3. UPDATE HISTORY (Append new stuff)
        if new_items:
            self.message_history.extend(new_items)

            # Trim old history if it gets too big
            if len(self.message_history) > self.max_history:
                self.message_history = self.message_history[-self.max_history:]

            return new_items  # Return specific new ones (useful for file logging)

        return []
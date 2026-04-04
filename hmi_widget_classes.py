from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
import time
import pyqtgraph as pg
from PyQt5.QtGui import QIntValidator, QDoubleValidator


# --- The Binder Base ---
# --- The Universal Base ---
class WidgetBinder:
    """Base class for all Binders. Handles the fundamental link between PLC and Widget."""

    def __init__(self, existing_widget, plc, enable_tag=None):
        self.widget = existing_widget
        self.plc = plc
        self.enable_tag = enable_tag

    def update_from_plc(self):
        """
        Main update method called by the cyclic loop.
        Subclasses should override this but call super().update_from_plc() first.
        """
        self._update_enable_state()

    def _update_enable_state(self):
        """Standard logic to disable a widget if its enable_tag is False."""
        if self.enable_tag:
            is_enabled = self.plc.tags[f"{self.enable_tag}"]['value']

            # Convert None (tag not found) to False for safety, or check explicit True
            should_be_enabled = bool(is_enabled)

            # Only hit the UI thread if state actually changed
            if self.widget.isEnabled() != should_be_enabled:
                self.widget.setEnabled(should_be_enabled)

            # Optional: You could add styling for disabled state here if desired
            # e.g. self.widget.setStyleSheet("opacity: 0.5") if not should_be_enabled


# --- 1. Readback Label Binder ---
class ReadbackLabelBinder(WidgetBinder):
    def __init__(self, existing_label, plc, tag_name, unit="", format_str="{:.2f}", value_map=None):
        """
        value_map: Optional dictionary {0: "Off", 1: "On", ...}
        If provided, this overrides the numeric formatting.
        """
        super().__init__(existing_label, plc)
        self.tag_name = tag_name
        self.unit = unit
        self.format_str = format_str
        self.value_map = value_map

    def update_from_plc(self):
        super().update_from_plc()  # Check enable state (rarely needed for labels but safe)

        val = self.plc.tags[f"{self.tag_name}"]['value']
        if val is not None:
            # 1. Check if we have a Map match (Status Mode)
            if self.value_map and val in self.value_map:
                # Use the mapped string exactly (ignore unit)
                text = self.value_map[val]

            # 2. Standard Numeric Formatting
            else:
                # Detect type to prevent crashing on strings/bools
                if isinstance(val, (int, float)):
                    text = f"{self.format_str.format(val)} {self.unit}"
                else:
                    # Fallback for strings or unexpected types
                    text = f"{val} {self.unit}"

            # 3. Update UI only if changed (prevents flickering)
            if self.widget.text() != text:
                self.widget.setText(text)
        else:
            self.widget.setText("---")


# --- 2. Status Indicator Binder (Images) ---
class StatusIndicatorBinder(WidgetBinder):
    def __init__(self, existing_label, plc, tag_name, image_map,
                 prefix=":/icons/images/", suffix=".png"):
        """
        image_map: { True: "green_led", False: "red_led" }
        prefix:    Folder path (default: ":/icons/images/")
        suffix:    File extension (default: ".png")
        """
        super().__init__(existing_label, plc)
        self.tag_name = tag_name
        self.widget.setScaledContents(True)


        # Convert the dictionary of {value: "path"} -> {value: QPixmap object}
        self.pixmap_cache = {}

        for val, filename in image_map.items():
            # Construct the full path automatically
            full_path = f"{prefix}{filename}{suffix}"

            pix = QPixmap(full_path)

            # Check if load was successful to avoid invisible errors later
            if not pix.isNull():
                self.pixmap_cache[val] = pix
            else:
                print(f"Warning: Could not load image for {self.tag_name} at '{full_path}'")
                # We don't add it to the cache, so it will fall back to text later

    def update_from_plc(self):
        super().update_from_plc()

        val = self.plc.tags[f"{self.tag_name}"]['value']

        # Check if we have a preloaded image for this value
        if val in self.pixmap_cache:
            # Extremely fast: just swapping pointers in memory
            self.widget.setPixmap(self.pixmap_cache[val])
        else:
            # Fallback: If value is unexpected (e.g. 99) or image failed to load
            self.widget.clear()  # Clear any old image
            self.widget.setText(str(val))  # Show the raw number/text


# --- 3. Number Entry Binder (With Clamping, Ints & Positive Only) ---
class NumberEntryBinder(WidgetBinder):
    def __init__(self, existing_line_edit, plc, write_tag, read_tag=None, enable_tag=None,
                 min_tag=None, max_tag=None, is_int=False, positive_only=False):  # <--- New Flag

        super().__init__(existing_line_edit, plc, enable_tag)
        self.write_tag = write_tag
        self.read_tag = read_tag or write_tag
        self.min_tag = min_tag
        self.max_tag = max_tag
        self.is_int = is_int
        self.positive_only = positive_only

        # 1. CONFIGURE VALIDATORS
        # These validators prevent invalid keystrokes (like letters or signs)
        if self.is_int:
            if self.positive_only:
                # Bottom = 0, Top = Max Standard Int. Prevents "-" sign.
                self.widget.setValidator(QIntValidator(0, 2147483647))
            else:
                self.widget.setValidator(QIntValidator())
        else:
            validator = QDoubleValidator()
            if self.positive_only:
                validator.setBottom(0.0)  # Prevents "-" sign for floats too
            self.widget.setValidator(validator)

        self.widget.editingFinished.connect(self.send_value)
        self.widget.setAlignment(Qt.AlignCenter)

    def send_value(self):
        try:
            text = self.widget.text()
            if not text: return

            # 2. PARSE BASED ON TYPE
            if self.is_int:
                # Handle edge case where user pastes "1.0" into an int field
                val = int(float(text))
            else:
                val = float(text)

            # 3. SET LIMITS
            # Start with Infinite limits
            min_limit = float('-inf')
            max_limit = float('inf')

            # Apply Tag Limits
            if self.min_tag:
                t = self.plc.tags[f"{self.min_tag}"]['value']
                if t is not None: min_limit = float(t)

            if self.max_tag:
                t = self.plc.tags[f"{self.max_tag}"]['value']
                if t is not None: max_limit = float(t)

            # Apply "Positive Only" Hard Limit
            if self.positive_only:
                min_limit = max(min_limit, 0)

            # 4. CLAMP
            final_val = max(min_limit, min(val, max_limit))

            # 5. CAST FINAL VALUE
            if self.is_int:
                final_val = int(final_val)

            # 6. UPDATE UI & WRITE
            # If we changed the value (clamping) or just want to format it nicely
            fmt = "{}" if self.is_int else "{:.2f}"
            self.widget.setText(fmt.format(final_val))

            self.plc.writeTag(self.write_tag, final_val)
            self.widget.clearFocus()

        except ValueError:
            pass

    def update_from_plc(self):
        super().update_from_plc()
        # Clean formatting on read-back
        if self.read_tag and self.is_int:
            val = self.plc.tags[f"{self.read_tag}"]['value']
            if val is not None and not self.widget.hasFocus():
                self.widget.setText(f"{int(val)}")

# --- 4. Text Entry Binder ---
class TextEntryBinder(WidgetBinder):
    def __init__(self, existing_line_edit, plc, write_tag):
        super().__init__(existing_line_edit, plc)
        self.write_tag = write_tag

        # Hook events
        self.widget.editingFinished.connect(self.send_value)
        self.widget.setAlignment(Qt.AlignCenter)


    def send_value(self):
        try:
            text = self.widget.text()
            if not text: return
            self.widget.setText(text)
            #self.write_tag['value'] = text #possibly a mistake
            self.plc.writeTag(self.write_tag, text)
            self.widget.clearFocus()

        except ValueError:
            pass  # Ignore garbage

    def update_from_plc(self):
        super().update_from_plc()


# --- 5. Basic Button Binder ---
class BaseHMIButtonBinder(WidgetBinder):
    """Handles common button styling and states."""

    def __init__(self, existing_button, plc, enable_tag=None, colors=None):
        super().__init__(existing_button, plc, enable_tag)
        self.colors = colors or {
            "disabled": "#A0A0A0",
            "off": "#E0E0E0",
            "on": "#4CAF50",  # Green
            "text": "black"
        }
        # Base CSS
        self.base_style = f"border-radius: 4px; padding: 5px; color: {self.colors['text']};"

    def update_from_plc(self):
        """
        Check enable state. If disabled, force the disabled style immediately.
        Subclasses call this first to ensure disabled state overrides others.
        """
        super().update_from_plc()

        if not self.widget.isEnabled():
            self.widget.setStyleSheet(f"{self.base_style} background-color: {self.colors['disabled']};")
            return False  # Signal to subclass: We are disabled, don't change color
        return True  # Signal to subclass: We are active, go ahead and color me


# --- 6. Smart Button Binder (PLC Writer) ---
class SmartButtonBinder(BaseHMIButtonBinder):
    def __init__(self, existing_button, plc, write_tag, read_tag=None, enable_tag=None,
                 momentary=False, on_value=True, off_value=False, colors=None,
                 text_on=None, text_off=None, invert=False):  # <--- NEW ARGS

        super().__init__(existing_button, plc, enable_tag, colors)

        self.write_tag = write_tag
        self.read_tag = read_tag
        self.momentary = momentary
        self.invert = invert
        if not self.invert:
            self.on_value = on_value
            self.off_value = off_value
        else:
            self.on_value = off_value
            self.off_value = on_value


        # Store the custom text labels
        self.text_on = text_on
        self.text_off = text_off

        self.widget.clicked.connect(self.perform_write)

    def perform_write(self):
        success = False
        new_val = self.on_value
        if self.momentary:
            # Write True -> False
            success = self.plc.writeTag(self.write_tag, self.on_value)
            time.sleep(0.1)  # Short block is usually acceptable for buttons
            self.plc.writeTag(self.write_tag, self.off_value)
        elif self.read_tag:
            # Toggle based on read value
            # Note: We use the cached value for the toggle logic to be instant
            current = self.plc.tags[f"{self.read_tag}"]['value']
            new_val = self.off_value if current else self.on_value
            success = self.plc.writeTag(self.write_tag, new_val)
        else:
            # Simple set
            success = self.plc.writeTag(self.write_tag, self.on_value)

        if success:
            self.plc.gui_messages.append(f"Successfully set {self.write_tag} to {new_val}")
        else:
            self.plc.gui_messages.append(f"Failed to set {self.write_tag} to {new_val}")

    def update_from_plc(self):
        # 1. Update Base (Enable/Disable checks)
        is_active = super().update_from_plc()
        if not is_active:
            return

        # 2. Update Color AND Text based on Read Tag
        if self.read_tag:
            state = self.plc.tags[f"{self.read_tag}"]['value']

            # A. Handle Color
            bg_color = self.colors['on'] if state else self.colors['off']
            font_weight = "bold" if state else "normal"

            # B. Handle Text (New Logic)
            if state and self.text_on:
                self.widget.setText(self.text_on)
            elif not state and self.text_off:
                self.widget.setText(self.text_off)

            self.widget.setStyleSheet(f"{self.base_style} background-color: {bg_color}; font-weight: {font_weight};")
        else:
            # No feedback tag, just show 'off' or 'default' color
            self.widget.setStyleSheet(f"{self.base_style} background-color: {self.colors['off']};")

# --- 7. Function Button Binder (Python Logic) ---
class FunctionButtonBinder(BaseHMIButtonBinder):
    def __init__(self, existing_button, plc, callback_func, enable_tag=None, colors=None):
        super().__init__(existing_button, plc, enable_tag, colors)
        self.callback = callback_func
        self.widget.clicked.connect(self.run_callback)

        # Set initial default style
        self.widget.setStyleSheet(f"{self.base_style} background-color: {self.colors['off']};")

    def run_callback(self):
        if self.callback:
            self.callback()

    def update_from_plc(self):
        # Just handle enable/disable. No dynamic color changes usually needed for function buttons.
        super().update_from_plc()


from datetime import datetime
from PyQt5.QtGui import QTextCursor


# --- 8. Message Log Binder ---
class MessageLogBinder(WidgetBinder):
    def __init__(self, existing_text_edit, plc, max_lines=50):
        """
        existing_text_edit: Must be a QTextEdit object from your UI.
        """
        super().__init__(existing_text_edit, plc)

        # 1. BORROW CENTRAL MANAGER
        if hasattr(self.plc, 'message_manager'):
            self.manager = self.plc.message_manager
        else:
            print("⚠️ Warning: PLC_Interface missing 'message_manager'.")
            self.manager = None

        self.max_lines = max_lines
        self.last_history_len = 0  # To track changes

        # Widget Setup
        self.widget.setReadOnly(True)
        self.widget.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E; 
                font-family: Consolas, Monaco, monospace;
                font-size: 10pt;
                color: #B0B0B0; 
            }
        """)

    def update_from_plc(self):
        if not self.manager:
            return

        # 1. GET HISTORY (The shared list in memory)
        full_history = self.manager.message_history

        # 2. CHECK FOR CHANGES
        # Simple check: has the length changed?
        # (Since we only append, length change = new message)
        if len(full_history) == self.last_history_len:
            return

        # Update our tracker
        self.last_history_len = len(full_history)

        # 3. PREPARE DISPLAY DATA
        # Slice to show only the last N messages
        display_items = full_history[-self.max_lines:]

        # 4. REDRAW
        # It is cleaner to rebuild the text block than to append partials
        # when syncing with a central list.
        self.widget.clear()

        for item in display_items:
            # item is {"time": "HH:MM:SS", "msg": "Pump Started"}
            timestamp = item['time']
            msg_text = item['msg']

            # A. Determine Color based on Prefix
            if msg_text.startswith("FAULT:"):
                color = "#FF5555"  # Bright Red
            elif msg_text.startswith("WARN:"):
                color = "#4FC3F7"  # Light Blue
            elif msg_text.startswith("INFO:"):
                color = "#FFFFFF"  # White
            else:
                color = "#CCCCCC"  # Light Grey (Fallback)

            # B. Format with HTML
            html_line = f'<span style="color:{color};">[{timestamp}] {msg_text}</span>'

            # C. Append
            self.widget.append(html_line)

        # 5. Scroll to bottom
        self.widget.moveCursor(QTextCursor.End)


# --- 9. Fault Display Binder ---
class FaultDisplayBinder(WidgetBinder):
    def __init__(self, text_edit_widget, plc):
        super().__init__(text_edit_widget, plc)

        # 1. BORROW existing manager (Do not create new ones)
        # We assume 'plc' is your PLC_Interface which now owns 'self.fault_manager'
        if hasattr(self.plc, 'fault_manager'):
            self.manager = self.plc.fault_manager
        else:
            print("⚠️ Warning: PLC_Interface missing 'fault_manager'. Check init.")
            self.manager = None

        # State tracking to prevent flickering
        self.last_fault_signature = None

        # Widget Setup
        self.widget.setReadOnly(True)

        # --- STYLE SETUP ---
        # Dark background, Monospace font
        self.widget.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E; 
                font-family: Consolas, Monaco, monospace;
                font-size: 10pt;
                color: #B0B0B0;
            }
        """)

    def update_from_plc(self):
        if not self.manager:
            return

        # 1. READ STATE (Don't run logic, just read the public list)
        current_faults = self.manager.active_messages

        # 2. CHECK IF CHANGED
        # We create a simple string signature or tuple to compare states
        # (Using a tuple is safer than a list for comparison)
        current_signature = tuple(current_faults)

        if current_signature != self.last_fault_signature:

            # 3. REDRAW ONLY ON CHANGE
            self.widget.clear()

            if not current_faults:
                # SYSTEM HEALTHY
                # Green text indicating all good
                html_line = '<span style="color:#55AA55;">✅ System Healthy - No Active Faults</span>'
                self.widget.append(html_line)
            else:
                # FAULTS ACTIVE
                # Loop through and print in Red
                for fault in current_faults:
                    color = "#FF5555"  # Bright Red
                    # Add a bullet point or warning icon for clarity
                    html_line = f'<span style="color:{color};">⚠️ {fault}</span>'
                    self.widget.append(html_line)

            self.widget.ensureCursorVisible()
            self.last_fault_signature = current_signature


class TimeAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.axis_offset = 0.0  # <--- NEW: Stores the large number

    def set_offset(self, offset):
        """Sets the timestamp offset (e.g. 1.7 Billion) to add back to labels."""
        self.axis_offset = offset

    def tickStrings(self, values, scale, spacing):
        """
        PyQtGraph gives us 'values' like 0, 1, 2.
        We add 'axis_offset' to get the real Unix Timestamp, then format it.
        """
        strings = []
        for v in values:
            # Reconstruct the absolute time
            ts = v + self.axis_offset

            try:
                # Format logic (Show HH:MM:SS if zoomed in, Date if zoomed out)
                dt = datetime.fromtimestamp(ts)
                if scale < 300:  # Less than 5 mins
                    strings.append(dt.strftime("%H:%M:%S"))
                elif scale < 86400:  # Less than 1 day
                    strings.append(dt.strftime("%H:%M"))
                else:
                    strings.append(dt.strftime("%Y-%m-%d"))
            except:
                strings.append("")
        return strings

class ScientificAxisItem(pg.AxisItem):
    """
    Forces scientific notation (1.2e-6) for Log scales.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            if v == 0:
                strings.append("0")
            elif abs(v) < 0.001 or abs(v) >= 10000:
                strings.append(f"{v:.1e}") # Scientific
            else:
                strings.append(f"{v:.4g}") # Standard
        return strings
import time
import csv
import datetime
from mass_scan_window import MassScanWindow  # Import the view above
import os
import pyqtgraph.exporters

class MassScanController:
    def __init__(self, plc_interface):
        self.plc = plc_interface

        self.default_dir = os.path.join(os.getcwd(), "MassScanData")
        if not os.path.exists(self.default_dir):
            os.makedirs(self.default_dir)

        # Instantiate the separate window (initially hidden)
        self.window = MassScanWindow()

        # Local Data Storage
        self.scan_x = []
        self.scan_y = []

        # State tracking
        self.is_scanning = False
        self.is_complete = False
        self.lastKnownStep = 0

        # Connect signals/slots if you have buttons in the window
        # e.g., self.window.save_btn.clicked.connect(self.save_data)

    def show_window(self):
        """Helper to open the window from the main menu"""
        self.window.show()
        self.window.raise_()

    def process_logic(self):
        """
        Call this function periodically (e.g. from Main GUI timer).
        It handles the state machine.
        """
        # 1. READ FLAGS
        # Assuming you have added these tags to your main tag list
        scan_status = self.plc.tags['system.beamline.massScan.feedback.status']['value']
        scan_active = (scan_status == 1) or (scan_status == 2)
        scan_complete = self.plc.tags['system.beamline.massScan.feedback.resultsReady']['value']

        # --- STATE: STARTING ---
        if scan_active and not self.is_scanning:
            self.is_complete = False
            self.start_new_scan()

        # --- STATE: RUNNING (Live Preview) ---
        if self.is_scanning and scan_active:
            self.update_live_preview()

        # --- STATE: FINISHING (Bulk Download) ---
        if self.is_scanning and scan_complete:
            self.finish_scan()

        # Update Status Label
        status = "RUNNING..." if self.is_scanning else "IDLE"
        if scan_complete: status = "DOWNLOAD COMPLETE"
        self.window.status_label.setText(f"Status: {status}")

    def start_new_scan(self):
        if self.is_complete:
            return

        self.plc.reset_scan_flag()
        self.is_scanning = True
        self.scan_x = []
        self.scan_y = []
        self.window.clear_plot()

        # Auto-open the window when scan starts
        if not self.window.isVisible():
            self.show_window()
        self.window.raise_()

    def update_live_preview(self):
        currentStep = self.plc.tags['system.beamline.massScan.feedback.currentStep']['value']
        if currentStep == self.lastKnownStep:
            return False
        else:
            self.lastKnownStep = currentStep


            """Reads the single 'live' tag for rough feedback"""
            # These tags must exist in your main hmiDB
            live_mag = self.plc.tags['system.beamline.magnet.readbackA']['value']
            live_curr = self.plc.tags['system.beamline.drop_in_cup.readbackA']['value']

            if live_mag > 0:
                self.scan_x.append(live_mag)
                self.scan_y.append(live_curr)

                # Filter out zeros, and sort if necessary
                if len(self.scan_x) > 1:
                    # Zip pairs: [(mag1, curr1), (mag2, curr2)...]
                    combined = zip(self.scan_x, self.scan_y)

                    # Sort based on X (Magnet Current)
                    sorted_pairs = sorted(combined, key=lambda pair: pair[0])

                    # Unzip back into separate lists ('*' operator unpacks)
                    sorted_x, sorted_y = zip(*sorted_pairs)

                    # Plot the sorted versions
                    self.window.curve.setData(sorted_x, sorted_y)
                else:
                    # Not enough points to sort yet, just plot raw
                    self.window.curve.setData(self.scan_x, self.scan_y)
                return True
            return False

    def finish_scan(self):
        """Trigger the bulk download and save"""
        print("Scan Complete flag detected. Downloading full dataset...")
        self.plc.gui_messages.append(f"Mass Scan completed - downloading full dataset...")

        # 1. BULK READ (This calls the function we wrote previously)

        num_points = self.plc.tags['system.beamline.massScan.feedback.currentStep']['value']
        full_data = self.plc.read_mass_scan_data(arrayLen=num_points)

        if full_data:
            # 2. UPDATE PLOT WITH HIGH-RES DATA
            # Unzip list of tuples: [(x,y), (x,y)] -> [x,x], [y,y]
            raw_x, raw_y = zip(*full_data)

            # Simple filter to remove empty trailing zeros from the struct array
            clean_x, clean_y = [], []
            for x, y in zip(raw_x, raw_y):
                if y != 0.0:  # Assuming beam current is never exactly 0.0 during a scan
                    clean_x.append(x)
                    clean_y.append(y)

            self.window.curve.setData(clean_x, clean_y)

            # 3. SAVE TO FILE
            self.auto_save_data(clean_x, clean_y)

            # 4. RESET PLC
            self.plc.reset_scan_flag()

        self.is_complete = True
        self.is_scanning = False

    def auto_save_data(self, x_data, y_data):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = os.path.join(self.default_dir, f"mass_scan_{timestamp}")

        # --- 1. SAVE CSV ---
        csv_name = f"{base_filename}.csv"
        try:
            with open(csv_name, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Magnet_Current_A", "Beam_Current_A"])
                for x, y in zip(x_data, y_data):
                    writer.writerow([x, y])
            self.plc.gui_messages.append(f"Saved mass scan data to {csv_name}")
        except Exception as e:
            self.plc.gui_messages.append(f"Failed to save mass scan data")

        # --- 2. SAVE PNG (Screenshot) ---
        png_name = f"{base_filename}.png"
        try:
            plot_item = self.window.plot_widget.plotItem
            exporter = pyqtgraph.exporters.ImageExporter(plot_item)
            exporter.parameters()['width'] = 1920
            exporter.export(png_name)
            self.plc.gui_messages.append(f"Saved mass scan image to {png_name}")

        except Exception as e:
            self.plc.gui_messages.append(f"Failed to save mass scan image")

        # --- 3. SAVE SVG (Vector) ---
        svg_name = f"{base_filename}.svg"
        try:
            plot_item = self.window.plot_widget.plotItem
            exporter = pyqtgraph.exporters.SVGExporter(plot_item)
            exporter.export(svg_name)
            self.plc.gui_messages.append(f"Saved mass scan vector image to {svg_name}")
        except Exception as e:
            self.plc.gui_messages.append(f"Failed to save mass scan vector image")


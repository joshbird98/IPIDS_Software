from PyQt5.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QLabel
import pyqtgraph as pg


class MassScanWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mass Analysis Scan")
        self.resize(800, 600)

        # Central Widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 1. Status Label
        self.status_label = QLabel("Status: IDLE")
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #555;")
        layout.addWidget(self.status_label)

        # 2. The Plot
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setLabel('left', 'Current', units='A')
        self.plot_widget.setLabel('bottom', 'Magnet Current', units='A')

        # Create a pen for the curve (Blue line)
        self.curve = self.plot_widget.plot(pen=pg.mkPen(color='b', width=2))

        layout.addWidget(self.plot_widget)

    def clear_plot(self):
        """Forces the graph to wipe all data immediately"""
        # 1. Clear the curve data
        self.curve.setData([], [])

        # 2. Force an update (sometimes needed if the GUI is busy)
        self.plot_widget.update()
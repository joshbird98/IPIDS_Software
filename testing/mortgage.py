import sys
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout,
                             QHBoxLayout, QWidget, QSlider, QLabel,
                             QTabWidget, QGroupBox, QFormLayout)
from PyQt6.QtCore import Qt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


class FinancialSimulator(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("30-Year Wealth & Cashflow Forecaster (Fixed Equity)")
        self.setGeometry(100, 100, 1300, 800)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        sidebar = QVBoxLayout()
        self.sliders = {}

        # Format: key: (Label text, min, max, default, scaling factor)
        controls = {
            'growth': ('Property Growth (%)', -50, 100, 30, 10),
            'rate': ('Mortgage Rate (%)', 10, 100, 45, 10),
            'overpay': ('Monthly Overpay (£)', 0, 3000, 0, 1),
            'nursery': ('Nursery Cost (£/mo, Yrs 3-6)', 0, 3000, 1300, 1),
            'living': ('Living Exp. (£/mo)', 1000, 4000, 1500, 1),
            'sav_rate': ('Savings Interest (%)', 0, 100, 30, 10)
        }

        control_group = QGroupBox("Dynamic Parameters")
        form_layout = QFormLayout()

        for key, (label, min_val, max_val, default, scale) in controls.items():
            lbl = QLabel(f"{label}: {default / scale}")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setMinimum(min_val)
            slider.setMaximum(max_val)
            slider.setValue(default)
            slider.valueChanged.connect(self.update_plots)

            form_layout.addRow(lbl, slider)
            self.sliders[key] = (slider, lbl, scale)

        control_group.setLayout(form_layout)
        sidebar.addWidget(control_group)
        sidebar.addStretch()
        layout.addLayout(sidebar, 1)

        self.tabs = QTabWidget()

        # Initialize UI Tabs
        self.tab_eq = QWidget();
        self.tabs.addTab(self.tab_eq, "Net Equity")
        self.tab_sav = QWidget();
        self.tabs.addTab(self.tab_sav, "Liquid Savings")
        self.tab_cf = QWidget();
        self.tabs.addTab(self.tab_cf, "Monthly Cashflow")
        layout.addWidget(self.tabs, 3)

        # Initialize Matplotlib Canvases
        self.fig_eq = Figure();
        self.canvas_eq = FigureCanvas(self.fig_eq);
        self.ax_eq = self.fig_eq.add_subplot(111)
        QVBoxLayout(self.tab_eq).addWidget(self.canvas_eq)

        self.fig_sav = Figure();
        self.canvas_sav = FigureCanvas(self.fig_sav);
        self.ax_sav = self.fig_sav.add_subplot(111)
        QVBoxLayout(self.tab_sav).addWidget(self.canvas_sav)

        self.fig_cf = Figure();
        self.canvas_cf = FigureCanvas(self.fig_cf);
        self.ax_cf = self.fig_cf.add_subplot(111)
        QVBoxLayout(self.tab_cf).addWidget(self.canvas_cf)

        self.update_plots()

    def calc_pmt(self, principal, rate, years):
        if rate == 0: return principal / (years * 12)
        r = rate / 12
        n = years * 12
        return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)

    def calc_sdlt_2026_standard(self, price):
        # 2026 standard rates (reverted post April 2025)
        if price <= 125000:
            return 0
        elif price <= 250000:
            return (price - 125000) * 0.02
        elif price <= 925000:
            return (125000 * 0.02) + (price - 250000) * 0.05
        else:
            return (125000 * 0.02) + (675000 * 0.05) + (price - 925000) * 0.10

    def generate_vectors(self, growth, rate, overpay, nursery, living, sav_rate):
        months = 360
        term_years = 30

        income = np.zeros(months)
        income[:60] = np.linspace(5400, 5700, 60)
        for i in range(60, months):
            income[i] = income[i - 1] * (1 + 0.02 / 12)

        expenses = np.full(months, living, dtype=float)
        expenses[36:84] += nursery

        def simulate_scenario(start_price, target_price, deposit, init_fees, move_month):
            mort = np.zeros(months)
            sav = np.zeros(months)
            prop = np.zeros(months)
            costs = np.zeros(months)

            sav[0] = 330000 - deposit - init_fees
            prop[0] = start_price
            mort[0] = start_price - deposit

            req_pmt = self.calc_pmt(mort[0], rate, term_years)

            for i in range(1, months):
                prop[i] = prop[i - 1] * (1 + growth / 12)

                # Execute moving transaction (BUG FIX APPLIED HERE)
                if move_month and i == move_month:
                    target_cost = target_price * ((1 + growth / 12) ** i)
                    sell_fee = prop[i] * 0.015 + 2000
                    sdlt = self.calc_sdlt_2026_standard(target_cost)

                    equity = prop[i] - mort[i - 1]
                    rolled_eq = equity - sell_fee - sdlt

                    prop[i] = target_cost  # Fixed index from [i-1] to [i]
                    mort[i - 1] = target_cost - rolled_eq
                    req_pmt = self.calc_pmt(mort[i - 1], rate, (months - i) / 12)

                interest = mort[i - 1] * (rate / 12)

                if mort[i - 1] > 0:
                    actual_pmt = min(mort[i - 1] + interest, req_pmt + overpay)
                    mort[i] = mort[i - 1] + interest - actual_pmt
                else:
                    actual_pmt = 0
                    mort[i] = 0

                costs[i] = actual_pmt + expenses[i]

                surplus = income[i] - costs[i]
                sav_interest = sav[i - 1] * (sav_rate / 12) if sav[i - 1] > 0 else 0
                sav[i] = sav[i - 1] + sav_interest + surplus

            costs[0] = req_pmt + expenses[0]
            return prop, mort, sav, costs

        # Option C: Straight to £575k (Over 500k = NO First Time Buyer Relief. SDLT = £20k + £2k legal)
        c_p, c_m, c_s, c_c = simulate_scenario(575000, 575000, 270000, 20000, None)

        # Option A: Buy £470k, move Y7 (Valid for FTB Relief. SDLT = £8.5k + £2k legal)
        a_p, a_m, a_s, a_c = simulate_scenario(470000,575000, 270000, 10500, 84)

        return (c_p, c_m, c_s, c_c), (a_p, a_m, a_s, a_c), income

    def update_plots(self):
        vals = {}
        for key, (slider, lbl, scale) in self.sliders.items():
            val = slider.value() / scale
            if key in ['overpay', 'nursery', 'living']:
                lbl.setText(f"{self.sliders[key][1].text().split(':')[0]}: £{int(val)}")
            else:
                lbl.setText(f"{self.sliders[key][1].text().split(':')[0]}: {val:.1f}%")
            vals[key] = val if key in ['overpay', 'nursery', 'living'] else val / 100

        c_data, a_data, income = self.generate_vectors(
            vals['growth'], vals['rate'], vals['overpay'],
            vals['nursery'], vals['living'], vals['sav_rate']
        )

        time = np.linspace(0, 30, 360)

        self.ax_eq.clear()
        self.ax_eq.plot(time, c_data[0] - c_data[1], label='Option C 575k)', lw=2, color='#1a73e8')
        self.ax_eq.plot(time, a_data[0] - a_data[1], label='Option A (Move Y7)', lw=2, color='#e53935', ls='--')
        self.ax_eq.set_title("Net Property Equity")
        self.ax_eq.set_ylabel("£")
        self.ax_eq.grid(True, alpha=0.3)
        self.ax_eq.legend()
        self.canvas_eq.draw()

        self.ax_sav.clear()
        self.ax_sav.plot(time, c_data[2], label='Option C Cash Reserve', lw=2, color='#1a73e8')
        self.ax_sav.plot(time, a_data[2], label='Option A Cash Reserve', lw=2, color='#e53935', ls='--')
        self.ax_sav.set_title("Total Liquid Savings (Compounded)")
        self.ax_sav.set_ylabel("£")
        self.ax_sav.grid(True, alpha=0.3)
        self.ax_sav.legend()
        self.canvas_sav.draw()

        self.ax_cf.clear()
        self.ax_cf.plot(time, income, label='Net Income Capacity', lw=2, color='green')
        self.ax_cf.plot(time, c_data[3], label='Option C Outgoings', lw=2, color='#1a73e8')
        self.ax_cf.plot(time, a_data[3], label='Option A Outgoings', lw=2, color='#e53935', ls='--')
        self.ax_cf.set_title("Monthly Cash Flow (Income vs Debt & Expenses)")
        self.ax_cf.set_ylabel("£ / month")
        self.ax_cf.grid(True, alpha=0.3)
        self.ax_cf.legend()
        self.canvas_cf.draw()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = FinancialSimulator()
    window.show()
    sys.exit(app.exec())
"""Pick the elements expected in the beam (their compounds are preferred when labelling peaks)."""
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QGridLayout, QPushButton, QVBoxLayout


class PeriodicTableDialog(QDialog):
    def __init__(self, current_selection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Expected Elements")
        self.selected = set(current_selection)
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setSpacing(2)

        pt_layout = [
            (1, "H", 0, 0), (2, "He", 0, 17),
            (3, "Li", 1, 0), (4, "Be", 1, 1), (5, "B", 1, 12), (6, "C", 1, 13), (7, "N", 1, 14), (8, "O", 1, 15),
            (9, "F", 1, 16), (10, "Ne", 1, 17),
            (11, "Na", 2, 0), (12, "Mg", 2, 1), (13, "Al", 2, 12), (14, "Si", 2, 13), (15, "P", 2, 14),
            (16, "S", 2, 15), (17, "Cl", 2, 16), (18, "Ar", 2, 17),
            (19, "K", 3, 0), (20, "Ca", 3, 1), (21, "Sc", 3, 2), (22, "Ti", 3, 3), (23, "V", 3, 4), (24, "Cr", 3, 5),
            (25, "Mn", 3, 6), (26, "Fe", 3, 7), (27, "Co", 3, 8), (28, "Ni", 3, 9), (29, "Cu", 3, 10),
            (30, "Zn", 3, 11), (31, "Ga", 3, 12), (32, "Ge", 3, 13), (33, "As", 3, 14), (34, "Se", 3, 15),
            (35, "Br", 3, 16), (36, "Kr", 3, 17),
            (37, "Rb", 4, 0), (38, "Sr", 4, 1), (39, "Y", 4, 2), (40, "Zr", 4, 3), (41, "Nb", 4, 4), (42, "Mo", 4, 5),
            (43, "Tc", 4, 6), (44, "Ru", 4, 7), (45, "Rh", 4, 8), (46, "Pd", 4, 9), (47, "Ag", 4, 10),
            (48, "Cd", 4, 11), (49, "In", 4, 12), (50, "Sn", 4, 13), (51, "Sb", 4, 14), (52, "Te", 4, 15),
            (53, "I", 4, 16), (54, "Xe", 4, 17),
            (55, "Cs", 5, 0), (56, "Ba", 5, 1), (72, "Hf", 5, 2), (73, "Ta", 5, 3), (74, "W", 5, 4), (75, "Re", 5, 5),
            (76, "Os", 5, 6), (77, "Ir", 5, 7), (78, "Pt", 5, 8), (79, "Au", 5, 9), (80, "Hg", 5, 10),
            (81, "Tl", 5, 11), (82, "Pb", 5, 12), (83, "Bi", 5, 13), (84, "Po", 5, 14), (85, "At", 5, 15),
            (86, "Rn", 5, 16),
            (87, "Fr", 6, 0), (88, "Ra", 6, 1), (104, "Rf", 6, 2), (105, "Db", 6, 3), (106, "Sg", 6, 4),
            (107, "Bh", 6, 5), (108, "Hs", 6, 6), (109, "Mt", 6, 7), (110, "Ds", 6, 8), (111, "Rg", 6, 9),
            (112, "Cn", 6, 10), (113, "Nh", 6, 11), (114, "Fl", 6, 12), (115, "Mc", 6, 13), (116, "Lv", 6, 14),
            (117, "Ts", 6, 15), (118, "Og", 6, 16),
        ]

        lanths = ["La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb"]
        actins = ["Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr"]

        for i, sym in enumerate(lanths):
            pt_layout.append((57 + i, sym, 8, i + 2))
        for i, sym in enumerate(actins):
            pt_layout.append((89 + i, sym, 9, i + 2))

        for z, sym, row, col in pt_layout:
            btn = QPushButton(sym)
            btn.setFixedSize(36, 36)
            btn.setCheckable(True)
            btn.setProperty("role", "toggle")      # (theme: a checked toggle is filled with the accent)
            if sym in self.selected:
                btn.setChecked(True)

            def make_toggle_handler(button, element):
                def handler(checked):
                    if checked:
                        self.selected.add(element)
                    else:
                        self.selected.discard(element)

                return handler

            btn.toggled.connect(make_toggle_handler(btn, sym))
            grid.addWidget(btn, row, col)

        layout.addLayout(grid)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)
        layout.addWidget(bbox)

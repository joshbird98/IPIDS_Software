import json
import os

_config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/theme_config.json'))

_data = {}
try:
    with open(_config_path, 'r') as f:
        _data = json.load(f)
except Exception as e:
    print(f"[Theme] Failed to load theme_config.json. Using fallbacks. Error: {e}")

theme_mode = _data.get("theme_mode", "dark").lower()
is_dark_mode = (theme_mode == "dark")

_active_palette = _data.get("dark_palette" if is_dark_mode else "light_palette", {
    "window_bg": "#121212", "panel_bg": "#1E1E1E", "border": "#555555",
    "text_main": "#E0E0E0", "text_muted": "#888888", "accent": "#2196F3", "input_bg": "#2A2A2A"
})

_styles = _data.get("scada_styles", {})
COLOR_OK = _styles.get("COLOR_OK", "background-color: #4CAF50; color: white; font-weight: bold; padding: 4px;")
COLOR_FAULT = _styles.get("COLOR_FAULT", "background-color: #F44336; color: white; font-weight: bold; padding: 4px;")
COLOR_INACTIVE = _styles.get("COLOR_INACTIVE",
                             "background-color: #555555; color: #E0E0E0; font-weight: bold; padding: 4px;")
COLOR_WARNING = _styles.get("COLOR_WARNING",
                            "background-color: #FF9800; color: black; font-weight: bold; padding: 4px;")
COLOR_BUTTON_STANDARD = _styles.get("COLOR_BUTTON_STANDARD",
                                    "background-color: #383838; color: #E0E0E0; font-weight: bold; padding: 6px; border: 1px solid #555;")


def get_global_stylesheet() -> str:
    p = _active_palette
    return f"""
    QMainWindow, QDialog, QWidget {{
        background-color: {p['window_bg']};
        color: {p['text_main']};
        font-family: 'Segoe UI', Arial, sans-serif;
    }}

    QDockWidget > QWidget, QScrollArea > QWidget > QWidget {{
        background-color: {p['window_bg']};
    }}

    QMenuBar {{
        background-color: {p['panel_bg']};
        color: {p['text_main']};
        border-bottom: 1px solid {p['border']};
        font-size: 13px;
    }}
    QMenuBar::item:selected {{ background-color: {p['border']}; }}

    QMenu {{
        background-color: {p['panel_bg']};
        color: {p['text_main']};
        border: 1px solid {p['border']};
    }}
    QMenu::item:selected {{ background-color: {p['accent']}; color: white; }}

    QDockWidget {{
        titlebar-close-icon: url(""); titlebar-normal-icon: url("");
        color: {p['text_main']}; font-weight: bold;
    }}
    QDockWidget::title {{
        background-color: {p['panel_bg']};
        border: 1px solid {p['border']};
        padding: 6px; text-align: center;
    }}

    QGroupBox {{
        border: 1px solid {p['border']};
        border-radius: 6px; margin-top: 14px;
        background-color: {p['panel_bg']};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; subcontrol-position: top left;
        left: 10px; color: {p['accent']}; font-weight: bold;
    }}

    QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {{
        background-color: {p['input_bg']}; color: {p['text_main']};
        border: 1px solid {p['border']}; border-radius: 4px; padding: 4px; min-height: 20px;
    }}
    QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus, QComboBox:focus {{
        border: 1px solid {p['accent']};
    }}
    QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled {{
        background-color: {p['window_bg']}; color: {p['text_muted']};
    }}

    QTreeWidget, QListWidget {{
        background-color: {p['input_bg']}; color: {p['text_main']};
        border: 1px solid {p['border']}; alternate-background-color: {p['panel_bg']};
    }}
    QTreeWidget::item:selected, QListWidget::item:selected {{
        background-color: {p['accent']}; color: white;
    }}
    QHeaderView::section {{
        background-color: {p['panel_bg']}; color: {p['text_main']};
        border: 1px solid {p['border']}; padding: 4px; font-weight: bold;
    }}

    QScrollBar:vertical {{ background: {p['window_bg']}; width: 14px; margin: 0px; }}
    QScrollBar::handle:vertical {{ background: {p['border']}; min-height: 20px; border-radius: 7px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {p['text_muted']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    
    QTabBar::tab {{
        background-color: {p['window_bg']};
        color: {p['text_muted']};
        padding: 6px 16px;
        border: 1px solid {p['border']};
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background-color: {p['panel_bg']};
        color: {p['text_main']};
        border-top: 2px solid {p['accent']}; /* Modern top-accent line */
    }}
    QTabBar::tab:hover:!selected {{
        background-color: {p['border']};
        color: {p['text_main']};
    }}
    
    /* --- Window Splitters (The boundaries between docks) --- */
    QMainWindow::separator {{
        background-color: {p['border']};
        width: 3px; /* Width of vertical dividers */
        height: 3px; /* Height of horizontal dividers */
    }}
    QMainWindow::separator:hover {{
        background-color: {p['accent']}; /* Highlights blue when the user hovers to resize */
    }}
    """
"""The built-in help: the Markdown pages in docs/, shown in a help window (window.py).

    from src.gui.help import add_help_menu, show_help
    add_help_menu(main_window, main_window.menuBar(), default="viewer/data-viewer.md")
    show_help("panels/vacuum.md")
"""
from src.gui.help.window import add_help_menu, context_help, context_page, show_help  # noqa: F401

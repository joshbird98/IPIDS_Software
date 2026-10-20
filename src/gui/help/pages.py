"""Where the help pages are, the contents list, and which page belongs to which panel.

The contents (the tree on the left of the help window) are read from the "Contents" section of
docs/README.md, so the home page and the help window always agree: a bold item without a link is
a group, a linked item a page, nesting by indentation.
"""
import glob
import os
import re

from src.core.paths import DOCS_DIR

HOME = "README.md"
SAFETY = "safety.md"
VIEWER = "viewer/data-viewer.md"
START_UP = "procedures/beam-start-up.md"

# The page F1 opens for each HMI panel (QDockWidget objectName -> page)
PANEL_PAGES = {
    "VacuumDock": "panels/vacuum.md",
    "HealthDock": "panels/system-health.md",
    "SourceDock": "panels/ion-source.md",
    "OpticsDock": "panels/beamline-optics.md",
    "FaradaySmuDock": "panels/faraday-cup-smu.md",
    "CurrentMonDock": "panels/current-monitoring.md",
    "MassScanDock": "panels/mass-scan.md",
    "OptimiserDock": "panels/optimiser.md",
    "AlarmDock": "panels/alarms.md",
    "RecipeDock": "panels/recipes.md",
    "LogDock": "panels/experiment-logging.md",
    "CommandLogDock": "panels/command-log.md",
    "ServicesDock": "panels/service-manager.md",
    "AutomationBar": "panels/automation-bar.md",
}


def path_of(page: str) -> str:
    """Absolute path of a page given relative to docs/ (forward slashes)."""
    return os.path.normpath(os.path.join(DOCS_DIR, *page.split("/")))


def relative(path: str) -> str:
    """docs/-relative name of a page file, with forward slashes."""
    return os.path.relpath(os.path.normpath(path), DOCS_DIR).replace(os.sep, "/")


def all_pages() -> list:
    """Every page in docs/ (docs/-relative), in a stable order."""
    found = glob.glob(os.path.join(DOCS_DIR, "**", "*.md"), recursive=True)
    return sorted(relative(p) for p in found)


def read(page: str) -> str:
    with open(path_of(page), "r", encoding="utf-8") as f:
        return f.read()


class Entry:
    """One line of the contents: a group (page None) or a page."""

    def __init__(self, title: str, page: str = None):
        self.title, self.page, self.children = title, page, []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def __repr__(self):
        return f"Entry({self.title!r}, {self.page!r}, {len(self.children)} children)"


_ITEM = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<body>.+?)\s*$")
_LINK = re.compile(r"^\[(?P<title>[^\]]+)\]\((?P<href>[^)\s]+)\)")
_BOLD = re.compile(r"^\*\*(?P<title>.+?)\*\*$")


def contents(home_text: str = None) -> list:
    """The tree of the home page's Contents section: a list of top-level Entries."""
    text = home_text if home_text is not None else read(HOME)
    m = re.search(r"^##\s+Contents\s*$(?P<body>.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    roots, stack = [], []          # stack of (indent, entry)
    for line in m.group("body").splitlines():
        item = _ITEM.match(line)
        if not item:
            continue
        body = item.group("body")
        link, bold = _LINK.match(body), _BOLD.match(body)
        if link:
            entry = Entry(link.group("title").replace("\\&", "&"), link.group("href").split("#")[0])
        elif bold:
            entry = Entry(bold.group("title"))
        else:
            entry = Entry(body)
        indent = len(item.group("indent").expandtabs(4))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(entry)
        stack.append((indent, entry))
    return roots


def contents_pages() -> list:
    """The pages listed in the contents, in order (the home page first)."""
    pages = [HOME]
    for root in contents():
        for entry in root.walk():
            if entry.page and entry.page not in pages:
                pages.append(entry.page)
    return pages


def group_of(page: str):
    """The contents group a page is listed under (for the window's title line), or None."""
    for root in contents():
        for entry in root.walk():
            if entry.page == page:
                return root.title if root.page is None else None
    return None

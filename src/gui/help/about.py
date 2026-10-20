"""Help > About SINDIE: which version of the software this is and where it keeps its files - the
details to quote when reporting a problem."""
import os
import platform
import sys

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from src.core import paths
from src.gui.style import set_role


def git_version(root: str = paths.PROJECT_ROOT):
    """(branch, commit) of the copy of the software that is running, read from its .git folder
    (without running git); None for what can't be found."""
    gitdir = os.path.join(root, ".git")
    try:
        if os.path.isfile(gitdir):                            # a worktree: ".git" names the real folder
            with open(gitdir, "r", encoding="utf-8") as f:
                line = f.read().strip()
            if line.startswith("gitdir:"):
                gitdir = os.path.normpath(os.path.join(root, line[len("gitdir:"):].strip()))
        with open(os.path.join(gitdir, "HEAD"), "r", encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None, None
    if not head.startswith("ref:"):
        return None, head[:10] or None                        # a detached checkout
    ref = head[len("ref:"):].strip()
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    folders = [gitdir]
    try:
        with open(os.path.join(gitdir, "commondir"), "r", encoding="utf-8") as f:
            folders.append(os.path.normpath(os.path.join(gitdir, f.read().strip())))
    except OSError:
        pass
    for folder in folders:
        try:
            with open(os.path.join(folder, *ref.split("/")), "r", encoding="utf-8") as f:
                return branch, f.read().strip()[:10]
        except OSError:
            pass
        try:
            with open(os.path.join(folder, "packed-refs"), "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return branch, parts[0][:10]
        except OSError:
            pass
    return branch, None


def details() -> list:
    """[(label, value)] shown in the About box."""
    branch, commit = git_version()
    version = " ".join(x for x in (f"commit {commit}" if commit else None,
                                   f"(branch {branch})" if branch else None) if x) or "not known"
    return [
        ("Version", version),
        ("Mode", "SIMULATION - fake devices" if paths.simulation_enabled() else "Lab - real devices"),
        ("Python", sys.version.split()[0]),
        ("Qt / PyQt", f"{QT_VERSION_STR} / {PYQT_VERSION_STR}"),
        ("Computer", f"{platform.node()} ({platform.system()} {platform.release()})"),
        ("Software folder", paths.PROJECT_ROOT),
        ("Data folder", paths.DATA_DIR),
        ("Log folder", paths.LOG_DIR),
    ]


def show_about(parent=None):
    dlg = QDialog(parent)
    dlg.setWindowTitle("About SINDIE")
    lay = QVBoxLayout(dlg)
    title = QLabel("SINDIE software")
    set_role(title, "title")
    lay.addWidget(title)
    intro = QLabel("The control software of the SINDIE ion implanter, Surrey Ion Beam Centre: the operator "
                   "HMI, the data viewer and the services behind them.")
    intro.setWordWrap(True)
    lay.addWidget(intro)
    rows = details()
    table = QLabel("<table cellspacing='0' cellpadding='3'>" + "".join(
        f"<tr><td style='padding-right:14px'><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows) + "</table>")
    table.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lay.addWidget(table)
    hint = QLabel("Quote these details when reporting a problem.")
    set_role(hint, "hint")
    lay.addWidget(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy = buttons.addButton("Copy details", QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(f"{k}: {v}" for k, v in rows)))
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    dlg.exec()

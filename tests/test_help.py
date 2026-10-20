"""The built-in help (docs/, src/gui/help/): every page is in the contents and opens, every link,
section link and screenshot points at something that exists, every HMI panel has a page for F1,
the pages render (call-outs, tables, scaled screenshots), search finds things, and a page saves
as PDF."""
import os
import re

import pytest
from PyQt6.QtWidgets import QApplication

from src.gui.help import pages, render

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _prose(text: str) -> str:
    """A page without its code (links and images written as examples don't count)."""
    return _CODE.sub("", _FENCE.sub("", text))


def _anchors(page: str) -> set:
    used, found = set(), set()
    for line in _FENCE.sub("", pages.read(page)).splitlines():
        m = _HEADING.match(line)
        if m:
            found.add(render.github_slug(render._inline_to_plain(m.group(2)), used))
    return found


def _resolve(page: str, href: str) -> str:
    return os.path.normpath(os.path.join(os.path.dirname(pages.path_of(page)), href))


# ------------------------------------------------------------------ the pages themselves
def test_every_page_is_in_the_contents_and_every_entry_exists():
    listed = pages.contents_pages()
    missing = [p for p in listed if not os.path.exists(pages.path_of(p))]
    assert not missing, f"in the contents but not written: {missing}"
    unlisted = [p for p in pages.all_pages() if p not in listed]
    assert not unlisted, f"pages missing from the contents on docs/README.md: {unlisted}"


def test_the_contents_has_its_groups():
    groups = [e.title for e in pages.contents() if e.page is None]
    assert groups[:3] == ["Start here", "Running the implanter", "HMI panels"]
    assert pages.contents()[0].children[0].page == pages.SAFETY          # safety first


def test_every_link_and_section_link_leads_somewhere():
    broken = []
    for page in pages.all_pages():
        for href in _LINK.findall(_prose(pages.read(page))):
            if re.match(r"^[a-z]+:", href):              # web links (https:, mailto:)
                continue
            target, _, anchor = href.partition("#")
            path = _resolve(page, target) if target else pages.path_of(page)
            if not os.path.exists(path):
                broken.append(f"{page}: {href} (no such page)")
            elif anchor and path.endswith(".md") and anchor not in _anchors(pages.relative(path)):
                broken.append(f"{page}: {href} (no such section)")
    assert not broken, "\n".join(broken)


def test_every_screenshot_exists_and_has_a_description():
    problems = []
    for page in pages.all_pages():
        for alt, src in _IMAGE.findall(_prose(pages.read(page))):
            if not os.path.exists(_resolve(page, src)):
                problems.append(f"{page}: {src} missing (python src/utils/make_help_screenshots.py)")
            if len(alt.strip()) < 8:
                problems.append(f"{page}: {src} needs a description of what it shows")
    assert not problems, "\n".join(problems)


def test_nested_lists_are_indented_by_four_spaces():
    """Python-Markdown (the help window) only nests a list indented by four spaces; GitHub takes
    two as well, so a page could look right on GitHub and wrong in the HMI."""
    bad = []
    for page in pages.all_pages():
        for n, line in enumerate(_FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), pages.read(page))
                                 .splitlines(), 1):
            m = re.match(r"^( +)([-*+]|\d+\.)\s", line)
            if m and len(m.group(1)) % 4:
                bad.append(f"{page}:{n}: {line.strip()[:50]}")
    assert not bad, "\n".join(bad)


def test_github_anchor_names():
    used = set()
    assert render.github_slug("Resetting faults", used) == "resetting-faults"
    assert render.github_slug("The safety relay won't switch on", used) == "the-safety-relay-wont-switch-on"
    assert render.github_slug("Faraday Cup & SMU", used) == "faraday-cup--smu"
    assert render.github_slug("Resetting faults", used) == "resetting-faults-1"      # a repeated heading


# ------------------------------------------------------------------ rendering
def test_callouts_tables_and_code_are_drawn(app):
    from src.gui import theme
    t = theme.tokens("light")
    page = render.to_html("# T\n\n> [!WARNING]\n> Mind the gap.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
                          "```\ncode here\n```\n", t, "Arial", "Courier", 10.5)
    assert "[!WARNING]" not in page and "Mind the gap." in page
    assert f'bgcolor="{t["warn"]}"' in page and ">Warning<" in page
    assert 'class="grid"' in page and f'bgcolor="{t["card_alt"]}"' in page          # table and its header
    assert 'class="code"' in page and "code here" in page
    assert '<a name="t">' in page                                                       # Qt's anchors


def test_search_text_and_titles():
    text = ("# Vacuum System\n\nIntro text.\n\n## Gate valve\n\nThe **OPEN** button.\n\n"
            "```\npython run_simulation.py\n# not a heading\n```\n")
    assert render.title_of(text) == "Vacuum System"
    sections = render.sections(text)
    assert len(sections) == 2
    assert sections[0][:2] == ("Vacuum System", "") and "Intro text." in sections[0][2]
    # (commands in code blocks can be searched for; a # in code doesn't start a section)
    assert sections[1] == ("Gate valve", "gate-valve", "The OPEN button. python run_simulation.py # not a heading")


@pytest.fixture
def help_window(app):
    from src.gui.help.window import HelpWindow
    w = HelpWindow()
    w.resize(1100, 800)
    w.show()
    app.processEvents()
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def test_every_page_opens_in_the_help_window(help_window, app):
    w = help_window
    for page in pages.all_pages():
        w.open_page(page)
        app.processEvents()
        assert w.current == page
        shown = w.browser.document().toPlainText()
        assert w.title_of(page) in shown, page
        # call-outs are drawn as boxes: a [!KIND] marker shows only where a page quotes one as code
        quoted = "".join(_FENCE.findall(pages.read(page)))
        for kind in render.CALLOUTS:
            marker = f"[!{kind.upper()}]"
            assert shown.count(marker) == quoted.count(marker), (page, marker)


def test_call_outs_back_to_back_are_two_boxes():
    """Markdown makes one quote of two with only a blank line between them."""
    from src.gui import theme
    page = render.body_html("> [!NOTE]\n> First.\n\n> [!WARNING]\n> Second.\n", theme.tokens("dark"))
    assert "[!" not in page
    assert page.count('class="callout"') == 2 and ">Note<" in page and ">Warning<" in page
    assert page.index("First.") < page.index(">Warning<") < page.index("Second.")
    quote = render.body_html("> Just a quotation.\n", theme.tokens("dark"))      # not a call-out
    assert "<blockquote>" in quote and 'class="callout"' not in quote


def test_without_the_markdown_package_qt_renders_the_pages(help_window, app, monkeypatch):
    """The Markdown package is in requirements.txt; a PC without it still gets the help, plainer."""
    monkeypatch.setattr(render, "markdown", None)
    w = help_window
    w.browser._cache.clear()
    w.open_page("procedures/alarms-and-faults.md")
    app.processEvents()
    shown = w.browser.document().toPlainText()
    assert "Alarms and faults" in shown and "RESET FAULTS" in shown


def test_screenshots_are_scaled_to_the_column(help_window, app):
    w = help_window
    w.resize(700, 700)
    app.processEvents()
    image = w.browser._image(os.path.join(pages.path_of("images/hmi-overview.png")))
    assert image is not None
    shown = image.width() / image.devicePixelRatio()
    assert shown <= w.browser.column_width() < 1536


def _wait(app, seconds):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def _count_loads(monkeypatch):
    """The number of pages loaded into the help window from here on."""
    from src.gui.help import window
    count = [0]
    original = window.HelpBrowser.doSetSource

    def counted(self, *args):
        count[0] += 1
        return original(self, *args)
    monkeypatch.setattr(window.HelpBrowser, "doSetSource", counted)
    return count


def _drawn_width(browser, image: str) -> float:
    """The width the page's screenshot is drawn at: the image the document holds for it."""
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QTextDocument
    url = QUrl.fromLocalFile(pages.path_of(image))
    held = browser.document().resource(QTextDocument.ResourceType.ImageResource.value, url)
    return held.width() / held.devicePixelRatio()


@pytest.mark.parametrize("width", [1000, 1180])
def test_a_page_is_not_loaded_again_while_it_is_read(help_window, app, monkeypatch, width):
    """The scrollbar used to change the text column, and a new column loaded the page again for its
    screenshots, which hid and showed the scrollbar: the page loaded several times a second for
    ever, and the mouse pointer flashed the wait cursor. (1180 wide is the window's first size.)"""
    w = help_window
    w.resize(width, 800)
    w.open_page(pages.SAFETY)
    _wait(app, 0.5)
    loads = _count_loads(monkeypatch)
    _wait(app, 1.0)
    assert loads[0] == 0


def test_a_resize_scales_the_screenshots_where_they_are(help_window, app, monkeypatch):
    w = help_window
    w.resize(1500, 800)
    w.open_page("getting-started/screen-tour.md")
    _wait(app, 0.4)
    wide = _drawn_width(w.browser, "images/hmi-overview.png")
    loads = _count_loads(monkeypatch)
    w.resize(950, 800)
    _wait(app, 0.6)
    narrow = _drawn_width(w.browser, "images/hmi-overview.png")
    assert loads[0] == 0                       # scaled where it is: no wait cursor, same place
    assert narrow < wide
    assert narrow <= w.browser.column_width()


def test_a_screenshot_scaled_where_it_is_does_not_outlive_its_page(help_window, app):
    w = help_window
    w.resize(1500, 800)
    w.open_page("getting-started/screen-tour.md")
    _wait(app, 0.4)
    w.resize(950, 800)                         # scaled for this width where it is
    _wait(app, 0.6)
    w.open_page(pages.SAFETY)
    _wait(app, 0.2)
    w.resize(1300, 800)
    _wait(app, 0.6)
    w.open_page("getting-started/screen-tour.md")
    _wait(app, 0.4)
    assert _drawn_width(w.browser, "images/hmi-overview.png") == pytest.approx(
        w.browser.column_width() - 4, abs=1)  # for the window as it is now, not as it was


def test_links_and_back(help_window, app):
    w = help_window
    w.open_page(pages.HOME)
    app.processEvents()
    w.open_page("procedures/alarms-and-faults.md", "when-an-alarm-sounds")
    app.processEvents()
    assert w.current == "procedures/alarms-and-faults.md"
    assert w.act_back.isEnabled()
    w.act_back.trigger()
    app.processEvents()
    assert w.current == pages.HOME
    assert w.crumb.text() == w.title_of(pages.HOME)


def test_search_finds_pages(help_window, app):
    hits = help_window.search_hits("gate valve")
    assert any(page == "panels/vacuum.md" for page, *_ in hits)
    hits = help_window.search_hits("PRESS PANEL START")
    assert any(page == "procedures/beam-start-up.md" for page, *_ in hits)
    assert help_window.search_hits("zzqqxx") == []


def test_theme_switch_redraws_the_page(help_window, app):
    from src.gui import theme
    w = help_window
    w.open_page(pages.SAFETY)
    app.processEvents()
    try:
        theme.apply_theme(app, "light")
        app.processEvents()
        assert theme.tokens("light")["fault"].lower() in w.browser.toHtml().lower()
    finally:
        theme.apply_theme(app, theme.DEFAULT_MODE)


def test_a_page_saves_as_pdf(tmp_path, app):
    from src.gui.help.window import write_pdf
    out = tmp_path / "safety.pdf"
    write_pdf([pages.SAFETY, "getting-started/screen-tour.md"], str(out))
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 20000          # (the screenshot is in it)


# ------------------------------------------------------------------ in the programs
@pytest.fixture
def hmi(app):
    from src.gui import theme
    theme.apply_theme(app, theme.DEFAULT_MODE)
    from src.gui.control.control_main import ControlMainWindow
    w = ControlMainWindow()
    w.watchdog_timer.stop()
    w.show()
    app.processEvents()
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def test_every_panel_has_a_page_and_f1_finds_it(hmi):
    from src.gui.help.window import context_page
    for dock in hmi._docks:
        page = pages.PANEL_PAGES.get(dock.objectName())
        assert page and os.path.exists(pages.path_of(page)), dock.objectName()
        assert context_page(hmi, widget=dock.widget()) == page
    assert context_page(hmi, widget=hmi.vac_widget) == "panels/vacuum.md"            # anything inside a panel
    assert context_page(hmi, widget=hmi.automation_bar) == "panels/automation-bar.md"
    assert context_page(hmi, widget=hmi.menuBar()) == pages.HOME


def test_the_hmi_help_menu(hmi, monkeypatch):
    texts = [a.text() for a in hmi.help_menu.actions() if a.text()]
    assert texts[0] == "Help contents\tF1" and "About SINDIE" in texts and "Safety rules" in texts
    opened = []
    from src.gui.help import window as help_window
    monkeypatch.setattr(help_window, "show_help", lambda page=None, anchor=None: opened.append(page))
    # Found in review: chosen from the menu, "Help contents" opened the page of whatever panel was
    # under the menu. The menu item opens the contents; F1 (the window's shortcut) looks at the pointer.
    monkeypatch.setattr(help_window, "context_page", lambda window, default=pages.HOME, widget=None:
                        "panels/vacuum.md")
    hmi.help_menu.actions()[0].trigger()
    assert opened == [pages.HOME]
    assert hmi._help_f1.shortcut().toString() == "F1" and hmi._help_f1 in hmi.actions()
    hmi._help_f1.trigger()
    assert opened == [pages.HOME, "panels/vacuum.md"]


def test_about_reads_the_version_without_git(tmp_path):
    from src.gui.help.about import details, git_version
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text("# pack-refs\n0123456789abcdef0123 refs/heads/main\n")
    assert git_version(str(tmp_path)) == ("main", "0123456789")
    (git / "refs" / "heads" / "main").write_text("fedcba9876543210\n")
    assert git_version(str(tmp_path)) == ("main", "fedcba9876")
    assert git_version(str(tmp_path / "nowhere")) == (None, None)
    assert [k for k, _v in details()][:2] == ["Version", "Mode"]

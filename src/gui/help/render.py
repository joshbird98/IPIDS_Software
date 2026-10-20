"""The help pages (docs/*.md) as HTML for the help window, in the current theme.

The pages are written in GitHub's flavour of Markdown so that they read the same here and on
GitHub. They are converted with the Markdown package (pure Python); three additions make them
match GitHub and suit Qt's rich-text renderer, which understands a subset of HTML and CSS:

- headings get GitHub's anchor names, so a link such as `alarms-and-faults.md#resetting-faults`
  works in both places (Qt finds anchors by <a name>, so one is put in each heading);
- GitHub call-outs (`> [!WARNING]` ...) become coloured boxes;
- tables and code blocks get borders, padding and backgrounds as attributes, which Qt honours
  more reliably than CSS.

Without the Markdown package the help window lets Qt render the Markdown itself (plainer:
no call-out boxes, no table borders) - see HelpBrowser.doSetSource.
"""
import html
import re
import xml.etree.ElementTree as etree

try:
    import markdown
    from markdown.extensions import Extension
    from markdown.treeprocessors import Treeprocessor
except ImportError:          # the help still works, rendered by Qt (see the module docstring)
    markdown = None
    Extension = Treeprocessor = object

CALLOUTS = {
    # kind: (title, theme token for the bar and title, token for the background)
    "note": ("Note", "info", "info_bg"),
    "tip": ("Tip", "ok", "ok_bg"),
    "important": ("Important", "seq", "seq_bg"),
    "warning": ("Warning", "warn", "warn_bg"),
    "caution": ("Caution", "fault", "fault_bg"),
}
_CALLOUT_RE = re.compile(r"\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", re.IGNORECASE)
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def available() -> bool:
    return markdown is not None


def github_slug(text: str, used: set) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens;
    a repeated heading gets -1, -2, ..."""
    slug = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    candidate, n = slug, 1
    while candidate in used:
        candidate = f"{slug}-{n}"
        n += 1
    used.add(candidate)
    return candidate


class _Styler(Treeprocessor):
    def __init__(self, md, colours: dict):
        super().__init__(md)
        self.c = colours

    def run(self, root):
        self._headings(root)
        self._callouts(root)
        self._tables(root)
        return root

    def _headings(self, root):
        used = set()
        for el in root.iter():
            if el.tag in _HEADINGS:
                slug = github_slug("".join(el.itertext()), used)
                el.set("id", slug)
                anchor = etree.Element("a", {"name": slug})
                anchor.tail, el.text = el.text, None
                el.insert(0, anchor)

    def _callouts(self, root):
        for parent in list(root.iter()):
            children = list(parent)
            if not any(child.tag == "blockquote" for child in children):
                continue
            rebuilt = []
            for child in children:
                rebuilt.extend(self._split_quote(child) if child.tag == "blockquote" else [child])
            for child in children:
                parent.remove(child)
            parent.extend(rebuilt)

    def _split_quote(self, quote) -> list:
        """A quote as call-out boxes. Markdown merges two quotes with only a blank line between
        them into one, so each paragraph that starts with a [!KIND] marker starts a new box."""
        groups = []                          # [kind or None, [children]]
        for child in list(quote):
            m = _CALLOUT_RE.match(child.text or "") if child.tag == "p" else None
            if m:
                child.text = child.text[m.end():]
                groups.append([m.group(1).lower(), []])
                if child.text.strip() or len(child):
                    groups[-1][1].append(child)
                continue
            if not groups:
                groups.append([None, []])
            groups[-1][1].append(child)
        out = []
        for kind, children in groups:
            if kind is None:                 # an ordinary quotation
                plain = etree.Element("blockquote")
                plain.extend(children)
                out.append(plain)
            else:
                out.append(self._callout(kind, children))
        out = out or [quote]
        out[-1].tail = quote.tail
        return out

    def _callout(self, kind: str, children) -> etree.Element:
        title, accent, background = CALLOUTS[kind]
        table = etree.Element("table", {"width": "100%", "cellspacing": "0", "cellpadding": "0",
                                        "class": "callout"})
        row = etree.SubElement(table, "tr")
        bar = etree.SubElement(row, "td", {"width": "4", "bgcolor": self.c[accent]})
        bar.text = " "
        body = etree.SubElement(row, "td", {"bgcolor": self.c[background], "class": "callout-body"})
        head = etree.SubElement(body, "p", {"class": "callout-title"})
        b = etree.SubElement(head, "span", {"style": f"color:{self.c[accent]}; font-weight:600;"})
        b.text = title
        body.extend(children)
        return table

    def _tables(self, root):
        for table in root.iter("table"):
            if table.get("class") == "callout":
                continue
            table.set("width", "100%")
            table.set("cellspacing", "0")
            table.set("cellpadding", "6")
            table.set("border", "1")
            table.set("class", "grid")
            table.set("style", f"border-collapse: collapse; border-color: {self.c['border']};"
                               f" border-style: solid;")
            for th in table.iter("th"):
                th.set("bgcolor", self.c["card_alt"])
                th.set("align", "left")


class _SindieExtension(Extension):
    def __init__(self, colours: dict):
        super().__init__()
        self.colours = colours

    def extendMarkdown(self, md):
        md.treeprocessors.register(_Styler(md, self.colours), "sindie_styler", 5)


def stylesheet(c: dict, ui_font: str, mono_font: str, base_pt: float) -> str:
    """CSS for a page (the subset Qt's rich text understands)."""
    return f"""
body {{ font-family: '{ui_font}'; font-size: {base_pt:.1f}pt; color: {c['text']}; }}
h1 {{ font-size: {base_pt * 1.9:.1f}pt; font-weight: 600; margin-top: 4px; margin-bottom: 12px; }}
h2 {{ font-size: {base_pt * 1.4:.1f}pt; font-weight: 600; margin-top: 26px; margin-bottom: 8px; }}
h3 {{ font-size: {base_pt * 1.15:.1f}pt; font-weight: 600; margin-top: 18px; margin-bottom: 6px; }}
h4 {{ font-size: {base_pt:.1f}pt; font-weight: 600; margin-top: 14px; margin-bottom: 4px; }}
p {{ margin-top: 0px; margin-bottom: 10px; line-height: 145%; }}
li {{ margin-bottom: 4px; line-height: 140%; }}
ul, ol {{ margin-top: 0px; margin-bottom: 10px; }}
a {{ color: {c['link']}; text-decoration: none; }}
code {{ font-family: '{mono_font}'; color: {c['code_text']}; background-color: {c['code_bg']}; }}
pre {{ font-family: '{mono_font}'; font-size: {base_pt * 0.92:.1f}pt; margin: 0px; color: {c['text']}; }}
table.grid {{ margin-top: 4px; margin-bottom: 14px; }}
table.callout {{ margin-top: 4px; margin-bottom: 14px; }}
table.code {{ margin-top: 2px; margin-bottom: 14px; }}
th {{ font-weight: 600; }}
td.callout-body {{ padding: 10px 14px 4px 14px; }}
p.callout-title {{ margin-bottom: 4px; }}
hr {{ color: {c['border']}; }}
"""


def colours(tokens: dict) -> dict:
    """The page colours, from the theme's tokens."""
    dark = _is_dark(tokens["card"])
    return {
        **tokens,
        "link": tokens["info"] if dark else tokens["accent"],
        "code_bg": tokens["card_alt"] if not dark else tokens["input"],
        "code_text": tokens["text"],
    }


def _is_dark(hex_colour: str) -> bool:
    h = hex_colour.lstrip("#")[:6]
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


_PRE = re.compile(r"<pre>(?:<code[^>]*>)?(.*?)(?:</code>)?</pre>", re.DOTALL)


def body_html(text: str, tokens: dict) -> str:
    """The HTML of one page's content (no <html>/<head>)."""
    c = colours(tokens)
    out = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", _SindieExtension(c)],
                            output_format="html")
    # Code blocks (the fenced-code extension adds them as finished HTML, after the tree stage):
    # a shaded box, which Qt draws only around a table cell
    return _PRE.sub(lambda m: (f'<table width="100%" cellspacing="0" cellpadding="10" bgcolor="{c["code_bg"]}" '
                               f'class="code"><tr><td><pre>{m.group(1).rstrip()}</pre></td></tr></table>'), out)


def document(body: str, tokens: dict, ui_font: str, mono_font: str, base_pt: float = 10.5) -> str:
    """A whole HTML document around page content, with the theme's stylesheet."""
    css = stylesheet(colours(tokens), ui_font, mono_font, base_pt)
    return f"<html><head><style>{css}</style></head><body>{body}</body></html>"


def to_html(text: str, tokens: dict, ui_font: str, mono_font: str, base_pt: float = 10.5) -> str:
    """A whole HTML document for one page."""
    return document(body_html(text, tokens), tokens, ui_font, mono_font, base_pt)


# --- For the contents and the search ------------------------------------------------------------
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def title_of(text: str, fallback: str = "") -> str:
    m = _H1.search(text)
    return _inline_to_plain(m.group(1)) if m else fallback


def _inline_to_plain(s: str) -> str:
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)          # images -> their description
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)           # links -> their text
    s = re.sub(r"(\*\*|\*|`)", "", s)                   # (not _: tag and file names have them)
    return html.unescape(s).strip()


def sections(text: str):
    """[(heading, anchor, plain text)] for a page: the part before the first ## heading is under
    the page title (anchor ''); each ## or ### heading starts a new section."""
    used, out = set(), []
    heading, anchor, lines = None, "", []
    in_code = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", raw)
        if m:
            plain = _inline_to_plain(m.group(2))
            slug = github_slug(plain, used)
            if len(m.group(1)) == 1:
                heading = heading or plain
                continue
            if len(m.group(1)) <= 3:
                out.append((heading, anchor, " ".join(lines)))
                heading, anchor, lines = plain, slug, []
            continue
        line = raw.strip()
        line = re.sub(r"^>\s?", "", line)
        line = _CALLOUT_RE.sub(" ", line)
        line = re.sub(r"^[-*+]\s+|^\d+\.\s+", "", line)
        line = line.replace("|", " ")
        if re.fullmatch(r"[\s:-]*", line):
            continue
        lines.append(_inline_to_plain(line))
    out.append((heading, anchor, " ".join(lines)))
    return [(h or "", a, re.sub(r"\s+", " ", t).strip()) for h, a, t in out]

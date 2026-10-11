"""Build the PDF documentation from the Markdown files in docs/.

Usage:  .venv\\Scripts\\python scripts\\build_docs.py

The Markdown files are the source of truth (they render on GitHub); this script
turns them into docs/Switchboard-Documentation.pdf with a cover page, table of
contents, PDF bookmarks and page numbers. If docs/private/*.md exists, it also
builds docs/private/Interview-Prep.pdf (git-ignored).

Supports the Markdown subset the docs use: # headings, paragraphs, - and 1. lists
(one nesting level), ``` code blocks, | tables |, > quotes, **bold**, *italic*,
`code`, [links](url) and bare https:// URLs.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

PROJECT_NAME = "Switchboard"
SUBTITLE = "An OpenAI-compatible LLM gateway with routing, fallback, caching, and cost tracking"
AUTHOR = "Shreyas Nimbalkar"

ACCENT = colors.HexColor("#1F3A5F")
ACCENT_LIGHT = colors.HexColor("#E8EEF5")
CODE_BG = colors.HexColor("#F4F6F8")
RULE = colors.HexColor("#C9D3DF")
MUTED = colors.HexColor("#5B6573")

PAGE_W, PAGE_H = LETTER
MARGIN = 0.9 * inch
CONTENT_W = PAGE_W - 2 * MARGIN

# ---------------------------------------------------------------- styles

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5, spaceAfter=7)
STYLES = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=30, leading=36,
                            textColor=ACCENT, alignment=TA_CENTER, spaceAfter=14),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=13, leading=18,
                               textColor=MUTED, alignment=TA_CENTER, spaceAfter=6),
    "cover_meta": ParagraphStyle("cover_meta", fontName="Helvetica", fontSize=10.5,
                                 leading=15, textColor=MUTED, alignment=TA_CENTER),
    "h1": ParagraphStyle("h1", keepWithNext=1, fontName="Helvetica-Bold", fontSize=20, leading=25,
                         textColor=ACCENT, spaceBefore=0, spaceAfter=12),
    "h2": ParagraphStyle("h2", keepWithNext=1, fontName="Helvetica-Bold", fontSize=14, leading=18,
                         textColor=ACCENT, spaceBefore=14, spaceAfter=6),
    "h3": ParagraphStyle("h3", keepWithNext=1, fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                         textColor=colors.black, spaceBefore=10, spaceAfter=4),
    "body": BODY,
    "bullet": ParagraphStyle("bullet", parent=BODY, leftIndent=16, bulletIndent=4, spaceAfter=3),
    "bullet2": ParagraphStyle("bullet2", parent=BODY, leftIndent=32, bulletIndent=20, spaceAfter=3),
    "quote": ParagraphStyle("quote", parent=BODY, leftIndent=14, textColor=MUTED,
                            fontName="Helvetica-Oblique", borderPadding=(2, 0, 2, 8),
                            borderColor=RULE, borderWidth=0),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=8.2, leading=10.4,
                           backColor=CODE_BG, borderColor=RULE, borderWidth=0.5,
                           borderPadding=7, leftIndent=7, rightIndent=7,
                           spaceBefore=6, spaceAfter=12),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.8, leading=11.5),
    "cell_head": ParagraphStyle("cell_head", fontName="Helvetica-Bold", fontSize=8.8,
                                leading=11.5, textColor=ACCENT),
    "toc_title": ParagraphStyle("toc_title", fontName="Helvetica-Bold", fontSize=20,
                                leading=25, textColor=ACCENT, spaceAfter=16),
}

# ---------------------------------------------------------------- inline markup

_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_URL = re.compile(r"(?<![\"=>])(https?://[^\s<)]+[^\s<).,;:])")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def inline(text: str) -> str:
    """Convert inline Markdown to ReportLab's mini-HTML."""
    protected: list[str] = []

    def protect(fragment: str) -> str:
        protected.append(fragment)
        return f"\x00{len(protected) - 1}\x00"

    text = _CODE.sub(
        lambda m: protect(f'<font face="Courier" size="9" color="#9B2C2C">{escape(m.group(1))}</font>'),
        text,
    )
    text = _LINK.sub(
        lambda m: protect(f'<link href="{escape(m.group(2))}" color="#1F5FAD">{escape(m.group(1))}</link>'),
        text,
    )
    text = _URL.sub(
        lambda m: protect(f'<link href="{escape(m.group(1))}" color="#1F5FAD">{escape(m.group(1))}</link>'),
        text,
    )
    text = escape(text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)


def plain(text: str) -> str:
    """Approximate rendered text (for measuring table columns)."""
    return re.sub(r"[*`]", "", _LINK.sub(r"\1", text))


# ---------------------------------------------------------------- block parsing


class Heading(Paragraph):
    """A Paragraph that registers itself in the table of contents and PDF outline."""

    def __init__(self, text: str, level: int):
        super().__init__(inline(text), STYLES[f"h{level}"])
        self.toc_level = level - 1
        self.toc_text = plain(text)


def _longest_word_width(cell: str, header: bool) -> float:
    """Width of the widest unbreakable word in a cell, in points."""
    widest = 0.0
    for span in re.split(r"(`[^`]+`)", cell):
        if span.startswith("`"):
            words, font, size = span.strip("`").split(), "Courier", 9
        else:
            words, font, size = plain(span).split(), "Helvetica-Bold" if header else "Helvetica", 8.8
        for word in words:
            widest = max(widest, stringWidth(word, font, size))
    return widest


def column_widths(rows: list[list[str]], ncols: int) -> list[float]:
    """Every column gets at least its longest word (so words never break mid-word);
    leftover space goes to columns in proportion to how much text they hold."""
    padding = 10
    minimum = [
        max(_longest_word_width(r[c], header=(i == 0)) for i, r in enumerate(rows)) + padding + 2
        for c in range(ncols)
    ]
    if sum(minimum) >= CONTENT_W:
        return [CONTENT_W * m / sum(minimum) for m in minimum]
    weights = [max(6, min(max(len(plain(r[c])) for r in rows), 48)) for c in range(ncols)]
    # Weight-proportional widths, but never below the minimum: fix the columns that
    # need their minimum, then share what's left among the rest.
    fixed: dict[int, float] = {}
    while True:
        free = [c for c in range(ncols) if c not in fixed]
        space = CONTENT_W - sum(fixed.values())
        total = sum(weights[c] for c in free)
        proposal = {c: space * weights[c] / total for c in free}
        too_small = [c for c in free if proposal[c] < minimum[c]]
        if not too_small:
            return [fixed.get(c, proposal.get(c, 0.0)) for c in range(ncols)]
        for c in too_small:
            fixed[c] = minimum[c]


def make_table(rows: list[list[str]]) -> Table:
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = column_widths(rows, ncols)

    data = [
        [Paragraph(inline(cell), STYLES["cell_head" if i == 0 else "cell"]) for cell in row]
        for i, row in enumerate(rows)
    ]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT_LIGHT),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


_LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_TABLE_SEP = re.compile(r"^:?-{2,}:?$")


def parse_markdown(md: str) -> list:
    flowables: list = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            flowables.append(Paragraph(inline(" ".join(s.strip() for s in para)), STYLES["body"]))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        if stripped.startswith("```"):
            flush_para()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            flowables.append(Preformatted("\n".join(code), STYLES["code"]))
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_para()
            level = len(heading.group(1))
            flowables.append(Heading(heading.group(2), level))
            i += 1
            continue

        if stripped.startswith("|"):
            flush_para()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(_TABLE_SEP.match(c) for c in cells if c):
                    rows.append(cells)
                i += 1
            flowables.append(make_table(rows))
            flowables.append(Spacer(1, 10))
            continue

        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            flowables.append(Paragraph(inline(" ".join(quote)), STYLES["quote"]))
            continue

        item = _LIST_ITEM.match(line)
        if item:
            flush_para()
            while i < len(lines):
                m = _LIST_ITEM.match(lines[i])
                if not m:
                    break
                indent, marker, text = m.groups()
                parts = [text]
                i += 1
                # continuation lines: indented, not a new list item, not blank
                while (
                    i < len(lines)
                    and lines[i].strip()
                    and lines[i].startswith("  ")
                    and not _LIST_ITEM.match(lines[i])
                ):
                    parts.append(lines[i].strip())
                    i += 1
                nested = len(indent) >= 2
                bullet = marker if marker[0].isdigit() else ("-" if nested else "•")
                flowables.append(
                    Paragraph(
                        inline(" ".join(parts)),
                        STYLES["bullet2" if nested else "bullet"],
                        bulletText=bullet,
                    )
                )
            flowables.append(Spacer(1, 5))
            continue

        para.append(line)
        i += 1

    flush_para()
    return flowables


# ---------------------------------------------------------------- document


class DocsTemplate(BaseDocTemplate):
    def __init__(self, path: Path, running_title: str, **kw):
        super().__init__(
            str(path), pagesize=LETTER, leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN, **kw,
        )
        self.running_title = running_title
        frame = Frame(MARGIN, MARGIN, CONTENT_W, PAGE_H - 2 * MARGIN, id="main")
        self.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[frame]),
                PageTemplate(id="content", frames=[frame], onPage=self._decorate),
            ]
        )
        self._bookmark = 0

    def _decorate(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, PAGE_H - MARGIN + 14, PAGE_W - MARGIN, PAGE_H - MARGIN + 14)
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, PAGE_H - MARGIN + 20, self.running_title)
        canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 24, f"Page {doc.page}")
        canvas.restoreState()

    def beforeDocument(self) -> None:
        # multiBuild runs several passes; keys must be identical in each one
        # or the table of contents never stabilizes.
        self._bookmark = 0

    def afterFlowable(self, flowable) -> None:
        heading = flowable
        if isinstance(flowable, KeepTogether):
            heading = next((f for f in flowable._content if isinstance(f, Heading)), None)
        if not isinstance(heading, Heading) or heading.toc_level > 1:
            return
        key = f"h{self._bookmark}"
        self._bookmark += 1
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(heading.toc_text, key, level=heading.toc_level, closed=True)
        self.notify("TOCEntry", (heading.toc_level, heading.toc_text, self.page, key))


def cover(title: str, subtitle: str, extra: list[str]) -> list:
    today = dt.date.today().strftime("%B %Y")
    story: list = [Spacer(1, 2.1 * inch), Paragraph(title, STYLES["title"])]
    story.append(Paragraph(subtitle, STYLES["subtitle"]))
    story.append(Spacer(1, 0.25 * inch))
    rule = Table([[""]], colWidths=[2.2 * inch], rowHeights=[2], hAlign="CENTER")
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]))
    story += [rule, Spacer(1, 0.35 * inch)]
    for line in [AUTHOR, today, *extra]:
        story.append(Paragraph(escape(line), STYLES["cover_meta"]))
    return story


def check_charset(name: str, text: str) -> None:
    """The built-in PDF fonts only cover Latin-1; warn about anything else."""
    bad = sorted({ch for ch in text if ord(ch) > 0xFF and ch != "•"})
    if bad:
        print(f"  warning: {name} has characters the PDF fonts can't draw: {''.join(bad)!r}")


def build(sources: list[Path], out: Path, title: str, subtitle: str, toc: bool) -> None:
    doc = DocsTemplate(out, running_title=f"{PROJECT_NAME} - {title}", title=f"{PROJECT_NAME}: {title}",
                       author=AUTHOR, subject=SUBTITLE)
    story: list = cover(PROJECT_NAME if toc else title, subtitle if toc else PROJECT_NAME,
                        ["Version 0.1"])
    story.append(NextPageTemplate("content"))
    story.append(PageBreak())

    if toc:
        contents = TableOfContents()
        contents.levelStyles = [
            ParagraphStyle("toc1", fontName="Helvetica-Bold", fontSize=11, leading=15,
                           leftIndent=0, spaceBefore=7, textColor=ACCENT),
            ParagraphStyle("toc2", fontName="Helvetica", fontSize=9.5, leading=12, leftIndent=16),
        ]
        contents.tableStyle = TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
        story += [Paragraph("Contents", STYLES["toc_title"]), contents, PageBreak()]

    for n, src in enumerate(sources):
        text = src.read_text(encoding="utf-8")
        check_charset(src.name, text)
        if n:
            story.append(PageBreak())
        story += parse_markdown(text)

    doc.multiBuild(story)
    print(f"  wrote {out.relative_to(ROOT)}")


def main() -> int:
    public = sorted(DOCS.glob("[0-9][0-9]-*.md"))
    if not public:
        print("no docs/NN-*.md files found")
        return 1
    print("Building PDFs...")
    build(public, DOCS / "Switchboard-Documentation.pdf", "Documentation", SUBTITLE, toc=True)

    private = sorted((DOCS / "private").glob("*.md"))
    if private:
        build(private, DOCS / "private" / "Interview-Prep.pdf", "Interview Prep",
              "Private notes - not committed", toc=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

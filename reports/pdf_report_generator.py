"""PDF analytics report generator (presentation layer).

Position::

    AnalyticsReportGenerator (Markdown)  ->  PdfReportGenerator  ->  analytics_report.pdf
                                                 ^ THIS MODULE

This module performs **PDF generation only**. It reuses
:class:`reports.analytics_report_generator.AnalyticsReportGenerator` to obtain
the existing Markdown report (the single source of truth) and renders it to a
professional PDF with reportlab. It does **not** recalculate metrics, **not**
access any analytics module directly, and duplicates **no** analytics logic —
the only dependency is the Markdown report string.

The Markdown produced by the report generator is a small, known subset, so a
compact parser maps each construct to a reportlab flowable (no third-party
Markdown engine):

    # title            -> title page
    ## / ### heading   -> section / sub-section heading
    > note             -> italic note paragraph
    | a | b |          -> styled table (header + separator + rows)
    - bullet           -> bullet paragraph
    ---                -> horizontal rule
    **bold** / `code`  -> inline styling

CLI::

    python reports/pdf_report_generator.py            # -> reports/analytics_report.pdf
"""

from __future__ import annotations

import html
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional, Union

# Make the project root importable when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_CENTER  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reports.analytics_report_generator import AnalyticsReportGenerator  # noqa: E402

__all__ = ["PdfReportGenerator", "generate_pdf_report"]

logger = logging.getLogger(__name__)

_DEFAULT_OUT = _PROJECT_ROOT / "reports" / "analytics_report.pdf"

# Brand palette (kept minimal).
_HEADING_COLOR = colors.HexColor("#1F3B57")
_HEADER_BG = colors.HexColor("#1F3B57")
_ROW_ALT = colors.HexColor("#F2F5F8")
_GRID = colors.HexColor("#B8C2CC")


# ---------------------------------------------------------------------------
# Inline Markdown -> reportlab markup
# ---------------------------------------------------------------------------


def _inline(text: str) -> str:
    """Convert inline markdown (`**bold**`, `` `code` ``) to reportlab markup.

    XML-escapes first so literal ``&``/``<``/``>`` are safe, then applies tags.
    """
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    return text


def _is_separator_row(cells: List[str]) -> bool:
    """True for a markdown table separator row (e.g. ``|---|---|``)."""
    return bool(cells) and all(set(c) <= set("-: ") and "-" in c for c in cells)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class PdfReportGenerator:
    """Render the Markdown analytics report into a professional PDF."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        dataset: Optional[str] = None,
        *,
        title: str = "MyJio Floater Analytics",
    ) -> None:
        self.path = path
        self.dataset = dataset
        self.title = title
        self._styles = self._build_styles()

    # -- public API --------------------------------------------------------

    def build(self, out_path: Union[str, Path] = _DEFAULT_OUT) -> Path:
        """Generate the Markdown report and write it as a PDF to ``out_path``."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Reuse the existing Markdown report (single source of truth).
        markdown = AnalyticsReportGenerator(path=self.path, dataset=self.dataset).generate()

        # 2. Build the document and render the markdown into flowables.
        doc = SimpleDocTemplate(
            str(out_path),
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2.2 * cm,
            title="MyJio Floater Analytics Report",
            author="MyJio Floater Analytics Platform",
        )
        flowables = self._render(markdown, doc.width)

        # 3. Build: cover footer (date + page number) on page 1, page-number
        #    footer on every later page.
        doc.build(flowables, onFirstPage=self._cover_footer, onLaterPages=self._footer)
        logger.info("Wrote PDF report to %s.", out_path)
        return out_path

    # -- styles ------------------------------------------------------------

    @staticmethod
    def _build_styles() -> dict:
        base = getSampleStyleSheet()
        styles = {
            "title": ParagraphStyle(
                "TitleBig", parent=base["Title"], fontSize=26, leading=30,
                alignment=TA_CENTER, textColor=_HEADING_COLOR,
            ),
            "subtitle": ParagraphStyle(
                "Subtitle", parent=base["Normal"], fontSize=13, leading=16,
                alignment=TA_CENTER, textColor=colors.HexColor("#5A6B7B"),
            ),
            "meta": ParagraphStyle(
                "Meta", parent=base["Normal"], fontSize=10, leading=15,
                alignment=TA_CENTER,
            ),
            "note": ParagraphStyle(
                "Note", parent=base["Italic"], fontSize=9, leading=13,
                textColor=colors.HexColor("#5A6B7B"),
            ),
            "cover_desc": ParagraphStyle(
                "CoverDesc", parent=base["Normal"], fontSize=11.5, leading=18,
                alignment=TA_CENTER, textColor=colors.HexColor("#3A4A59"),
                leftIndent=1.5 * cm, rightIndent=1.5 * cm,
            ),
            "h2": ParagraphStyle(
                "H2", parent=base["Heading2"], fontSize=15, leading=19,
                spaceBefore=14, spaceAfter=6, textColor=_HEADING_COLOR,
            ),
            "h3": ParagraphStyle(
                "H3", parent=base["Heading3"], fontSize=12, leading=15,
                spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#2C4A66"),
            ),
            "body": ParagraphStyle(
                "Body", parent=base["Normal"], fontSize=10, leading=14, spaceAfter=4,
            ),
            "bullet": ParagraphStyle(
                "Bullet", parent=base["Normal"], fontSize=10, leading=14,
                leftIndent=14, spaceAfter=2,
            ),
            "cell": ParagraphStyle(
                "Cell", parent=base["Normal"], fontSize=9, leading=12,
            ),
            "cell_header": ParagraphStyle(
                "CellHeader", parent=base["Normal"], fontSize=9, leading=12,
                textColor=colors.white, fontName="Helvetica-Bold",
            ),
        }
        return styles

    # -- footer (page numbers) --------------------------------------------

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(
            A4[0] / 2.0,
            1.3 * cm,
            f"{self.title}  ·  Page {canvas.getPageNumber()}",
        )
        canvas.restoreState()

    def _cover_footer(self, canvas, doc) -> None:
        """First-page footer: the cover date plus the running page number."""
        self._footer(canvas, doc)  # keep page numbering on the cover too
        canvas.saveState()
        canvas.setFont("Helvetica", 11)
        canvas.setFillColor(colors.HexColor("#5A6B7B"))
        canvas.drawCentredString(A4[0] / 2.0, 2.5 * cm, "June 2026")
        canvas.restoreState()

    # -- rendering ---------------------------------------------------------

    def _render(self, markdown: str, doc_width: float) -> List:
        lines = markdown.splitlines()
        # Title block = everything before the first "## " section heading.
        split_at = next(
            (i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines)
        )
        # The Markdown title block (H1 + Dataset/Source/Generated-at metadata)
        # is intentionally NOT rendered: the cover is a fixed management layout.
        flow = self._title_page()
        flow += self._body(lines[split_at:], doc_width)
        return flow

    def _title_page(self) -> List:
        """Fixed management-style cover page (no telemetry metadata)."""
        s = self._styles
        description_1 = (
            "This report summarizes telemetry-derived customer engagement, "
            "campaign exposure, interaction behaviour, and analytics insights "
            "generated from the configurable telemetry analytics framework."
        )
        description_2 = (
            "The report is generated automatically from telemetry data using "
            "the analytics pipeline."
        )
        return [
            Spacer(1, 6 * cm),
            Paragraph("MyJio Telemetry Analytics Platform", s["title"]),
            Spacer(1, 0.5 * cm),
            Paragraph("Management Analytics Report", s["subtitle"]),
            Spacer(1, 2.2 * cm),
            Paragraph(description_1, s["cover_desc"]),
            Spacer(1, 0.5 * cm),
            Paragraph(description_2, s["cover_desc"]),
            PageBreak(),
        ]

    def _body(self, lines: List[str], doc_width: float) -> List:
        s = self._styles
        flow: List = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i].rstrip()
            stripped = line.strip()

            if not stripped:
                i += 1
                continue
            if line.startswith("## "):
                flow.append(Paragraph(_inline(line[3:]), s["h2"]))
                i += 1
            elif line.startswith("### "):
                flow.append(Paragraph(_inline(line[4:]), s["h3"]))
                i += 1
            elif line.startswith("> "):
                flow.append(Paragraph(_inline(line[2:]), s["note"]))
                i += 1
            elif stripped.startswith("---"):
                flow.append(Spacer(1, 4))
                flow.append(HRFlowable(width="100%", thickness=0.6, color=_GRID))
                flow.append(Spacer(1, 4))
                i += 1
            elif stripped.startswith("|"):
                block = []
                while i < n and lines[i].strip().startswith("|"):
                    block.append(lines[i])
                    i += 1
                table = self._table(block, doc_width)
                if table is not None:
                    flow.append(table)
                    flow.append(Spacer(1, 8))
            elif stripped.startswith("- "):
                flow.append(Paragraph("• " + _inline(stripped[2:]), s["bullet"]))
                i += 1
            else:
                flow.append(Paragraph(_inline(stripped), s["body"]))
                i += 1
        return flow

    def _table(self, block: List[str], doc_width: float):
        s = self._styles
        rows = [
            [c.strip() for c in raw.strip().strip("|").split("|")] for raw in block
        ]
        rows = [r for r in rows if not _is_separator_row(r)]
        if not rows:
            return None

        header, body = rows[0], rows[1:]
        ncols = max(len(header), 1)
        col_widths = [doc_width / ncols] * ncols

        data = [[Paragraph(_inline(c), s["cell_header"]) for c in header]]
        data += [[Paragraph(_inline(c), s["cell"]) for c in r] for r in body]

        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
                    ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return table


def generate_pdf_report(
    out_path: Union[str, Path] = _DEFAULT_OUT,
    *,
    path: Optional[Union[str, Path]] = None,
    dataset: Optional[str] = None,
) -> Path:
    """Convenience one-call entrypoint; returns the written PDF path."""
    return PdfReportGenerator(path=path, dataset=dataset).build(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Generate a PDF analytics report.")
    parser.add_argument("--path", default=None, help="Telemetry source (defaults to sample).")
    parser.add_argument("--dataset", default=None, help="Semantic dataset key.")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="Output PDF path.")
    parser.add_argument("--quiet", action="store_true", help="Suppress pipeline logs.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    written = PdfReportGenerator(path=args.path, dataset=args.dataset).build(args.out)
    print(f"PDF report written to {written}")

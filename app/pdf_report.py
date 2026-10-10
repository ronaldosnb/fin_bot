from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from app.config import get_settings
from app.finance import format_money
from app.report_data import MonthlyReport


GREEN = colors.HexColor("#1c5443")
INK = colors.HexColor("#18231f")
MUTED = colors.HexColor("#52645a")
LINE = colors.HexColor("#d9dfd5")
PAPER = colors.HexColor("#edf3ed")


def render_monthly_pdf(data: MonthlyReport) -> bytes:
    font_dir = Path(__file__).parent / "static" / "fonts"
    for name, filename in (("FinBot", "DejaVuSans.ttf"), ("FinBot-Bold", "DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    pdfmetrics.registerFontFamily("FinBot", normal="FinBot", bold="FinBot-Bold")
    body = ParagraphStyle("body", fontName="FinBot", fontSize=8, leading=12, textColor=INK)
    heading = ParagraphStyle("heading", parent=body, fontName="FinBot-Bold", fontSize=13,
                             leading=18, textColor=GREEN, spaceBefore=16, spaceAfter=10, keepWithNext=True)
    title = ParagraphStyle("title", parent=heading, fontSize=24, leading=30, spaceBefore=0)
    small = ParagraphStyle("small", parent=body, textColor=MUTED, spaceAfter=8)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)

    def paragraph(value, style=body):
        # Descriptions are data, never ReportLab markup.
        clean = "".join(c for c in str(value) if c >= " " or c in "\n\t")
        return Paragraph(escape(clean).replace("\n", "<br/>"), style)

    def table(headers, rows, widths):
        result = LongTable([[paragraph(h) for h in headers]] + rows, colWidths=widths,
                           repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PAPER),
            ("LINEBELOW", (0, 0), (-1, 0), 1, GREEN),
            ("LINEBELOW", (0, 1), (-1, -1), .4, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return result

    generated = datetime.now(ZoneInfo(get_settings().timezone))
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                            topMargin=20*mm, bottomMargin=20*mm,
                            title=f"FinBot - Relatório {data.month:02d}/{data.year}", author="FinBot")
    width = A4[0] - 36*mm
    story = [paragraph("FinBot", small), paragraph("Relatório mensal", title),
             paragraph(f"{data.ledger_name} | {data.month:02d}/{data.year}", heading),
             paragraph(f"Gerado em {generated:%d/%m/%Y às %H:%M} ({get_settings().timezone})", small)]
    summary_rows = []
    for status, label in (("realized", "Realizados"), ("planned", "Previstos")):
        income = data.total(status, "income")
        expense = data.total(status, "expense")
        summary_rows.append([paragraph(label), paragraph(format_money(income), right),
                             paragraph(format_money(expense), right), paragraph(format_money(income-expense), right)])
    story.append(table(["Valores", "Receitas", "Despesas", "Saldo"], summary_rows, [width/4]*4))
    story.append(Spacer(1, 8))
    story.append(paragraph("O saldo realizado considera apenas lançamentos realizados. Valores previstos são projeções.", small))
    if not data.entries:
        story.append(paragraph("Nenhum lançamento neste mês.", heading))
    for status, status_label in (("realized", "realizadas"), ("planned", "previstas")):
        for kind, kind_label in (("income", "Receitas"), ("expense", "Despesas")):
            categories = data.categories(status, kind)
            if not categories:
                continue
            story.append(paragraph(f"{kind_label} por categoria - {status_label}", heading))
            story.append(table(["Categoria", "Total"],
                               [[paragraph(name), paragraph(format_money(value), right)] for name, value in categories],
                               [width*.73, width*.27]))
    for status, status_label in (("realized", "realizadas"), ("planned", "previstas")):
        for kind, kind_label in (("income", "Receitas"), ("expense", "Despesas")):
            entries = [e for e in data.entries if e.status == status and e.kind == kind]
            if not entries:
                continue
            story.append(paragraph(f"{kind_label} - {status_label}", heading))
            rows = []
            for entry in entries:
                description = entry.description
                if entry.installment_count:
                    description += f" (parcela {entry.installment_number}/{entry.installment_count})"
                rows.append([paragraph(f"#{entry.id}"), paragraph(entry.occurred_on.strftime("%d/%m/%Y")),
                             paragraph(description), paragraph(entry.category),
                             paragraph(format_money(entry.amount_cents), right)])
            story.append(table(["ID", "Data", "Descrição", "Categoria", "Valor"], rows,
                               [width*.08, width*.16, width*.37, width*.20, width*.19]))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(18*mm, 14*mm, A4[0]-18*mm, 14*mm)
        canvas.setFont("FinBot", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18*mm, 9*mm, f"FinBot | {data.month:02d}/{data.year}")
        canvas.drawRightString(A4[0]-18*mm, 9*mm, f"Página {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()

"""PDFs for the visa case: invoices, and signature pages on contracts / data confirmations."""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.core.config import settings

NAVY = colors.HexColor("#0B2A4A")
GOLD = colors.HexColor("#C9A227")
GREY = colors.HexColor("#545D6B")


def money(cents: int, currency: str = "AUD") -> str:
    symbol = "A$" if currency.upper() == "AUD" else f"{currency.upper()} "
    return f"{symbol}{cents / 100:,.2f}"


def _local(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.business_timezone)).strftime("%d %b %Y, %I:%M %p %Z")


def invoice_pdf(
    *,
    company_name: str,
    company_lines: list[str],
    number: str,
    issued: datetime,
    customer_name: str,
    customer_email: str,
    case_ref: str,
    visa_type: str,
    description: str,
    amount_cents: int,
    currency: str,
    paid_at: datetime | None = None,
    payment_ref: str | None = None,
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setTitle(f"Invoice {number}")

    # Header
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, h - 25 * mm, company_name)
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    y = h - 31 * mm
    for line in company_lines:
        c.drawString(20 * mm, y, line)
        y -= 4.5 * mm
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 20 * mm, h - 25 * mm, "INVOICE")
    c.setFillColor(GREY)
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 20 * mm, h - 32 * mm, f"No. {number}")
    c.drawRightString(w - 20 * mm, h - 37 * mm, f"Issued {issued.strftime('%d %b %Y')}")

    # Bill to
    y = h - 62 * mm
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Bill to")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.black)
    c.drawString(20 * mm, y - 6 * mm, customer_name)
    c.drawString(20 * mm, y - 11 * mm, customer_email)
    c.drawRightString(w - 20 * mm, y - 6 * mm, f"Case {case_ref}")
    c.drawRightString(w - 20 * mm, y - 11 * mm, f"{visa_type} visa")

    # Line item
    y -= 30 * mm
    c.setFillColor(colors.HexColor("#F5EBC8"))
    c.rect(20 * mm, y - 2 * mm, w - 40 * mm, 9 * mm, stroke=0, fill=1)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(23 * mm, y + 1 * mm, "Description")
    c.drawRightString(w - 23 * mm, y + 1 * mm, "Amount")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(23 * mm, y - 9 * mm, description[:90])
    c.drawRightString(w - 23 * mm, y - 9 * mm, money(amount_cents, currency))
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.line(20 * mm, y - 14 * mm, w - 20 * mm, y - 14 * mm)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(23 * mm, y - 22 * mm, f"Total ({currency.upper()})")
    c.drawRightString(w - 23 * mm, y - 22 * mm, money(amount_cents, currency))

    # Status
    y -= 45 * mm
    if paid_at:
        c.setFillColor(colors.HexColor("#16A34A"))
        c.setFont("Helvetica-Bold", 28)
        c.drawString(20 * mm, y, "PAID")
        c.setFont("Helvetica", 10)
        c.setFillColor(GREY)
        c.drawString(20 * mm, y - 7 * mm, f"Paid {_local(paid_at)}")
        if payment_ref:
            c.drawString(20 * mm, y - 12 * mm, f"Payment reference {payment_ref}")
    else:
        c.setFillColor(GREY)
        c.setFont("Helvetica", 10)
        c.drawString(20 * mm, y, "Pay securely in the Nikee Migrations app.")

    c.setFont("Helvetica", 8)
    c.setFillColor(GREY)
    c.drawString(20 * mm, 15 * mm, f"{company_name} · Invoice {number} · Case {case_ref}")
    c.showPage()
    c.save()
    return buf.getvalue()


def sign_pdf(
    original: bytes,
    *,
    signature_png: bytes,
    signer_name: str,
    signed_at: datetime,
    document_title: str,
    case_ref: str,
    device: str | None,
) -> bytes:
    """Returns the original PDF with a footer on every page and a signature page at the end."""
    # Pages must belong to the writer before they can be stamped (pypdf 7).
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(original)))
    footer_text = f"Signed by {signer_name} on {_local(signed_at)} · Case {case_ref}"
    for page in writer.pages:
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)
        stamp_buf = io.BytesIO()
        sc = canvas.Canvas(stamp_buf, pagesize=(pw, ph))
        sc.setFont("Helvetica", 7)
        sc.setFillColor(GREY)
        sc.drawString(10 * mm, 6 * mm, footer_text)
        sc.save()
        page.merge_page(PdfReader(io.BytesIO(stamp_buf.getvalue())).pages[0])

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(20 * mm, h - 30 * mm, "Signature")
    c.setFont("Helvetica", 11)
    c.setFillColor(colors.black)
    c.drawString(20 * mm, h - 40 * mm, document_title)
    c.drawString(20 * mm, h - 46 * mm, f"Case {case_ref}")
    img = ImageReader(io.BytesIO(signature_png))
    iw, ih = img.getSize()
    box_w, box_h = 120 * mm, 45 * mm
    scale = min(box_w / iw, box_h / ih)
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.rect(20 * mm, h - 100 * mm, box_w, box_h, stroke=1, fill=0)
    c.drawImage(
        img, 20 * mm, h - 100 * mm, iw * scale, ih * scale, mask="auto", preserveAspectRatio=True
    )
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, h - 108 * mm, f"Signed by: {signer_name}")
    c.drawString(20 * mm, h - 114 * mm, f"Date and time: {_local(signed_at)}")
    if device:
        c.setFillColor(GREY)
        c.setFont("Helvetica", 8)
        c.drawString(20 * mm, h - 120 * mm, f"Device: {device[:110]}")
    c.showPage()
    c.save()
    writer.add_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

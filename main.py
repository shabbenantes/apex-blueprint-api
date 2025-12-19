from flask import Flask, request, jsonify
import os
import uuid
import json
import re
import time
from typing import Dict, Any, Optional, List, Tuple

from openai import OpenAI
import boto3

# ReportLab imports
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    PageBreak,
    Table,
    TableStyle,
    KeepTogether,
    CondPageBreak,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.lib.units import inch

from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.widgets.markers import makeMarker

app = Flask(__name__)

# ---------- OpenAI ----------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ---------- S3 CONFIG ----------
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-2")
s3_client = boto3.client("s3", region_name=S3_REGION)

# ---------- Context store (in-memory) ----------
CONTEXT_TTL_SECONDS = int(os.environ.get("CONTEXT_TTL_SECONDS", "86400"))  # 24h default
_CONTEXT_BY_PHONE: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------
def clean_value(v: object) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"null", "none", "n/a", "na"}:
        return ""
    return s


def normalize_phone(phone: str) -> str:
    p = clean_value(phone)
    digits = re.sub(r"\D+", "", p)
    if len(digits) == 10:
        digits = "1" + digits
    return digits


def to_e164(phone_digits: str) -> str:
    d = re.sub(r"\D+", "", phone_digits or "")
    if not d:
        return ""
    return f"+{d}"


def cleanup_context_store() -> None:
    now = time.time()
    expired = [k for k, v in _CONTEXT_BY_PHONE.items() if v.get("expires_at", 0) <= now]
    for k in expired:
        _CONTEXT_BY_PHONE.pop(k, None)


def store_context_for_phone(phone: str, context: Dict[str, Any]) -> None:
    cleanup_context_store()
    key = normalize_phone(phone)
    if not key:
        return
    _CONTEXT_BY_PHONE[key] = {**context, "expires_at": time.time() + CONTEXT_TTL_SECONDS}


def get_context_for_phone(phone: str) -> Optional[Dict[str, Any]]:
    cleanup_context_store()
    key = normalize_phone(phone)
    if not key:
        return None
    item = _CONTEXT_BY_PHONE.get(key)
    if not item:
        return None
    out = dict(item)
    out.pop("expires_at", None)
    return out


def parse_int(s: str) -> Optional[int]:
    s = clean_value(s)
    if not s:
        return None
    m = re.search(r"(\d{1,7})", s.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def safe_p(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_bullet_prefix(s: str) -> str:
    s = s.strip()
    if s.startswith("- "):
        return s[2:].strip()
    if s.startswith("• "):
        return s[2:].strip()
    if s.startswith("-"):
        return s[1:].strip()
    if s.startswith("•"):
        return s[1:].strip()
    return s


def _shorten_bullet(text: str, max_words: int = 10, max_chars: int = 72) -> str:
    """
    Hard-limit bullets so we can safely keep font large on phones.
    """
    t = clean_value(text)
    if not t:
        return ""

    # Trim after first "sentence-ish" break
    for sep in [". ", "; ", " — ", " - "]:
        if sep in t:
            t = t.split(sep, 1)[0].strip()

    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip() + "…"

    if len(t) > max_chars:
        t = t[: max_chars - 1].rstrip() + "…"

    return t


def _shorten_list(items: List[str], max_items: int, max_words: int = 10, max_chars: int = 72) -> List[str]:
    out = []
    for x in items:
        s = _shorten_bullet(x, max_words=max_words, max_chars=max_chars)
        if s:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


# --------------------------------------------------------------------
# PDF DESIGN SYSTEM
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    # Palette (brighter, cleaner)
    NAVY = colors.HexColor("#0B1B2B")
    BLUE = colors.HexColor("#2563EB")
    BLUE_DK = colors.HexColor("#1E40AF")
    MUTED = colors.HexColor("#64748B")
    WHITE = colors.white

    # Cards
    CARD_BG = colors.HexColor("#FFFFFF")
    CARD_BG_ALT = colors.HexColor("#F3F7FF")
    BORDER = colors.HexColor("#D8E1EE")
    SOFT = colors.HexColor("#E6ECF5")

    # Bigger typography
    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=36,
        leading=40,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=6,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=17,
        leading=21,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=10,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=21,
        leading=25,
        textColor=NAVY,
        spaceBefore=8,
        spaceAfter=6,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=NAVY,
        spaceBefore=1,
        spaceAfter=1,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=15,
        leading=20,
        textColor=colors.HexColor("#111827"),
        spaceAfter=2,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        textColor=MUTED,
        spaceAfter=3,
    )

    # Bigger “Week” body so Section 5 fills pages better
    body_week = ParagraphStyle(
        "ApexBodyWeek",
        parent=body,
        fontSize=16,
        leading=21,
        spaceAfter=3,
    )

    fix_header = ParagraphStyle(
        "FixHeader",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=WHITE,
        alignment=TA_LEFT,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "BLUE_DK": BLUE_DK,
        "MUTED": MUTED,
        "CARD_BG": CARD_BG,
        "CARD_BG_ALT": CARD_BG_ALT,
        "BORDER": BORDER,
        "SOFT": SOFT,
        "WHITE": WHITE,
        "title": title,
        "subtitle": subtitle,
        "h1": h1,
        "h2": h2,
        "body": body,
        "body_week": body_week,
        "small": small,
        "fix_header": fix_header,
    }


def _header_footer(canvas, doc):
    st = _brand_styles()
    canvas.saveState()
    w, h = letter

    canvas.setStrokeColor(st["SOFT"])
    canvas.setLineWidth(1)
    canvas.line(42, h - 44, w - 42, h - 44)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(42, h - 36, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 42, h - 36, time.strftime("%b %d, %Y"))

    canvas.setStrokeColor(st["SOFT"])
    canvas.line(42, 44, w - 42, 44)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(42, 32, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 42, 32, f"Page {doc.page}")

    canvas.restoreState()


def _estimate_card_height(num_lines: int, st, week: bool = False, extra_padding: int = 0) -> float:
    line_h = float(st["body_week"].leading if week else st["body"].leading)
    title_h = float(st["h2"].leading)
    padding = (12 + extra_padding) + (12 + extra_padding) + 14
    return title_h + (num_lines * line_h) + padding


def _card_table(
    title: str,
    bullets: List[str],
    st,
    bg=None,
    placeholder_if_empty: bool = True,
    week: bool = False,
    extra_padding: int = 0,
) -> Table:
    bg_color = bg if bg is not None else st["CARD_BG"]
    body_style = st["body_week"] if week else st["body"]

    rows: List[List[Any]] = [[Paragraph(f"<b>{safe_p(title)}</b>", st["h2"])]]

    clean_bullets = [clean_value(b) for b in bullets if clean_value(b)]
    if not clean_bullets and placeholder_if_empty:
        rows.append([Paragraph("No details provided.", body_style)])
    else:
        for b in clean_bullets:
            rows.append([Paragraph("• " + safe_p(b), body_style)])

    tbl = Table(rows, colWidths=[7.55 * inch], hAlign="LEFT")
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg_color),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LINEBEFORE", (0, 0), (0, -1), 4, st["BLUE"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12 + extra_padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12 + extra_padding),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return tbl


def _add_card_no_split(
    story: List[Any],
    title: str,
    bullets: List[str],
    st,
    bg,
    max_bullets: int = 7,
    placeholder_if_empty: bool = True,
    week: bool = False,
    extra_padding: int = 0,
    spacer_after: int = 12,
):
    """
    Ensures a card NEVER splits across pages by chunking into multiple cards.
    """
    bullets = [clean_value(x) for x in bullets if clean_value(x)]

    if not bullets and placeholder_if_empty:
        chunks = [[]]
    else:
        chunks = [bullets[i:i + max_bullets] for i in range(0, len(bullets), max_bullets)] or [[]]

    for idx, chunk in enumerate(chunks):
        t = title if idx == 0 else f"{title} (cont.)"
        est_h = _estimate_card_height(max(1, len(chunk) + 1), st, week=week, extra_padding=extra_padding)
        story.append(CondPageBreak(est_h + 14))

        card = _card_table(
            t,
            chunk,
            st,
            bg=bg,
            placeholder_if_empty=placeholder_if_empty,
            week=week,
            extra_padding=extra_padding,
        )
        story.append(KeepTogether([card, Spacer(1, spacer_after)]))


def _fix_header_bar(title: str, st) -> Table:
    tbl = Table([[Paragraph(safe_p(title), st["fix_header"])]], colWidths=[7.55 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BLUE_DK"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BLUE_DK"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return tbl


def _bar_chart(title: str, labels: List[str], values: List[int], st, compact: bool = False) -> Drawing:
    height = 155 if compact else 215
    plot_h = 85 if compact else 135
    top_y = height - 18

    d = Drawing(460, height)
    d.add(String(0, top_y, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 28
    bc.width = 380
    bc.height = plot_h
    bc.data = [values]

    bc.strokeColor = colors.transparent
    bc.bars[0].fillColor = st["BLUE"]

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 9
    bc.categoryAxis.labels.fillColor = st["MUTED"]

    vmax = max(values + [10])
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = int(vmax * 1.25) if vmax > 0 else 10
    bc.valueAxis.valueStep = max(1, int(bc.valueAxis.valueMax / 5))
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 9
    bc.valueAxis.labels.fillColor = st["MUTED"]

    d.add(bc)
    return d


def _line_chart(title: str, labels: List[str], y_values: List[int], st) -> Drawing:
    d = Drawing(460, 215)
    d.add(String(0, 195, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    lc = HorizontalLineChart()
    lc.x = 40
    lc.y = 35
    lc.width = 380
    lc.height = 135

    # IMPORTANT: y-values only (NOT tuples)
    lc.data = [y_values]
    lc.joinedLines = 1
    lc.lines[0].strokeColor = st["BLUE"]
    lc.lines[0].strokeWidth = 2
    lc.lines[0].symbol = makeMarker("FilledCircle")
    lc.lines[0].symbol.size = 4

    lc.categoryAxis.categoryNames = labels
    lc.categoryAxis.labels.fontName = "Helvetica"
    lc.categoryAxis.labels.fontSize = 9
    lc.categoryAxis.labels.fillColor = st["MUTED"]

    lc.valueAxis.valueMin = 0
    lc.valueAxis.valueMax = 100
    lc.valueAxis.valueStep = 20
    lc.valueAxis.labels.fontName = "Helvetica"
    lc.valueAxis.labels.fontSize = 9
    lc.valueAxis.labels.fillColor = st["MUTED"]

    d.add(lc)
    return d


def _hours_saved_chart(st) -> Drawing:
    """
    Simple static third chart to add “designed” value:
    Estimated hours saved per week after fixes.
    """
    labels = ["Follow-ups", "Payroll", "Scheduling"]
    values = [10, 6, 4]  # conservative defaults
    d = Drawing(460, 215)
    d.add(String(0, 195, "Estimated Hours Saved Per Week (after fixes)", fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 35
    bc.width = 380
    bc.height = 135
    bc.data = [values]
    bc.strokeColor = colors.transparent
    bc.bars[0].fillColor = colors.HexColor("#3B82F6")

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 9
    bc.categoryAxis.labels.fillColor = st["MUTED"]

    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 12
    bc.valueAxis.valueStep = 2
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 9
    bc.valueAxis.labels.fillColor = st["MUTED"]

    d.add(bc)
    return d


# --------------------------------------------------------------------
# BLUEPRINT PARSING
# --------------------------------------------------------------------
def _extract_section_lines(blueprint_text: str, section_number: int) -> List[str]:
    lines = blueprint_text.splitlines()
    start = None
    target = f"SECTION {section_number}"
    for i, ln in enumerate(lines):
        if ln.strip().upper().startswith(target):
            start = i + 1
            break
    if start is None:
        return []

    out: List[str] = []
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("SECTION "):
            break
        out.append(s)
    return out


def _group_subsections(lines: List[str]) -> List[Tuple[str, List[str]]]:
    blocks: List[Tuple[str, List[str]]] = []
    current_title = "Highlights"
    current_items: List[str] = []

    for ln in lines:
        if ln.endswith(":") and len(ln) <= 45 and not ln.upper().startswith("FIX "):
            if current_items:
                blocks.append((current_title, current_items))
            current_title = ln.replace(":", "").strip()
            current_items = []
            continue
        current_items.append(_strip_bullet_prefix(ln))

    if current_items:
        blocks.append((current_title, current_items))

    return blocks


def _parse_fixes(section3_lines: List[str]) -> List[Dict[str, Any]]:
    fixes: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_bucket = None

    for ln in section3_lines:
        s = ln.strip()
        if not s:
            continue

        if s.upper().startswith("FIX "):
            if current:
                fixes.append(current)
            current = {"title": s, "fixes": [], "does": [], "included": []}
            current_bucket = None
            continue

        if current is None:
            continue

        low = s.lower()
        if low.startswith("what this fixes"):
            current_bucket = "fixes"
            continue
        if low.startswith("what this does"):
            current_bucket = "does"
            continue
        if low.startswith("what’s included") or low.startswith("what's included"):
            current_bucket = "included"
            continue

        if current_bucket in {"fixes", "does", "included"}:
            current[current_bucket].append(_strip_bullet_prefix(s))

    if current:
        fixes.append(current)

    return fixes[:6]


def _parse_week_blocks(section5_lines: List[str]) -> List[Tuple[str, List[str]]]:
    blocks: List[Tuple[str, List[str]]] = []
    current_title = None
    current_items: List[str] = []

    for ln in section5_lines:
        s = ln.strip()
        if not s:
            continue

        if s.upper().startswith("WEEK "):
            if current_title and current_items:
                blocks.append((current_title, current_items))
            current_title = s
            current_items = []
            continue

        if current_title is None:
            current_title = "30-Day Plan"
        current_items.append(_strip_bullet_prefix(s))

    if current_title and current_items:
        blocks.append((current_title, current_items))

    return blocks


# --------------------------------------------------------------------
# PDF GENERATION (V8)
# --------------------------------------------------------------------
def generate_pdf_v8(
    blueprint_text: str,
    pdf_path: str,
    lead_name: str,
    business_name: str,
    business_type: str,
    team_size: str,
    leads_per_week: str,
    jobs_per_week: str,
    lead_response_time: str,
):
    st = _brand_styles()

    # Slightly smaller margins to reduce “dead space” on each page
    # (header/footer is drawn outside the frame anyway).
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Automation Blueprint",
        author="Apex Automation",
        leftMargin=42,
        rightMargin=42,
        topMargin=52,
        bottomMargin=52,
    )

    story: List[Any] = []

    # ------------------- COVER -------------------
    story.append(Spacer(1, 22))
    story.append(Paragraph(safe_p(business_name) if business_name else "Your Business", st["title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Service Business", st["subtitle"]))

    cover_lines = [
        f"Prepared for: {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"Team size: {safe_p(team_size) if team_size else 'Not specified'}",
        f"Leads/week: {safe_p(leads_per_week) if leads_per_week else 'Not specified'}",
        f"Jobs/week: {safe_p(jobs_per_week) if jobs_per_week else 'Not specified'}",
        f"Response time: {safe_p(lead_response_time) if lead_response_time else 'Not specified'}",
    ]
    story.append(_card_table("Snapshot", cover_lines, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False, extra_padding=2))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Where you’re leaking time + money, and the fastest wins to fix it.", st["body"]))

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)

    story.append(Spacer(1, 6))
    story.append(Paragraph("Workload Snapshot", st["h1"]))

    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st, compact=True))

        ratio = None
        if leads_n > 0:
            ratio = int(round((jobs_n / leads_n) * 100))

        insights = []
        if ratio is not None:
            insights.append(f"Close rate around {ratio}%.")
        if clean_value(lead_response_time).lower().startswith(("immediate", "instant")):
            insights.append("Fast response is a real advantage.")
        insights.append("Biggest ROI: follow-ups, payroll, paperwork.")
        insights = _shorten_list(insights, max_items=3, max_words=9, max_chars=60)

        story.append(CondPageBreak(_estimate_card_height(5, st, extra_padding=2) + 26))
        story.append(_card_table("At a glance", insights, st, bg=st["CARD_BG"], placeholder_if_empty=False, extra_padding=2))
    else:
        story.append(CondPageBreak(_estimate_card_height(3, st, extra_padding=2) + 26))
        story.append(_card_table("At a glance", ["Add leads/week + jobs/week to unlock visuals."], st, bg=st["CARD_BG"], placeholder_if_empty=False, extra_padding=2))

    story.append(PageBreak())

    # ------------------- EXEC SUMMARY -------------------
    story.append(Paragraph("Executive Summary", st["h1"]))

    sec1_lines = _extract_section_lines(blueprint_text, 1)
    sec2_lines = _extract_section_lines(blueprint_text, 2)

    # IMPORTANT: remove “SECTION X:” from card titles
    sec1_items = _shorten_list([_strip_bullet_prefix(x) for x in sec1_lines], max_items=10)
    _add_card_no_split(
        story,
        "Quick Snapshot",
        sec1_items,
        st,
        bg=st["CARD_BG"],
        max_bullets=7,
        extra_padding=4,
        spacer_after=14,
    )

    if sec2_lines:
        sec2_blocks = _group_subsections(sec2_lines)
        alt = True
        for title, items in sec2_blocks:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]
            short_items = _shorten_list(items, max_items=9)
            _add_card_no_split(
                story,
                title,  # no “SECTION 2: …”
                short_items,
                st,
                bg=bg,
                max_bullets=7,
                extra_padding=4,
                spacer_after=14,
            )
            alt = not alt
    else:
        _add_card_no_split(
            story,
            "What You Told Me",
            ["(No details found)"],
            st,
            bg=st["CARD_BG_ALT"],
            max_bullets=6,
            extra_padding=4,
            spacer_after=14,
        )

    story.append(PageBreak())

    # ------------------- HOW THIS BECOMES A WORKING SYSTEM (FIXED) -------------------
    # Replaces the awkward floating sentence from BP10.
    story.append(Paragraph("How this becomes a working system", st["h1"]))
    story.append(Paragraph("Plain-English view of what changes once this is installed.", st["small"]))
    story.append(Spacer(1, 6))

    # These cards were already in your BP10 — keep them, but make them “designed” and spaced better.
    replaces = [
        "Manual texting, emailing, and chasing leads.",
        "Paper forms, photos, and scattered job notes.",
        "Manual payroll checks and time tracking.",
        "Back-and-forth scheduling and staff updates.",
    ]
    day_to_day = [
        "New lead triggers texts until booked or closed.",
        "You see missed calls, leads, and pipeline daily.",
        "Team gets schedules, reminders, updates automatically.",
        "Payroll prep is ready with approvals and alerts.",
        "Weekly snapshot shows what to fix next.",
    ]
    auto = [
        "Lead follow-ups, reminders, and no-show nudges.",
        "Confirmations, reschedules, and review requests.",
        "Staff reminders and schedule notifications.",
        "Payroll prep, time logs, and approval alerts.",
        "Simple reporting: leads, jobs, response time.",
    ]
    human = [
        "Pricing, quoting, and final customer decisions.",
        "Complex objections and special situations.",
        "Quality control, training, and leadership.",
        "High-value upsells and relationship building.",
    ]

    # Use chunking so cards never split. Add extra padding to reduce bottom whitespace.
    _add_card_no_split(story, "What this replaces", replaces, st, bg=st["CARD_BG"], max_bullets=6, extra_padding=6, spacer_after=16)
    _add_card_no_split(story, "What this looks like day-to-day", day_to_day, st, bg=st["CARD_BG_ALT"], max_bullets=6, extra_padding=6, spacer_after=16)
    _add_card_no_split(story, "What we automate", auto, st, bg=st["CARD_BG"], max_bullets=6, extra_padding=6, spacer_after=16)
    _add_card_no_split(story, "What stays human", human, st, bg=st["CARD_BG_ALT"], max_bullets=6, extra_padding=6, spacer_after=12)

    story.append(PageBreak())

    # ------------------- METRICS & VISUALS -------------------
    story.append(Paragraph("Key Metrics & Visuals", st["h1"]))
    story.append(Paragraph("Generated from the numbers you submitted.", st["small"]))
    story.append(Spacer(1, 8))

    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st))
        story.append(Spacer(1, 10))
    else:
        story.append(Paragraph("Leads/jobs numbers weren’t clear, so that chart was skipped.", st["small"]))
        story.append(Spacer(1, 10))

    rt = clean_value(lead_response_time).lower()
    if rt:
        labels = ["Immediate", "5m", "15m", "1h", "4h", "24h"]
        conv = [85, 75, 60, 45, 30, 15]
        if "immediate" in rt or "instant" in rt:
            conv = [90, 78, 62, 48, 32, 16]
        elif "hour" in rt or "1h" in rt:
            conv = [70, 65, 55, 45, 32, 18]
        elif "day" in rt or "24" in rt:
            conv = [55, 50, 40, 30, 20, 10]
        story.append(_line_chart("Response Time vs Likely Conversion (estimated)", labels, conv, st))
        story.append(Spacer(1, 10))

    # Third chart for “more professional” density
    story.append(_hours_saved_chart(st))
    story.append(Spacer(1, 10))

    # Keep “What the numbers suggest” but make it a proper card and fill space
    insights = []
    if leads_n is not None and jobs_n is not None and leads_n > 0:
        insights.append(f"Workload is heavy: {jobs_n} jobs/week.")
        insights.append("Automation prevents missed follow-ups and delays.")
    if clean_value(lead_response_time).lower().startswith(("immediate", "instant")):
        insights.append("Fast response helps you win more jobs.")
    insights.append("Best ROI: follow-ups, payroll, scheduling, paperwork.")
    insights = _shorten_list(insights, max_items=6, max_words=10, max_chars=70)

    _add_card_no_split(
        story,
        "What the numbers suggest",
        insights,
        st,
        bg=st["CARD_BG_ALT"],
        max_bullets=7,
        placeholder_if_empty=False,
        extra_padding=4,
        spacer_after=8,
    )

    story.append(PageBreak())

    # ------------------- SECTION 3: FIXES -------------------
    story.append(Paragraph("Top 3 Automation Fixes", st["h1"]))

    sec3_lines = _extract_section_lines(blueprint_text, 3)
    fixes = _parse_fixes(sec3_lines)

    if not fixes:
        _add_card_no_split(story, "Automation Fixes", ["(No fixes found)"], st, bg=st["CARD_BG"], max_bullets=6, extra_padding=4, spacer_after=14)
    else:
        alt = True
        for fx in fixes[:3]:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]

            fixes_list = _shorten_list(fx.get("fixes", []), 8)
            does_list = _shorten_list(fx.get("does", []), 8)
            incl_list = _shorten_list(fx.get("included", []), 8)

            first_chunk = fixes_list[:7] if fixes_list else []
            est_combo = 80 + _estimate_card_height(max(2, len(first_chunk) + 1), st, extra_padding=3)
            story.append(CondPageBreak(est_combo + 20))

            header = _fix_header_bar(fx["title"], st)
            first_card = _card_table("What this fixes", first_chunk, st, bg=bg, placeholder_if_empty=True, extra_padding=3)
            story.append(KeepTogether([header, Spacer(1, 10), first_card, Spacer(1, 14)]))

            remaining = fixes_list[7:]
            if remaining:
                _add_card_no_split(story, "What this fixes", remaining, st, bg=bg, max_bullets=7, extra_padding=3, spacer_after=14)

            _add_card_no_split(story, "What this does for you", does_list, st, bg=bg, max_bullets=7, extra_padding=3, spacer_after=14)
            _add_card_no_split(story, "What’s included", incl_list, st, bg=bg, max_bullets=7, extra_padding=3, spacer_after=16)

            alt = not alt

    story.append(PageBreak())

    # ------------------- SECTION 4 -------------------
    story.append(Paragraph("Automation Scorecard", st["h1"]))
    sec4_lines = _extract_section_lines(blueprint_text, 4)
    sec4_items = _shorten_list([_strip_bullet_prefix(x) for x in sec4_lines], max_items=12)

    _add_card_no_split(story, "Scorecard (0–100)", sec4_items, st, bg=st["CARD_BG_ALT"], max_bullets=7, extra_padding=5, spacer_after=14)

    story.append(PageBreak())

    # ------------------- SECTION 5 (FORCED 2 WEEKS PER PAGE) -------------------
    story.append(Paragraph("30-Day Action Plan", st["h1"]))
    sec5_lines = _extract_section_lines(blueprint_text, 5)
    week_blocks = _parse_week_blocks(sec5_lines)
    week_blocks = week_blocks[:4] if week_blocks else []

    if not week_blocks:
        _add_card_no_split(story, "30-Day Plan", ["(No week plan found)"], st, bg=st["CARD_BG"], max_bullets=6, extra_padding=6, spacer_after=14)
    else:
        pair1 = week_blocks[:2]
        pair2 = week_blocks[2:4]

        alt = True
        for title, items in pair1:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]
            items = _shorten_list(items, 3, max_words=9, max_chars=65)
            _add_card_no_split(
                story,
                title,
                items,
                st,
                bg=bg,
                max_bullets=3,
                week=True,
                extra_padding=10,     # bigger padding to “fill” the page
                spacer_after=18,
            )
            alt = not alt

        story.append(PageBreak())

        alt = True
        for title, items in pair2:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]
            items = _shorten_list(items, 3, max_words=9, max_chars=65)
            _add_card_no_split(
                story,
                title,
                items,
                st,
                bg=bg,
                max_bullets=3,
                week=True,
                extra_padding=10,
                spacer_after=18,
            )
            alt = not alt

    story.append(PageBreak())

    # ------------------- SECTION 6 -------------------
    story.append(Paragraph("Final Recommendations", st["h1"]))
    sec6_lines = _extract_section_lines(blueprint_text, 6)
    sec6_items = _shorten_list([_strip_bullet_prefix(x) for x in sec6_lines], max_items=12)
    _add_card_no_split(story, "Recommendations", sec6_items, st, bg=st["CARD_BG_ALT"], max_bullets=7, extra_padding=6, spacer_after=12)

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


# --------------------------------------------------------------------
# CONTEXT LOOKUP (DEBUG)
# --------------------------------------------------------------------
@app.route("/context", methods=["GET"])
def context_lookup_query():
    phone = clean_value(request.args.get("phone"))
    if not phone:
        return jsonify({"success": False, "error": "missing phone query parameter", "phone": phone}), 400

    ctx = get_context_for_phone(phone)
    if not ctx:
        return jsonify({"success": False, "error": "no context found for that phone", "phone": phone}), 404

    return jsonify({"success": True, "phone": phone, "context": ctx})


@app.route("/context/<phone>", methods=["GET"])
def context_lookup_path(phone: str):
    ctx = get_context_for_phone(phone)
    if not ctx:
        return jsonify({"success": False, "error": "no context found for that phone", "phone": phone}), 404
    return jsonify({"success": True, "phone": phone, "context": ctx})


# --------------------------------------------------------------------
# /run – BLUEPRINT GENERATION
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    t0 = time.time()
    data = request.get_json(force=True) or {}

    contact = data.get("contact", {}) or data.get("contact_data", {}) or {}
    form_fields = (
        data.get("form_fields")
        or data.get("form")
        or data.get("form_submission", {}).get("form_fields")
        or {}
    )

    name = clean_value(
        contact.get("full_name")
        or contact.get("name")
        or contact.get("first_name")
        or contact.get("firstName")
    ) or "there"

    email = clean_value(contact.get("email"))
    phone_raw = clean_value(contact.get("phone") or contact.get("phone_number") or contact.get("phoneNumber"))
    phone_digits = normalize_phone(phone_raw)
    phone_e164 = to_e164(phone_digits)

    business_name = clean_value(form_fields.get("business_name") or form_fields.get("Business Name"))
    business_type = clean_value(form_fields.get("business_type") or form_fields.get("Business Type"))
    services_offered = clean_value(form_fields.get("services_offered") or form_fields.get("Services You Offer"))
    ideal_customer = clean_value(form_fields.get("ideal_customer") or form_fields.get("Ideal Customer"))
    bottlenecks = clean_value(form_fields.get("bottlenecks") or form_fields.get("Biggest Operational Bottlenecks"))
    manual_tasks = clean_value(form_fields.get("manual_tasks") or form_fields.get("Manual Tasks You Want Automated"))
    current_software = clean_value(form_fields.get("current_software") or form_fields.get("Software You Currently Use"))
    lead_response_time = clean_value(form_fields.get("lead_response_time") or form_fields.get("Average Lead Response Time"))
    leads_per_week = clean_value(form_fields.get("leads_per_week") or form_fields.get("Leads Per Week"))
    jobs_per_week = clean_value(form_fields.get("jobs_per_week") or form_fields.get("Jobs Per Week"))
    growth_goals = clean_value(
        form_fields.get("growth_goals")
        or form_fields.get("growth_goals_6_12_months")
        or form_fields.get("Growth Goals (6–12 months)")
    )
    frustrations = clean_value(form_fields.get("frustrations") or form_fields.get("What Frustrates You Most"))
    extra_notes = clean_value(form_fields.get("extra_notes") or form_fields.get("Anything Else We Should Know"))
    team_size = clean_value(
        form_fields.get("team_size")
        or form_fields.get("Number of Employees")
        or form_fields.get("number_of_employees")
    )

    # Prompt fallbacks
    bn = business_name or "Not specified"
    bt = business_type or "Not specified"
    so = services_offered or "Not specified"
    ic = ideal_customer or "Not specified"
    bo = bottlenecks or "Not specified"
    mt = manual_tasks or "Not specified"
    cs = current_software or "Not specified"
    lrt = lead_response_time or "Not specified"
    lpw = leads_per_week or "Not specified"
    jpw = jobs_per_week or "Not specified"
    gg = growth_goals or "Not specified"
    fr = frustrations or "Not specified"
    en = extra_notes or "Not specified"
    ts = team_size or "Not specified"

    source_json = {
        "contact": {
            "name": name,
            "email": email,
            "phone_raw": phone_raw,
            "phone_digits": phone_digits,
            "phone_e164": phone_e164,
        },
        "form_fields": form_fields,
    }
    raw_json = json.dumps(source_json, indent=2, ensure_ascii=False)

    prompt = f"""
You are a senior automation consultant writing a professional client blueprint.

STYLE RULES
- Simple business language. No tech jargon.
- Speak to the owner as "you".
- NO mention of AI, prompts, JSON, or generation.
- BULLETS MUST BE SHORT: 8–10 words max per bullet.
- 1 sentence per bullet. No long explanations.
- Avoid filler words.
- Keep it skimmable on a phone screen.

OWNER INFO
- Owner name: {name}
- Business name: {bn}
- Business type: {bt}
- Services you offer: {so}
- Ideal customer: {ic}
- Biggest bottlenecks: {bo}
- Manual tasks to automate: {mt}
- Current software: {cs}
- Lead response time: {lrt}
- Leads per week: {lpw}
- Jobs per week: {jpw}
- Growth goals: {gg}
- Biggest frustration: {fr}
- Extra notes: {en}
- Team size: {ts}

SOURCE DATA (JSON)
{raw_json}

WRITE THE BLUEPRINT WITH THIS STRUCTURE:

Prepared for: {name}
Business: {bn}
Business type: {bt}

SECTION 1: Quick Snapshot
- Exactly 4–6 short bullets.

SECTION 2: What You Told Me
Your Goals:
- 3–4 bullets.
Your Challenges:
- 3–4 bullets.
Where Time Is Being Lost:
- 3–4 bullets.
Opportunities You’re Not Using Yet:
- 3–5 bullets.

SECTION 3: Your Top 3 Automation Fixes
FIX 1 – Title:
What This Fixes:
- 2–3 bullets.
What This Does For You:
- 2–3 bullets.
What’s Included:
- 3–4 bullets.
FIX 2 – Title:
(same structure)
FIX 3 – Title:
(same structure)

SECTION 4: Your Automation Scorecard (0–100)
- Include: "Score: __"
- 4–6 bullets.

SECTION 5: Your 30-Day Action Plan
Week 1 — ...
- 3 bullets.
Week 2 — ...
- 3 bullets.
Week 3 — ...
- 3 bullets.
Week 4 — ...
- 3 bullets.

SECTION 6: Final Recommendations
- 5–7 bullets.
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        blueprint_text = ""
        try:
            blueprint_text = response.output[0].content[0].text.strip()
        except Exception:
            blueprint_text = str(response)

        # Summary = up through Section 2
        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate PDF (V8)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        generate_pdf_v8(
            blueprint_text=blueprint_text,
            pdf_path=pdf_path,
            lead_name=name,
            business_name=business_name,
            business_type=business_type,
            team_size=team_size,
            leads_per_week=leads_per_week,
            jobs_per_week=jobs_per_week,
            lead_response_time=lead_response_time,
        )

        # Upload PDF to S3
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME env var is not set in Render")

        s3_key = f"blueprints/{pdf_filename}"
        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={"ContentType": "application/pdf", "ACL": "public-read"},
        )

        pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"

        # Store context
        context_blob = {
            "lead_name": name,
            "lead_email": email,
            "lead_phone_e164": phone_e164,
            "business_name": business_name,
            "business_type": business_type,
            "summary": summary_section,
            "pdf_url": pdf_url,
        }
        if phone_e164:
            store_context_for_phone(phone_e164, context_blob)
        elif phone_raw:
            store_context_for_phone(phone_raw, context_blob)

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,
                "summary": summary_section,
                "pdf_url": pdf_url,
                "name": name,
                "email": email,
                "phone_e164": phone_e164,
                "team_size": team_size,
                "seconds": round(time.time() - t0, 2),
            }
        )

    except Exception as e:
        print("Error generating blueprint:", repr(e), flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

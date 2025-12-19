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
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


# --------------------------------------------------------------------
# PDF DESIGN SYSTEM (PHONE-FRIENDLY + NO SPLIT CARDS)
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    # Palette
    NAVY = colors.HexColor("#0B1B2B")
    BLUE = colors.HexColor("#2563EB")
    BLUE_DK = colors.HexColor("#1E40AF")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    CARD_BG = colors.HexColor("#F5F7FB")
    CARD_BG_ALT = colors.HexColor("#EEF2FF")
    BORDER = colors.HexColor("#D7DEE8")
    SOFT = colors.HexColor("#E6ECF5")
    WHITE = colors.white

    # Bigger typography (iPhone readable)
    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=32,
        leading=36,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=8,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=15,
        leading=19,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=14,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=21,
        textColor=NAVY,
        spaceBefore=10,
        spaceAfter=6,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=17,
        textColor=SLATE,
        spaceBefore=2,
        spaceAfter=2,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=12.5,
        leading=17,
        textColor=colors.HexColor("#111827"),
        spaceAfter=3,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14,
        textColor=MUTED,
        spaceAfter=4,
    )

    pill = ParagraphStyle(
        "ApexPill",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=12.5,
        alignment=TA_CENTER,
        textColor=WHITE,
    )

    fix_header = ParagraphStyle(
        "FixHeader",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=WHITE,
        alignment=TA_LEFT,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "BLUE_DK": BLUE_DK,
        "SLATE": SLATE,
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
        "small": small,
        "pill": pill,
        "fix_header": fix_header,
    }


def _header_footer(canvas, doc):
    st = _brand_styles()
    canvas.saveState()
    w, h = letter

    # Header
    canvas.setStrokeColor(st["SOFT"])
    canvas.setLineWidth(1)
    canvas.line(48, h - 44, w - 48, h - 44)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(48, h - 36, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 48, h - 36, time.strftime("%b %d, %Y"))

    # Footer
    canvas.setStrokeColor(st["SOFT"])
    canvas.line(48, 44, w - 48, 44)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(48, 32, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 48, 32, f"Page {doc.page}")

    canvas.restoreState()


def _estimate_card_height(num_lines: int, st) -> float:
    """
    Rough height estimate to decide if we should pagebreak before a card.
    This doesn't have to be perfect — just conservative.
    """
    # title line + padding + each bullet line
    line_h = float(st["body"].leading)  # ~17
    title_h = float(st["h2"].leading)   # ~17
    padding = 12 + 12 + 10  # top+bottom+pseudo spacing
    return title_h + (num_lines * line_h) + padding


def _card_table(title: str, bullets: List[str], st, bg=None, placeholder_if_empty: bool = True) -> Table:
    """
    Stable card: each bullet is its own row so it can render reliably.
    We STILL enforce 'no split across pages' by chunking bullets and KeepTogether.
    """
    bg_color = bg if bg is not None else st["CARD_BG"]
    rows: List[List[Any]] = [[Paragraph(f"<b>{safe_p(title)}</b>", st["h2"])]]

    clean_bullets = [clean_value(b) for b in bullets if clean_value(b)]
    if not clean_bullets and placeholder_if_empty:
        rows.append([Paragraph("No details provided.", st["body"])])
    else:
        for b in clean_bullets:
            rows.append([Paragraph("• " + safe_p(b), st["body"])])

    tbl = Table(rows, colWidths=[7.35 * inch], hAlign="LEFT")
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg_color),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
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
    max_bullets: int = 9,
    placeholder_if_empty: bool = True,
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
        # Conservative page-break if not enough room
        est_h = _estimate_card_height(max(1, len(chunk) + 1), st)
        story.append(CondPageBreak(est_h + 24))

        card = _card_table(t, chunk, st, bg=bg, placeholder_if_empty=placeholder_if_empty)
        story.append(KeepTogether([card, Spacer(1, 10)]))


def _fix_header_bar(title: str, st) -> Table:
    """
    A colored header bar for FIX titles (prevents the 'No details provided' issue).
    """
    tbl = Table([[Paragraph(safe_p(title), st["fix_header"])]], colWidths=[7.35 * inch])
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


def _mini_kpi_row(items: List[str], st) -> Table:
    chips = []
    for it in items[:3]:
        t = Table([[Paragraph(safe_p(it), st["pill"])]], colWidths=[2.3 * inch])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), st["BLUE"]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        chips.append(t)
    return Table([chips], hAlign="CENTER")


def _bar_chart(title: str, labels: List[str], values: List[int], st) -> Drawing:
    d = Drawing(460, 215)
    d.add(String(0, 195, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 35
    bc.width = 380
    bc.height = 135
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
    # IMPORTANT: y-values only (NOT tuples) — prevents tuple/int errors
    d = Drawing(460, 215)
    d.add(String(0, 195, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    lc = HorizontalLineChart()
    lc.x = 40
    lc.y = 35
    lc.width = 380
    lc.height = 135

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
# PDF GENERATION (V5 PRO + NO SPLIT BUBBLES)
# --------------------------------------------------------------------
def generate_pdf_v5(
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

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Automation Blueprint",
        author="Apex Automation",
        leftMargin=48,
        rightMargin=48,
        topMargin=58,
        bottomMargin=58,
    )

    story: List[Any] = []

    # ------------------- COVER -------------------
    story.append(Spacer(1, 52))
    story.append(Paragraph(safe_p(business_name) if business_name else "Your Business", st["title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Service Business", st["subtitle"]))

    cover_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Team size:</b> {safe_p(team_size) if team_size else 'Not specified'}",
        f"<b>Leads/week:</b> {safe_p(leads_per_week) if leads_per_week else 'Not specified'}",
        f"<b>Jobs/week:</b> {safe_p(jobs_per_week) if jobs_per_week else 'Not specified'}",
        f"<b>Response time:</b> {safe_p(lead_response_time) if lead_response_time else 'Not specified'}",
    ]
    story.append(_card_table("Snapshot", cover_lines, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "This blueprint shows where your business is leaking time and money — and the fastest automation wins to fix it over the next 30 days.",
            st["body"],
        )
    )

    # Cover chart (no overlap, stacked)
    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    story.append(Spacer(1, 12))
    story.append(Paragraph("Workload Snapshot", st["h1"]))
    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st))
    else:
        chips = []
        if clean_value(leads_per_week):
            chips.append(f"Leads/week: {leads_per_week}")
        if clean_value(jobs_per_week):
            chips.append(f"Jobs/week: {jobs_per_week}")
        if clean_value(lead_response_time):
            chips.append(f"Response: {lead_response_time}")
        if chips:
            story.append(_mini_kpi_row(chips, st))

    story.append(PageBreak())

    # ------------------- EXEC SUMMARY -------------------
    story.append(Paragraph("Executive Summary", st["h1"]))

    sec1_lines = _extract_section_lines(blueprint_text, 1)
    sec2_lines = _extract_section_lines(blueprint_text, 2)

    sec1_items = [_strip_bullet_prefix(x) for x in sec1_lines if x]
    _add_card_no_split(
        story,
        "SECTION 1: Quick Snapshot",
        sec1_items[:18],
        st,
        bg=st["CARD_BG"],
        max_bullets=9,
        placeholder_if_empty=True,
    )

    if sec2_lines:
        sec2_blocks = _group_subsections(sec2_lines)
        alt = True
        for title, items in sec2_blocks:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]
            _add_card_no_split(
                story,
                f"SECTION 2: {title}",
                items[:18],
                st,
                bg=bg,
                max_bullets=9,
                placeholder_if_empty=True,
            )
            alt = not alt
    else:
        _add_card_no_split(
            story,
            "SECTION 2: What You Told Me",
            ["(No details found)"],
            st,
            bg=st["CARD_BG_ALT"],
            max_bullets=8,
            placeholder_if_empty=True,
        )

    story.append(PageBreak())

    # ------------------- METRICS & VISUALS -------------------
    story.append(Paragraph("Key Metrics & Visuals", st["h1"]))
    story.append(Paragraph("Visuals are created from the numbers you submitted.", st["small"]))
    story.append(Spacer(1, 10))

    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st))
        story.append(Spacer(1, 18))
    else:
        story.append(Paragraph("Leads/jobs numbers were not provided clearly, so that chart was skipped.", st["small"]))
        story.append(Spacer(1, 12))

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

    story.append(PageBreak())

    # ------------------- SECTION 3: FIXES -------------------
    story.append(Paragraph("SECTION 3: Your Top 3 Automation Fixes", st["h1"]))
    sec3_lines = _extract_section_lines(blueprint_text, 3)
    fixes = _parse_fixes(sec3_lines)

    if not fixes:
        _add_card_no_split(
            story,
            "Automation Fixes",
            ["(No fixes found in SECTION 3)"],
            st,
            bg=st["CARD_BG"],
            max_bullets=8,
            placeholder_if_empty=True,
        )
    else:
        alt = True
        for fx in fixes[:3]:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]

            # FIX header bar (no placeholder, no empty card)
            story.append(CondPageBreak(120))
            story.append(KeepTogether([_fix_header_bar(fx["title"], st), Spacer(1, 10)]))

            _add_card_no_split(story, "What This Fixes", fx.get("fixes", [])[:18], st, bg=bg, max_bullets=9)
            _add_card_no_split(story, "What This Does For You", fx.get("does", [])[:18], st, bg=bg, max_bullets=9)
            _add_card_no_split(story, "What’s Included", fx.get("included", [])[:18], st, bg=bg, max_bullets=9)

            story.append(Spacer(1, 6))
            alt = not alt

    story.append(PageBreak())

    # ------------------- SECTION 4 -------------------
    story.append(Paragraph("SECTION 4: Automation Scorecard", st["h1"]))
    sec4_lines = _extract_section_lines(blueprint_text, 4)
    sec4_items = [_strip_bullet_prefix(x) for x in sec4_lines if x]
    _add_card_no_split(story, "Scorecard (0–100)", sec4_items[:22], st, bg=st["CARD_BG_ALT"], max_bullets=10)

    story.append(PageBreak())

    # ------------------- SECTION 5 -------------------
    story.append(Paragraph("SECTION 5: 30-Day Action Plan", st["h1"]))
    sec5_lines = _extract_section_lines(blueprint_text, 5)
    week_blocks = _parse_week_blocks(sec5_lines)

    if not week_blocks:
        _add_card_no_split(story, "30-Day Plan", ["(No week plan found in SECTION 5)"], st, bg=st["CARD_BG"], max_bullets=8)
    else:
        alt = True
        for title, items in week_blocks[:6]:
            bg = st["CARD_BG_ALT"] if alt else st["CARD_BG"]
            _add_card_no_split(story, title, items[:18], st, bg=bg, max_bullets=9)
            alt = not alt

    story.append(PageBreak())

    # ------------------- SECTION 6 -------------------
    story.append(Paragraph("SECTION 6: Final Recommendations", st["h1"]))
    sec6_lines = _extract_section_lines(blueprint_text, 6)
    sec6_items = [_strip_bullet_prefix(x) for x in sec6_lines if x]
    _add_card_no_split(story, "Recommendations", sec6_items[:26], st, bg=st["CARD_BG_ALT"], max_bullets=10)

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
You are APEX AI, a senior automation consultant who writes premium,
clear, confidence-building business blueprints for home-service owners.

STYLE RULES
- Use simple business language (no tech jargon).
- Sound calm, professional, and confident.
- Speak directly to the owner as "you" and "your business".
- Prefer short paragraphs and bullet points.
- Do NOT mention AI, prompts, JSON, or that this was generated.
- Do NOT scold the owner for missing information.
- Do NOT include "END OF BLUEPRINT".
- Start directly with the "Prepared for" line.

OWNER INFO (parsed fields)
- Owner name: {name}
- Business name: {bn}
- Business type: {bt}
- Services you offer: {so}
- Ideal customer: {ic}
- Biggest operational bottlenecks: {bo}
- Manual tasks you want automated: {mt}
- Current software: {cs}
- Average lead response time: {lrt}
- Leads per week: {lpw}
- Jobs per week: {jpw}
- Growth goals (6–12 months): {gg}
- What frustrates you most: {fr}
- Extra notes: {en}
- Team size / number of employees: {ts}

SOURCE DATA (JSON)
{raw_json}

NOW WRITE THE BLUEPRINT USING THIS EXACT STRUCTURE AND HEADINGS:

Prepared for: {name}
Business: {bn}
Business type: {bt}

SECTION 1: Quick Snapshot
- 4–6 bullets.

SECTION 2: What You Told Me
Your Goals:
- 3–5 bullets.
Your Challenges:
- 3–6 bullets.
Where Time Is Being Lost:
- 3–5 bullets.
Opportunities You’re Not Using Yet:
- 4–6 bullets.

SECTION 3: Your Top 3 Automation Fixes
FIX 1 – Title:
What This Fixes:
- 2–4 bullets.
What This Does For You:
- 3–4 bullets.
What’s Included:
- 3–5 bullets.
FIX 2 – Title:
(same structure)
FIX 3 – Title:
(same structure)

SECTION 4: Your Automation Scorecard (0–100)
- Score then 4–6 bullets.

SECTION 5: Your 30-Day Action Plan
Week 1 — Stabilize the Business
- 3–4 bullets.
Week 2 — Capture and Convert More Leads
- 3–4 bullets.
Week 3 — Improve Customer Experience
- 3–4 bullets.
Week 4 — Optimize and Prepare to Scale
- 3–4 bullets.

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

        # Generate PDF (V5)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        generate_pdf_v5(
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

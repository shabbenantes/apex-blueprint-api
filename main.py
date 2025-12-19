from flask import Flask, request, jsonify
import os
import uuid
import json
import re
import time
from typing import Dict, Any, Optional, List, Tuple

from openai import OpenAI
import boto3

# ReportLab
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    PageBreak,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib import colors
from reportlab.lib.units import inch

from reportlab.graphics.shapes import Drawing, Rect, String
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
CONTEXT_TTL_SECONDS = int(os.environ.get("CONTEXT_TTL_SECONDS", "86400"))  # 24h
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
    except:
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


def _clip(s: str, max_len: int) -> str:
    s = clean_value(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _clean_bullets(lines: List[str], max_items: int = 8, max_len_each: int = 150) -> List[str]:
    out: List[str] = []
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        # strip common bullet markers
        t = re.sub(r"^(?:•|-)\s+", "", t).strip()
        if not t:
            continue
        out.append(_clip(t, max_len_each))
        if len(out) >= max_items:
            break
    return out


# --------------------------------------------------------------------
# PDF V3 — CONSULTANT REPORT DESIGN
# --------------------------------------------------------------------
def _brand():
    styles = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    BG = colors.HexColor("#F4F7FB")
    BORDER = colors.HexColor("#E2E8F0")
    SOFT_BLUE_BG = colors.HexColor("#EEF2FF")
    SOFT_BLUE_BORDER = colors.HexColor("#C7D2FE")

    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=28,
        leading=32,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=10,
    )

    cover_sub = ParagraphStyle(
        "ApexCoverSub",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=8,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=NAVY,
        spaceBefore=10,
        spaceAfter=6,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=SLATE,
        spaceBefore=6,
        spaceAfter=4,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=MUTED,
        spaceAfter=4,
    )

    chip = ParagraphStyle(
        "ApexChip",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "SLATE": SLATE,
        "MUTED": MUTED,
        "BG": BG,
        "BORDER": BORDER,
        "SOFT_BLUE_BG": SOFT_BLUE_BG,
        "SOFT_BLUE_BORDER": SOFT_BLUE_BORDER,
        "title": title,
        "cover_sub": cover_sub,
        "h1": h1,
        "h2": h2,
        "body": body,
        "small": small,
        "chip": chip,
    }


def _header_footer(canvas, doc):
    st = _brand()
    w, h = doc.pagesize

    canvas.saveState()

    # header rule
    canvas.setStrokeColor(st["BORDER"])
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(54, h - 38, "Apex Automation — Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # footer rule
    canvas.setStrokeColor(st["BORDER"])
    canvas.line(54, 46, w - 54, 46)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(54, 34, "Confidential — prepared for the business owner listed in this report")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


def _card(title: str, bullets: List[str], st, width: float, accent_color=None, subtitle: str = "") -> Table:
    """
    Fixed-width card. Bullets are clipped/limited BEFORE calling this.
    Avoid KeepTogether to prevent LayoutError.
    """
    accent_color = accent_color or st["BLUE"]

    parts: List[Any] = []
    parts.append(Paragraph(safe_p(title), st["h2"]))
    if subtitle:
        parts.append(Paragraph(safe_p(subtitle), st["small"]))
        parts.append(Spacer(1, 3))

    for b in bullets:
        parts.append(Paragraph(f"• {safe_p(b)}", st["body"]))

    tbl = Table([[parts]], colWidths=[width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    # left accent bar
    accent = Table([[None]], colWidths=[4], rowHeights=[None])
    accent.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), accent_color),
                ("BOX", (0, 0), (-1, -1), 0, colors.transparent),
            ]
        )
    )

    wrap = Table([[accent, tbl]], colWidths=[6, width])
    wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return wrap


def _chip(text: str, st, width: float) -> Table:
    t = Table([[Paragraph(safe_p(text), st["chip"])]], colWidths=[width])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BLUE"]),
                ("BOX", (0, 0), (-1, -1), 0, colors.transparent),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def _bar_chart(title: str, labels: List[str], values: List[int], st, width_px: int, height_px: int) -> Drawing:
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    # defensive: ensure ints
    vals = [int(v) if v is not None else 0 for v in values]
    vmax = max(vals) if vals else 10
    vmax = max(int(vmax * 1.25), 10)

    d = Drawing(width_px, height_px)
    d.add(String(0, height_px - 14, title, fontName="Helvetica-Bold", fontSize=9, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 28
    bc.y = 18
    bc.height = height_px - 44
    bc.width = width_px - 40
    bc.data = [vals]
    bc.strokeColor = colors.transparent

    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = vmax
    bc.valueAxis.valueStep = max(int(vmax / 5), 1)

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 7
    bc.categoryAxis.labels.fillColor = MUTED

    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 7
    bc.valueAxis.labels.fillColor = MUTED

    bc.bars[0].fillColor = BLUE

    d.add(bc)
    return d


def _line_chart(title: str, labels: List[str], points: List[Tuple[int, int]], st, width_px: int, height_px: int) -> Drawing:
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    d = Drawing(width_px, height_px)
    d.add(String(0, height_px - 14, title, fontName="Helvetica-Bold", fontSize=9, fillColor=NAVY))

    lc = HorizontalLineChart()
    lc.x = 28
    lc.y = 18
    lc.height = height_px - 44
    lc.width = width_px - 40
    lc.data = [points]
    lc.joinedLines = 1
    lc.lines[0].strokeColor = BLUE
    lc.lines[0].strokeWidth = 2
    lc.lines.symbol = makeMarker("FilledCircle")
    lc.lines.symbol.size = 3

    lc.categoryAxis.categoryNames = labels
    lc.categoryAxis.labels.fontName = "Helvetica"
    lc.categoryAxis.labels.fontSize = 7
    lc.categoryAxis.labels.fillColor = MUTED

    lc.valueAxis.valueMin = 0
    lc.valueAxis.valueMax = 100
    lc.valueAxis.valueStep = 20
    lc.valueAxis.labels.fontName = "Helvetica"
    lc.valueAxis.labels.fontSize = 7
    lc.valueAxis.labels.fillColor = MUTED

    d.add(lc)
    return d


def _score_gauge(score: int, st, width_px: int = 460, height_px: int = 70) -> Drawing:
    score = max(0, min(100, int(score)))
    d = Drawing(width_px, height_px)

    # title
    d.add(String(0, height_px - 14, "Automation Scorecard", fontName="Helvetica-Bold", fontSize=9, fillColor=st["NAVY"]))

    # background bar
    bar_x, bar_y = 0, 22
    bar_w, bar_h = width_px, 14
    d.add(Rect(bar_x, bar_y, bar_w, bar_h, fillColor=colors.HexColor("#E5E7EB"), strokeColor=colors.transparent))

    # fill
    fill_w = int(bar_w * (score / 100.0))
    d.add(Rect(bar_x, bar_y, fill_w, bar_h, fillColor=st["BLUE"], strokeColor=colors.transparent))

    # label
    d.add(String(width_px, 4, f"Score: {score}/100", fontName="Helvetica-Bold", fontSize=10, fillColor=st["NAVY"], textAnchor="end"))
    return d


def _split_sections(blueprint_text: str) -> Dict[str, List[str]]:
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    sections: Dict[str, List[str]] = {}
    current = "ROOT"
    sections[current] = []

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("SECTION "):
            current = s.strip()
            sections[current] = []
            continue
        sections[current].append(s)
    return sections


def _extract_fix_blocks(blueprint_text: str) -> List[Dict[str, List[str]]]:
    """
    Extract FIX 1/2/3 blocks into structured dicts:
    title, fixes, does, includes
    """
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    fixes: List[Dict[str, List[str]]] = []
    current: Optional[Dict[str, List[str]]] = None
    mode = ""

    def push():
        nonlocal current
        if current:
            fixes.append(current)
        current = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()

        if up.startswith("FIX ") and "–" in s:
            push()
            current = {"title": [s], "fixes": [], "does": [], "includes": []}
            mode = ""
            continue

        if current is None:
            continue

        if up.startswith("WHAT THIS FIXES"):
            mode = "fixes"
            continue
        if up.startswith("WHAT THIS DOES"):
            mode = "does"
            continue
        if up.startswith("WHAT’S INCLUDED") or up.startswith("WHAT'S INCLUDED"):
            mode = "includes"
            continue
        if up.startswith("FIX "):
            # a new fix starts
            push()
            current = {"title": [s], "fixes": [], "does": [], "includes": []}
            mode = ""
            continue

        # collect bullets
        if mode in {"fixes", "does", "includes"}:
            current[mode].append(s)

    push()
    return fixes[:3]


def _extract_section2_quads(section2_lines: List[str]) -> Dict[str, List[str]]:
    """
    Pull Goals / Challenges / Time Lost / Opportunities from Section 2
    """
    buckets = {
        "Your Goals": [],
        "Your Challenges": [],
        "Where Time Is Being Lost": [],
        "Opportunities You’re Not Using Yet": [],
    }
    current = None
    for ln in section2_lines:
        s = ln.strip()
        if not s:
            continue
        up = s.lower()
        if up.startswith("your goals"):
            current = "Your Goals"
            continue
        if up.startswith("your challenges"):
            current = "Your Challenges"
            continue
        if up.startswith("where time is being lost"):
            current = "Where Time Is Being Lost"
            continue
        if up.startswith("opportunities you"):
            current = "Opportunities You’re Not Using Yet"
            continue
        if current:
            buckets[current].append(s)
    return buckets


def _heuristic_score(leads_per_week: str, jobs_per_week: str, response_time: str, manual_tasks: str) -> int:
    # conservative scoring just to ensure we always have a number
    score = 55
    lpw = parse_int(leads_per_week)
    jpw = parse_int(jobs_per_week)
    if lpw is not None and jpw is not None and lpw > 0:
        close_rate = jpw / max(lpw, 1)
        if close_rate >= 0.6:
            score += 10
        elif close_rate >= 0.35:
            score += 5
        else:
            score -= 5

    rt = clean_value(response_time).lower()
    if "immediate" in rt or "instan" in rt or rt == "0":
        score += 10
    elif "5" in rt or "10" in rt or "15" in rt:
        score += 6
    elif "hour" in rt or "1h" in rt:
        score += 0
    elif "day" in rt or "24" in rt:
        score -= 10

    mt = len([x for x in re.split(r"[\n•]+", clean_value(manual_tasks)) if x.strip()])
    if mt >= 6:
        score -= 5
    elif mt <= 2 and mt > 0:
        score += 3

    return max(0, min(100, int(score)))


def generate_pdf_consultant_v3(
    blueprint_text: str,
    pdf_path: str,
    lead_name: str,
    business_name: str,
    business_type: str,
    team_size: str,
    leads_per_week: str,
    jobs_per_week: str,
    lead_response_time: str,
    bottlenecks: str,
    manual_tasks: str,
):
    st = _brand()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="Automation Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=60,
        bottomMargin=60,
    )

    story: List[Any] = []
    sections = _split_sections(blueprint_text)
    fixes = _extract_fix_blocks(blueprint_text)

    # -----------------------
    # PAGE 1 — COVER
    # -----------------------
    story.append(Spacer(1, 80))

    bn = business_name.strip() if business_name.strip() else "Your Business"
    bt = business_type.strip() if business_type.strip() else "Service Business"
    owner = lead_name.strip() if lead_name.strip() else "Business Owner"

    story.append(Paragraph(safe_p(bn), st["title"]))
    story.append(Paragraph(f"{safe_p(bt)} • 30-Day Automation Roadmap", st["cover_sub"]))
    story.append(Spacer(1, 14))

    cover_block = Table(
        [[
            Paragraph(f"<b>Prepared for:</b> {safe_p(owner)}", st["body"]),
            Paragraph(f"<b>Date:</b> {time.strftime('%b %d, %Y')}", st["body"]),
        ]],
        colWidths=[3.5 * inch, 3.5 * inch],
    )
    cover_block.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(cover_block)
    story.append(Spacer(1, 18))

    summary_line = (
        "This report summarizes the highest-impact automation opportunities for your business "
        "and lays out a clear 30-day implementation plan."
    )
    story.append(Paragraph(safe_p(summary_line), st["body"]))

    # small “signature” bar
    story.append(Spacer(1, 200))
    sig = Table(
        [[
            Paragraph("<b>Apex Automation</b>", st["body"]),
            Paragraph("Automation Strategy & Implementation", st["small"]),
        ]],
        colWidths=[7.0 * inch],
    )
    sig.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["SOFT_BLUE_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["SOFT_BLUE_BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(sig)

    story.append(PageBreak())

    # -----------------------
    # PAGE 2 — EXEC SUMMARY DASHBOARD
    # -----------------------
    story.append(Paragraph("Executive Summary", st["h1"]))
    story.append(Paragraph("A concise snapshot of your current situation and the highest-impact next steps.", st["small"]))
    story.append(Spacer(1, 10))

    chips: List[str] = []
    if clean_value(leads_per_week):
        chips.append(f"Leads/week: {clean_value(leads_per_week)}")
    if clean_value(jobs_per_week):
        chips.append(f"Jobs/week: {clean_value(jobs_per_week)}")
    if clean_value(lead_response_time):
        chips.append(f"Response time: {clean_value(lead_response_time)}")

    if chips:
        chip_cells = [_chip(c, st, width=2.2 * inch) for c in chips[:3]]
        story.append(Table([chip_cells], colWidths=[2.2 * inch, 2.2 * inch, 2.2 * inch], hAlign="CENTER"))
        story.append(Spacer(1, 10))

    # Quick Snapshot card (from Section 1)
    sec1_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 1")), None)
    sec1_lines = sections.get(sec1_key, []) if sec1_key else []
    quick_items = _clean_bullets(sec1_lines, max_items=7)

    # Hero chart = Workload Snapshot (safe sizing, always fits right column)
    leads_n = parse_int(leads_per_week) or 0
    jobs_n = parse_int(jobs_per_week) or 0
    team_n = parse_int(team_size) or 0
    jobs_per_person = int(jobs_n / max(team_n, 1)) if (team_n and jobs_n) else 0

    hero_chart = _bar_chart(
        "Workload Snapshot",
        ["Leads", "Jobs", "Jobs/Person"],
        [leads_n, jobs_n, jobs_per_person],
        st,
        width_px=240,
        height_px=170,
    )

    left_w = 3.9 * inch
    right_w = 3.1 * inch

    left_card = _card("Quick Snapshot", quick_items or ["No snapshot bullets were generated for this submission."], st, width=left_w)
    right_box = Table([[hero_chart]], colWidths=[right_w])
    right_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    story.append(
        Table(
            [[left_card, right_box]],
            colWidths=[left_w + 6, right_w],
            hAlign="LEFT",
        )
    )
    story.append(Spacer(1, 12))

    # Top 3 fixes titles (small cards)
    fix_titles: List[str] = []
    for fx in fixes:
        t = fx.get("title", [""])[0]
        t = t.replace("FIX", "Fix").strip()
        fix_titles.append(t)

    if fix_titles:
        t1 = _card("Top Recommendation #1", [_clip(fix_titles[0], 110)], st, width=2.25 * inch, accent_color=st["BLUE"])
        t2 = _card("Top Recommendation #2", [_clip(fix_titles[1], 110)] if len(fix_titles) > 1 else ["—"], st, width=2.25 * inch, accent_color=st["BLUE"])
        t3 = _card("Top Recommendation #3", [_clip(fix_titles[2], 110)] if len(fix_titles) > 2 else ["—"], st, width=2.25 * inch, accent_color=st["BLUE"])
        story.append(Table([[t1, t2, t3]], colWidths=[2.35 * inch, 2.35 * inch, 2.35 * inch]))
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # -----------------------
    # PAGE 3 — WHAT YOU TOLD ME (4 cards)
    # -----------------------
    story.append(Paragraph("What You Told Me", st["h1"]))
    story.append(Paragraph("Your goals, constraints, and where the business is currently losing time.", st["small"]))
    story.append(Spacer(1, 10))

    sec2_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 2")), None)
    sec2_lines = sections.get(sec2_key, []) if sec2_key else []
    quads = _extract_section2_quads(sec2_lines)

    cw = 3.35 * inch
    c1 = _card("Your Goals", _clean_bullets(quads["Your Goals"], 6), st, width=cw)
    c2 = _card("Your Challenges", _clean_bullets(quads["Your Challenges"], 6), st, width=cw)
    c3 = _card("Where Time Is Being Lost", _clean_bullets(quads["Where Time Is Being Lost"], 6), st, width=cw)
    c4 = _card("Opportunities You’re Not Using Yet", _clean_bullets(quads["Opportunities You’re Not Using Yet"], 6), st, width=cw)

    story.append(Table([[c1, c2], [c3, c4]], colWidths=[cw + 6, cw + 6], hAlign="LEFT"))
    story.append(PageBreak())

    # -----------------------
    # PAGES 4–5 — TOP 3 FIXES (each gets its own page if needed)
    # -----------------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["h1"]))
    story.append(Paragraph("These are the highest-impact fixes based on your answers.", st["small"]))
    story.append(Spacer(1, 10))

    for idx, fx in enumerate(fixes):
        title_line = fx.get("title", ["Fix"])[0]
        story.append(Paragraph(safe_p(title_line), st["h2"]))
        story.append(Spacer(1, 6))

        left = []
        left += ["What this fixes:"] + _clean_bullets(fx.get("fixes", []), 4)
        left += [""] + ["What this does for you:"] + _clean_bullets(fx.get("does", []), 4)

        # Remove label lines from bullets visually: we’ll bold labels separately
        left_bullets = []
        for item in left:
            if item.endswith(":"):
                left_bullets.append(item)  # will be rendered as bullet, but clipped
            else:
                left_bullets.append(item)

        right_bullets = ["What’s included:"] + _clean_bullets(fx.get("includes", []), 7)

        left_card = _card("Impact & Outcome", left_bullets, st, width=3.25 * inch)
        right_card = _card("Implementation Checklist", right_bullets, st, width=3.25 * inch)

        # micro visual: response-time curve (small)
        labels = ["Now", "5m", "15m", "1h", "4h", "24h"]
        rt = clean_value(lead_response_time).lower()
        conv = [85, 75, 60, 45, 30, 15]
        if "immediate" in rt or "instan" in rt or rt == "0":
            conv = [90, 78, 62, 48, 32, 16]
        elif "day" in rt or "24" in rt:
            conv = [55, 50, 40, 30, 20, 10]
        pts = [(i, conv[i]) for i in range(len(labels))]
        micro = _line_chart("Conversion vs Response Speed (est.)", labels, pts, st, width_px=460, height_px=140)

        story.append(Table([[left_card, right_card]], colWidths=[3.45 * inch, 3.45 * inch]))
        story.append(Spacer(1, 10))
        story.append(micro)

        if idx < len(fixes) - 1:
            story.append(PageBreak())

    story.append(PageBreak())

    # -----------------------
    # SCORECARD PAGE
    # -----------------------
    story.append(Paragraph("Scorecard & Priorities", st["h1"]))
    story.append(Paragraph("A simple snapshot of readiness and the next best actions.", st["small"]))
    story.append(Spacer(1, 10))

    # Try to parse score from section 4; otherwise use heuristic.
    sec4_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 4")), None)
    sec4_lines = sections.get(sec4_key, []) if sec4_key else []
    score = None
    for ln in sec4_lines:
        m = re.search(r"(\d{1,3})\s*/\s*100", ln)
        if m:
            score = int(m.group(1))
            break
        m2 = re.search(r"\b(\d{1,3})\b", ln)
        if m2 and "score" in ln.lower():
            score = int(m2.group(1))
            break
    if score is None:
        score = _heuristic_score(leads_per_week, jobs_per_week, lead_response_time, manual_tasks)

    story.append(_score_gauge(score, st, width_px=460, height_px=70))
    story.append(Spacer(1, 12))

    priorities = [
        "Prioritize response speed and follow-up consistency (fast wins).",
        "Automate lead intake → booking → confirmation to reduce drop-off.",
        "Remove manual admin tasks that steal hours from revenue work.",
        "Track weekly leads, jobs, and response time so improvements are measurable.",
    ]
    story.append(_card("Priority Recommendations", priorities, st, width=7.0 * inch))
    story.append(PageBreak())

    # -----------------------
    # 30-DAY ACTION PLAN
    # -----------------------
    story.append(Paragraph("30-Day Action Plan", st["h1"]))
    story.append(Paragraph("A practical week-by-week implementation roadmap.", st["small"]))
    story.append(Spacer(1, 10))

    sec5_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 5")), None)
    sec5_lines = sections.get(sec5_key, []) if sec5_key else []
    # split weeks
    weeks: Dict[str, List[str]] = {}
    current_week = None
    for ln in sec5_lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("WEEK "):
            current_week = s
            weeks[current_week] = []
            continue
        if current_week:
            weeks[current_week].append(s)

    # 4 week cards
    wk_keys = list(weeks.keys())[:4]
    while len(wk_keys) < 4:
        wk_keys.append(f"Week {len(wk_keys)+1}")

    wkw = 3.35 * inch
    wk_cards = []
    for wk in wk_keys:
        items = _clean_bullets(weeks.get(wk, []), 4)
        if not items:
            items = ["(No tasks listed for this week in the generated text.)"]
        wk_cards.append(_card(wk, items, st, width=wkw))

    story.append(Table([[wk_cards[0], wk_cards[1]], [wk_cards[2], wk_cards[3]]], colWidths=[wkw + 6, wkw + 6]))
    story.append(PageBreak())

    # -----------------------
    # FINAL RECOMMENDATIONS
    # -----------------------
    story.append(Paragraph("Final Recommendations", st["h1"]))
    story.append(Paragraph("The bottom-line actions that will produce the biggest ROI first.", st["small"]))
    story.append(Spacer(1, 10))

    sec6_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 6")), None)
    sec6_lines = sections.get(sec6_key, []) if sec6_key else []
    final_items = _clean_bullets(sec6_lines, 10)

    if not final_items:
        final_items = [
            "Implement the top fixes in order — speed-to-lead is the first lever.",
            "Standardize intake, follow-up, and booking workflows so the business runs consistently.",
            "Track a weekly KPI snapshot (leads, jobs, response time) to stay in control.",
        ]

    story.append(_card("Recommendations", final_items, st, width=7.0 * inch))
    story.append(Spacer(1, 14))

    cta = Table(
        [[
            Paragraph("<b>Next Step:</b> Book your strategy call to review this report and choose the fastest wins.", st["body"]),
        ]],
        colWidths=[7.0 * inch],
    )
    cta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["SOFT_BLUE_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["SOFT_BLUE_BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(cta)

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


# --------------------------------------------------------------------
# CONTEXT LOOKUP (GHL / DEBUG)
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
    print("Incoming payload keys:", list(data.keys()), flush=True)

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
        t_ai = time.time()
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        full_text = response.output[0].content[0].text.strip()
        print("OpenAI seconds:", round(time.time() - t_ai, 2), "chars:", len(full_text), flush=True)

        blueprint_text = full_text

        # Summary = up through Section 2
        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate PDF (Consultant V3)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        t_pdf = time.time()
        generate_pdf_consultant_v3(
            blueprint_text=blueprint_text,
            pdf_path=pdf_path,
            lead_name=name,
            business_name=business_name,
            business_type=business_type,
            team_size=team_size,
            leads_per_week=leads_per_week,
            jobs_per_week=jobs_per_week,
            lead_response_time=lead_response_time,
            bottlenecks=bottlenecks,
            manual_tasks=manual_tasks,
        )
        print("PDF seconds:", round(time.time() - t_pdf, 2), flush=True)

        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME env var is not set in Render")

        s3_key = f"blueprints/{pdf_filename}"
        t_s3 = time.time()
        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={"ContentType": "application/pdf", "ACL": "public-read"},
        )
        print("S3 seconds:", round(time.time() - t_s3, 2), flush=True)

        pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"
        print("Generated PDF URL:", pdf_url, flush=True)

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

        print("TOTAL /run seconds:", round(time.time() - t0, 2), flush=True)

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
            }
        )

    except Exception as e:
        print("Error generating blueprint:", repr(e), flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


# --------------------------------------------------------------------
# Legacy /pdf route (not used now)
# --------------------------------------------------------------------
@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    return "PDFs are now stored on S3.", 410


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

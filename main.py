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
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.lib.units import inch

from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart

app = Flask(__name__)

# ---------- OpenAI ----------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ---------- S3 CONFIG ----------
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-2")
s3_client = boto3.client("s3", region_name=S3_REGION)

# ---------- Context store (in-memory) ----------
# NOTE: resets if Render restarts/redeploys.
CONTEXT_TTL_SECONDS = int(os.environ.get("CONTEXT_TTL_SECONDS", "86400"))  # 24h default
_CONTEXT_BY_PHONE: Dict[str, Dict[str, Any]] = {}  # key: normalized digits, value: {context..., expires_at}


# --------------------------------------------------------------------
# HELPERS (cleaning + context)
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
    # accept "90", "about 90", "90-100", "90 leads"
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


def clamp_text(s: str, max_len: int = 180) -> str:
    t = clean_value(s)
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def split_bullets(text: str) -> List[str]:
    t = clean_value(text)
    if not t:
        return []
    # split on newlines / bullets / hyphens
    parts = re.split(r"[\n•]+", t)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("-"):
            p = p[1:].strip()
        if not p:
            continue
        out.append(p)
    # light dedupe
    seen = set()
    final = []
    for x in out:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(x)
    return final


# --------------------------------------------------------------------
# PDF ARCHITECTURE: Consultant Blueprint (Option A)
# --------------------------------------------------------------------
def _styles():
    base = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    BG = colors.HexColor("#F6F8FC")
    BORDER = colors.HexColor("#E2E8F0")
    SOFT_BLUE = colors.HexColor("#EEF2FF")
    SOFT_BLUE_BORDER = colors.HexColor("#C7D2FE")

    # Bigger, more readable typography (requested)
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=30,
        leading=34,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=8,
    )

    cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        parent=base["Heading2"],
        fontName="Helvetica",
        fontSize=13,
        leading=18,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=18,
    )

    page_title = ParagraphStyle(
        "PageTitle",
        parent=base["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=NAVY,
        spaceAfter=8,
    )

    section_title = ParagraphStyle(
        "SectionTitle",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=NAVY,
        spaceBefore=6,
        spaceAfter=6,
    )

    subhead = ParagraphStyle(
        "Subhead",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=SLATE,
        spaceBefore=6,
        spaceAfter=4,
    )

    body = ParagraphStyle(
        "Body",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )

    small = ParagraphStyle(
        "Small",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=MUTED,
        spaceAfter=4,
    )

    metric_label = ParagraphStyle(
        "MetricLabel",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=12,
        textColor=MUTED,
        spaceAfter=1,
    )

    metric_value = ParagraphStyle(
        "MetricValue",
        parent=base["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
        textColor=NAVY,
        spaceAfter=0,
    )

    pill = ParagraphStyle(
        "Pill",
        parent=base["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.white,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "SLATE": SLATE,
        "MUTED": MUTED,
        "BG": BG,
        "BORDER": BORDER,
        "SOFT_BLUE": SOFT_BLUE,
        "SOFT_BLUE_BORDER": SOFT_BLUE_BORDER,
        "cover_title": cover_title,
        "cover_subtitle": cover_subtitle,
        "page_title": page_title,
        "section_title": section_title,
        "subhead": subhead,
        "body": body,
        "small": small,
        "metric_label": metric_label,
        "metric_value": metric_value,
        "pill": pill,
    }


def _header_footer(canvas, doc):
    st = _styles()
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    w, h = doc.pagesize  # SAFE: doc.pagesize is a tuple, we unpack it here

    canvas.saveState()

    # top rule
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(NAVY)
    canvas.drawString(54, h - 38, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # footer rule
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.line(54, 46, w - 54, 46)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(54, 34, "Confidential — prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


def _metric_tile(label: str, value: str, st) -> Table:
    """Small fixed-height tile that cannot explode the layout."""
    value = clamp_text(value, 24)
    label = clamp_text(label, 40)

    inner = [
        Paragraph(safe_p(label), st["metric_label"]),
        Paragraph(safe_p(value), st["metric_value"]),
    ]
    t = Table([[inner]], colWidths=[3.05 * inch])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return t


def _card(title: str, bullets: List[str], st, max_items: int = 8) -> Table:
    """Card with safe max bullets + truncation to avoid LayoutError."""
    items = []
    for b in bullets[:max_items]:
        items.append(Paragraph("• " + safe_p(clamp_text(b, 170)), st["body"]))

    if not items:
        items = [Paragraph("• Not specified.", st["body"])]

    content = [Paragraph(safe_p(title), st["section_title"]), Spacer(1, 4)] + items
    tbl = Table([[content]], colWidths=[7.0 * inch])
    tbl.setStyle(
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
    return tbl


def _bar_chart(title: str, labels: List[str], values: List[int], st, width=420, height=180) -> Drawing:
    """Simple, stable bar chart (no line chart to avoid tuple-related surprises)."""
    d = Drawing(width, height)
    d.add(String(0, height - 14, title, fontName="Helvetica-Bold", fontSize=10, fillColor=st["NAVY"]))

    # normalize
    safe_vals = [int(v) if isinstance(v, (int, float)) else 0 for v in values]
    vmax = max(safe_vals) if safe_vals else 0
    vmax = max(int(vmax * 1.25), 10)

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 22
    bc.width = width - 55
    bc.height = height - 55
    bc.data = [safe_vals]
    bc.strokeColor = colors.transparent
    bc.bars[0].fillColor = st["BLUE"]

    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = vmax
    bc.valueAxis.valueStep = max(int(vmax / 5), 1)
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = st["MUTED"]

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = st["MUTED"]

    d.add(bc)
    return d


def _extract_section_lines(blueprint_text: str, section_prefix: str) -> List[str]:
    """
    Extract lines for a section like 'SECTION 1' or 'SECTION 6'
    until the next 'SECTION ' heading.
    """
    lines = blueprint_text.splitlines()
    out: List[str] = []
    in_sec = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("SECTION "):
            if in_sec:
                # stop when next section begins
                break
            if s.upper().startswith(section_prefix.upper()):
                in_sec = True
                continue
        if in_sec:
            # skip obvious subheaders but keep bullets
            out.append(s)
    return out


def _only_bullets(lines: List[str]) -> List[str]:
    bullets: List[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("- "):
            bullets.append(s[2:].strip())
        elif s.startswith("• "):
            bullets.append(s[2:].strip())
        elif s.startswith("-"):
            bullets.append(s[1:].strip())
    # If none, keep short lines
    if not bullets:
        for ln in lines:
            if len(ln) <= 160 and not ln.upper().startswith("YOUR ") and not ln.upper().startswith("DATA"):
                bullets.append(ln.strip())
    return [b for b in bullets if b]


def _derive_top_problems(bottlenecks: str, frustrations: str, manual_tasks: str) -> List[str]:
    pool = split_bullets(bottlenecks) + split_bullets(frustrations) + split_bullets(manual_tasks)
    # fallback if empty
    if not pool:
        return [
            "Follow-up and customer communication are inconsistent.",
            "Manual admin work is taking time away from revenue.",
            "Scheduling and internal handoffs aren’t standardized.",
        ]
    # pick first 6, then choose top 3
    trimmed = [clamp_text(x, 160) for x in pool[:10] if x]
    # ensure 3
    while len(trimmed) < 3:
        trimmed.append("Operational workload is higher than it should be due to manual processes.")
    return trimmed[:3]


def _extract_fix_blocks(blueprint_text: str) -> List[Tuple[str, List[str]]]:
    """
    Extract FIX blocks. Returns list of (fix_title_line, bullet_lines) for up to 3 fixes.
    """
    lines = blueprint_text.splitlines()
    fixes: List[Tuple[str, List[str]]] = []
    current_title = ""
    current_lines: List[str] = []
    in_fix = False

    def flush():
        nonlocal current_title, current_lines, fixes
        if current_title:
            bullets = _only_bullets(current_lines)
            fixes.append((current_title, bullets))
        current_title = ""
        current_lines = []

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()

        if up.startswith("SECTION "):
            if in_fix:
                flush()
                in_fix = False
            continue

        if up.startswith("FIX "):
            if in_fix:
                flush()
            in_fix = True
            current_title = s
            current_lines = []
            continue

        if in_fix:
            # stop if a new FIX begins handled above
            current_lines.append(s)

    if in_fix:
        flush()

    return fixes[:3]


def _extract_weeks(blueprint_text: str) -> Dict[str, List[str]]:
    """
    Extract Week 1..4 bullets under SECTION 5 if present.
    """
    sec5 = _extract_section_lines(blueprint_text, "SECTION 5")
    # crude parse inside section 5
    weeks = {"Week 1": [], "Week 2": [], "Week 3": [], "Week 4": []}
    current = ""
    for ln in sec5:
        s = ln.strip()
        up = s.upper()
        if up.startswith("WEEK 1"):
            current = "Week 1"
            continue
        if up.startswith("WEEK 2"):
            current = "Week 2"
            continue
        if up.startswith("WEEK 3"):
            current = "Week 3"
            continue
        if up.startswith("WEEK 4"):
            current = "Week 4"
            continue
        if current:
            if s.startswith("- "):
                weeks[current].append(s[2:].strip())
            elif s.startswith("• "):
                weeks[current].append(s[2:].strip())
            elif s.startswith("-"):
                weeks[current].append(s[1:].strip())
    # cap
    for k in weeks:
        weeks[k] = [clamp_text(x, 140) for x in weeks[k][:4]]
        if not weeks[k]:
            weeks[k] = ["Not specified."]
    return weeks


def generate_pdf_consultant(
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
    frustrations: str,
):
    """
    9-page Consultant Blueprint (Option A):
      1) Cover
      2) Executive Snapshot (tiles + ONE chart)
      3) Quick Snapshot (card)
      4) What You Told Me (card)
      5) Top 3 Problems (3 mini-cards)
      6) Opportunity Map (chart + takeaway)
      7) Top 3 Fixes (3 cards)
      8) 30-Day Action Plan (timeline table)
      9) Final Recommendations + Next Step (card)
    No appendix, no raw full dump.
    Built to avoid LayoutError by capping bullets + truncating.
    """
    st = _styles()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Automation Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=64,
        bottomMargin=64,
    )

    story: List[Any] = []

    # ---------------- PAGE 1: COVER (no header/footer) ----------------
    story.append(Spacer(1, 90))
    story.append(Paragraph(safe_p(business_name) if business_name else "AI Automation Blueprint", st["cover_title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Service Business", st["cover_subtitle"]))

    # Prepared block
    cover_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Business:</b> {safe_p(business_name) if business_name else 'Not specified'}",
        f"<b>Business type:</b> {safe_p(business_type) if business_type else 'Not specified'}",
        f"<b>Date:</b> {time.strftime('%b %d, %Y')}",
    ]
    cover_tbl = Table([[Paragraph("<br/>".join(cover_lines), st["body"])]], colWidths=[6.2 * inch])
    cover_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 18),
                ("RIGHTPADDING", (0, 0), (-1, -1), 18),
                ("TOPPADDING", (0, 0), (-1, -1), 16),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
            ]
        )
    )
    story.append(Spacer(1, 24))
    story.append(cover_tbl)
    story.append(Spacer(1, 28))
    story.append(Paragraph("Apex Automation", st["small"]))
    story.append(PageBreak())

    # Everything after cover uses header/footer
    # ---------------- PAGE 2: EXECUTIVE SNAPSHOT ----------------
    story.append(Paragraph("Executive Snapshot", st["page_title"]))
    story.append(Paragraph("A fast, 30-second view of what matters most from your submission.", st["small"]))
    story.append(Spacer(1, 8))

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    team_n = parse_int(team_size)

    # Metric tiles (left)
    tiles = []
    tiles.append(_metric_tile("Leads per week", leads_per_week or "Not specified", st))
    tiles.append(_metric_tile("Jobs per week", jobs_per_week or "Not specified", st))
    tiles.append(_metric_tile("Lead response time", lead_response_time or "Not specified", st))
    tiles.append(_metric_tile("Team size", team_size or "Not specified", st))

    tiles_tbl = Table([[tiles[0]], [tiles[1]], [tiles[2]], [tiles[3]]], colWidths=[3.2 * inch])
    tiles_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    # One chart (right): Workload Snapshot
    chart_cell: List[Any] = []
    if team_n and jobs_n:
        jpp = max(jobs_n / max(team_n, 1), 0)
        chart = _bar_chart(
            "Workload Snapshot",
            ["Team", "Jobs/wk", "Jobs/person"],
            [team_n, jobs_n, int(round(jpp))],
            st,
            width=300,
            height=210,
        )
        chart_cell = [chart]
    elif leads_n is not None and jobs_n is not None:
        chart = _bar_chart(
            "Leads vs Jobs (weekly)",
            ["Leads", "Jobs"],
            [leads_n, jobs_n],
            st,
            width=300,
            height=210,
        )
        chart_cell = [chart]
    else:
        chart_cell = [Paragraph("Not enough numeric data to generate the snapshot chart for this submission.", st["body"])]

    # Two-column layout: left tiles, right chart
    top_tbl = Table([[tiles_tbl, chart_cell]], colWidths=[3.3 * inch, 3.7 * inch])
    top_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(top_tbl)
    story.append(Spacer(1, 10))

    # Short takeaway box
    takeaway_text = (
        "This snapshot helps us prioritize the fastest wins first. "
        "Your full blueprint focuses on reducing manual workload, improving follow-up consistency, "
        "and creating a clear 30-day execution path."
    )
    takeaway = Table([[Paragraph(safe_p(takeaway_text), st["body"])]], colWidths=[7.0 * inch])
    takeaway.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["SOFT_BLUE"]),
                ("BOX", (0, 0), (-1, -1), 1, st["SOFT_BLUE_BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(takeaway)
    story.append(PageBreak())

    # ---------------- PAGE 3: QUICK SNAPSHOT CARD ----------------
    sec1_lines = _extract_section_lines(blueprint_text, "SECTION 1")
    sec1_bullets = _only_bullets(sec1_lines)
    story.append(_card("Quick Snapshot", sec1_bullets, st, max_items=8))
    story.append(PageBreak())

    # ---------------- PAGE 4: WHAT YOU TOLD ME CARD ----------------
    sec2_lines = _extract_section_lines(blueprint_text, "SECTION 2")
    sec2_bullets = _only_bullets(sec2_lines)
    story.append(_card("What You Told Me (Highlights)", sec2_bullets, st, max_items=10))
    story.append(PageBreak())

    # ---------------- PAGE 5: TOP 3 PROBLEMS ----------------
    story.append(Paragraph("Top 3 Problems to Solve First", st["page_title"]))
    story.append(Paragraph("These are the issues most likely to cost you time, money, and consistency.", st["small"]))
    story.append(Spacer(1, 10))

    probs = _derive_top_problems(bottlenecks=bottlenecks, frustrations=frustrations, manual_tasks=manual_tasks)
    prob_cards = []
    for i, p in enumerate(probs[:3], start=1):
        tbl = _card(f"Problem {i}", [p], st, max_items=1)
        prob_cards.append(tbl)

    story.append(prob_cards[0])
    story.append(prob_cards[1])
    story.append(prob_cards[2])
    story.append(PageBreak())

    # ---------------- PAGE 6: OPPORTUNITY MAP ----------------
    story.append(Paragraph("Automation Opportunity Map", st["page_title"]))
    story.append(Paragraph("A clear view of where automation can remove friction in your operations.", st["small"]))
    story.append(Spacer(1, 10))

    manual_count = len(split_bullets(manual_tasks))
    bottleneck_count = len(split_bullets(bottlenecks))
    # If empty, still show 0/0 chart (keeps structure consistent)
    chart = _bar_chart(
        "Operations Load Indicators (from your answers)",
        ["Manual tasks", "Bottlenecks"],
        [manual_count, bottleneck_count],
        st,
        width=460,
        height=220,
    )
    story.append(chart)
    story.append(Spacer(1, 10))

    opp_takeaway = (
        "If we reduce manual tasks and bottlenecks even by 30–50%, you regain hours each week "
        "and your business becomes easier to run — without hiring more admin help."
    )
    story.append(_card("Key Takeaway", [opp_takeaway], st, max_items=2))
    story.append(PageBreak())

    # ---------------- PAGE 7: TOP 3 FIXES ----------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["page_title"]))
    story.append(Paragraph("These fixes are designed for fast impact and simple implementation.", st["small"]))
    story.append(Spacer(1, 10))

    fixes = _extract_fix_blocks(blueprint_text)
    if not fixes:
        fixes = [
            ("FIX 1 — Follow-up & Booking System", ["Automate follow-ups, reminders, and booking handoffs."]),
            ("FIX 2 — Internal Operations Cleanup", ["Reduce paperwork and standardize daily workflows."]),
            ("FIX 3 — Visibility & Reporting", ["Track leads, jobs, and performance without manual updates."]),
        ]

    for title, bullets in fixes[:3]:
        # Make each fix card concise and readable
        nice_title = title.replace("FIX", "Fix").replace("–", "-")
        story.append(_card(nice_title, bullets, st, max_items=6))

    story.append(PageBreak())

    # ---------------- PAGE 8: 30-DAY ACTION PLAN (TIMELINE STYLE) ----------------
    story.append(Paragraph("30-Day Action Plan", st["page_title"]))
    story.append(Paragraph("A week-by-week plan to stabilize operations and start scaling.", st["small"]))
    story.append(Spacer(1, 10))

    weeks = _extract_weeks(blueprint_text)

    def week_cell(week_name: str, bullets: List[str]) -> Table:
        inner = [Paragraph(safe_p(week_name), st["subhead"]), Spacer(1, 2)]
        for b in bullets[:4]:
            inner.append(Paragraph("• " + safe_p(clamp_text(b, 120)), st["body"]))
        t = Table([[inner]], colWidths=[1.65 * inch])
        t.setStyle(
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
        return t

    row = [
        week_cell("Week 1", weeks["Week 1"]),
        week_cell("Week 2", weeks["Week 2"]),
        week_cell("Week 3", weeks["Week 3"]),
        week_cell("Week 4", weeks["Week 4"]),
    ]
    plan_tbl = Table([row], colWidths=[1.75 * inch] * 4)
    plan_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(plan_tbl)
    story.append(PageBreak())

    # ---------------- PAGE 9: FINAL RECOMMENDATIONS + NEXT STEP ----------------
    story.append(Paragraph("Final Recommendations", st["page_title"]))
    story.append(Paragraph("What to focus on first — and what to ignore for now.", st["small"]))
    story.append(Spacer(1, 10))

    sec6_lines = _extract_section_lines(blueprint_text, "SECTION 6")
    recs = _only_bullets(sec6_lines)
    if not recs:
        recs = [
            "Build the follow-up + booking flow first to capture more leads consistently.",
            "Standardize internal handoffs so work doesn’t rely on memory or sticky notes.",
            "Add simple reporting so you can see what’s working without manual tracking.",
            "Avoid overbuilding — start with the smallest system that removes the biggest friction.",
        ]

    story.append(_card("Recommendations", recs, st, max_items=7))

    next_step = (
        "Next Step: On your strategy call, we’ll review your blueprint, pick the fastest wins, "
        "and map your implementation plan. Come prepared with your current software logins and "
        "a clear picture of your lead flow."
    )
    story.append(_card("Next Step", [next_step], st, max_items=3))

    # BUILD
    doc.build(story, onFirstPage=None, onLaterPages=_header_footer)


# --------------------------------------------------------------------
# CONTEXT LOOKUP (BLAND / DEBUG)
# --------------------------------------------------------------------
@app.route("/context", methods=["GET"])
def context_lookup_query():
    """
    Fetch the saved context using a query param.
    Example: /context?phone=+14155551212
    """
    phone = clean_value(request.args.get("phone"))
    if not phone:
        return jsonify({"success": False, "error": "missing phone query parameter", "phone": phone}), 400

    ctx = get_context_for_phone(phone)
    if not ctx:
        return jsonify({"success": False, "error": "no context found for that phone", "phone": phone}), 404

    return jsonify({"success": True, "phone": phone, "context": ctx})


@app.route("/context/<phone>", methods=["GET"])
def context_lookup_path(phone: str):
    """
    Fetch saved context via path.
    Example: /context/+14155551212 or /context/14155551212
    """
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

    # Extract contact basics
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

    # Extract form fields
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

    # Fallback labels for the prompt
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

    # Keep source JSON small
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
clear, confidence-building business blueprints for service-business owners.

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

DATA (for internal use):
Data:
- business_name: {bn}
- business_type: {bt}
- team_size: {ts}
- leads_per_week: {lpw}
- jobs_per_week: {jpw}
- average_lead_response_time: {lrt}
- growth_goals: {gg}
- biggest_bottlenecks: {bo}
- manual_tasks: {mt}
- current_software: {cs}
- frustrations: {fr}
- extra_notes: {en}
"""

    try:
        # OpenAI call
        t_ai = time.time()
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        full_text = response.output[0].content[0].text.strip()
        print("OpenAI seconds:", round(time.time() - t_ai, 2), "chars:", len(full_text), flush=True)

        # Split out DATA block
        data_block = ""
        split_marker = "\nDATA (for internal use):"
        if split_marker in full_text:
            main_text, data_part = full_text.split(split_marker, 1)
            blueprint_text = main_text.strip()
            data_block = "DATA (for internal use):" + data_part
        else:
            blueprint_text = full_text

        # Summary = up through Section 2
        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate PDF (Consultant Option A)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        t_pdf = time.time()
        generate_pdf_consultant(
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
            frustrations=frustrations,
        )
        print("PDF seconds:", round(time.time() - t_pdf, 2), flush=True)

        # Upload PDF
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

        # Store context for lookup
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
                "data_block": data_block,
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

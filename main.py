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
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.lib.units import inch

from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
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


def safe_p(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_int(s: str) -> Optional[int]:
    s = clean_value(s)
    if not s:
        return None
    m = re.search(r"(\d{1,8})", s.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except:
        return None


def split_bullets(text: str) -> List[str]:
    t = clean_value(text)
    if not t:
        return []
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
        if "," in p and len(p) > 50:
            for c in [x.strip() for x in p.split(",") if x.strip()]:
                out.append(c)
        else:
            out.append(p)

    seen = set()
    final = []
    for x in out:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(x)
    return final[:12]


def cap_bullets(items: List[str], max_items: int, tail_note: str = "More details are included in the Appendix.") -> List[str]:
    """Prevents ReportLab LayoutError by ensuring cards never become taller than a page."""
    items = [clean_value(x) for x in items if clean_value(x)]
    if len(items) <= max_items:
        return items
    return items[: max_items - 1] + [tail_note]


# --------------------------------------------------------------------
# PDF 3.0 DESIGN SYSTEM
# --------------------------------------------------------------------
def _styles():
    base = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#111827")
    MUTED = colors.HexColor("#64748B")
    CARD_BG = colors.HexColor("#F4F7FB")
    BORDER = colors.HexColor("#E2E8F0")

    title = ParagraphStyle(
        "ApexTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=26,
        leading=30,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=6,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=15,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=10,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=NAVY,
        spaceBefore=6,
        spaceAfter=8,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#334155"),
        spaceBefore=4,
        spaceAfter=4,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=SLATE,
        spaceAfter=4,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=MUTED,
        spaceAfter=2,
    )

    pill = ParagraphStyle(
        "ApexPill",
        parent=base["BodyText"],
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
        "CARD_BG": CARD_BG,
        "BORDER": BORDER,
        "title": title,
        "subtitle": subtitle,
        "h1": h1,
        "h2": h2,
        "body": body,
        "small": small,
        "pill": pill,
    }


def _header_footer(canvas, doc):
    st = _styles()
    w, h = letter

    canvas.saveState()

    canvas.setStrokeColor(st["BORDER"])
    canvas.setLineWidth(1)
    canvas.line(54, h - 48, w - 54, h - 48)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(54, h - 40, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 54, h - 40, time.strftime("%b %d, %Y"))

    canvas.setStrokeColor(st["BORDER"])
    canvas.line(54, 48, w - 54, 48)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(54, 36, "Confidential — Prepared for the recipient on the cover page")
    canvas.drawRightString(w - 54, 36, f"Page {doc.page}")

    canvas.restoreState()


def _card(title: str, bullets: List[str], st, width: float) -> Table:
    elems: List[Any] = []
    if title:
        elems.append(Paragraph(safe_p(title), st["h2"]))
        elems.append(Spacer(1, 4))

    if not bullets:
        bullets = ["Not enough information provided in this section."]

    for b in bullets:
        elems.append(Paragraph("• " + safe_p(b), st["body"]))

    tbl = Table([[elems]], colWidths=[width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["CARD_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return tbl


def _pill_row(items: List[str], st) -> Table:
    pills: List[Any] = []
    for x in items[:4]:
        t = Table([[Paragraph(safe_p(x), st["pill"])]], colWidths=[1.6 * inch])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), st["BLUE"]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        pills.append(t)

    if not pills:
        pills.append(Spacer(1, 1))

    return Table([pills], hAlign="LEFT")


def _bar_chart(title: str, labels: List[str], values: List[int], st) -> Drawing:
    values = [int(v) for v in values if v is not None]
    if not values:
        values = [1]

    d = Drawing(260, 160)
    d.add(String(0, 145, title, fontName="Helvetica-Bold", fontSize=10, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 30
    bc.y = 20
    bc.height = 110
    bc.width = 220

    bc.data = [values]
    bc.strokeColor = colors.transparent
    bc.valueAxis.valueMin = 0

    vmax = max(values)
    bc.valueAxis.valueMax = max(int(vmax * 1.25), 10)
    bc.valueAxis.valueStep = max(int(bc.valueAxis.valueMax / 5), 1)

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = st["MUTED"]

    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = st["MUTED"]

    bc.bars[0].fillColor = st["BLUE"]

    d.add(bc)
    return d


def _line_chart(title: str, labels: List[str], yvals: List[int], st) -> Drawing:
    if not yvals:
        yvals = [10, 20, 30]

    d = Drawing(260, 160)
    d.add(String(0, 145, title, fontName="Helvetica-Bold", fontSize=10, fillColor=st["NAVY"]))

    lp = LinePlot()
    lp.x = 30
    lp.y = 28
    lp.height = 100
    lp.width = 220

    pts = [(i, int(yvals[i])) for i in range(len(yvals))]
    lp.data = [pts]

    lp.lines[0].strokeColor = st["BLUE"]
    lp.lines[0].strokeWidth = 2
    lp.lines.symbol = makeMarker("FilledCircle")
    lp.lines.symbol.size = 4

    lp.xValueAxis.valueMin = 0
    lp.xValueAxis.valueMax = max(len(yvals) - 1, 1)
    lp.xValueAxis.valueSteps = list(range(len(yvals)))

    lp.yValueAxis.valueMin = 0
    lp.yValueAxis.valueMax = 100
    lp.yValueAxis.valueStep = 20

    lp.xValueAxis.labels.fontName = "Helvetica"
    lp.xValueAxis.labels.fontSize = 7
    lp.xValueAxis.labels.fillColor = st["MUTED"]

    lp.yValueAxis.labels.fontName = "Helvetica"
    lp.yValueAxis.labels.fontSize = 8
    lp.yValueAxis.labels.fillColor = st["MUTED"]

    d.add(lp)

    if labels:
        for i, lab in enumerate(labels[: len(yvals)]):
            x = 30 + (220 * (i / max(len(yvals) - 1, 1)))
            d.add(String(x - 8, 10, lab, fontName="Helvetica", fontSize=7, fillColor=st["MUTED"]))

    return d


def _parse_sections(blueprint_text: str) -> Dict[str, List[str]]:
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    sections: Dict[str, List[str]] = {}
    current = "FULL"
    sections[current] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("SECTION "):
            current = s
            sections[current] = []
            continue
        sections[current].append(s)
    return sections


def _extract_fix_blocks(blueprint_text: str) -> List[Dict[str, List[str]]]:
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    fixes: List[Dict[str, List[str]]] = []
    cur: Optional[Dict[str, List[str]]] = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("FIX "):
            if cur:
                fixes.append(cur)
            cur = {"title": s, "bullets": []}
            continue
        if cur:
            if up.startswith("SECTION "):
                fixes.append(cur)
                cur = None
                continue
            if s.startswith("-"):
                cur["bullets"].append(s[1:].strip())
            elif s.startswith("•"):
                cur["bullets"].append(s[1:].strip())
            else:
                if len(s) <= 140 and not up.startswith("WHAT ") and not up.endswith(":"):
                    cur["bullets"].append(s)

    if cur:
        fixes.append(cur)

    return fixes[:3]


def _extract_weeks(blueprint_text: str) -> Dict[str, List[str]]:
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    weeks: Dict[str, List[str]] = {}
    current = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("WEEK "):
            current = s
            weeks[current] = []
            continue
        if current:
            if up.startswith("SECTION "):
                current = None
                continue
            if s.startswith("-"):
                weeks[current].append(s[1:].strip())
            elif s.startswith("•"):
                weeks[current].append(s[1:].strip())
            else:
                if len(s) <= 140 and not up.endswith(":"):
                    weeks[current].append(s)

    return weeks


def generate_pdf_v3(
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
    st = _styles()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Automation Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=60,
        bottomMargin=60,
    )

    story: List[Any] = []

    sections = _parse_sections(blueprint_text)
    fixes = _extract_fix_blocks(blueprint_text)
    weeks = _extract_weeks(blueprint_text)

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    team_n = parse_int(team_size)

    left_w = 3.35 * inch
    right_w = 3.35 * inch
    gutter = 0.3 * inch

    # ---------------- PAGE 1 — COVER ----------------
    story.append(Spacer(1, 16))
    story.append(Paragraph("Apex Automation", st["title"]))
    story.append(Paragraph("AI Automation Blueprint for Your Service Business", st["subtitle"]))
    story.append(Spacer(1, 8))

    prepared = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Business:</b> {safe_p(business_name) if business_name else 'Not specified'}",
        f"<b>Business type:</b> {safe_p(business_type) if business_type else 'Not specified'}",
    ]
    if clean_value(team_size):
        prepared.append(f"<b>Team size:</b> {safe_p(team_size)}")

    prepared_tbl = Table([[Paragraph("<br/>".join(prepared), st["body"])]], colWidths=[left_w])
    prepared_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["CARD_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    pills = []
    if clean_value(leads_per_week):
        pills.append(f"Leads/week: {clean_value(leads_per_week)}")
    if clean_value(jobs_per_week):
        pills.append(f"Jobs/week: {clean_value(jobs_per_week)}")
    if clean_value(lead_response_time):
        pills.append(f"Response: {clean_value(lead_response_time)}")
    if clean_value(team_size):
        pills.append(f"Team: {clean_value(team_size)}")

    pill_tbl = _pill_row(pills, st)

    hero = None
    if team_n is not None and jobs_n is not None:
        jpp = max(int(round(jobs_n / max(team_n, 1))), 0)
        hero = _bar_chart("Workload Snapshot", ["Team", "Jobs", "Jobs/person"], [team_n, jobs_n, jpp], st)
    elif leads_n is not None and jobs_n is not None:
        hero = _bar_chart("Leads vs Jobs (weekly)", ["Leads", "Jobs"], [leads_n, jobs_n], st)
    else:
        hero = _bar_chart("Submission Snapshot", ["Data"], [10], st)

    right_block = [hero, Spacer(1, 8),
                   Paragraph("What this is:", st["h2"]),
                   Paragraph(
                       "A clear 30-day automation plan built from your answers — designed to help you save time, respond faster, and convert more leads.",
                       st["body"],
                   )]

    left_block = [prepared_tbl, Spacer(1, 10), pill_tbl]

    top_grid = Table([[left_block, "", right_block]], colWidths=[left_w, gutter, right_w])
    top_grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(top_grid)
    story.append(Spacer(1, 10))

    sec1_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 1")), None)
    findings: List[str] = []
    if sec1_key:
        for ln in sections.get(sec1_key, []):
            s = ln.strip()
            if s.startswith("-"):
                findings.append(s[1:].strip())
            elif s.startswith("•"):
                findings.append(s[1:].strip())
            else:
                if len(s) <= 140:
                    findings.append(s)
    findings = cap_bullets(findings, 6)
    story.append(_card("Top Findings (Quick Snapshot)", findings, st, width=7.0 * inch))

    story.append(PageBreak())

    # ---------------- PAGE 2 — EXEC SUMMARY ----------------
    story.append(Paragraph("Executive Summary", st["h1"]))

    sec2_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 2")), None)

    sec1_bul = cap_bullets(findings, 6)
    sec2_bul: List[str] = []
    if sec2_key:
        for ln in sections.get(sec2_key, []):
            s = ln.strip()
            if s.startswith("-"):
                sec2_bul.append(s[1:].strip())
            elif s.startswith("•"):
                sec2_bul.append(s[1:].strip())
            else:
                if len(s) <= 140 and not s.lower().startswith("section"):
                    sec2_bul.append(s)

    sec2_bul = cap_bullets(sec2_bul, 7)

    left_card = _card("Quick Snapshot", sec1_bul, st, width=left_w)
    right_card = _card("What You Told Me (highlights)", sec2_bul, st, width=right_w)

    grid = Table([[left_card, "", right_card]], colWidths=[left_w, gutter, right_w])
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(grid)
    story.append(Spacer(1, 10))

    mini_left = None
    mini_right = None
    if leads_n is not None and jobs_n is not None:
        mini_left = _bar_chart("Leads vs Jobs", ["Leads", "Jobs"], [leads_n, jobs_n], st)
    if manual_tasks or bottlenecks:
        mcount = len(split_bullets(manual_tasks))
        bcount = len(split_bullets(bottlenecks))
        mini_right = _bar_chart("Ops Load (counts)", ["Manual", "Bottlenecks"], [mcount, bcount], st)

    if mini_left or mini_right:
        row = [mini_left if mini_left else Spacer(1, 1), "", mini_right if mini_right else Spacer(1, 1)]
        story.append(Table([row], colWidths=[left_w, gutter, right_w]))

    story.append(PageBreak())

    # ---------------- PAGE 3 — TOP 3 FIXES ----------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["h1"]))
    story.append(Paragraph("These are the fastest wins based on your submission.", st["small"]))
    story.append(Spacer(1, 6))

    fix_cards: List[Any] = []
    for fx in fixes:
        title = fx.get("title", "Fix")
        bullets = cap_bullets(fx.get("bullets", []), 7)
        fix_cards.append(_card(title, bullets, st, width=2.25 * inch))

    while len(fix_cards) < 3:
        fix_cards.append(_card("Fix", ["Not enough details were generated for this fix section."], st, width=2.25 * inch))

    fixes_tbl = Table([fix_cards], colWidths=[2.25 * inch, 2.25 * inch, 2.25 * inch])
    fixes_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(fixes_tbl)
    story.append(Spacer(1, 10))

    rt = clean_value(lead_response_time).lower()
    if rt:
        labels = ["Now", "5m", "15m", "1h", "4h", "24h"]
        curve = [85, 75, 60, 45, 30, 15]
        if "immediate" in rt or "instan" in rt:
            curve = [92, 80, 65, 50, 34, 18]
        elif "hour" in rt:
            curve = [72, 66, 58, 45, 30, 18]
        elif "day" in rt or "24" in rt:
            curve = [55, 50, 40, 30, 20, 10]
        story.append(_line_chart("Response Time vs Likely Conversion (est.)", labels, curve, st))

    story.append(PageBreak())

    # ---------------- PAGE 4 — 30-DAY ROADMAP ----------------
    story.append(Paragraph("Your 30-Day Action Plan", st["h1"]))
    story.append(Paragraph("Follow this week-by-week plan to stabilize operations and start scaling.", st["small"]))
    story.append(Spacer(1, 6))

    wk_keys = list(weeks.keys())

    def wk_num(k: str) -> int:
        m = re.search(r"WEEK\s+(\d+)", k.upper())
        return int(m.group(1)) if m else 999

    wk_keys.sort(key=wk_num)

    wk_cards = []
    for k in wk_keys[:4]:
        wk_cards.append(_card(k, cap_bullets(weeks.get(k, []), 6), st, width=left_w))

    while len(wk_cards) < 4:
        wk_cards.append(_card("Week", ["Not enough data generated for this week section."], st, width=left_w))

    row1 = Table([[wk_cards[0], "", wk_cards[1]]], colWidths=[left_w, gutter, right_w])
    row2 = Table([[wk_cards[2], "", wk_cards[3]]], colWidths=[left_w, gutter, right_w])
    row1.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    row2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(row1)
    story.append(Spacer(1, 10))
    story.append(row2)

    story.append(PageBreak())

    # ---------------- APPENDIX — FULL BLUEPRINT ----------------
    story.append(Paragraph("Appendix: Full Blueprint", st["h1"]))
    story.append(Paragraph("Below is the complete plan, preserved for reference.", st["small"]))
    story.append(Spacer(1, 8))

    for raw_line in blueprint_text.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 3))
            continue
        up = line.upper()

        if up.startswith("SECTION "):
            story.append(Spacer(1, 6))
            story.append(Paragraph(safe_p(line), st["h1"]))
            continue
        if up.startswith("FIX ") or up.startswith("WEEK ") or (len(line) <= 60 and line.endswith(":")):
            story.append(Spacer(1, 4))
            story.append(Paragraph(safe_p(line), st["h2"]))
            continue

        if line.startswith("- "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        elif line.startswith("• "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        else:
            story.append(Paragraph(safe_p(line), st["body"]))

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


# --------------------------------------------------------------------
# CONTEXT LOOKUP
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
Suggested Graph Views:
- Graph: Leads per Week vs Jobs per Week
- Graph: Response Time vs Likely Conversion
- Graph: Manual Tasks vs Automated Opportunities
- Graph: Team Size vs Workload

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

        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        t_pdf = time.time()
        generate_pdf_v3(
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


@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    return "PDFs are now stored on S3.", 410


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

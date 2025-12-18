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
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.lib.units import inch

from reportlab.graphics.shapes import Drawing, String, Rect
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
# NOTE: This resets if Render restarts/redeploys. Useful for immediate post-submit calls.
CONTEXT_TTL_SECONDS = int(os.environ.get("CONTEXT_TTL_SECONDS", "86400"))  # 24h default
_CONTEXT_BY_PHONE: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------
def clean_value(v: object) -> str:
    """Turn raw values into clean strings. Treat 'null', 'None', 'N/A', etc. as empty."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"null", "none", "n/a", "na"}:
        return ""
    return s


def normalize_phone(phone: str) -> str:
    """
    Normalize to digits only (no +). Examples:
      +1 (415) 555-1212 -> 14155551212
      415-555-1212 -> 4155551212 (if 10 digits, assume US and prefix 1)
    """
    p = clean_value(phone)
    digits = re.sub(r"\D+", "", p)
    if len(digits) == 10:
        digits = "1" + digits
    return digits


def to_e164(phone_digits: str) -> str:
    """Convert digits-only into E.164 like +14155551212."""
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
    """
    Pull a reasonable int from messy user input like:
    "90", "90 leads", "about 90", "90-100", "N/A"
    """
    s = clean_value(s)
    if not s:
        return None
    m = re.search(r"(\d{1,6})", s.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except:
        return None


def split_bullets(text: str) -> List[str]:
    """
    Turn a paragraph or user list into bullet-like items.
    """
    t = clean_value(text)
    if not t:
        return []
    parts = re.split(r"[\n•\-]+", t)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if "," in p and len(p) > 40:
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


def safe_p(s: str) -> str:
    """Escape minimal HTML-sensitive chars for ReportLab Paragraph."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------
# PDF V3: BRAND SYSTEM
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    LIGHT_BG = colors.HexColor("#F4F7FB")
    LIGHT_BG_2 = colors.HexColor("#F8FAFC")
    BORDER = colors.HexColor("#E2E8F0")
    CALL_OUT_BG = colors.HexColor("#EEF2FF")
    CALL_OUT_BORDER = colors.HexColor("#C7D2FE")

    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=28,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=6,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=14,
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
        spaceBefore=8,
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

    body_tight = ParagraphStyle(
        "ApexBodyTight",
        parent=body,
        spaceAfter=3,
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

    pill = ParagraphStyle(
        "ApexPill",
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
        "LIGHT_BG": LIGHT_BG,
        "LIGHT_BG_2": LIGHT_BG_2,
        "BORDER": BORDER,
        "CALL_OUT_BG": CALL_OUT_BG,
        "CALL_OUT_BORDER": CALL_OUT_BORDER,
        "title": title,
        "subtitle": subtitle,
        "h1": h1,
        "h2": h2,
        "body": body,
        "body_tight": body_tight,
        "small": small,
        "pill": pill,
    }


def _header_footer(canvas, doc):
    st = _brand_styles()
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]

    canvas.saveState()
    w, h = letter

    # Header line
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(NAVY)
    canvas.drawString(54, h - 38, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # Footer line
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.line(54, 46, w - 54, 46)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(54, 34, "Confidential — Prepared for the business owner listed on page 1")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


def _divider_line(st) -> Table:
    t = Table([[""]], colWidths=[7.0 * inch], rowHeights=[0.12 * inch])
    t.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return t


def _chips_row(chips: List[str], st) -> Table:
    cells = []
    for c in chips[:3]:
        t = Table([[Paragraph(safe_p(c), st["pill"])]], colWidths=[2.2 * inch])
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
        cells.append(t)
    return Table([cells], hAlign="CENTER")


def _section_card(title: str, items: List[str], st) -> KeepTogether:
    bullets = [Paragraph(f"• {safe_p(x)}", st["body"]) for x in items if clean_value(x)]
    inner = [Paragraph(safe_p(title), st["h1"]), Spacer(1, 4)] + bullets

    tbl = Table([[inner]], colWidths=[7.0 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["LIGHT_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return KeepTogether([tbl, Spacer(1, 10)])


def _soft_panel(title: str, bullets: List[str], st) -> KeepTogether:
    # Lighter “consultant panel” used inside FIX sections
    body = st["body_tight"]
    inner = [Paragraph(f"<b>{safe_p(title)}</b>", st["body"]), Spacer(1, 3)]
    inner += [Paragraph(f"• {safe_p(x)}", body) for x in bullets if clean_value(x)]

    tbl = Table([[inner]], colWidths=[7.0 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["LIGHT_BG_2"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return KeepTogether([tbl, Spacer(1, 8)])


def _callout(text: str, st) -> KeepTogether:
    tbl = Table([[Paragraph(safe_p(text), st["body"])]], colWidths=[7.0 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["CALL_OUT_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["CALL_OUT_BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return KeepTogether([tbl, Spacer(1, 10)])


# ---------------- Charts ----------------
def _simple_bar_chart(title: str, labels: List[str], values: List[int], st) -> Drawing:
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    # Guard: no empty
    if not values:
        values = [0]

    vmax = max(values) if values else 0
    vmax = max(int(vmax * 1.25) if vmax else 10, 10)
    step = max(int(vmax / 5), 1)

    d = Drawing(460, 180)
    d.add(String(0, 165, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 20
    bc.height = 120
    bc.width = 400

    bc.data = [values]
    bc.strokeColor = colors.transparent
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = vmax
    bc.valueAxis.valueStep = step

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = MUTED

    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = MUTED

    bc.bars[0].fillColor = BLUE
    d.add(bc)
    return d


def _simple_line_chart(title: str, labels: List[str], y_values: List[int], st) -> Drawing:
    """
    HorizontalLineChart expects y-values (not tuples). We'll use category labels for x.
    """
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    # Guard lengths
    if not y_values:
        y_values = [0] * max(len(labels), 1)
    if len(y_values) != len(labels):
        # pad/trim
        y_values = (y_values + [y_values[-1]] * len(labels))[: len(labels)]

    d = Drawing(460, 180)
    d.add(String(0, 165, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    lc = HorizontalLineChart()
    lc.x = 40
    lc.y = 25
    lc.height = 120
    lc.width = 400
    lc.data = [y_values]

    lc.joinedLines = 1
    lc.lines[0].strokeColor = BLUE
    lc.lines[0].strokeWidth = 2
    lc.lines.symbol = makeMarker("FilledCircle")
    lc.lines.symbol.size = 4

    lc.categoryAxis.categoryNames = labels
    lc.categoryAxis.labels.fontName = "Helvetica"
    lc.categoryAxis.labels.fontSize = 8
    lc.categoryAxis.labels.fillColor = MUTED

    lc.valueAxis.valueMin = 0
    lc.valueAxis.valueMax = 100
    lc.valueAxis.valueStep = 20
    lc.valueAxis.labels.fontName = "Helvetica"
    lc.valueAxis.labels.fontSize = 8
    lc.valueAxis.labels.fillColor = MUTED

    d.add(lc)
    return d


def _timeline_bar(st, active_range: str) -> Drawing:
    """
    Simple timeline visual for action plan pages.
    active_range: "1-2" or "3-4"
    """
    BLUE = st["BLUE"]
    BORDER = st["BORDER"]
    MUTED = st["MUTED"]
    NAVY = st["NAVY"]

    d = Drawing(460, 50)
    d.add(String(0, 36, "30-Day Roadmap", fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    x0 = 0
    y0 = 10
    w = 460
    h = 12

    # Background
    d.add(Rect(x0, y0, w, h, fillColor=colors.HexColor("#F1F5F9"), strokeColor=BORDER, strokeWidth=1))

    # Active segment
    if active_range == "1-2":
        d.add(Rect(x0, y0, w * 0.5, h, fillColor=BLUE, strokeColor=BLUE, strokeWidth=0))
    else:
        d.add(Rect(w * 0.5, y0, w * 0.5, h, fillColor=BLUE, strokeColor=BLUE, strokeWidth=0))

    d.add(String(0, 0, "Weeks 1–2", fontName="Helvetica", fontSize=9, fillColor=MUTED))
    d.add(String(380, 0, "Weeks 3–4", fontName="Helvetica", fontSize=9, fillColor=MUTED))

    return d


# --------------------------------------------------------------------
# Blueprint parsing helpers
# --------------------------------------------------------------------
def _parse_blueprint_sections(blueprint_text: str) -> Dict[str, List[str]]:
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


def _extract_week_blocks(section5_lines: List[str]) -> Dict[str, List[str]]:
    """
    From Section 5 lines, extract bullets under Week 1-4.
    """
    out = {"Week 1": [], "Week 2": [], "Week 3": [], "Week 4": []}
    current = None
    for ln in section5_lines:
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
                out[current].append(s[2:].strip())
            elif s.startswith("• "):
                out[current].append(s[2:].strip())
            else:
                # keep short helpful lines
                if len(s) <= 140 and not s.lower().startswith("section"):
                    out[current].append(s)

    # Trim
    for k in out:
        out[k] = [x for x in out[k] if clean_value(x)][:10]
    return out


def _build_fix_panels(blueprint_text: str) -> List[Tuple[str, Dict[str, List[str]]]]:
    """
    Parse SECTION 3 fix blocks into panels:
    Returns list of (fix_title, {panel_title: [bullets]})
    Works on your consistent blueprint structure.
    """
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    in_section3 = False
    fixes: List[Tuple[str, Dict[str, List[str]]]] = []

    current_fix_title = ""
    current_panels: Dict[str, List[str]] = {}
    current_panel = ""

    def commit_fix():
        nonlocal current_fix_title, current_panels, current_panel
        if current_fix_title:
            # cleanup panel bullets
            for k in list(current_panels.keys()):
                current_panels[k] = [x for x in current_panels[k] if clean_value(x)][:10]
                if not current_panels[k]:
                    current_panels.pop(k, None)
            fixes.append((current_fix_title, current_panels))
        current_fix_title = ""
        current_panels = {}
        current_panel = ""

    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        up = s.upper()
        if up.startswith("SECTION 3"):
            in_section3 = True
            continue
        if in_section3 and up.startswith("SECTION 4"):
            break

        if not in_section3:
            continue

        # New FIX title
        if up.startswith("FIX "):
            # commit previous
            commit_fix()
            current_fix_title = s
            continue

        # Panel headings inside fix
        if s.endswith(":") and len(s) <= 40:
            current_panel = s.rstrip(":").strip()
            current_panels.setdefault(current_panel, [])
            continue

        # bullets
        if s.startswith("- "):
            bullet = s[2:].strip()
            if current_panel:
                current_panels.setdefault(current_panel, []).append(bullet)
        elif s.startswith("• "):
            bullet = s[2:].strip()
            if current_panel:
                current_panels.setdefault(current_panel, []).append(bullet)
        else:
            # sometimes they write short lines; treat as bullet if a panel is active
            if current_panel and len(s) <= 160:
                current_panels.setdefault(current_panel, []).append(s)

    # commit last
    commit_fix()
    return fixes[:3]


# --------------------------------------------------------------------
# PDF V3 generator
# --------------------------------------------------------------------
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
    st = _brand_styles()

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
    sections = _parse_blueprint_sections(blueprint_text)

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    team_n = parse_int(team_size)

    # ------------------------------------------------------------
    # COVER PAGE (NOW WITH A VISUAL HOOK)
    # ------------------------------------------------------------
    story.append(Spacer(1, 14))
    story.append(Paragraph("Apex Automation", st["title"]))
    story.append(Paragraph("AI Automation Blueprint for Your Service Business", st["subtitle"]))

    prepared_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Business:</b> {safe_p(business_name) if business_name else 'Not specified'}",
        f"<b>Business type:</b> {safe_p(business_type) if business_type else 'Not specified'}",
    ]
    if clean_value(team_size):
        prepared_lines.append(f"<b>Team size:</b> {safe_p(team_size)}")

    prepared_tbl = Table([[Paragraph("<br/>".join(prepared_lines), st["body"])]], colWidths=[7.0 * inch])
    prepared_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["LIGHT_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(Spacer(1, 8))
    story.append(prepared_tbl)
    story.append(Spacer(1, 10))

    story.append(
        Paragraph(
            "This blueprint shows where your business is currently leaking time and money, "
            "and the simplest automation wins to fix it over the next 30 days.",
            st["body"],
        )
    )
    story.append(Spacer(1, 8))

    chips = []
    if clean_value(leads_per_week):
        chips.append(f"Leads/week: {safe_p(leads_per_week)}")
    if clean_value(jobs_per_week):
        chips.append(f"Jobs/week: {safe_p(jobs_per_week)}")
    if clean_value(lead_response_time):
        chips.append(f"Response time: {safe_p(lead_response_time)}")

    if chips:
        story.append(_chips_row(chips, st))
        story.append(Spacer(1, 10))

    # Visual hook: Workload snapshot on cover
    if team_n and jobs_n:
        jobs_per_person = max(jobs_n / max(team_n, 1), 0)
        d = _simple_bar_chart(
            "Workload Snapshot (based on your answers)",
            ["Team", "Jobs/wk", "Jobs/person"],
            [team_n, jobs_n, int(round(jobs_per_person))],
            st,
        )
        story.append(KeepTogether([d, Spacer(1, 6)]))
        story.append(Paragraph("Tip: If ‘Jobs/person’ feels high, automation is usually your fastest relief valve.", st["small"]))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # EXECUTIVE SUMMARY (CARDS) + 1 SMALL CHART INLINE
    # ------------------------------------------------------------
    # Section 1 card
    sec1_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 1")), None)
    if sec1_key:
        sec1_lines = sections.get(sec1_key, [])
        sec1_items = []
        for ln in sec1_lines:
            if ln.startswith("-"):
                sec1_items.append(ln[1:].strip())
            elif ln.startswith("•"):
                sec1_items.append(ln[1:].strip())
            elif len(ln) <= 140:
                sec1_items.append(ln)
        sec1_items = [x for x in sec1_items if x][:8]
        if sec1_items:
            story.append(_section_card("Quick Snapshot", sec1_items, st))

    # Section 2 card
    sec2_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 2")), None)
    if sec2_key:
        sec2_lines = sections.get(sec2_key, [])
        items = []
        for ln in sec2_lines:
            if ln.startswith("-"):
                items.append(ln[1:].strip())
            elif ln.startswith("•"):
                items.append(ln[1:].strip())
            elif len(ln) <= 140 and not ln.lower().startswith("section"):
                items.append(ln)
        items = [x for x in items if x][:10]
        if items:
            story.append(_section_card("What You Told Me (highlights)", items, st))

    # Inline small chart: Response-time curve if we have response time text
    rt = clean_value(lead_response_time).lower()
    if rt:
        labels = ["Immediate", "5m", "15m", "1h", "4h", "24h"]
        conv = [85, 75, 60, 45, 30, 15]
        if "immediate" in rt or "instan" in rt or rt == "0":
            conv = [90, 78, 62, 48, 32, 16]
        elif "hour" in rt or "1h" in rt:
            conv = [70, 65, 55, 45, 32, 18]
        elif "day" in rt or "24" in rt:
            conv = [55, 50, 40, 30, 20, 10]

        d = _simple_line_chart(
            "Response Time vs Likely Conversion (estimated)",
            labels,
            conv,
            st,
        )
        story.append(KeepTogether([d, Spacer(1, 6)]))
        story.append(Paragraph("This is a visual estimate to make the tradeoff easy to see.", st["small"]))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # SECTION 3 (TOP 3 FIXES) — NOW AS PROFESSIONAL PANELS
    # ------------------------------------------------------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["h1"]))
    story.append(Paragraph("These are the highest-leverage changes based on your submission.", st["small"]))
    story.append(Spacer(1, 8))

    fixes = _build_fix_panels(blueprint_text)
    if not fixes:
        story.append(Paragraph("No FIX blocks detected in the blueprint text.", st["body"]))
    else:
        for i, (fix_title, panels) in enumerate(fixes, start=1):
            story.append(Paragraph(safe_p(fix_title), st["h1"]))
            story.append(_divider_line(st))
            story.append(Spacer(1, 6))

            # Panels in a consistent order if present
            for key in ["What This Fixes", "What This Does For You", "What’s Included", "What's Included", "Whats Included"]:
                if key in panels:
                    story.append(_soft_panel(key, panels[key], st))

            # If panels keys differ, include remaining
            for k in panels.keys():
                if k in {"What This Fixes", "What This Does For You", "What’s Included", "What's Included", "Whats Included"}:
                    continue
                story.append(_soft_panel(k, panels[k], st))

            # Add one contextual chart after FIX 1 only (prevents "chart museum")
            if i == 1:
                manual_count = len(split_bullets(manual_tasks))
                bottleneck_count = len(split_bullets(bottlenecks))
                if manual_count or bottleneck_count:
                    d = _simple_bar_chart(
                        "Operations Load Indicators (counted from your answers)",
                        ["Manual tasks", "Bottlenecks"],
                        [manual_count, bottleneck_count],
                        st,
                    )
                    story.append(KeepTogether([d, Spacer(1, 6)]))

            story.append(Spacer(1, 8))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # SECTION 5 ACTION PLAN — SPLIT INTO 2 PAGES WITH TIMELINE
    # ------------------------------------------------------------
    sec5_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 5")), None)
    sec5_lines = sections.get(sec5_key, []) if sec5_key else []
    weeks = _extract_week_blocks(sec5_lines) if sec5_lines else {}

    # Weeks 1–2 page
    story.append(Paragraph("Your 30-Day Action Plan (Weeks 1–2)", st["h1"]))
    story.append(KeepTogether([_timeline_bar(st, "1-2"), Spacer(1, 10)]))

    w1 = weeks.get("Week 1", []) if weeks else []
    w2 = weeks.get("Week 2", []) if weeks else []
    if w1:
        story.append(_section_card("Week 1 — Stabilize the Business", w1, st))
    if w2:
        story.append(_section_card("Week 2 — Capture and Convert More Leads", w2, st))

    if not w1 and not w2:
        story.append(Paragraph("No Week 1–2 bullets detected. The blueprint text may have changed format.", st["body"]))

    story.append(PageBreak())

    # Weeks 3–4 page
    story.append(Paragraph("Your 30-Day Action Plan (Weeks 3–4)", st["h1"]))
    story.append(KeepTogether([_timeline_bar(st, "3-4"), Spacer(1, 10)]))

    w3 = weeks.get("Week 3", []) if weeks else []
    w4 = weeks.get("Week 4", []) if weeks else []
    if w3:
        story.append(_section_card("Week 3 — Improve Customer Experience", w3, st))
    if w4:
        story.append(_section_card("Week 4 — Optimize and Prepare to Scale", w4, st))

    if not w3 and not w4:
        story.append(Paragraph("No Week 3–4 bullets detected. The blueprint text may have changed format.", st["body"]))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # FULL BLUEPRINT (REFERENCE SECTION) — BETTER VISUAL RHYTHM
    # ------------------------------------------------------------
    story.append(Paragraph("Full Blueprint (reference)", st["h1"]))
    story.append(Paragraph("Below is the complete plan, exactly as generated for this submission.", st["small"]))
    story.append(Spacer(1, 8))

    # Lightweight callout early
    story.append(_callout("Use this section as a reference while you implement the 30-day plan.", st))

    for raw_line in blueprint_text.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        upper = line.upper()

        if upper.startswith("SECTION "):
            story.append(Spacer(1, 6))
            story.append(Paragraph(safe_p(line), st["h1"]))
            story.append(_divider_line(st))
            story.append(Spacer(1, 6))
            continue

        if upper.startswith("FIX ") or upper.startswith("WEEK "):
            story.append(Spacer(1, 5))
            story.append(Paragraph(safe_p(line), st["h2"]))
            continue

        if len(line) <= 60 and line.endswith(":"):
            story.append(Paragraph(safe_p(line), st["h2"]))
            continue

        if line.startswith("- "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        elif line.startswith("• "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        else:
            story.append(Paragraph(safe_p(line), st["body"]))

    story.append(Spacer(1, 14))
    story.append(
        _callout(
            "<b>Next Step:</b> Book your strategy call and we’ll prioritize the fastest wins based on your blueprint.",
            st,
        )
    )

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


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
    """
    Called by GoHighLevel on form submit.
    Generates blueprint + summary + PDF URL, returns JSON.
    Also stores a small context blob keyed by the lead phone number for later lookup.
    """
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

        # Generate PDF (V3)
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

        # Store context for later lookup (keyed by best phone we have)
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


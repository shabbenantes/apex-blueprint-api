from flask import Flask, request, jsonify
import os
import uuid
import json
import re
import time
import traceback
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
CONTEXT_TTL_SECONDS = int(os.environ.get("CONTEXT_TTL_SECONDS", "86400"))  # 24h default
_CONTEXT_BY_PHONE: Dict[str, Dict[str, Any]] = {}  # key: normalized phone digits (no +), value: {context..., expires_at}


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
    m = re.search(r"(\d{1,6})", s.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def split_bullets(text: str, limit: int = 12) -> List[str]:
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
        # light comma-splitting if it's a long run-on list
        if "," in p and len(p) > 60:
            for c in [x.strip() for x in p.split(",") if x.strip()]:
                out.append(c)
        else:
            out.append(p)
    # de-dup
    seen = set()
    final = []
    for x in out:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(x)
    return final[:limit]


def safe_p(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clamp_list(items: List[str], n: int) -> List[str]:
    items = [x.strip() for x in items if x and x.strip()]
    return items[:n]


# --------------------------------------------------------------------
# PDF V3 (OPTION A): CONSULTANT REPORT LAYOUT (REPORTLAB-SAFE)
# --------------------------------------------------------------------
def _brand_styles():
    base = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    LIGHT_BG = colors.HexColor("#F4F7FB")
    BORDER = colors.HexColor("#E2E8F0")
    SOFT_BLUE_BG = colors.HexColor("#EEF2FF")
    SOFT_BLUE_BORDER = colors.HexColor("#C7D2FE")

    title = ParagraphStyle(
        "ApexTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=28,
        leading=32,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=8,
    )

    cover_sub = ParagraphStyle(
        "ApexCoverSub",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=10,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=NAVY,
        spaceBefore=10,
        spaceAfter=6,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=SLATE,
        spaceBefore=6,
        spaceAfter=4,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=MUTED,
        spaceAfter=4,
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

    section_bar = ParagraphStyle(
        "ApexSectionBar",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=14,
        textColor=NAVY,
        spaceBefore=12,
        spaceAfter=8,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "SLATE": SLATE,
        "MUTED": MUTED,
        "LIGHT_BG": LIGHT_BG,
        "BORDER": BORDER,
        "SOFT_BLUE_BG": SOFT_BLUE_BG,
        "SOFT_BLUE_BORDER": SOFT_BLUE_BORDER,
        "title": title,
        "cover_sub": cover_sub,
        "h1": h1,
        "h2": h2,
        "body": body,
        "small": small,
        "pill": pill,
        "section_bar": section_bar,
    }


def _header_footer_later(canvas, doc):
    st = _brand_styles()
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    w, h = letter

    canvas.saveState()

    # Header line
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(NAVY)
    canvas.drawString(54, h - 38, "Apex Automation — Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # Footer line
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.line(54, 46, w - 54, 46)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(54, 34, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


def _cover_page(canvas, doc):
    # No header/footer on cover (premium feel)
    pass


def _card(title: str, bullets: List[str], st, max_bullets: int = 8) -> Table:
    bullets = clamp_list(bullets, max_bullets)
    inner: List[Any] = [Paragraph(safe_p(title), st["h2"]), Spacer(1, 4)]
    for b in bullets:
        inner.append(Paragraph(f"• {safe_p(b)}", st["body"]))

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
    return tbl


def _kpi_chips(kpis: List[str], st) -> Table:
    chips: List[Any] = []
    for c in kpis[:3]:
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
        chips.append(t)
    return Table([chips], hAlign="CENTER")


def _bar_chart_full_width(title: str, labels: List[str], values: List[int], st) -> Drawing:
    # IMPORTANT: values must be ints (NOT tuples)
    vals = []
    for v in values:
        try:
            vals.append(int(v))
        except Exception:
            vals.append(0)

    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    d = Drawing(460, 190)
    d.add(String(0, 175, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 25
    bc.height = 130
    bc.width = 400

    bc.data = [vals]
    bc.strokeColor = colors.transparent

    vmax = max(vals) if vals else 0
    vmax = max(int(vmax * 1.25), 10)
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = vmax
    bc.valueAxis.valueStep = max(int(vmax / 5), 1)

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.boxAnchor = "ne"
    bc.categoryAxis.labels.dx = 0
    bc.categoryAxis.labels.dy = -2
    bc.categoryAxis.labels.angle = 0
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = MUTED

    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = MUTED

    bc.bars[0].fillColor = BLUE
    d.add(bc)
    return d


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


def _extract_section_bullets(section_lines: List[str], hard_limit: int = 10) -> List[str]:
    items: List[str] = []
    for ln in section_lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("-"):
            items.append(s[1:].strip())
        elif s.startswith("•"):
            items.append(s[1:].strip())
        else:
            # keep short lines as bullets so it’s digestible
            if len(s) <= 140 and not s.lower().startswith("your "):
                items.append(s)
    return clamp_list(items, hard_limit)


def generate_pdf_option_a(
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
        title="Automation Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=60,
        bottomMargin=60,
    )

    story: List[Any] = []
    sections = _parse_blueprint_sections(blueprint_text)

    # -----------------------
    # PAGE 1: COVER
    # -----------------------
    story.append(Spacer(1, 80))
    story.append(Paragraph("Automation Blueprint", st["title"]))

    bn = business_name if clean_value(business_name) else "Business Name Not Provided"
    bt = business_type if clean_value(business_type) else "Business Type Not Provided"

    story.append(Paragraph(safe_p(bn), st["cover_sub"]))
    story.append(Paragraph(safe_p(bt), st["cover_sub"]))

    cover_block_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Date:</b> {time.strftime('%b %d, %Y')}",
    ]
    if clean_value(team_size):
        cover_block_lines.append(f"<b>Team size:</b> {safe_p(team_size)}")

    cover_tbl = Table([[Paragraph("<br/>".join(cover_block_lines), st["body"])]], colWidths=[6.0 * inch])
    cover_tbl.setStyle(
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
    story.append(Spacer(1, 22))
    story.append(cover_tbl)

    story.append(Spacer(1, 26))
    story.append(
        Paragraph(
            "This report summarizes where time and money are leaking, and the fastest automation wins to fix it in the next 30 days.",
            st["small"],
        )
    )

    story.append(Spacer(1, 140))
    story.append(Paragraph("Apex Automation", st["small"]))
    story.append(PageBreak())

    # -----------------------
    # PAGE 2: DASHBOARD (KPI + HERO CHART + 2 CARDS)
    # -----------------------
    story.append(Paragraph("Executive Dashboard", st["h1"]))
    story.append(Paragraph("A quick, visual snapshot of your current situation and opportunities.", st["small"]))
    story.append(Spacer(1, 10))

    kpis: List[str] = []
    if clean_value(leads_per_week):
        kpis.append(f"Leads/week: {safe_p(leads_per_week)}")
    if clean_value(jobs_per_week):
        kpis.append(f"Jobs/week: {safe_p(jobs_per_week)}")
    if clean_value(lead_response_time):
        kpis.append(f"Response time: {safe_p(lead_response_time)}")

    if kpis:
        story.append(_kpi_chips(kpis, st))
        story.append(Spacer(1, 14))

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    team_n = parse_int(team_size)

    # Hero chart (full width): Leads vs Jobs (preferred)
    if leads_n is not None and jobs_n is not None:
        d = _bar_chart_full_width(
            "Leads per Week vs Jobs per Week",
            ["Leads", "Jobs"],
            [leads_n, jobs_n],
            st,
        )
        story.append(d)
        story.append(Spacer(1, 10))
    else:
        # Fallback hero chart: Operations indicator counts
        manual_count = len(split_bullets(manual_tasks, limit=12))
        bottleneck_count = len(split_bullets(bottlenecks, limit=12))
        d = _bar_chart_full_width(
            "Operations Load Indicators (from your answers)",
            ["Manual tasks", "Bottlenecks"],
            [manual_count, bottleneck_count],
            st,
        )
        story.append(d)
        story.append(Spacer(1, 10))

    # Cards: Quick Snapshot + Highlights
    sec1_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 1")), None)
    sec2_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 2")), None)

    quick_items = _extract_section_bullets(sections.get(sec1_key, []), hard_limit=8) if sec1_key else []
    told_items = _extract_section_bullets(sections.get(sec2_key, []), hard_limit=10) if sec2_key else []

    if not quick_items:
        quick_items = clamp_list(split_bullets(manual_tasks, limit=8), 8) or ["Not enough detail provided to summarize quick snapshot."]
    if not told_items:
        told_items = clamp_list(split_bullets(bottlenecks, limit=10), 10) or ["Not enough detail provided to summarize highlights."]

    story.append(_card("Quick Snapshot", quick_items, st, max_bullets=8))
    story.append(Spacer(1, 10))
    story.append(_card("What You Told Me — Highlights", told_items, st, max_bullets=10))

    story.append(PageBreak())

    # -----------------------
    # PAGE 3: TOP 3 FIXES (CARDS)
    # -----------------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["h1"]))
    story.append(Paragraph("These are the highest-impact upgrades based on what you submitted.", st["small"]))
    story.append(Spacer(1, 10))

    # Extract FIX blocks from text (simple scan)
    lines = [ln.rstrip() for ln in blueprint_text.splitlines()]
    fix_blocks: List[List[str]] = []
    current_fix: Optional[List[str]] = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("FIX "):
            if current_fix:
                fix_blocks.append(current_fix)
            current_fix = [s]
            continue
        if current_fix is not None:
            # stop FIX parsing when we hit next SECTION
            if up.startswith("SECTION "):
                fix_blocks.append(current_fix)
                current_fix = None
                continue
            current_fix.append(s)

    if current_fix:
        fix_blocks.append(current_fix)

    # Build up to 3 cards; keep short to prevent overflow
    if not fix_blocks:
        story.append(_card("Fix 1 — Follow-Up Speed", ["Set up instant lead capture and response within 1–5 minutes."], st, 6))
        story.append(Spacer(1, 10))
        story.append(_card("Fix 2 — Quote & Booking", ["Make it easy to get quotes and book without phone tag."], st, 6))
        story.append(Spacer(1, 10))
        story.append(_card("Fix 3 — Reviews & Referrals", ["Automate review requests and referral follow-ups."], st, 6))
    else:
        for block in fix_blocks[:3]:
            title = block[0]
            # grab bullets/short lines for the card
            items: List[str] = []
            for s in block[1:]:
                if s.startswith("-"):
                    items.append(s[1:].strip())
                elif s.startswith("•"):
                    items.append(s[1:].strip())
                else:
                    if len(s) <= 140 and not s.upper().startswith("SECTION"):
                        items.append(s)
            items = clamp_list(items, 10)  # card hard cap
            story.append(_card(title, items if items else ["Details not provided."], st, max_bullets=10))
            story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------
    # PAGE 4: 30-DAY PLAN (WEEK CARDS)
    # -----------------------
    story.append(Paragraph("Your 30-Day Action Plan", st["h1"]))
    story.append(Paragraph("A simple week-by-week roadmap. Focus on execution, not complexity.", st["small"]))
    story.append(Spacer(1, 10))

    # Extract week blocks from SECTION 5 if present
    sec5_key = next((k for k in sections.keys() if k.upper().startswith("SECTION 5")), None)
    week_lines = sections.get(sec5_key, []) if sec5_key else []

    weeks: Dict[str, List[str]] = {}
    current_week = None
    for s in week_lines:
        t = s.strip()
        if not t:
            continue
        if t.upper().startswith("WEEK "):
            current_week = t
            weeks[current_week] = []
            continue
        if current_week:
            if t.startswith("-"):
                weeks[current_week].append(t[1:].strip())
            elif t.startswith("•"):
                weeks[current_week].append(t[1:].strip())
            else:
                if len(t) <= 140:
                    weeks[current_week].append(t)

    if not weeks:
        weeks = {
            "Week 1 — Stabilize": ["Confirm tracking, pipeline, and lead routing.", "Clean up inboxes and missed calls."],
            "Week 2 — Capture & Convert": ["Instant SMS follow-up.", "Quote + booking flow.", "No-show prevention."],
            "Week 3 — Customer Experience": ["Job reminders.", "Post-job checklist and upsell prompts."],
            "Week 4 — Optimize & Scale": ["Review request automation.", "Referral prompts.", "Basic reporting dashboard."],
        }

    # render in order
    for wk, items in list(weeks.items())[:4]:
        story.append(_card(wk, clamp_list(items, 6), st, max_bullets=6))
        story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------
    # PAGE 5+: FULL BLUEPRINT (WITH SECTION BARS)
    # -----------------------
    story.append(Paragraph("Full Blueprint", st["h1"]))
    story.append(Paragraph("Below is the full plan in a clean, report-style format.", st["small"]))
    story.append(Spacer(1, 10))

    def section_bar(text: str) -> Table:
        t = Table([[Paragraph(safe_p(text), st["section_bar"])]], colWidths=[7.0 * inch])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), st["SOFT_BLUE_BG"]),
                    ("BOX", (0, 0), (-1, -1), 1, st["SOFT_BLUE_BORDER"]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return t

    for raw_line in blueprint_text.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        up = line.upper()

        if up.startswith("SECTION "):
            story.append(Spacer(1, 8))
            story.append(section_bar(line))
            story.append(Spacer(1, 6))
            continue

        if up.startswith("FIX ") or up.startswith("WEEK ") or (len(line) <= 70 and line.endswith(":")):
            story.append(Paragraph(safe_p(line), st["h2"]))
            continue

        if line.startswith("- "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        elif line.startswith("• "):
            story.append(Paragraph("• " + safe_p(line[2:].strip()), st["body"]))
        else:
            # split very long paragraphs gently by turning them into smaller lines
            if len(line) > 240 and ". " in line:
                parts = [p.strip() for p in line.split(". ") if p.strip()]
                for p in parts[:6]:
                    story.append(Paragraph(safe_p(p if p.endswith(".") else p + "."), st["body"]))
            else:
                story.append(Paragraph(safe_p(line), st["body"]))

    doc.build(story, onFirstPage=_cover_page, onLaterPages=_header_footer_later)


# --------------------------------------------------------------------
# CONTEXT LOOKUP (BLAND / DEBUG)
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

        # Generate PDF (Option A)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

        t_pdf = time.time()
        generate_pdf_option_a(
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
        # show the real line that broke
        traceback.print_exc()
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

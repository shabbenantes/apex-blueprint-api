from flask import Flask, request, jsonify
import os
import uuid
import json
import re
import time
from typing import Dict, Any, Optional, List

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


def split_bullets(text: str, max_items: int = 12) -> List[str]:
    t = clean_value(text)
    if not t:
        return []
    parts = re.split(r"[\n•]+", t)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # handle "- item"
        if p.startswith("-"):
            p = p[1:].strip()
        if p:
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
    return final[:max_items]


# --------------------------------------------------------------------
# PDF V3 (STABLE + PROFESSIONAL)
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2563EB")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    CARD_BG = colors.HexColor("#F1F5F9")
    BORDER = colors.HexColor("#CBD5E1")

    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=30,
        leading=34,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=10,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=18,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=NAVY,
        spaceBefore=14,
        spaceAfter=8,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=SLATE,
        spaceBefore=10,
        spaceAfter=6,
    )

    body = ParagraphStyle(
        "ApexBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=MUTED,
        spaceAfter=6,
    )

    pill = ParagraphStyle(
        "ApexPill",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=12,
        alignment=TA_CENTER,
        textColor=colors.white,
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
    st = _brand_styles()
    canvas.saveState()
    w, h = letter

    # Header line
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(54, h - 38, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # Footer
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.line(54, 46, w - 54, 46)
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(54, 34, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


def _card_block(title: str, bullets: List[str], st) -> Table:
    """
    IMPORTANT: Bullet lines are separate rows so the table can split across pages.
    This avoids LayoutError / 16777221-height explosions.
    """
    rows = [[Paragraph(f"<b>{safe_p(title)}</b>", st["h2"])]]
    if not bullets:
        rows.append([Paragraph("No details provided.", st["body"])])
    else:
        for b in bullets:
            rows.append([Paragraph("• " + safe_p(b), st["body"])])

    tbl = Table(rows, colWidths=[7.1 * inch], hAlign="LEFT")
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["CARD_BG"]),
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


def _bar_chart(title: str, labels: List[str], values: List[int], st) -> Drawing:
    d = Drawing(460, 200)
    d.add(String(0, 180, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 30
    bc.width = 380
    bc.height = 120
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
    """
    Uses HorizontalLineChart safely: we pass y-values only and use category names.
    Avoids tuple/int math issues completely.
    """
    d = Drawing(460, 200)
    d.add(String(0, 180, title, fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))

    lc = HorizontalLineChart()
    lc.x = 40
    lc.y = 30
    lc.width = 380
    lc.height = 120

    lc.data = [y_values]  # <-- just y-values, not tuples
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


def _extract_section_block(blueprint_text: str, section_prefix: str) -> List[str]:
    """
    Grab lines from a section until the next SECTION appears.
    Returns clean bullet-like items (best effort).
    """
    lines = blueprint_text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().upper().startswith(section_prefix.upper()):
            start = i + 1
            break
    if start is None:
        return []

    out = []
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("SECTION "):
            break
        # strip common bullet marks
        if s.startswith("- "):
            s = s[2:].strip()
        if s.startswith("• "):
            s = s[2:].strip()
        if s:
            out.append(s)

    # keep it digestible
    return out[:14]


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

    # ------------------- COVER -------------------
    story.append(Spacer(1, 70))
    story.append(Paragraph(safe_p(business_name) if business_name else "Your Business", st["title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Service Business", st["subtitle"]))
    story.append(Spacer(1, 14))

    cover_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"<b>Team size:</b> {safe_p(team_size) if team_size else 'Not specified'}",
        f"<b>Leads/week:</b> {safe_p(leads_per_week) if leads_per_week else 'Not specified'}",
        f"<b>Jobs/week:</b> {safe_p(jobs_per_week) if jobs_per_week else 'Not specified'}",
        f"<b>Response time:</b> {safe_p(lead_response_time) if lead_response_time else 'Not specified'}",
    ]
    cover_tbl = Table([[Paragraph("<br/>".join(cover_lines), st["body"])]], colWidths=[7.1 * inch])
    cover_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["CARD_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 16),
                ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )
    story.append(cover_tbl)

    story.append(Spacer(1, 18))
    story.append(
        Paragraph(
            "This blueprint shows where your business is leaking time and money — and the fastest automation wins to fix it over the next 30 days.",
            st["body"],
        )
    )

    story.append(PageBreak())

    # ------------------- EXEC SUMMARY (CARDS) -------------------
    story.append(Paragraph("Executive Summary", st["h1"]))
    sec1 = _extract_section_block(blueprint_text, "SECTION 1")
    sec2 = _extract_section_block(blueprint_text, "SECTION 2")

    story.append(_card_block("Quick Snapshot", sec1 if sec1 else ["(No snapshot section found)"], st))
    story.append(Spacer(1, 12))
    story.append(_card_block("What You Told Me (Highlights)", sec2 if sec2 else ["(No details found)"], st))
    story.append(PageBreak())

    # ------------------- METRICS & CHARTS -------------------
    story.append(Paragraph("Key Metrics & Visuals", st["h1"]))
    story.append(Paragraph("Visuals are created from the numbers you submitted. If something is missing, we skip it.", st["small"]))
    story.append(Spacer(1, 8))

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)

    # Chart 1: Leads vs Jobs
    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st))
        story.append(Spacer(1, 16))
    else:
        story.append(Paragraph("Leads/jobs numbers were not provided clearly, so that chart was skipped.", st["small"]))
        story.append(Spacer(1, 10))

    # Chart 2: Response time heuristic curve (safe)
    rt = clean_value(lead_response_time).lower()
    if rt:
        labels = ["Immediate", "5m", "15m", "1h", "4h", "24h"]
        # default curve
        conv = [85, 75, 60, 45, 30, 15]
        if "immediate" in rt or "instant" in rt:
            conv = [90, 78, 62, 48, 32, 16]
        elif "hour" in rt or "1h" in rt:
            conv = [70, 65, 55, 45, 32, 18]
        elif "day" in rt or "24" in rt:
            conv = [55, 50, 40, 30, 20, 10]

        story.append(_line_chart("Response Time vs Likely Conversion (estimated)", labels, conv, st))
        story.append(Spacer(1, 12))
    else:
        story.append(Paragraph("Response time was not provided clearly, so that chart was skipped.", st["small"]))
        story.append(Spacer(1, 10))

    story.append(PageBreak())

    # ------------------- FULL BLUEPRINT (CLEAN) -------------------
    story.append(Paragraph("Full Blueprint", st["h1"]))
    story.append(Paragraph("Below is the complete plan, as generated for this submission.", st["small"]))
    story.append(Spacer(1, 10))

    for raw in blueprint_text.splitlines():
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        upper = line.upper()

        if upper.startswith("SECTION "):
            story.append(Spacer(1, 10))
            story.append(Paragraph(safe_p(line), st["h1"]))
            continue

        if upper.startswith("FIX ") or upper.startswith("WEEK ") or (len(line) <= 60 and line.endswith(":")):
            story.append(Spacer(1, 6))
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
        # OpenAI call
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        # Robust extraction
        full_text = ""
        try:
            full_text = response.output[0].content[0].text.strip()
        except Exception:
            # fallback: stringify response if structure differs
            full_text = str(response)

        # Summary = up through Section 2
        blueprint_text = full_text.strip()
        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate PDF (V3 stable)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = os.path.join("/tmp", pdf_filename)

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
        )

        # Upload PDF
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


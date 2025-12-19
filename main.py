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
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        if p:
            out.append(p)
    # de-dupe lightly
    seen = set()
    final = []
    for x in out:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(x)
    return final[:limit]


# --------------------------------------------------------------------
# STYLES / BRAND
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    NAVY = colors.HexColor("#0A1A2F")
    BLUE = colors.HexColor("#2F6FED")
    SLATE = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748B")
    LIGHT_BG = colors.HexColor("#F4F7FB")
    BORDER = colors.HexColor("#E2E8F0")
    SOFT_PURPLE = colors.HexColor("#EEF2FF")
    SOFT_PURPLE_BORDER = colors.HexColor("#C7D2FE")

    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=26,
        leading=30,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=8,
    )

    cover_big = ParagraphStyle(
        "ApexCoverBig",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        alignment=TA_CENTER,
        textColor=SLATE,
        spaceAfter=6,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=16,
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

    pill = ParagraphStyle(
        "ApexPill",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    score_big = ParagraphStyle(
        "ApexScoreBig",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=36,
        leading=38,
        alignment=TA_LEFT,
        textColor=BLUE,
        spaceAfter=4,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "SLATE": SLATE,
        "MUTED": MUTED,
        "LIGHT_BG": LIGHT_BG,
        "BORDER": BORDER,
        "SOFT_PURPLE": SOFT_PURPLE,
        "SOFT_PURPLE_BORDER": SOFT_PURPLE_BORDER,
        "title": title,
        "cover_big": cover_big,
        "subtitle": subtitle,
        "h1": h1,
        "h2": h2,
        "body": body,
        "small": small,
        "pill": pill,
        "score_big": score_big,
    }


def _header_footer(canvas, doc):
    st = _brand_styles()
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]

    canvas.saveState()
    w, h = letter

    # Header line + label
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(1)
    canvas.line(54, h - 46, w - 54, h - 46)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(NAVY)
    canvas.drawString(54, h - 38, "Apex Automation — AI Automation Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - 54, h - 38, time.strftime("%b %d, %Y"))

    # Footer line + page
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.line(54, 46, w - 54, 46)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(54, 34, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 54, 34, f"Page {doc.page}")

    canvas.restoreState()


# --------------------------------------------------------------------
# COMPONENTS
# --------------------------------------------------------------------
def _divider(st):
    return Table(
        [[Paragraph("", st["small"])]],
        colWidths=[7.0 * inch],
        style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0"))]),
    )


def _pill_row(items: List[str], st):
    chips = []
    for c in items[:4]:
        t = Table([[Paragraph(safe_p(c), st["pill"])]], colWidths=[1.65 * inch])
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

    if not chips:
        return None

    return Table([chips], hAlign="CENTER")


def _card(title: str, bullets: List[str], st, max_bullets: int = 10):
    bullets = [b for b in bullets if clean_value(b)]
    bullets = bullets[:max_bullets]

    content: List[Any] = [Paragraph(safe_p(title), st["h1"]), Spacer(1, 4)]
    for b in bullets:
        content.append(Paragraph(f"• {safe_p(b)}", st["body"]))

    tbl = Table([[content]], colWidths=[7.0 * inch])
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


def _cta_box(text1: str, text2: str, st):
    t = Table(
        [[Paragraph(safe_p(text1), st["body"]), Paragraph(safe_p(text2), st["small"])]],
        colWidths=[7.0 * inch],
    )
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["SOFT_PURPLE"]),
                ("BOX", (0, 0), (-1, -1), 1, st["SOFT_PURPLE_BORDER"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return t


# --------------------------------------------------------------------
# CHARTS (safe + predictable sizing)
# --------------------------------------------------------------------
def _bar_chart(title: str, labels: List[str], values: List[int], st) -> Drawing:
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    safe_vals = [int(v) if v is not None else 0 for v in values]
    vmax = max(safe_vals) if safe_vals else 10
    vmax = int(max(vmax * 1.25, 10))

    d = Drawing(460, 180)
    d.add(String(0, 165, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 20
    bc.height = 120
    bc.width = 400
    bc.data = [safe_vals]
    bc.strokeColor = colors.transparent

    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = vmax
    bc.valueAxis.valueStep = max(int(vmax / 5), 1)

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


def _line_chart(title: str, labels: List[str], yvals: List[int], st) -> Drawing:
    NAVY = st["NAVY"]
    MUTED = st["MUTED"]
    BLUE = st["BLUE"]

    d = Drawing(460, 180)
    d.add(String(0, 165, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    lc = HorizontalLineChart()
    lc.x = 40
    lc.y = 25
    lc.height = 120
    lc.width = 400

    pts = [(i, int(yvals[i])) for i in range(len(labels))]
    lc.data = [pts]
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


# --------------------------------------------------------------------
# BLUEPRINT PARSING (structured extraction)
# --------------------------------------------------------------------
def _extract_between(text: str, start: str, end: Optional[str]) -> str:
    if not text:
        return ""
    idx = text.find(start)
    if idx == -1:
        return ""
    sub = text[idx + len(start) :]
    if end:
        j = sub.find(end)
        if j != -1:
            sub = sub[:j]
    return sub.strip()


def _extract_section(text: str, section_prefix: str, next_prefix: Optional[str]) -> str:
    # section_prefix like "SECTION 2:"
    start = section_prefix
    end = next_prefix
    return _extract_between(text, start, end)


def _extract_subsection(section_text: str, label: str, next_label: Optional[str]) -> str:
    return _extract_between(section_text, label, next_label)


def _extract_fix_block(text: str, fix_num: int) -> str:
    start = f"FIX {fix_num}"
    # find next FIX or next SECTION
    idx = text.find(start)
    if idx == -1:
        return ""
    sub = text[idx:]
    # cut at next FIX (different number) or SECTION 4
    for marker in [f"\nFIX {fix_num+1}", "\nSECTION 4:", "\nSECTION 5:", "\nSECTION 6:"]:
        j = sub.find(marker)
        if j != -1:
            sub = sub[:j]
            break
    return sub.strip()


def _first_line(s: str) -> str:
    lines = [ln.strip() for ln in (s or "").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _extract_fix_title(fix_block: str) -> str:
    # expected: "FIX 1 – Title:" or "FIX 1 - Title:"
    first = _first_line(fix_block)
    if not first:
        return ""
    # remove leading "FIX X"
    first = re.sub(r"^FIX\s*\d+\s*[–\-]?\s*", "", first).strip()
    # remove trailing colon
    if first.endswith(":"):
        first = first[:-1].strip()
    return first


def _bullets_from_block(block: str, limit: int = 8) -> List[str]:
    # Try to use '-' bullets; fall back to short lines
    lines = [ln.strip() for ln in (block or "").splitlines() if ln.strip()]
    bullets = []
    for ln in lines:
        if ln.startswith("- "):
            bullets.append(ln[2:].strip())
        elif ln.startswith("• "):
            bullets.append(ln[2:].strip())
    if not bullets:
        # fallback: include short lines that aren't headers
        for ln in lines:
            if len(ln) <= 160 and not ln.lower().startswith(("section", "fix ", "week ")):
                bullets.append(ln)
    return [b for b in bullets if b][:limit]


# --------------------------------------------------------------------
# PDF V3 (Locked Layout)
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

    # -----------------------------
    # PAGE 1 — COVER (no header/footer)
    # -----------------------------
    story.append(Spacer(1, 36))
    story.append(Paragraph("Apex Automation", st["subtitle"]))
    story.append(Paragraph("AI Automation Blueprint", st["title"]))

    biz = business_name if clean_value(business_name) else "Your Business"
    btype = business_type if clean_value(business_type) else "Service Business"

    story.append(Spacer(1, 10))
    story.append(Paragraph(safe_p(biz), st["cover_big"]))
    story.append(Paragraph(safe_p(btype), st["subtitle"]))

    meta_lines = [
        f"<b>Prepared for:</b> {safe_p(lead_name) if clean_value(lead_name) else 'Business Owner'}",
        f"<b>Date:</b> {time.strftime('%b %d, %Y')}",
    ]
    if clean_value(team_size):
        meta_lines.append(f"<b>Team size:</b> {safe_p(team_size)}")

    meta_tbl = Table([[Paragraph("<br/>".join(meta_lines), st["body"])]], colWidths=[7.0 * inch])
    meta_tbl.setStyle(
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
    story.append(Spacer(1, 14))
    story.append(meta_tbl)
    story.append(Spacer(1, 16))

    story.append(
        Paragraph(
            "This blueprint highlights where your business is leaking time and money, "
            "and the simplest automation wins to fix it over the next 30 days.",
            st["body"],
        )
    )

    story.append(Spacer(1, 18))
    story.append(_divider(st))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Confidential. Intended only for the business owner listed above.", st["small"]))

    story.append(PageBreak())

    # -----------------------------
    # Extract sections from blueprint text
    # -----------------------------
    sec1 = _extract_section(blueprint_text, "SECTION 1:", "SECTION 2:")
    sec2 = _extract_section(blueprint_text, "SECTION 2:", "SECTION 3:")
    sec4 = _extract_section(blueprint_text, "SECTION 4:", "SECTION 5:")
    sec5 = _extract_section(blueprint_text, "SECTION 5:", "SECTION 6:")
    sec6 = _extract_section(blueprint_text, "SECTION 6:", None)

    # Section 2 subsections
    goals_txt = _extract_subsection(sec2, "Your Goals:", "Your Challenges:")
    challenges_txt = _extract_subsection(sec2, "Your Challenges:", "Where Time Is Being Lost:")
    time_lost_txt = _extract_subsection(sec2, "Where Time Is Being Lost:", "Opportunities You’re Not Using Yet:")
    opps_txt = _extract_subsection(sec2, "Opportunities You’re Not Using Yet:", None)

    # Fix blocks
    fix1 = _extract_fix_block(blueprint_text, 1)
    fix2 = _extract_fix_block(blueprint_text, 2)
    fix3 = _extract_fix_block(blueprint_text, 3)

    # -----------------------------
    # PAGE 2 — EXECUTIVE OVERVIEW
    # -----------------------------
    story.append(Paragraph("Executive Overview", st["h1"]))
    story.append(Paragraph("At-a-glance snapshot based on your submission.", st["small"]))
    story.append(Spacer(1, 8))

    chips = []
    if clean_value(leads_per_week):
        chips.append(f"Leads/week: {leads_per_week}")
    if clean_value(jobs_per_week):
        chips.append(f"Jobs/week: {jobs_per_week}")
    if clean_value(lead_response_time):
        chips.append(f"Response: {lead_response_time}")
    if clean_value(team_size):
        chips.append(f"Team: {team_size}")

    pill = _pill_row(chips, st)
    if pill:
        story.append(pill)
        story.append(Spacer(1, 12))

    # Hero chart: Workload Snapshot (full width)
    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)
    team_n = parse_int(team_size)

    if jobs_n is not None and team_n is not None and team_n > 0:
        jobs_per_person = max(int(round(jobs_n / max(team_n, 1))), 0)
        d = _bar_chart(
            "Workload Snapshot",
            ["Team size", "Jobs/week", "Jobs per person"],
            [team_n, jobs_n, jobs_per_person],
            st,
        )
        story.append(d)
        story.append(Spacer(1, 10))
    elif leads_n is not None and jobs_n is not None:
        d = _bar_chart(
            "Leads per Week vs Jobs per Week",
            ["Leads", "Jobs"],
            [leads_n, jobs_n],
            st,
        )
        story.append(d)
        story.append(Spacer(1, 10))

    # Quick Snapshot card (Section 1)
    sec1_bullets = _bullets_from_block(sec1, limit=10)
    if sec1_bullets:
        story.append(_card("Quick Snapshot", sec1_bullets, st, max_bullets=10))
        story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------------
    # PAGE 3 — WHAT YOU TOLD ME (cards)
    # -----------------------------
    story.append(Paragraph("What You Told Me", st["h1"]))
    story.append(Paragraph("The highlights that matter most for your roadmap.", st["small"]))
    story.append(Spacer(1, 8))

    goals = _bullets_from_block(goals_txt, limit=6)
    if goals:
        story.append(_card("Your Goals", goals, st, max_bullets=6))
        story.append(Spacer(1, 10))

    challenges = _bullets_from_block(challenges_txt, limit=8)
    if challenges:
        story.append(_card("Your Challenges", challenges, st, max_bullets=8))
        story.append(Spacer(1, 10))

    time_lost = _bullets_from_block(time_lost_txt, limit=8)
    if time_lost:
        story.append(_card("Where Time Is Being Lost", time_lost, st, max_bullets=8))
        story.append(Spacer(1, 10))

    opps = _bullets_from_block(opps_txt, limit=8)
    if opps:
        story.append(_card("Opportunities You’re Not Using Yet", opps, st, max_bullets=8))
        story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------------
    # PAGE 4 — TOP 3 FIXES (three clean cards)
    # -----------------------------
    story.append(Paragraph("Your Top 3 Automation Fixes", st["h1"]))
    story.append(Paragraph("These are the highest-leverage wins to reduce workload and increase booked jobs.", st["small"]))
    story.append(Spacer(1, 8))

    def fix_card(fix_block: str, fallback_title: str):
        if not fix_block:
            return None
        title = _extract_fix_title(fix_block) or fallback_title

        wf = _extract_between(fix_block, "What This Fixes:", "What This Does For You:")
        wd = _extract_between(fix_block, "What This Does For You:", "What’s Included:")
        wi = _extract_between(fix_block, "What’s Included:", None)

        bullets = []
        bullets += [f"[Fixes] {x}" for x in _bullets_from_block(wf, limit=4)]
        bullets += [f"[Does] {x}" for x in _bullets_from_block(wd, limit=4)]
        bullets += [f"[Includes] {x}" for x in _bullets_from_block(wi, limit=5)]

        return _card(title, bullets, st, max_bullets=13)

    for idx, blk in [(1, fix1), (2, fix2), (3, fix3)]:
        fc = fix_card(blk, f"Fix {idx}")
        if fc:
            story.append(fc)
            story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------------
    # PAGE 5 — SCORECARD + VISUALS
    # -----------------------------
    story.append(Paragraph("Scorecard & Visuals", st["h1"]))
    story.append(Paragraph("Visual breakdown of key inputs and impact indicators.", st["small"]))
    story.append(Spacer(1, 8))

    # Score extraction (try to find first number 0-100 in SECTION 4)
    score = None
    if sec4:
        m = re.search(r"(\b\d{1,3}\b)", sec4)
        if m:
            try:
                sc = int(m.group(1))
                if 0 <= sc <= 100:
                    score = sc
            except:
                score = None

    if score is not None:
        story.append(Paragraph(str(score), st["score_big"]))
        story.append(Paragraph("Automation Score (0–100)", st["small"]))
        story.append(Spacer(1, 6))
        score_bullets = _bullets_from_block(sec4, limit=6)
        if score_bullets:
            story.append(_card("What the score means", score_bullets, st, max_bullets=6))
            story.append(Spacer(1, 10))

    # Charts (full width, stacked)
    if leads_n is not None and jobs_n is not None:
        story.append(_bar_chart("Leads per Week vs Jobs per Week", ["Leads", "Jobs"], [leads_n, jobs_n], st))
        story.append(Spacer(1, 10))

    manual_count = len(split_bullets(manual_tasks, limit=20))
    bottleneck_count = len(split_bullets(bottlenecks, limit=20))
    if manual_count or bottleneck_count:
        story.append(_bar_chart("Operations Load Indicators (from your answers)", ["Manual tasks", "Bottlenecks"], [manual_count, bottleneck_count], st))
        story.append(Spacer(1, 10))

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
        story.append(_line_chart("Response Time vs Likely Conversion (estimated)", labels, conv, st))
        story.append(Spacer(1, 10))

    story.append(PageBreak())

    # -----------------------------
    # PAGE 6 — 30-DAY ACTION PLAN (table)
    # -----------------------------
    story.append(Paragraph("Your 30-Day Action Plan", st["h1"]))
    story.append(Paragraph("A clean, week-by-week build order based on your blueprint.", st["small"]))
    story.append(Spacer(1, 10))

    week1 = _extract_between(sec5, "Week 1", "Week 2")
    week2 = _extract_between(sec5, "Week 2", "Week 3")
    week3 = _extract_between(sec5, "Week 3", "Week 4")
    week4 = _extract_between(sec5, "Week 4", None)

    def week_bullets(block: str) -> str:
        bs = _bullets_from_block(block, limit=6)
        if not bs:
            return "—"
        return "<br/>".join([f"• {safe_p(x)}" for x in bs])

    plan_rows = [
        ["Week 1", Paragraph(week_bullets(week1), st["body"])],
        ["Week 2", Paragraph(week_bullets(week2), st["body"])],
        ["Week 3", Paragraph(week_bullets(week3), st["body"])],
        ["Week 4", Paragraph(week_bullets(week4), st["body"])],
    ]

    plan_tbl = Table(plan_rows, colWidths=[1.0 * inch, 6.0 * inch])
    plan_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), st["LIGHT_BG"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BORDER"]),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, st["BORDER"]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 0), (0, -1), st["SLATE"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(plan_tbl)
    story.append(Spacer(1, 14))

    story.append(_cta_box("Next Step: Book Your Automation Strategy Call",
                          "We’ll walk through this plan, prioritize the fastest wins, and map implementation.",
                          st))

    story.append(PageBreak())

    # -----------------------------
    # PAGE 7 — FINAL RECOMMENDATIONS
    # -----------------------------
    story.append(Paragraph("Final Recommendations", st["h1"]))
    story.append(Paragraph("The highest-impact guidance based on your blueprint.", st["small"]))
    story.append(Spacer(1, 8))

    final_bullets = _bullets_from_block(sec6, limit=10)
    if final_bullets:
        story.append(_card("What to do next", final_bullets, st, max_bullets=10))
        story.append(Spacer(1, 10))

    story.append(_cta_box("Want us to build this with you?",
                          "Reply to the email and we’ll schedule a quick call to map your implementation plan.",
                          st))

    # Build: cover has no header/footer; rest does
    def _no_header_footer(canvas, doc):
        pass

    doc.build(story, onFirstPage=_no_header_footer, onLaterPages=_header_footer)


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

        # Summary: up through Section 2
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

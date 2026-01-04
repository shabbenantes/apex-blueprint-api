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

from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.barcharts import VerticalBarChart

app = Flask(__name__)

# ---------- OpenAI ----------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ---------- S3 CONFIG ----------
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-2")
s3_client = boto3.client("s3", region_name=S3_REGION)

# ---------- CTA / CALENDAR ----------
DEFAULT_CALENDAR_URL = "https://api.leadconnectorhq.com/widget/bookings/automation-strategy-call-1"
CALENDAR_URL = (os.environ.get("CALENDAR_URL", "") or "").strip() or DEFAULT_CALENDAR_URL

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
    if s.strip() in {"--", "—", "-", "•", "• --"}:
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
    # Pull first integer from strings like "About 50 per week"
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
    s = (s or "").strip()
    if s.startswith("- "):
        return s[2:].strip()
    if s.startswith("• "):
        return s[2:].strip()
    if s.startswith("-"):
        return s[1:].strip()
    if s.startswith("•"):
        return s[1:].strip()
    return s


def _shorten_bullet(text: str, max_words: int = 10, max_chars: int = 78) -> str:
    t = clean_value(text)
    if not t:
        return ""
    for sep in [". ", "; ", " — ", " - "]:
        if sep in t:
            t = t.split(sep, 1)[0].strip()
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip() + "…"
    if len(t) > max_chars:
        t = t[: max_chars - 1].rstrip() + "…"
    return t


def _shorten_list(items: List[str], max_items: int, max_words: int = 10, max_chars: int = 78) -> List[str]:
    out: List[str] = []
    for x in items:
        s = _shorten_bullet(x, max_words=max_words, max_chars=max_chars)
        if s:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _get_any(form_fields: dict, keys: List[str]) -> str:
    """
    Safely get the first matching field from:
    - exact key
    - key match ignoring case
    - key match ignoring curly quotes / punctuation differences
    """
    if not isinstance(form_fields, dict):
        return ""

    # 1) exact
    for k in keys:
        if k in form_fields:
            return clean_value(form_fields.get(k))

    # 2) case-insensitive
    lower_map = {str(k).strip().lower(): k for k in form_fields.keys()}
    for k in keys:
        lk = str(k).strip().lower()
        if lk in lower_map:
            return clean_value(form_fields.get(lower_map[lk]))

    # 3) normalized (remove non-alnum)
    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(x).strip().lower())

    norm_map = {norm(k): k for k in form_fields.keys()}
    for k in keys:
        nk = norm(k)
        if nk in norm_map:
            return clean_value(form_fields.get(norm_map[nk]))

    return ""


def _extract_json_object(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


# --------------------------------------------------------------------
# FIX DEFINITIONS (LOCKED)
# --------------------------------------------------------------------
FIX_1 = {
    "key": "fix_1",
    "name": "Lead Intake & Follow-Up",
    "what_this_fixes": [
        "People reach out in different places.",
        "Some messages get missed.",
        "Follow-up depends on memory.",
    ],
    "what_this_does": [
        "Everyone gets a reply.",
        "Nothing gets forgotten.",
        "You don’t have to keep it all in your head.",
    ],
    "whats_included": [
        "One clear place for new leads.",
        "Auto-replies for new messages.",
        "A simple follow-up list.",
    ],
    "short_summary": "Reply fast. Follow up every time.",
}

FIX_2 = {
    "key": "fix_2",
    "name": "Scheduling & Admin Help",
    "what_this_fixes": [
        "Too much back-and-forth booking.",
        "People miss appointments.",
        "Your day gets interrupted.",
    ],
    "what_this_does": [
        "Booking is simple.",
        "Fewer messages.",
        "Your time is protected.",
    ],
    "whats_included": [
        "One booking link.",
        "Confirmations and reminders.",
        "Easy reschedule options.",
    ],
    "short_summary": "Make booking simple and calm.",
}

FIX_3 = {
    "key": "fix_3",
    "name": "Client Follow-Through",
    "what_this_fixes": [
        "The job gets done, then nothing happens.",
        "Clients don’t hear back.",
        "Reviews and repeat work get missed.",
    ],
    "what_this_does": [
        "Clients feel taken care of.",
        "You stay top of mind.",
        "More repeat work over time.",
    ],
    "whats_included": [
        "After-job follow-ups.",
        "Light review requests.",
        "Simple check-ins over time.",
    ],
    "short_summary": "Follow up after the job.",
}

ALL_FIXES = [FIX_1, FIX_2, FIX_3]


def _pick_and_rank_fixes(services: str, stress: str, remember: str) -> List[dict]:
    """
    Returns fixes ranked [Fix #1, Fix #2, Fix #3].
    Simple keyword scoring. If unclear, defaults to Fix 1, Fix 2, Fix 3.
    """
    text = " ".join([services or "", stress or "", remember or ""]).lower()

    score_1 = 0
    score_2 = 0
    score_3 = 0

    # Fix 1: messages, leads, follow-up, missed
    if any(k in text for k in [
        "lead", "leads", "inquiry", "inquiries", "message", "messages", "dm",
        "text", "reply", "respond", "response", "follow", "follow-up",
        "forgot", "forget", "miss", "missed", "ghost", "instagram", "facebook"
    ]):
        score_1 += 3
    if any(k in text for k in ["email", "website", "call", "phone"]):
        score_1 += 1

    # Fix 2: booking, schedule, appointments
    if any(k in text for k in [
        "schedule", "scheduling", "calendar", "appointment", "appointments",
        "book", "booking", "reschedule", "no-show", "noshow", "availability"
    ]):
        score_2 += 3
    if "back and forth" in text or "back-and-forth" in text:
        score_2 += 2

    # Fix 3: after job, reviews, repeat work
    if any(k in text for k in [
        "review", "reviews", "google", "yelp", "testimonial",
        "repeat", "return", "retention", "check in", "check-in",
        "after", "afterward", "follow through", "follow-through"
    ]):
        score_3 += 3

    scores = [(score_1, FIX_1), (score_2, FIX_2), (score_3, FIX_3)]
    scores.sort(key=lambda x: x[0], reverse=True)

    if scores[0][0] == 0:
        return [FIX_1, FIX_2, FIX_3]

    ranked = [scores[0][1]]
    for _, fx in scores[1:]:
        if fx not in ranked:
            ranked.append(fx)
    return ranked[:3]


def _estimate_score(stress: str, remember: str, leads: Optional[int], jobs: Optional[int]) -> int:
    """
    Simple score (0–100). Lower = more chaos.
    """
    base = 78
    text = " ".join([stress or "", remember or ""]).lower()

    chaos_words = [
        "miss", "missed", "forgot", "forget",
        "overwhelm", "overwhelmed", "behind", "stress",
        "mess", "chaos", "dropped", "drop"
    ]
    for w in chaos_words:
        if w in text:
            base -= 4

    # If volume is high, strain can be higher
    if leads is not None and leads >= 20:
        base -= 6
    if jobs is not None and jobs >= 20:
        base -= 6

    # Missing numbers: small penalty
    if leads is None:
        base -= 3
    if jobs is None:
        base -= 3

    return max(25, min(92, base))


# --------------------------------------------------------------------
# PDF DESIGN SYSTEM
# --------------------------------------------------------------------
def _brand_styles():
    styles = getSampleStyleSheet()

    NAVY = colors.HexColor("#0B1B2B")
    BLUE = colors.HexColor("#2563EB")
    BLUE_DK = colors.HexColor("#1E40AF")
    MUTED = colors.HexColor("#64748B")
    WHITE = colors.white

    CARD_BG = colors.HexColor("#FFFFFF")
    CARD_BG_ALT = colors.HexColor("#F3F7FF")
    BORDER = colors.HexColor("#D8E1EE")
    SOFT = colors.HexColor("#E6ECF5")

    title = ParagraphStyle(
        "ApexTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=34,
        leading=38,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=6,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=16,
        leading=20,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=10,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=NAVY,
        spaceBefore=6,
        spaceAfter=6,
    )

    h2 = ParagraphStyle(
        "ApexH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
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

    fix_header = ParagraphStyle(
        "FixHeader",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=WHITE,
        alignment=TA_LEFT,
    )

    cta_btn = ParagraphStyle(
        "CtaBtn",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=20,
        alignment=TA_CENTER,
        textColor=WHITE,
    )

    return {
        "NAVY": NAVY,
        "BLUE": BLUE,
        "BLUE_DK": BLUE_DK,
        "MUTED": MUTED,
        "WHITE": WHITE,
        "CARD_BG": CARD_BG,
        "CARD_BG_ALT": CARD_BG_ALT,
        "BORDER": BORDER,
        "SOFT": SOFT,
        "title": title,
        "subtitle": subtitle,
        "h1": h1,
        "h2": h2,
        "body": body,
        "small": small,
        "fix_header": fix_header,
        "cta_btn": cta_btn,
    }


def _header_footer(canvas, doc):
    st = _brand_styles()
    canvas.saveState()
    w, h = letter

    canvas.setStrokeColor(st["SOFT"])
    canvas.setLineWidth(1)
    canvas.line(38, h - 44, w - 38, h - 44)

    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(st["NAVY"])
    canvas.drawString(38, h - 36, "Apex Automation — Business Blueprint")

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawRightString(w - 38, h - 36, time.strftime("%b %d, %Y"))

    canvas.setStrokeColor(st["SOFT"])
    canvas.line(38, 44, w - 38, 44)

    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(st["MUTED"])
    canvas.drawString(38, 32, "Confidential — Prepared for the business owner listed on the cover")
    canvas.drawRightString(w - 38, 32, f"Page {doc.page}")

    canvas.restoreState()


# --------------------------------------------------------------------
# PDF BUILDING HELPERS
# --------------------------------------------------------------------
def _card_table(
    title: str,
    bullets: List[str],
    st,
    bg=None,
    placeholder_if_empty: bool = True,
    extra_padding: int = 0,
) -> Table:
    bg_color = bg if bg is not None else st["CARD_BG"]

    rows: List[List[Any]] = [[Paragraph(f"<b>{safe_p(title)}</b>", st["h2"])]]
    clean_bullets = [clean_value(b) for b in bullets if clean_value(b)]

    if not clean_bullets and placeholder_if_empty:
        rows.append([Paragraph("No details provided.", st["body"])])
    else:
        for b in clean_bullets:
            rows.append([Paragraph("• " + safe_p(b), st["body"])])

    tbl = Table(rows, colWidths=[7.44 * inch], hAlign="LEFT")
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


def _fix_header_bar(title: str, st) -> Table:
    tbl = Table([[Paragraph(safe_p(title), st["fix_header"])]], colWidths=[7.44 * inch])
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
    height = 155 if compact else 190
    plot_h = 85 if compact else 110
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


def _score_gauge(score: int, st) -> Drawing:
    score = max(0, min(100, int(score)))
    w = 460
    h = 72
    pad_x = 10
    bar_y = 26
    bar_h = 14
    bar_w = w - (pad_x * 2)

    d = Drawing(w, h)
    d.add(String(0, 54, "Score (0–100)", fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))
    d.add(Rect(pad_x, bar_y, bar_w, bar_h, strokeColor=st["BORDER"], fillColor=st["SOFT"], strokeWidth=1))

    fill_w = int(bar_w * (score / 100.0))
    d.add(Rect(pad_x, bar_y, fill_w, bar_h, strokeColor=None, fillColor=st["BLUE"]))

    d.add(String(pad_x, 10, "0", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))
    d.add(String(pad_x + int(bar_w / 2) - 6, 10, "50", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))
    d.add(String(pad_x + bar_w - 16, 10, "100", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))

    label_x = pad_x + fill_w
    label_x = max(pad_x + 18, min(pad_x + bar_w - 18, label_x))
    d.add(String(label_x - 14, 44, f"{score}", fontName="Helvetica-Bold", fontSize=11, fillColor=st["NAVY"]))
    return d


def _cta_block(st) -> List[Any]:
    url = CALENDAR_URL

    title = _card_table(
        "Want help fixing this?",
        [
            "If you want, we can talk it through.",
            "No pressure. Just a quick call.",
        ],
        st,
        bg=st["CARD_BG_ALT"],
        placeholder_if_empty=False,
    )

    btn_text = f'<link href="{safe_p(url)}" color="white"><b>Book a quick call →</b></link>'

    btn = Table(
        [[Paragraph(btn_text, st["cta_btn"])]],
        colWidths=[7.44 * inch],
        rowHeights=[0.70 * inch],
        hAlign="LEFT",
    )
    btn.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BLUE_DK"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BLUE_DK"]),
                ("TOPPADDING", (0, 0), (-1, -1), 18),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    return [KeepTogether([title, Spacer(1, 12), btn])]


# --------------------------------------------------------------------
# BLUEPRINT CONTENT BUILD (SIMPLE + STABLE)
# --------------------------------------------------------------------
def _fallback_quick_snapshot(business_name: str, services: str, stress: str, remember: str, leads_n: Optional[int], jobs_n: Optional[int]) -> List[str]:
    out = []
    if business_name:
        out.append(f"Business: {business_name}.")
    if services:
        out.append("You do: " + services)
    if stress:
        out.append("Hardest right now: " + stress)
    if remember:
        out.append("You are trying to remember: " + remember)
    if leads_n is not None:
        out.append(f"New messages each week: about {leads_n}.")
    if jobs_n is not None:
        out.append(f"Work each week: about {jobs_n} jobs/orders.")
    if not out:
        out = ["You want things to feel easier.", "You want a clear next step."]
    return _shorten_list(out, 6, max_words=12)


def _fallback_plan_30_days() -> Dict[str, List[str]]:
    return {
        "week_1": ["Get all messages in one place.", "Reply fast to new people.", "Stop missing requests."],
        "week_2": ["Make follow-up automatic.", "Keep a simple next-step list.", "Cut down back-and-forth."],
        "week_3": ["Make booking simple.", "Send reminders so people show up.", "Protect your time."],
        "week_4": ["Do quick check-ins.", "Ask for reviews the easy way.", "Keep the system working."],
    }


def _ask_model_for_parts(
    business_name: str,
    services: str,
    stress: str,
    remember: str,
    leads_per_week: str,
    jobs_per_week: str,
    fix1_name: str,
) -> dict:
    """
    Optional: ask the model for better quick snapshot + plan.
    Must stay third-grade level. Must be short. JSON only.
    """
    prompt = f"""
Write for a stressed business owner.
Third-grade reading level.
Short sentences. No tech words.

Business name: {business_name or "Your Business"}
What they do: {services or "Not provided"}
Hardest right now: {stress or "Not provided"}
Always trying to remember: {remember or "Not provided"}
Leads/messages per week: {leads_per_week or "Not provided"}
Jobs/orders/clients per week: {jobs_per_week or "Not provided"}

The best first fix is: {fix1_name}

Return ONLY valid JSON in this exact shape:

{{
  "quick_snapshot": ["...", "...", "...", "..."],
  "improve": ["...", "...", "..."],
  "plan_30_days": {{
    "week_1": ["...", "...", "..."],
    "week_2": ["...", "...", "..."],
    "week_3": ["...", "...", "..."],
    "week_4": ["...", "...", "..."]
  }}
}}

Rules:
- quick_snapshot = 4 to 6 bullets
- improve = 3 bullets
- each bullet is short
- simple words only
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )

    raw_text = ""
    try:
        raw_text = response.output[0].content[0].text.strip()
    except Exception:
        raw_text = str(response)

    out = _extract_json_object(raw_text)
    return out if isinstance(out, dict) else {}


def _bp_to_text(bp: dict, lead_name: str, business_name: str) -> str:
    lines = []
    lines.append(f"Prepared for: {lead_name}")
    lines.append(f"Business: {business_name or 'Your Business'}")
    lines.append("")

    lines.append("Quick Snapshot:")
    for x in bp.get("quick_snapshot", []) or []:
        lines.append("- " + _strip_bullet_prefix(str(x)))

    lines.append("")
    lines.append("Fix #1 (Do this first): " + (bp.get("fix_1", {}).get("name", "")))
    for label, key in [("What this fixes", "what_this_fixes"), ("What this does", "what_this_does"), ("What's included", "whats_included")]:
        lines.append(label + ":")
        for x in (bp.get("fix_1", {}).get(key, []) or []):
            lines.append("- " + _strip_bullet_prefix(str(x)))

    lines.append("")
    lines.append("Fix #2: " + (bp.get("fix_2", {}).get("name", "")))
    lines.append("Fix #3: " + (bp.get("fix_3", {}).get("name", "")))

    score = bp.get("score", None)
    if isinstance(score, int):
        lines.append("")
        lines.append(f"Score: {score}/100")

    lines.append("")
    lines.append("30-Day Direction:")
    plan = bp.get("plan_30_days", {}) or {}
    for wk in ["week_1", "week_2", "week_3", "week_4"]:
        items = plan.get(wk, []) or []
        if items:
            lines.append(wk.replace("_", " ").title() + ":")
            for x in items:
                lines.append("- " + _strip_bullet_prefix(str(x)))

    return "\n".join(lines).strip()


# --------------------------------------------------------------------
# PDF GENERATION (CLEANER FLOW, NO WEEK 3/4 SPLIT)
# --------------------------------------------------------------------
def generate_pdf_blueprint(
    bp: dict,
    pdf_path: str,
    lead_name: str,
    business_name: str,
    business_type: str,
    leads_per_week: str,
    jobs_per_week: str,
):
    st = _brand_styles()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="Business Blueprint",
        author="Apex Automation",
        leftMargin=38,
        rightMargin=38,
        topMargin=58,
        bottomMargin=58,
    )

    story: List[Any] = []
    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)

    # ------------------- PAGE 1: COVER -------------------
    story.append(Spacer(1, 18))
    story.append(Paragraph(safe_p(business_name) if business_name else "Your Business", st["title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Business", st["subtitle"]))

    cover_lines = [
        f"Prepared for: {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"Leads/messages per week: {safe_p(leads_per_week) if leads_per_week else 'Not specified'}",
        f"Jobs/orders/clients per week: {safe_p(jobs_per_week) if jobs_per_week else 'Not specified'}",
    ]
    story.append(_card_table("Snapshot", cover_lines, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(Paragraph("This shows what to fix first.", st["body"]))
    story.append(Spacer(1, 14))

    if leads_n is not None and jobs_n is not None:
        story.append(Paragraph("Workload Snapshot", st["h1"]))
        story.append(
            _bar_chart(
                "Leads/messages vs jobs/orders (per week)",
                ["Leads/messages", "Jobs/orders"],
                [leads_n, jobs_n],
                st,
                compact=True,
            )
        )

    story.append(PageBreak())

    # ------------------- PAGE 2: QUICK SUMMARY -------------------
    quick_snapshot = _shorten_list([_strip_bullet_prefix(x) for x in (bp.get("quick_snapshot") or [])], 6, max_words=12)
    if not quick_snapshot:
        quick_snapshot = ["You want things to feel easier.", "You want a clear next step."]

    what_you_said = bp.get("what_you_said") or []
    if not what_you_said:
        what_you_said = ["You want fewer missed things.", "You want fast replies.", "You want less to remember."]
    what_you_said = _shorten_list([_strip_bullet_prefix(x) for x in what_you_said], 5, max_words=12)

    story.append(Paragraph("Quick Summary", st["h1"]))
    story.append(Spacer(1, 6))
    story.append(_card_table("Quick Snapshot", quick_snapshot, st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 12))
    story.append(_card_table("What You Told Me", what_you_said, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(PageBreak())

    # ------------------- PAGE 3: FIX #1 (MAIN) -------------------
    fix1 = bp.get("fix_1") or {}
    fix1_name = fix1.get("name", "Fix #1")

    story.append(Paragraph("The First Thing to Fix", st["h1"]))
    story.append(Paragraph("This is the best first step right now.", st["small"]))
    story.append(Spacer(1, 6))

    story.append(_fix_header_bar(f"Fix #1: {fix1_name}", st))
    story.append(Spacer(1, 8))

    story.append(_card_table("What this fixes", _shorten_list(fix1.get("what_this_fixes", []) or [], 6), st, bg=st["CARD_BG_ALT"]))
    story.append(Spacer(1, 10))
    story.append(_card_table("What this does for you", _shorten_list(fix1.get("what_this_does", []) or [], 6), st, bg=st["CARD_BG"]))
    story.append(Spacer(1, 10))
    story.append(_card_table("What’s included", _shorten_list(fix1.get("whats_included", []) or [], 7), st, bg=st["CARD_BG_ALT"]))
    story.append(PageBreak())

    # ------------------- PAGE 4: FIX #2 + FIX #3 + 30-DAY DIRECTION (ALL TOGETHER) -------------------
    fix2 = bp.get("fix_2") or {}
    fix3 = bp.get("fix_3") or {}

    story.append(Paragraph("Other Helpful Fixes", st["h1"]))
    story.append(Paragraph("These can come later. Not required now.", st["small"]))
    story.append(Spacer(1, 6))

    other_fixes = [
        f'{fix2.get("name","Fix #2")}: {fix2.get("short_summary","")}'.strip(),
        f'{fix3.get("name","Fix #3")}: {fix3.get("short_summary","")}'.strip(),
    ]
    story.append(_card_table("Fix #2 and Fix #3", _shorten_list(other_fixes, 4, max_words=14), st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 14))

    plan = bp.get("plan_30_days") or {}
    w1 = _shorten_list(plan.get("week_1", []) or [], 3, max_words=10)
    w2 = _shorten_list(plan.get("week_2", []) or [], 3, max_words=10)
    w3 = _shorten_list(plan.get("week_3", []) or [], 3, max_words=10)
    w4 = _shorten_list(plan.get("week_4", []) or [], 3, max_words=10)

    if not w1 and not w2 and not w3 and not w4:
        plan = _fallback_plan_30_days()
        w1 = plan["week_1"]
        w2 = plan["week_2"]
        w3 = plan["week_3"]
        w4 = plan["week_4"]

    story.append(Paragraph("30-Day Direction", st["h1"]))
    story.append(Spacer(1, 6))

    # Pack weeks 1–4 onto 2 pages, grouped correctly
    story.append(_card_table("Week 1", w1, st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(_card_table("Week 2", w2, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(PageBreak())

    story.append(Paragraph("30-Day Direction (continued)", st["h1"]))
    story.append(Spacer(1, 6))
    story.append(_card_table("Week 3", w3, st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(_card_table("Week 4", w4, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(PageBreak())

    # ------------------- PAGE 6: SCORE + CTA -------------------
    score = bp.get("score")
    if not isinstance(score, int):
        score = 70

    improve = _shorten_list(bp.get("improve") or [], 4, max_words=8)
    if not improve:
        improve = ["Reply speed", "Clear next steps", "Less to remember"]

    story.append(Paragraph("Overall Business Health", st["h1"]))
    story.append(Paragraph("A simple score to show where you are.", st["small"]))
    story.append(Spacer(1, 10))
    story.append(_score_gauge(score, st))
    story.append(Spacer(1, 10))
    story.append(_card_table("Biggest areas to improve", improve, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 14))
    story.extend(_cta_block(st))

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


# --------------------------------------------------------------------
# OPTIONAL: Context lookup endpoints
# --------------------------------------------------------------------
@app.route("/context", methods=["GET"])
def context_lookup_query():
    phone = clean_value(request.args.get("phone"))
    if not phone:
        return jsonify({"success": False, "error": "missing phone query parameter"}), 400
    ctx = get_context_for_phone(phone)
    if not ctx:
        return jsonify({"success": False, "error": "no context found"}), 404
    return jsonify({"success": True, "context": ctx})


@app.route("/context/<phone>", methods=["GET"])
def context_lookup_path(phone: str):
    ctx = get_context_for_phone(phone)
    if not ctx:
        return jsonify({"success": False, "error": "no context found"}), 404
    return jsonify({"success": True, "context": ctx})


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

    # Lead basics
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

    # Business fields (optional; keep compatibility)
    business_name = _get_any(form_fields, ["business_name", "Business Name"])
    business_type = _get_any(form_fields, ["business_type", "Business Type"])

    # Your 5-question form fields (robust matching)
    services_offered = _get_any(form_fields, [
        "services_offered",
        "Services You Offer",
        "In a sentence or two, what do you sell or do?",
        "What do you sell or do?",
        "What do you do?",
    ])

    stress = _get_any(form_fields, [
        "frustrations",
        "What Frustrates You Most",
        "What feels hardest or most stressful right now?",
        "What feels hardest or most stressful right now",
    ])

    remember = _get_any(form_fields, [
        "bottlenecks",
        "Biggest Operational Bottlenecks",
        "What do you feel like you’re always trying to remember or keep track of?",
        "What do you feel like you're always trying to remember or keep track of?",
        "What are you always trying to remember?",
    ])

    leads_per_week = _get_any(form_fields, [
        "leads_per_week",
        "Leads Per Week",
        "About how many new leads or messages do you get in a week?",
        "About how many new leads or messages do you get in a week",
        "New customers/leads per week",
        "Leads/messages per week",
    ])

    jobs_per_week = _get_any(form_fields, [
        "jobs_per_week",
        "Jobs Per Week",
        "About how many jobs, orders, or clients do you handle in a week?",
        "About how many jobs, orders, or clients do you handle in a week",
        "Jobs/orders per week",
        "Jobs/orders/clients per week",
    ])

    leads_n = parse_int(leads_per_week)
    jobs_n = parse_int(jobs_per_week)

    # Rank the 3 locked fixes
    ranked = _pick_and_rank_fixes(services_offered, stress, remember)
    fix1, fix2, fix3 = ranked[0], ranked[1], ranked[2]

    # Base blueprint (stable, always works)
    bp: Dict[str, Any] = {
        "quick_snapshot": _fallback_quick_snapshot(business_name, services_offered, stress, remember, leads_n, jobs_n),
        "what_you_said": _shorten_list(
            [x for x in [
                ("You do: " + services_offered) if services_offered else "",
                ("Hardest right now: " + stress) if stress else "",
                ("You are trying to remember: " + remember) if remember else "",
                (f"You get about {leads_n} new messages a week.") if leads_n is not None else "",
                (f"You handle about {jobs_n} jobs a week.") if jobs_n is not None else "",
            ] if x],
            5,
            max_words=12
        ),
        "fix_1": {
            "name": fix1["name"],
            "what_this_fixes": fix1["what_this_fixes"],
            "what_this_does": fix1["what_this_does"],
            "whats_included": fix1["whats_included"],
        },
        "fix_2": {"name": fix2["name"], "short_summary": fix2["short_summary"]},
        "fix_3": {"name": fix3["name"], "short_summary": fix3["short_summary"]},
        "plan_30_days": _fallback_plan_30_days(),
        "improve": ["Reply speed", "Clear next steps", "Less to remember"],
        "score": _estimate_score(stress, remember, leads_n, jobs_n),
    }

    # Optional: model improves quick snapshot + plan (but never breaks the blueprint)
    try:
        model_part = _ask_model_for_parts(
            business_name=business_name,
            services=services_offered,
            stress=stress,
            remember=remember,
            leads_per_week=leads_per_week,
            jobs_per_week=jobs_per_week,
            fix1_name=fix1["name"],
        )
        if isinstance(model_part.get("quick_snapshot"), list) and model_part["quick_snapshot"]:
            bp["quick_snapshot"] = _shorten_list(model_part["quick_snapshot"], 6, max_words=12)
        if isinstance(model_part.get("improve"), list) and model_part["improve"]:
            bp["improve"] = _shorten_list(model_part["improve"], 4, max_words=8)
        if isinstance(model_part.get("plan_30_days"), dict) and model_part["plan_30_days"]:
            bp["plan_30_days"] = model_part["plan_30_days"]
    except Exception:
        pass

    # Build PDF
    pdf_id = uuid.uuid4().hex
    pdf_filename = f"business_blueprint_{pdf_id}.pdf"
    pdf_path = os.path.join("/tmp", pdf_filename)

    generate_pdf_blueprint(
        bp=bp,
        pdf_path=pdf_path,
        lead_name=name,
        business_name=business_name,
        business_type=business_type,
        leads_per_week=leads_per_week,
        jobs_per_week=jobs_per_week,
    )

    if not S3_BUCKET:
        return jsonify({"success": False, "error": "S3_BUCKET_NAME env var is not set"}), 500

    s3_key = f"blueprints/{pdf_filename}"
    s3_client.upload_file(
        Filename=pdf_path,
        Bucket=S3_BUCKET,
        Key=s3_key,
        ExtraArgs={"ContentType": "application/pdf", "ACL": "public-read"},
    )

    pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"

    # Human-readable text version
    blueprint_text = _bp_to_text(bp, lead_name=name, business_name=business_name)

    # Proposal-ready fields (pre-populate Fix 1/2/3 in your proposal)
    proposal_fields = {
        "fix_1_name": bp["fix_1"]["name"],
        "fix_1_what_this_fixes": bp["fix_1"]["what_this_fixes"],
        "fix_1_what_this_does": bp["fix_1"]["what_this_does"],
        "fix_1_whats_included": bp["fix_1"]["whats_included"],
        "fix_2_name": bp["fix_2"]["name"],
        "fix_2_short_summary": bp["fix_2"]["short_summary"],
        "fix_3_name": bp["fix_3"]["name"],
        "fix_3_short_summary": bp["fix_3"]["short_summary"],
        "score": bp.get("score", 70),
        "leads_per_week": leads_per_week or "",
        "jobs_per_week": jobs_per_week or "",
    }

    # Store context for later lookup
    context_blob = {
        "lead_name": name,
        "lead_email": email,
        "lead_phone_e164": phone_e164,
        "business_name": business_name,
        "business_type": business_type,
        "pdf_url": pdf_url,
        "proposal_fields": proposal_fields,
        "quick_snapshot": bp.get("quick_snapshot", []),
        "seconds": round(time.time() - t0, 2),
    }

    if phone_raw:
        store_context_for_phone(phone_raw, context_blob)

    return jsonify(
        {
            "success": True,
            "pdf_url": pdf_url,
            "blueprint_text": blueprint_text,
            "proposal_fields": proposal_fields,
            "name": name,
            "email": email,
            "phone_e164": phone_e164,
            "seconds": round(time.time() - t0, 2),
        }
    )


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

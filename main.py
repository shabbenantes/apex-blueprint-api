from flask import Flask, request, jsonify
import os
import uuid
import json
import re
import time
import math
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
    - key match ignoring punctuation differences
    """
    if not isinstance(form_fields, dict):
        return ""

    for k in keys:
        if k in form_fields:
            return clean_value(form_fields.get(k))

    lower_map = {str(k).strip().lower(): k for k in form_fields.keys()}
    for k in keys:
        lk = str(k).strip().lower()
        if lk in lower_map:
            return clean_value(form_fields.get(lower_map[lk]))

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
# VOLUME PARSING (ROBUST)
# --------------------------------------------------------------------
def _parse_range_or_number(s: str) -> Optional[float]:
    """
    Returns a float from:
    - "10", "about 10", "10.5"
    - "10-15", "10 to 15", "10–15" -> average
    - "1,200" -> 1200
    """
    s = clean_value(s)
    if not s:
        return None

    t = s.lower().replace(",", " ")
    t = t.replace("–", "-").replace("—", "-")
    t = re.sub(r"\bto\b", "-", t)

    nums = re.findall(r"(\d+(?:\.\d+)?)", t)
    if not nums:
        return None

    if re.search(r"\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?", t) and len(nums) >= 2:
        a = float(nums[0])
        b = float(nums[1])
        return (a + b) / 2.0

    return float(nums[0])


def _weekly_multiplier_from_text(s: str) -> float:
    """
    Detects time units and converts given number to "per week".
    Defaults to 1.0 (assume already weekly) if unknown.
    Supports: per day, daily, per month, monthly, per year, yearly.
    Also supports business days -> *5.
    """
    t = clean_value(s).lower()
    if not t:
        return 1.0

    if re.search(r"\b(per\s*week|weekly|wk|/wk|/week)\b", t):
        return 1.0

    if re.search(r"\b(per\s*day|daily|/day|a\s*day|each\s*day)\b", t):
        if re.search(r"\b(business\s*day|weekday|week\s*day)\b", t):
            return 5.0
        return 7.0

    if re.search(r"\b(per\s*month|monthly|/month)\b", t):
        return 1.0 / 4.33

    if re.search(r"\b(per\s*year|yearly|annually|/year)\b", t):
        return 1.0 / 52.0

    if "/d" in t:
        return 7.0
    if "/m" in t:
        return 1.0 / 4.33
    if "/y" in t:
        return 1.0 / 52.0

    return 1.0


def parse_volume_to_weekly(raw: str) -> Tuple[Optional[int], str]:
    """
    Converts user input into weekly integer for charts, with a normalized display string.
    """
    s = clean_value(raw)
    if not s:
        return None, ""

    n = _parse_range_or_number(s)
    if n is None:
        return None, ""

    mult = _weekly_multiplier_from_text(s)
    weekly = int(round(n * mult))

    base = f"{int(round(n))}" if n >= 1 else f"{n:.1f}"
    if mult == 1.0:
        return max(0, weekly), f"{weekly}/week" if str(weekly) in s else f"≈{weekly}/week"
    if mult == 7.0:
        return max(0, weekly), f"{base}/day → ≈{weekly}/week"
    if mult == 5.0:
        return max(0, weekly), f"{base}/business day → ≈{weekly}/week"
    if abs(mult - (1.0 / 4.33)) < 1e-6:
        return max(0, weekly), f"{base}/month → ≈{weekly}/week"
    if abs(mult - (1.0 / 52.0)) < 1e-6:
        return max(0, weekly), f"{base}/year → ≈{weekly}/week"

    return max(0, weekly), f"{base} → ≈{weekly}/week"


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
    text = " ".join([services or "", stress or "", remember or ""]).lower()

    score_1 = 0
    score_2 = 0
    score_3 = 0

    if any(k in text for k in [
        "lead", "leads", "inquiry", "inquiries", "message", "messages", "dm",
        "text", "reply", "respond", "response", "follow", "follow-up",
        "forgot", "forget", "miss", "missed", "ghost", "instagram", "facebook"
    ]):
        score_1 += 3
    if any(k in text for k in ["email", "website", "call", "phone"]):
        score_1 += 1

    if any(k in text for k in [
        "schedule", "scheduling", "calendar", "appointment", "appointments",
        "book", "booking", "reschedule", "no-show", "noshow", "availability"
    ]):
        score_2 += 3
    if "back and forth" in text or "back-and-forth" in text:
        score_2 += 2

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


def _estimate_score(stress: str, remember: str, leads_weekly: Optional[int], jobs_weekly: Optional[int]) -> int:
    """
    Follow-up slip risk score (0–100). Higher = more likely things slip.
    """
    risk = 35
    text = " ".join([stress or "", remember or ""]).lower()

    chaos_words = [
        "miss", "missed", "forgot", "forget",
        "overwhelm", "overwhelmed", "behind", "stress",
        "mess", "chaos", "dropped", "drop"
    ]
    for w in chaos_words:
        if w in text:
            risk += 6

    if leads_weekly is not None:
        if leads_weekly >= 50:
            risk += 18
        elif leads_weekly >= 20:
            risk += 10
        elif leads_weekly >= 10:
            risk += 6
    else:
        risk += 6

    if jobs_weekly is not None:
        if jobs_weekly >= 50:
            risk += 14
        elif jobs_weekly >= 20:
            risk += 8
        elif jobs_weekly >= 10:
            risk += 5
    else:
        risk += 6

    if any(k in text for k in ["schedule", "booking", "no-show", "calendar", "appointment"]):
        risk += 6

    return max(10, min(95, int(risk)))


# --------------------------------------------------------------------
# IMPROVEMENT AREAS
# --------------------------------------------------------------------
ALLOWED_IMPROVE_BUCKETS = [
    "Faster replies",
    "No missed messages",
    "Clear next steps",
    "Consistent follow-up",
    "Less back-and-forth scheduling",
    "Fewer no-shows",
    "Better handoffs",
    "Simple after-job check-ins",
    "More reviews over time",
    "Less to remember",
]


def _build_improve_list(stress: str, remember: str) -> List[str]:
    text = (" ".join([stress or "", remember or ""])).lower()
    out: List[str] = []

    out.append("Faster replies")
    out.append("Consistent follow-up")

    if any(k in text for k in ["appointment", "schedule", "calendar", "booking", "no-show", "noshow"]):
        out.append("Less back-and-forth scheduling")
    elif any(k in text for k in ["miss", "missed", "forgot", "forget", "inbox", "messages", "dm", "text"]):
        out.append("No missed messages")
    else:
        out.append("Clear next steps")

    if len(out) < 3:
        out.append("Clear next steps")

    seen = set()
    cleaned = []
    for x in out:
        if x in ALLOWED_IMPROVE_BUCKETS and x not in seen:
            cleaned.append(x)
            seen.add(x)

    while len(cleaned) < 3:
        if "Clear next steps" not in cleaned:
            cleaned.append("Clear next steps")
        elif "No missed messages" not in cleaned:
            cleaned.append("No missed messages")
        else:
            break

    return cleaned[:4]


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
        fontSize=32,
        leading=36,
        alignment=TA_CENTER,
        textColor=NAVY,
        spaceAfter=4,
    )

    subtitle = ParagraphStyle(
        "ApexSubtitle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceAfter=6,
    )

    h1 = ParagraphStyle(
        "ApexH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
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
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#111827"),
        spaceAfter=2,
    )

    small = ParagraphStyle(
        "ApexSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=14,
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
        fontSize=22,
        leading=24,
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
                ("TOPPADDING", (0, 0), (-1, -1), 10 + extra_padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10 + extra_padding),
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


def _bar_chart(title: str, labels: List[str], values: List[int], st, height: int = 115) -> Drawing:
    """
    Compact bar chart that fits on the cover.
    """
    height = int(height)
    plot_h = max(52, height - 52)

    d = Drawing(460, height)
    d.add(String(0, height - 14, title, fontName="Helvetica-Bold", fontSize=11, fillColor=st["NAVY"]))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 16
    bc.width = 380
    bc.height = plot_h
    bc.data = [values]

    bc.strokeColor = colors.transparent
    bc.bars[0].fillColor = st["BLUE"]

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = st["MUTED"]

    vmax = max(values + [10])
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = int(vmax * 1.25) if vmax > 0 else 10
    bc.valueAxis.valueStep = max(1, int(bc.valueAxis.valueMax / 5))
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = st["MUTED"]

    d.add(bc)
    return d


def _estimate_admin_hours(leads_weekly: int, jobs_weekly: int) -> Dict[str, int]:
    """
    Simple + believable estimate in HOURS/WEEK.
    """
    leads_weekly = max(0, int(leads_weekly))
    jobs_weekly = max(0, int(jobs_weekly))

    replying = (leads_weekly * 4) / 60.0
    scheduling = (jobs_weekly * 6) / 60.0
    follow_up = (jobs_weekly * 3) / 60.0

    return {
        "Replying": int(round(replying)),
        "Scheduling": int(round(scheduling)),
        "Follow-up": int(round(follow_up)),
    }


def _slip_risk_gauge(score: int, st) -> Drawing:
    """
    Clarified + labeled score that makes sense to a business owner.
    """
    score = max(0, min(100, int(score)))
    w = 460
    h = 82
    pad_x = 10
    bar_y = 28
    bar_h = 14
    bar_w = w - (pad_x * 2)

    d = Drawing(w, h)
    d.add(String(0, 62, "Follow-Up Slip Risk (0–100)", fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))
    d.add(String(
        0, 48,
        "How likely follow-up and next steps get missed when things get busy.",
        fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]
    ))

    d.add(String(pad_x, 10, "low", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))
    d.add(String(pad_x + int(bar_w / 2) - 12, 10, "medium", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))
    d.add(String(pad_x + bar_w - 18, 10, "high", fontName="Helvetica", fontSize=9, fillColor=st["MUTED"]))

    d.add(Rect(pad_x, bar_y, bar_w, bar_h, strokeColor=st["BORDER"], fillColor=st["SOFT"], strokeWidth=1))
    fill_w = int(bar_w * (score / 100.0))
    d.add(Rect(pad_x, bar_y, fill_w, bar_h, strokeColor=None, fillColor=st["BLUE"]))

    label_x = pad_x + fill_w
    label_x = max(pad_x + 18, min(pad_x + bar_w - 18, label_x))
    d.add(String(label_x - 10, 56, f"{score}", fontName="Helvetica-Bold", fontSize=12, fillColor=st["NAVY"]))
    return d


def _what_i_help_with_block(st) -> Table:
    bullets = [
        "I help you reply fast to new people.",
        "I help you follow up without forgetting.",
        "I help make booking feel simple.",
        "I help reduce no-shows with reminders.",
        "I help with simple after-job check-ins.",
    ]
    return _card_table("What I can help with", bullets, st, bg=st["CARD_BG"], placeholder_if_empty=False)


def _next_step_cta_block(st) -> Table:
    """
    CTA-style 'what to do next' that naturally follows 'What I can help with'.
    """
    bullets = [
        "Book a quick call.",
        "We’ll walk through your blueprint together.",
        "We’ll pick the first fix and map simple next steps.",
    ]
    return _card_table("What to do next", bullets, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False)


def _cta_block(st) -> List[Any]:
    url = CALENDAR_URL

    # New: clear "what the call is" section + length (15–20 min)
    call_details = _card_table(
        "What the call is",
        [
            "15–20 minutes.",
            "We review your blueprint and your biggest bottleneck.",
            "You leave with a simple first step and a clear plan.",
        ],
        st,
        bg=st["CARD_BG"],
        placeholder_if_empty=False,
    )

    # Existing expanded CTA explanation
    title = _card_table(
        "Want help implementing this?",
        [
            "We’ll walk through your blueprint together.",
            "We’ll pick the first fix that makes the biggest difference.",
            "If it fits, we’ll talk through simple implementation.",
            "No pressure. Just a calm plan.",
        ],
        st,
        bg=st["CARD_BG_ALT"],
        placeholder_if_empty=False,
    )

    btn_text = f'<link href="{safe_p(url)}" color="white"><b>Book a quick call →</b></link>'

    btn = Table(
        [[Paragraph(btn_text, st["cta_btn"])]],
        colWidths=[7.44 * inch],
        rowHeights=[0.95 * inch],
        hAlign="LEFT",
    )
    btn.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), st["BLUE_DK"]),
                ("BOX", (0, 0), (-1, -1), 1, st["BLUE_DK"]),
                ("TOPPADDING", (0, 0), (-1, -1), 22),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 22),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    # KeepTogether so the booking page stays visually grouped
    return [KeepTogether([call_details, Spacer(1, 12), title, Spacer(1, 12), btn])]


# --------------------------------------------------------------------
# BLUEPRINT CONTENT
# --------------------------------------------------------------------
def _diagnosis_summary(services: str, stress: str, remember: str, leads_weekly: Optional[int], jobs_weekly: Optional[int]) -> List[str]:
    out: List[str] = []

    if leads_weekly is not None:
        out.append(f"You get about {leads_weekly} messages each week.")
    if jobs_weekly is not None:
        out.append(f"You handle about {jobs_weekly} jobs each week.")

    if stress:
        out.append("The hardest part feels heavy right now.")
    if remember:
        out.append("Too much is living in your head.")

    out.append("When messages slip, money slips.")
    out.append("A simple system can make things calm.")

    return _shorten_list(out, 6, max_words=12)


def _what_you_told_me(services: str, stress: str, remember: str, leads_norm: str, jobs_norm: str) -> List[str]:
    out: List[str] = []
    if services:
        out.append(f"Your business: {services}")
    if stress:
        out.append(f"What feels hardest right now: {stress}")
    if remember:
        out.append(f"What keeps slipping: {remember}")

    if leads_norm:
        out.append(f"New leads/messages: {leads_norm}")
    if jobs_norm:
        out.append(f"Jobs/orders handled: {jobs_norm}")

    if not out:
        out = ["You want fewer missed things.", "You want a clear next step."]
    return _shorten_list(out, 6, max_words=12)


def _plan_30_days_aligned() -> Dict[str, List[str]]:
    return {
        "week_1": [
            "Get all new messages in one place.",
            "Send a fast first reply every time.",
            "Stop leads from slipping through cracks.",
        ],
        "week_2": [
            "Make follow-up automatic and simple.",
            "Keep one clear next step per lead.",
            "Cut down on “just checking in” chaos.",
        ],
        "week_3": [
            "Make booking easy with one link.",
            "Send reminders so people show up.",
            "Reduce back-and-forth scheduling.",
        ],
        "week_4": [
            "Add simple after-job check-ins.",
            "Ask for reviews the easy way.",
            "Keep the system running smoothly.",
        ],
    }


def _ask_model_for_parts(
    business_name: str,
    services: str,
    stress: str,
    remember: str,
    leads_raw: str,
    jobs_raw: str,
    fix1_name: str,
) -> dict:
    prompt = f"""
Write for a stressed business owner.
Third-grade reading level.
Short sentences. No tech words.

IMPORTANT:
- Do NOT mention inventory systems, ads, SEO, or marketing strategy.
- Keep it in this lane only: missed messages, follow-up, scheduling, no-shows, after-job check-ins, reviews.

Business name: {business_name or "Your Business"}
What they do: {services or "Not provided"}
Hardest right now: {stress or "Not provided"}
Always trying to remember: {remember or "Not provided"}
Leads/messages (raw): {leads_raw or "Not provided"}
Jobs/orders (raw): {jobs_raw or "Not provided"}

Best first fix is: {fix1_name}

Return ONLY valid JSON in this exact shape:

{{
  "quick_snapshot": ["...", "...", "...", "..."]
}}

Rules:
- quick_snapshot = 4 to 6 bullets
- bullets must stay inside the allowed lane above
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


# --------------------------------------------------------------------
# PDF GENERATION
# --------------------------------------------------------------------
def generate_pdf_blueprint(
    bp: dict,
    pdf_path: str,
    lead_name: str,
    business_name: str,
    business_type: str,
    leads_weekly: Optional[int],
    jobs_weekly: Optional[int],
    leads_norm: str,
    jobs_norm: str,
    risk_score: int,
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

    # ------------------- PAGE 1: COVER -------------------
    story.append(Spacer(1, 8))
    story.append(Paragraph(safe_p(business_name) if business_name else "Your Business", st["title"]))
    story.append(Paragraph(safe_p(business_type) if business_type else "Business", st["subtitle"]))

    cover_lines = [
        f"Prepared for: {safe_p(lead_name) if lead_name else 'Business Owner'}",
        f"Leads/messages: {safe_p(leads_norm) if leads_norm else 'Not specified'}",
        f"Jobs/orders: {safe_p(jobs_norm) if jobs_norm else 'Not specified'}",
    ]
    story.append(_card_table("Snapshot", cover_lines, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))

    story.append(Spacer(1, 6))
    story.append(Paragraph("If your business feels harder than it should, there’s usually a reason.", st["body"]))
    story.append(Spacer(1, 8))

    if leads_weekly is not None and jobs_weekly is not None:
        story.append(Paragraph("Workload Snapshot (weekly)", st["h1"]))
        story.append(
            _bar_chart(
                "Leads/messages vs jobs/orders (per week)",
                ["Leads/messages", "Jobs/orders"],
                [int(leads_weekly), int(jobs_weekly)],
                st,
                height=112,
            )
        )
        story.append(Spacer(1, 8))

        est = _estimate_admin_hours(int(leads_weekly), int(jobs_weekly))
        story.append(
            _bar_chart(
                "Estimated admin time (hours per week)",
                list(est.keys()),
                list(est.values()),
                st,
                height=112,
            )
        )

    story.append(PageBreak())

    # ------------------- PAGE 2: DIAGNOSIS -------------------
    story.append(Paragraph("What this means", st["h1"]))
    story.append(Paragraph("A quick diagnosis in plain words.", st["small"]))
    story.append(Spacer(1, 6))
    story.append(_card_table("Quick Snapshot", bp.get("quick_snapshot", []) or [], st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 12))
    story.append(_card_table("What you told me", bp.get("what_you_told_me", []) or [], st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(PageBreak())

    # ------------------- PAGE 3: FIX #1 -------------------
    fix1 = bp.get("fix_1") or {}
    fix1_name = fix1.get("name", "Fix #1")

    story.append(Paragraph("What to fix first", st["h1"]))
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

    # ------------------- PAGE 4: FIX #2/#3 + WEEK 1/2 -------------------
    fix2 = bp.get("fix_2") or {}
    fix3 = bp.get("fix_3") or {}

    story.append(Paragraph("What can come next", st["h1"]))
    story.append(Paragraph("Helpful later. Not required right now.", st["small"]))
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

    story.append(Paragraph("30-Day Direction", st["h1"]))
    story.append(Spacer(1, 6))
    story.append(_card_table("Week 1", w1, st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(_card_table("Week 2", w2, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(PageBreak())

    # ------------------- PAGE 5: WEEK 3/4 + QUICK WINS + SLIP RISK (BOTTOM) -------------------
    w3 = _shorten_list(plan.get("week_3", []) or [], 3, max_words=10)
    w4 = _shorten_list(plan.get("week_4", []) or [], 3, max_words=10)

    story.append(Paragraph("30-Day Direction (continued)", st["h1"]))
    story.append(Spacer(1, 6))
    story.append(_card_table("Week 3", w3, st, bg=st["CARD_BG"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(_card_table("Week 4", w4, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 12))

    quick_wins = [
        "Reply in 5 minutes when you can.",
        "Use one short saved reply.",
        "Write the next step right away.",
    ]
    story.append(_card_table("Quick wins you can do this week", quick_wins, st, bg=st["CARD_BG"], placeholder_if_empty=False))

    # Move Slip Risk to bottom of Page 5 (your request)
    story.append(Spacer(1, 14))
    story.append(_slip_risk_gauge(risk_score, st))

    story.append(PageBreak())

    # ------------------- PAGE 6: STRESS + HELP + NEXT STEP (CTA STYLE) -------------------
    improve = bp.get("improve") or []
    improve = _shorten_list(improve, 4, max_words=6)
    if not improve:
        improve = ["Faster replies", "Consistent follow-up", "Clear next steps"]

    while len(improve) < 3:
        if "Clear next steps" not in improve:
            improve.append("Clear next steps")
        else:
            improve.append("No missed messages")
            break

    story.append(Paragraph("What’s most likely causing stress", st["h1"]))
    story.append(Paragraph("This is the part that usually slips first.", st["small"]))
    story.append(Spacer(1, 8))

    story.append(_card_table("Big areas causing stress", improve, st, bg=st["CARD_BG_ALT"], placeholder_if_empty=False))
    story.append(Spacer(1, 10))
    story.append(_what_i_help_with_block(st))
    story.append(Spacer(1, 10))

    # Replace generic "next steps" with CTA-oriented next step block
    story.append(_next_step_cta_block(st))

    story.append(PageBreak())

    # ------------------- PAGE 7: BOOKING CTA PAGE (EXTRA EXPLANATION + BUTTON) -------------------
    story.append(Spacer(1, 18))
    story.extend(_cta_block(st))

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


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

    business_name = _get_any(form_fields, ["business_name", "Business Name"])
    business_type = _get_any(form_fields, ["business_type", "Business Type"])

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

    leads_raw = _get_any(form_fields, [
        "leads_per_week",
        "Leads Per Week",
        "About how many new leads or messages do you get in a week?",
        "About how many new leads or messages do you get in a week",
        "New customers/leads per week",
        "Leads/messages per week",
    ])

    jobs_raw = _get_any(form_fields, [
        "jobs_per_week",
        "Jobs Per Week",
        "About how many jobs, orders, or clients do you handle in a week?",
        "About how many jobs, orders, or clients do you handle in a week",
        "Jobs/orders per week",
        "Jobs/orders/clients per week",
    ])

    leads_weekly, leads_norm = parse_volume_to_weekly(leads_raw)
    jobs_weekly, jobs_norm = parse_volume_to_weekly(jobs_raw)

    ranked = _pick_and_rank_fixes(services_offered, stress, remember)
    fix1, fix2, fix3 = ranked[0], ranked[1], ranked[2]

    improve = _build_improve_list(stress, remember)
    plan_30 = _plan_30_days_aligned()
    risk_score = _estimate_score(stress, remember, leads_weekly, jobs_weekly)

    bp: Dict[str, Any] = {
        "quick_snapshot": _diagnosis_summary(services_offered, stress, remember, leads_weekly, jobs_weekly),
        "what_you_told_me": _what_you_told_me(services_offered, stress, remember, leads_norm, jobs_norm),
        "fix_1": {
            "name": fix1["name"],
            "what_this_fixes": fix1["what_this_fixes"],
            "what_this_does": fix1["what_this_does"],
            "whats_included": fix1["whats_included"],
        },
        "fix_2": {"name": fix2["name"], "short_summary": fix2["short_summary"]},
        "fix_3": {"name": fix3["name"], "short_summary": fix3["short_summary"]},
        "plan_30_days": plan_30,
        "improve": improve,
        "score": risk_score,
    }

    # Optional model polish for quick snapshot only (safe)
    try:
        model_part = _ask_model_for_parts(
            business_name=business_name,
            services=services_offered,
            stress=stress,
            remember=remember,
            leads_raw=leads_raw,
            jobs_raw=jobs_raw,
            fix1_name=fix1["name"],
        )
        if isinstance(model_part.get("quick_snapshot"), list) and model_part["quick_snapshot"]:
            qs = _shorten_list([_strip_bullet_prefix(str(x)) for x in model_part["quick_snapshot"]], 6, max_words=12)
            if qs:
                bp["quick_snapshot"] = qs
    except Exception:
        pass

    pdf_id = uuid.uuid4().hex
    pdf_filename = f"business_blueprint_{pdf_id}.pdf"
    pdf_path = os.path.join("/tmp", pdf_filename)

    generate_pdf_blueprint(
        bp=bp,
        pdf_path=pdf_path,
        lead_name=name,
        business_name=business_name,
        business_type=business_type,
        leads_weekly=leads_weekly,
        jobs_weekly=jobs_weekly,
        leads_norm=leads_norm,
        jobs_norm=jobs_norm,
        risk_score=risk_score,
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

    proposal_fields = {
        "fix_1_name": bp["fix_1"]["name"],
        "fix_1_what_this_fixes": bp["fix_1"]["what_this_fixes"],
        "fix_1_what_this_does": bp["fix_1"]["what_this_does"],
        "fix_1_whats_included": bp["fix_1"]["whats_included"],
        "fix_2_name": bp["fix_2"]["name"],
        "fix_2_short_summary": bp["fix_2"]["short_summary"],
        "fix_3_name": bp["fix_3"]["name"],
        "fix_3_short_summary": bp["fix_3"]["short_summary"],
        "slip_risk_score": bp.get("score", 70),
        "leads_raw": leads_raw or "",
        "jobs_raw": jobs_raw or "",
        "leads_weekly": leads_weekly if leads_weekly is not None else "",
        "jobs_weekly": jobs_weekly if jobs_weekly is not None else "",
        "leads_normalized": leads_norm or "",
        "jobs_normalized": jobs_norm or "",
    }

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


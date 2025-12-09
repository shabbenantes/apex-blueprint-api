
from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
import json
from openai import OpenAI

# PDF generation imports
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors

app = Flask(__name__)

# OpenAI client using env var (set OPENAI_API_KEY in Render)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def generate_pdf(blueprint_text: str, pdf_path: str, name: str, business_name: str):
    """
    Turn the blueprint text into a clean, branded PDF.
    """
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#000000"),
        spaceAfter=12,
    )

    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=12,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
        spaceAfter=18,
    )

    heading_style = ParagraphStyle(
        "HeadingStyle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=colors.HexColor("#000000"),
        spaceBefore=12,
        spaceAfter=6,
    )

    body_style = ParagraphStyle(
        "BodyStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#222222"),
        spaceAfter=6,
    )

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Business Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )

    story = []

    # Header / cover (basic for now – we can upgrade later)
    story.append(Paragraph("Apex Automation", title_style))
    story.append(
        Paragraph("AI Business Blueprint for Your Service Business", subtitle_style)
    )

    owner_line = f"For: {name if name else 'Your Business Owner'}"
    if business_name:
        owner_line += f"  |  Business: {business_name}"

    story.append(Paragraph(owner_line, body_style))
    story.append(Spacer(1, 18))

    # Intro block
    story.append(
        Paragraph(
            "This document outlines where your business is currently leaking time and money, and the simplest automation wins to fix it over the next 30 days.",
            body_style,
        )
    )
    story.append(Spacer(1, 12))

    # Split the blueprint text into chunks by double line breaks
    sections = blueprint_text.split("\n\n")

    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue

        # Simple handling: if a line starts with '#', treat it as a heading
        if stripped.startswith("# "):
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 12))
            story.append(Paragraph(heading_text, heading_style))

        elif stripped.startswith("## "):
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 8))
            story.append(Paragraph(heading_text, heading_style))

        else:
            # Regular paragraph
            story.append(Paragraph(stripped.replace("\n", "<br/>"), body_style))

    # Final CTA
    story.append(Spacer(1, 18))
    story.append(
        Paragraph(
            "<b>Next Step:</b> Book a quick strategy call so we can walk through this blueprint together and decide what to build first.",
            body_style,
        )
    )
    story.append(
        Paragraph(
            "On the call, we’ll help you prioritize the fastest wins for more booked jobs, fewer missed calls, and 10–20 hours back per week.",
            body_style,
        )
    )

    doc.build(story)


# ------------ APEX MULTI-STEP BLUEPRINT ENGINE HELPERS ------------

def call_openai(prompt: str) -> str:
    """
    Small helper to call the AI and return plain text.
    If anything goes wrong, we return a safe fallback message.
    """
    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        return resp.output[0].content[0].text
    except Exception as e:
        print("OpenAI error while generating section:", e)
        return "There was an issue generating this section. Please contact us so we can re-send your blueprint."


def build_industry_context(raw_form_text: str) -> dict:
    """
    Ask the model to classify the business into an industry and return
    JSON we can pass into later prompts.
    """
    prompt = f"""
You are APEX AI, a consultant for home-service companies.

Your job is to identify the business type based on the owner's answers
and output a small JSON object.

Owner's raw answers:
{raw_form_text}

Respond ONLY with valid JSON (no backticks, no commentary) in this structure:

{{
  "industry": "one of: plumbing, hvac, electrical, cleaning, lawn care, landscaping, pest control, roofing, pressure washing, handyman, remodeling, painting, pool service, general service",
  "description": "2-3 sentences describing what this business does",
  "job_value_range": "e.g. Jobs typically range from $300–$1,800",
  "common_leaks": [
    "short bullet about a real money/time leak",
    "another bullet"
  ],
  "biggest_opportunities": [
    "short bullet about a high-impact opportunity",
    "another bullet"
  ]
}}
"""
    raw = call_openai(prompt).strip()
    try:
        data = json.loads(raw)
        return data
    except Exception as e:
        print("Error parsing industry JSON:", e, "RAW:", raw)
        # Safe fallback
        return {
            "industry": "general service",
            "description": "A local home-service business.",
            "job_value_range": "Jobs typically range from $150–$1,500.",
            "common_leaks": [
                "Missed calls and slow response to new leads.",
                "No consistent follow-up with quotes or estimates.",
            ],
            "biggest_opportunities": [
                "Faster response to new leads.",
                "Automatic follow-up to win more booked jobs.",
            ],
        }


def build_summary_section(name: str, business_name: str, industry_info: dict, raw_form_text: str) -> str:
    prompt = f"""
You are APEX AI, a calm, confident business automation consultant for home-service companies.

Write a short “1-Page Business Summary” for this owner.

Use only simple business language.
No tech jargon. No tool names.

Contact name: {name}
Business name: {business_name if business_name else "Not specified"}

Industry info:
{json.dumps(industry_info, indent=2)}

Owner's raw answers:
{raw_form_text}

Write 5–8 bullet points that clearly explain:
- what type of business this is
- their biggest bottlenecks
- where they are losing time
- where they are losing money
- the largest opportunities automation could unlock
- what will change for them if these problems are fixed

Keep it concise, clear, and written directly to the owner ("you", "your business").
"""
    return call_openai(prompt).strip()


def build_top_wins_section(industry_info: dict, raw_form_text: str) -> str:
    prompt = f"""
You are APEX AI. Create the "Top 3 Automation Wins" section for this business.

Industry info:
{json.dumps(industry_info, indent=2)}

Owner's raw answers:
{raw_form_text}

Your output must contain exactly 3 wins.

For each win, use this structure:

### WIN: Short, outcome-focused title

**What this fixes in your business:**
- 2–4 bullets of the pain it solves

**What this does for you:**
- 3–5 bullets explaining the outcomes (more booked jobs, fewer missed calls, less stress, saved time, more revenue)

**Included in this win:**
- 3–6 items described in plain business language
  (Example: "Instant text reply to new leads", "Automatic follow-up messages", "After-hours call capture")

Everything must match the industry and sound like a premium consultant wrote it.
Do NOT mention specific tools or software.
Only describe what happens for the business.
"""
    return call_openai(prompt).strip()


def build_cost_section(industry_info: dict) -> str:
    prompt = f"""
You are APEX AI. Create the "Cost of Doing Nothing" section for this business.

Industry info:
{json.dumps(industry_info, indent=2)}

Estimate realistic ranges for:
- missed call losses
- slow response losses
- no follow-up losses
- no review system losses
- no reactivation losses

Output:
1) A short paragraph (3–5 sentences) explaining the financial risk in plain English.

2) A clean table in text with 3 columns:
Problem | Monthly Loss | Annual Loss

Make the numbers realistic but eye-opening for this industry.
Keep everything easy to read.
"""
    return call_openai(prompt).strip()


def build_automation_map_section(industry_info: dict) -> str:
    prompt = f"""
You are APEX AI. Create an "Automation Map" showing the simplified customer journey
and where automation improves it, for this specific home-service industry.

Industry info:
{json.dumps(industry_info, indent=2)}

Use ASCII-style clean visuals. No emojis.

Example layout style (you will adapt wording to fit the industry):

NEW LEADS
   ↓
INSTANT RESPONSE
   ↓
FOLLOW-UP UNTIL BOOKED
   ↓
JOB SCHEDULED
   ↓
AFTER-JOB REVIEW FLOW
   ↓
REPEAT CUSTOMER / REACTIVATION

Keep it simple, clean, and easy to understand.
"""
    return call_openai(prompt).strip()


def build_30_day_plan_section(industry_info: dict, raw_form_text: str) -> str:
    prompt = f"""
You are APEX AI. Build a 30-Day Game Plan for this business.

Industry info:
{json.dumps(industry_info, indent=2)}

Owner's raw answers:
{raw_form_text}

Output 4 weeks. Each week has 3–5 bullets.

Use this structure:

### Week 1 — Stabilize
- ...

### Week 2 — Increase Booked Jobs
- ...

### Week 3 — Improve Customer Experience
- ...

### Week 4 — Scale and Optimize
- ...

Focus on business outcomes (more booked jobs, fewer missed calls, better follow-up, more reviews, time saved).
Do NOT describe technical setup steps.
"""
    return call_openai(prompt).strip()


def build_what_you_told_me_section(raw_form_text: str) -> str:
    prompt = f"""
You are APEX AI. Rewrite the owner's answers into clear categories.

Owner's raw answers:
{raw_form_text}

Create these sections:

### Your Goals
- ...

### Your Challenges
- ...

### Where You're Losing Time
- ...

### Missed Revenue Opportunities
- ...

Rewrite everything in fresh language.
Do NOT copy their sentences directly.
Make it feel like a consultant summarizing their situation back to them.
"""
    return call_openai(prompt).strip()


def build_final_recommendations_section(industry_info: dict, raw_form_text: str) -> str:
    prompt = f"""
You are APEX AI. Create the "Final Recommendations" section.

Industry info:
{json.dumps(industry_info, indent=2)}

Owner's raw answers:
{raw_form_text}

Include 4–7 bullets that cover:
- What they should prioritize first
- What will move the needle fastest
- A reminder they don't need to fix everything at once
- Why focusing on fewer things done well is better
- What they should have ready before an automation strategy call

Tone: calm, confident, consultant-like.
Do not sell or push.
Just guide them clearly.
"""
    return call_openai(prompt).strip()


@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    Takes the contact + form answers, generates a blueprint,
    generates a PDF, and returns everything as JSON.
    """
    data = request.get_json(force=True) or {}

    # Try to pull out contact info from typical payload shapes
    contact = data.get("contact", {}) or data.get("contact_data", {})
    form_fields = data.get("form_fields", {}) or data.get("form", {}) or {}

    # Safe fallbacks so it never crashes if a key is missing
    name = (
        contact.get("first_name")
        or contact.get("firstName")
        or contact.get("name")
        or "there"
    )
    email = contact.get("email", "")
    business_name = (
        form_fields.get("business_name")
        or form_fields.get("Business Name")
        or form_fields.get("business")
        or ""
    )

    # Keep raw form fields as a string so the model sees everything
    raw_form_text_lines = []
    for key, value in form_fields.items():
        raw_form_text_lines.append(f"{key}: {value}")
    raw_form_text = "\n".join(raw_form_text_lines) if raw_form_text_lines else "N/A"

    try:
        # 1) Industry context
        industry_info = build_industry_context(raw_form_text)

        # 2) Sections
        summary_section = build_summary_section(name, business_name, industry_info, raw_form_text)
        wins_section = build_top_wins_section(industry_info, raw_form_text)
        cost_section = build_cost_section(industry_info)
        map_section = build_automation_map_section(industry_info)
        plan_section = build_30_day_plan_section(industry_info, raw_form_text)
        what_you_told_me_section = build_what_you_told_me_section(raw_form_text)
        final_recs_section = build_final_recommendations_section(industry_info, raw_form_text)

        # 3) Assemble final blueprint text
        blueprint_parts = []

        blueprint_parts.append("# AI Automation Blueprint\n")
        blueprint_parts.append(f"Prepared for: {name}\n")
        if business_name:
            blueprint_parts.append(f"Business: {business_name}\n")
        blueprint_parts.append("")

        blueprint_parts.append("## 1. Your 1-Page Summary\n")
        blueprint_parts.append(summary_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 2. Your Top 3 Automation Wins\n")
        blueprint_parts.append(wins_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 3. The Cost of Doing Nothing\n")
        blueprint_parts.append(cost_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 4. Your Automation Map\n")
        blueprint_parts.append(map_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 5. Your 30-Day Game Plan\n")
        blueprint_parts.append(plan_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 6. What You Told Me\n")
        blueprint_parts.append(what_you_told_me_section)
        blueprint_parts.append("")

        blueprint_parts.append("## 7. Final Recommendations\n")
        blueprint_parts.append(final_recs_section)
        blueprint_parts.append("")

        blueprint_text = "\n\n".join(blueprint_parts)

        # For the email "summary", just use the summary section alone
        email_summary = summary_section

        # Generate a unique PDF file in /tmp (for now – later we'll move to S3)
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # Build a URL that your automation system can use
        base_url = request.host_url.rstrip("/")
        pdf_url = f"{base_url}/pdf/{pdf_id}"

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,  # full document
                "summary": email_summary,     # quick overview section
                "pdf_url": pdf_url,           # link to the PDF
                "name": name,
                "email": email,
                "business_name": business_name,
                "industry": industry_info.get("industry", "general service"),
            }
        )

    except Exception as e:
        print("Error generating blueprint:", e)
        return jsonify(
            {
                "success": False,
                "error": str(e),
            }
        ), 500


@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    """
    Serve the generated PDF files from /tmp.
    """
    pdf_dir = "/tmp"
    filename = f"blueprint_{pdf_id}.pdf"
    return send_from_directory(pdf_dir, filename, mimetype="application/pdf")


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

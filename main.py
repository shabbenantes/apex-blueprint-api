from flask import Flask, request, jsonify
import os
import uuid

from openai import OpenAI
import boto3

# PDF generation imports
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors

app = Flask(__name__)

# ---------- OpenAI ----------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ---------- S3 CONFIG ----------
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-2")

s3_client = boto3.client("s3", region_name=S3_REGION)

# ---------- BOOKING URL (for CTA in PDF) ----------
BOOKING_URL = os.environ.get("BOOKING_URL", "")


# --------------------------------------------------------------------
# PDF GENERATION (with CTA)
# --------------------------------------------------------------------
def generate_pdf(
    blueprint_text: str,
    pdf_path: str,
    name: str,
    business_name: str,
    booking_url: str,
):
    """
    Turn the blueprint text into a clean, branded PDF with clearer sections,
    plus a booking CTA at the end.
    """
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0A1A2F"),  # deep navy
        spaceAfter=6,
    )

    tagline_style = ParagraphStyle(
        "TaglineStyle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=12,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
        spaceAfter=16,
    )

    small_label_style = ParagraphStyle(
        "SmallLabelStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#777777"),
        spaceAfter=4,
    )

    heading_style = ParagraphStyle(
        "HeadingStyle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=colors.HexColor("#0A1A2F"),
        spaceBefore=16,
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

    cta_heading_style = ParagraphStyle(
        "CTAHeadingStyle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0A7FFF"),
        spaceBefore=18,
        spaceAfter=8,
    )

    cta_body_style = ParagraphStyle(
        "CTABodyStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#222222"),
        spaceAfter=4,
    )

    cta_link_style = ParagraphStyle(
        "CTALinkStyle",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0A7FFF"),
        spaceAfter=10,
    )

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        title="AI Automation Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )

    story = []

    # ------- COVER BLOCK -------
    story.append(Paragraph("Apex Automation", title_style))
    story.append(
        Paragraph("AI Automation Blueprint for Your Service Business", tagline_style)
    )

    owner_line = f"Prepared for: {name if name else 'Your Business Owner'}"
    if business_name:
        owner_line += f"  •  Business: {business_name}"

    story.append(Paragraph(owner_line, small_label_style))
    story.append(Paragraph("30-Day Automation Roadmap", small_label_style))
    story.append(Spacer(1, 18))

    # Simple horizontal rule
    story.append(
        Paragraph(
            "<para alignment='center'><font size=8 color='#CCCCCC'>"
            "────────────────────────────"
            "</font></para>",
            small_label_style,
        )
    )
    story.append(Spacer(1, 12))

    # Short intro
    story.append(
        Paragraph(
            "This blueprint shows where your business is currently leaking time and money, "
            "and the simplest automation wins to fix it over the next 30 days.",
            body_style,
        )
    )
    story.append(Spacer(1, 12))

    # ------- BODY FROM BLUEPRINT TEXT -------
    sections = blueprint_text.split("\n\n")

    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue

        # Detect headings from markdown-style text
        if stripped.startswith("# "):
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 8))
            story.append(Paragraph(heading_text, heading_style))

        elif stripped.startswith("## "):
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 6))
            story.append(Paragraph(heading_text, heading_style))

        else:
            # Render bullets and normal paragraphs
            story.append(Paragraph(stripped.replace("\n", "<br/>"), body_style))

    # ------- BOOKING CTA BLOCK -------
    story.append(Spacer(1, 24))
    story.append(Paragraph("Ready To Implement This Blueprint?", cta_heading_style))
    story.append(
        Paragraph(
            "The fastest way to turn this into more booked jobs, fewer missed calls, "
            "and 10–20 hours per week back is to walk through it together on a quick call.",
            cta_body_style,
        )
    )
    story.append(
        Paragraph(
            "On your strategy call, we’ll prioritize your top wins and decide exactly "
            "what to build first in the next 30 days.",
            cta_body_style,
        )
    )

    if booking_url:
        story.append(Spacer(1, 6))
        story.append(
            Paragraph(
                f"<link href='{booking_url}'>👉 Click here to book your Automation Strategy Call</link>",
                cta_link_style,
            )
        )

    doc.build(story)


# --------------------------------------------------------------------
# /run – SINGLE-PROMPT BLUEPRINT GENERATION
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    Takes the contact + form answers, generates a blueprint in 1 AI call,
    generates a PDF, uploads it to S3 (public), and returns everything as JSON.
    """
    data = request.get_json(force=True) or {}

    # Contact + form info
    contact = data.get("contact", {}) or data.get("contact_data", {})
    form_fields = data.get("form_fields", {}) or data.get("form", {}) or {}

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

    # Raw form text for the model
    raw_form_text_lines = [f"{k}: {v}" for k, v in form_fields.items()]
    raw_form_text = "\n".join(raw_form_text_lines) if raw_form_text_lines else "N/A"

    # --------- SINGLE PROMPT WITH IMPROVED RULES ----------
    prompt = f"""
You are APEX AI, a business automation consultant for home service companies.
Your job is to create a clean, premium, easy-to-read AI Automation Blueprint
based on the owner's answers.

Owner name: {name}
Business name: {business_name if business_name else "Not specified"}

Owner's raw answers:
{raw_form_text}

STYLE RULES (apply to EVERYTHING you write):
- Use SIMPLE business language (no jargon: no “CRM”, no “API”, no “backend”)
- Be extremely clear and concrete
- Sound like a calm, professional consultant
- Talk directly to the owner using “you” and “your business”
- Prefer bullet points over long paragraphs
- Each bullet must be a single sentence and under 20 words
- Do NOT repeat the same idea in different words across bullets or sections
- Do NOT refer to “the form” or “questions”
- Do NOT give step-by-step tech instructions
- Do NOT talk about specific tools or software
- Keep sections tight, clean, and easy to scan
- Always put a blank line between headings and text

Now write the FULL blueprint in Markdown with the following structure.
Follow the headings and order exactly.

# AI Automation Blueprint

## 1. Your 1-Page Business Summary
Write 3–6 short bullets that clearly describe:
- What type of business you appear to run
- Your biggest pain points in your own words
- The biggest opportunities for automation
- What is costing you the most money right now
- What feels overwhelming or chaotic in your current process

This should feel like: "You really understand my situation."

## 2. What You Told Me
Rewrite their answers into clean categories. For each category, write 2–4 bullets.

### Your Goals

### Your Challenges

### Where You’re Losing Time

### Opportunities You’re Not Taking Advantage Of

Make sure each bullet adds a new insight. No repeating the same idea.

## 3. Your Top 3 Automation Wins

Create exactly 3 wins. For each win, follow this structure:

### WIN: Short outcome-focused title
(4 words or less, for example: Never Miss Another Call, Faster Booked Jobs, Follow-Up That Never Stops)

**What this fixes in your business:**
- 2–3 bullets describing the specific business problem in simple terms

**What this does for you:**
- 2–4 bullets describing the benefits (time saved, more booked jobs, fewer headaches)

**What’s included in this win:**
- 3–5 bullets in plain English, describing what the automation does
  (for example: instant text replies, lead follow-up messages, automatic reminders, after-hours handling)

Do NOT explain how to build anything. Only what it does and why it matters.

## 4. Your Automation Scorecard (0–100)

Give the business a simple "automation maturity score" from 0–100.

Then write 4–6 bullets that explain:
- Where they are strong
- Where they are weak
- What this score means in plain English
- What is most urgent to improve first

## 5. Your 30-Day Game Plan

Break the next 30 days into 4 weeks.
For each week, give exactly 3–4 simple bullets.

### Week 1 — Stabilize the Business

### Week 2 — Increase Booked Jobs

### Week 3 — Build Customer Experience

### Week 4 — Scale and Optimize

Focus on actions that reduce missed calls, speed up responses, and improve follow-up.
Use simple, non-technical language.

## 6. Final Recommendations

Write 5–7 short bullets with clear guidance, such as:
- Which automation win to start with first
- What will bring the fastest return
- Reassurance that they don’t need to fix everything at once
- What they should have ready before an automation strategy call
- Where their biggest long-term opportunity is

Do NOT sell anything directly.
Do NOT mention this being an "AI" blueprint.
Keep the tone calm, confident, and supportive.
"""

    try:
        # One OpenAI call for the entire blueprint
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        blueprint_text = response.output[0].content[0].text.strip()

        # Summary = everything before "## 3. Your Top 3 Automation Wins"
        summary_section = blueprint_text
        marker = "## 3. Your Top 3 Automation Wins"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # --------- GENERATE PDF LOCALLY ----------
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name, BOOKING_URL)

        # --------- UPLOAD PDF TO S3 (PUBLIC) ----------
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME env var is not set in Render")

        s3_key = f"blueprints/{pdf_filename}"

        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={
                "ContentType": "application/pdf",
                "ACL": "public-read",  # public URL
            },
        )

        pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"
        print("Generated PDF URL:", pdf_url, flush=True)

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,
                "summary": summary_section,
                "pdf_url": pdf_url,
                "name": name,
                "email": email,
                "business_name": business_name,
            }
        )

    except Exception as e:
        print("Error generating blueprint:", e, flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


# --------------------------------------------------------------------
# Legacy /pdf route (not used now)
# --------------------------------------------------------------------
@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    return "PDFs are now stored on S3.", 410


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API (Render + S3, single-prompt improved version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

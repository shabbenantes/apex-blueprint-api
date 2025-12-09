from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
from openai import OpenAI
import boto3  # NEW: for S3 upload

# PDF generation imports
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors

app = Flask(__name__)

# OpenAI client using env var (set OPENAI_API_KEY in Render)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# NEW: S3 client using env vars (set these in Render)
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=S3_REGION)


# --------------------------------------------------------------------
# PDF GENERATION
# --------------------------------------------------------------------
def generate_pdf(blueprint_text: str, pdf_path: str, name: str, business_name: str):
    """
    Turn the blueprint text into a clean, branded PDF with clearer sections.
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

    # Simple horizontal rule effect
    story.append(
        Paragraph(
            "<para alignment='center'><font size=8 color='#CCCCCC'>────────────────────────────</font></para>",
            small_label_style,
        )
    )
    story.append(Spacer(1, 12))

    # Short intro
    story.append(
        Paragraph(
            "This blueprint shows where your business is currently leaking time and money, and the simplest automation wins to fix it over the next 30 days.",
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

    # Final CTA block
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


# --------------------------------------------------------------------
# /run – 3-PROMPT BLUEPRINT GENERATION
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    Takes the contact + form answers, generates a blueprint in 3 AI calls,
    generates a PDF, uploads it to S3, and returns everything as JSON.
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

    # --------- SHARED CONTEXT FOR ALL PROMPTS ----------
    shared_context = f"""
You are APEX AI, a business automation consultant for home service companies.
Your job is to create a clean, premium, easy-to-read AI Automation Blueprint
based on the owner's answers.

Owner name: {name}
Business name: {business_name if business_name else "Not specified"}

Owner's raw answers:
{raw_form_text}

STYLE RULES (apply to ALL sections you write):
- Use SIMPLE business language (no jargon: no “CRM”, no “API”, no “backend”)
- Be extremely clear
- Be structured and visually clean
- Sound like a calm, professional consultant
- Be outcome-focused: more booked jobs, fewer missed calls, faster response, less stress
- Make the owner feel understood
- Make each section feel valuable, but NOT overwhelming
- Do NOT give step-by-step tech instructions
- Do NOT talk about tools, software, or integrations
- Do NOT refer to “the form” or “the user”
- Talk directly to the owner using “you” and “your business”
- Prefer bullet points over long paragraphs
- Keep sections tight, clean, and easy to scan
"""

    try:
        # --------- PROMPT 1: Summary + What You Told Me ----------
        prompt_1 = f"""{shared_context}

Write ONLY the following sections in Markdown:

# AI Automation Blueprint

## 1. Your 1-Page Business Summary
Write 3–6 short bullets that clearly describe:
- What type of business they appear to run
- Their biggest pain points in your own words
- The biggest opportunities for automation
- What is costing them the most money right now
- What feels overwhelming or chaotic in their current process

This should feel like: "You really understand my situation."

## 2. What You Told Me
Rewrite their answers into clean categories:

### Your Goals
- 2–4 bullets summarizing their main goals

### Your Challenges
- 3–5 bullets summarizing the problems they described

### Where You’re Losing Time
- 2–4 bullets explaining where time is being wasted

### Opportunities You’re Not Taking Advantage Of
- 3–5 bullets showing where they could be getting more value

Do NOT include anything else. Start directly with "# AI Automation Blueprint".
"""

        resp1 = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt_1,
        )
        part1_text = resp1.output[0].content[0].text
        summary_section = part1_text.strip()  # used in the email

        # --------- PROMPT 2: Top Wins + Scorecard ----------
        prompt_2 = f"""{shared_context}

Write ONLY the following sections in Markdown.
Continue the numbering from the previous content.

## 3. Your Top 3 Automation Wins

For each win, follow this structure:

### WIN: Short, outcome-focused title
Examples of good titles:
- Never Miss Another Call
- Faster Booked Jobs
- Follow-Up That Never Stops
- More Reviews on Autopilot

**What this fixes in your business:**
- 2–4 bullets describing the specific business problem

**What this does for you:**
- 3–4 bullets describing the benefits (time saved, more booked jobs, fewer headaches)

**What’s included in this win:**
- 3–5 bullets in plain English, describing what the automation actually does
  (for example: instant text replies, lead follow-up messages, automatic reminders, after-hours handling)

Do NOT explain how to build anything. Only what it does and why it matters.

## 4. Your Automation Scorecard (0–100)

Give the business a simple "automation maturity score" from 0–100.
Then write 4–6 bullets that explain:
- Where they are strong
- Where they are weak
- What this score means in plain English
- What is most urgent to fix

Do NOT include anything else.
"""

        resp2 = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt_2,
        )
        part2_text = resp2.output[0].content[0].text

        # --------- PROMPT 3: 30-Day Plan + Final Recommendations ----------
        prompt_3 = f"""{shared_context}

Write ONLY the following sections in Markdown.
Continue the numbering from the previous content.

## 5. Your 30-Day Game Plan

Break the next 30 days into 4 weeks.
For each week, give 3–4 simple bullets.

### Week 1 — Stabilize the Business
Focus on fixing the biggest leaks first (missed calls, slow response, lost leads).

### Week 2 — Increase Booked Jobs
Focus on follow-up, no-shows, and response times.

### Week 3 — Build Customer Experience
Focus on reviews, rebooking, and customer communication.

### Week 4 — Scale and Optimize
Focus on adding a bit more automation and better visibility.

Use simple, non-technical bullets for each week.

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

Do NOT include anything else.
"""

        resp3 = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt_3,
        )
        part3_text = resp3.output[0].content[0].text

        # --------- COMBINE ALL PARTS ----------
        blueprint_text = "\n\n".join(
            [
                part1_text.strip(),
                part2_text.strip(),
                part3_text.strip(),
            ]
        ).strip()

        # --------- GENERATE PDF LOCALLY ----------
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # --------- UPLOAD PDF TO S3 (PERSISTENT) ----------
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME env var is not set in Render")

        s3_key = f"blueprints/{pdf_filename}"

        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={
                "ContentType": "application/pdf",
                "ACL": "public-read",  # make the file visible by link
            },
        )

        # Public S3 URL (assuming bucket allows public-read via ACL)
        pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,   # full document
                "summary": summary_section,    # first sections only
                "pdf_url": pdf_url,            # 🔥 S3 link that won't disappear
                "name": name,
                "email": email,
                "business_name": business_name,
            }
        )

    except Exception as e:
        # Log the error to Render's logs
        print("Error generating blueprint:", e, flush=True)
        return jsonify(
            {
                "success": False,
                "error": str(e),
            }
        ), 500


# --------------------------------------------------------------------
# (Optional) /pdf endpoint still here, but now unused
# --------------------------------------------------------------------
@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    """
    Legacy route. PDFs are now stored on S3 instead of local /tmp.
    Kept only so old links don't 500, but they won't find files after reboot.
    """
    return "PDFs are now stored on S3.", 410


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API (Render + S3, 3-prompt version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

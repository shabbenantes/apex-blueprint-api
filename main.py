from flask import Flask, request, jsonify
import os
import uuid
import re  # for cleaning up markdown-like symbols

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


# --------------------------------------------------------------------
# PDF GENERATION
# --------------------------------------------------------------------
def generate_pdf(blueprint_text: str, pdf_path: str, name: str, business_name: str):
    """
    Turn the blueprint text into a clean, branded PDF with clearer sections.
    Removes raw ### and ** markers and formats headings/bullets nicely.
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
        spaceBefore=14,
        spaceAfter=6,
    )

    subheading_style = ParagraphStyle(
        "SubHeadingStyle",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=colors.HexColor("#0A1A2F"),
        spaceBefore=10,
        spaceAfter=4,
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
        fontSize=13,
        textColor=colors.HexColor("#0A1A2F"),
        spaceBefore=20,
        spaceAfter=4,
        alignment=TA_CENTER,
    )

    cta_body_style = ParagraphStyle(
        "CTABodyStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#222222"),
        alignment=TA_CENTER,
        spaceAfter=4,
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

    # Helper: convert **bold** to <b>bold</b>
    def convert_inline_formatting(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        return text

    # ------- BODY FROM BLUEPRINT TEXT -------
    lines = blueprint_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        style = body_style
        cleaned = line

        # Strip leading markdown-like # if the model ever sneaks them in
        if cleaned.startswith("#"):
            cleaned = cleaned.lstrip("#").strip()

        upper = cleaned.upper()

        # MAIN SECTION HEADINGS
        if upper.startswith("TITLE:"):
            style = heading_style
        elif upper.startswith("SECTION "):
            style = heading_style
        # SUBSECTIONS (we strip the "Subsection:" label in display)
        elif upper.startswith("SUBSECTION:"):
            style = subheading_style
            cleaned = cleaned.split(":", 1)[1].strip() or cleaned
        # WEEKS
        elif upper.startswith("WEEK "):
            style = subheading_style
        # WINS, e.g. "Win 1 — Automated Booking"
        elif cleaned.startswith("Win "):
            style = subheading_style

        # BULLETS: "- something" or "* something" → "• something"
        if cleaned.startswith("- "):
            cleaned = "• " + cleaned[2:].strip()
        elif cleaned.startswith("* "):
            cleaned = "• " + cleaned[2:].strip()

        # Inline bold if any **text** sneaks in
        cleaned = convert_inline_formatting(cleaned)

        story.append(Paragraph(cleaned, style))

    # ------- CTA BLOCK AT END -------
    story.append(Spacer(1, 20))
    story.append(
        Paragraph("Next Step: Book Your Automation Strategy Call", cta_heading_style)
    )
    story.append(
        Paragraph(
            "On this call, we’ll walk through your blueprint together, "
            "choose the fastest wins, and map out your implementation plan.",
            cta_body_style,
        )
    )
    story.append(
        Paragraph(
            "Use the booking link in your email to pick a time that works best for you.",
            cta_body_style,
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
    Takes the contact + form answers, generates a blueprint in ONE AI call,
    generates a PDF, uploads it to S3, and returns everything as JSON.
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

    # --------- SINGLE PROMPT (Option A / cleaned headings) ----------
    prompt = f"""
You are APEX AI, a senior automation consultant who writes premium,
clear, confidence-building business blueprints for home-service owners.

Your goal: create a clean, structured, easy-to-read written blueprint
that will later be inserted into a Google Docs template and exported as
a polished PDF.

IMPORTANT WRITING RULES:
- Use simple business language (no tech jargon).
- Keep every section short, clear, and easy to scan.
- Use bullets more than long paragraphs.
- Sound calm, professional, and confident.
- Speak directly to the reader using “you” and “your business”.
- Never mention AI, prompts, or that this text was generated.
- Do NOT use markdown symbols (#, ##, ###).
- Do NOT use asterisks (*) for bold or emphasis.
- Do NOT include emojis.
- Do NOT refer to “form”, “survey”, or “questions”.
- No long paragraphs; keep everything concise.
- The blueprint should feel personalized, but not overly specific.
- Maintain the same structure every time.

------------------------------------------------------------
INPUT DATA
Owner Name: {name}
Business Name: {business_name if business_name else "Not specified"}
Owner's Raw Answers:
{raw_form_text}
------------------------------------------------------------

NOW WRITE THE BLUEPRINT USING THIS EXACT STRUCTURE:

TITLE: AI Automation Blueprint

SECTION 1: Quick Snapshot
Write 4–6 short bullets describing:
- What type of business they appear to run
- Their biggest pain points in plain English
- Where they are losing time or money
- The biggest opportunities for automation
- What feels chaotic or overwhelming today

SECTION 2: What You Told Me
Rewrite their answers into the following labeled subsections:

Subsection: Your Goals
Write 3–5 bullets summarizing their goals.

Subsection: Your Challenges
Write 3–6 bullets summarizing the problems they’re dealing with.

Subsection: Where Time Is Being Lost
Write 3–5 bullets describing inefficiencies or bottlenecks.

Subsection: Opportunities You’re Not Leveraging Yet
Write 3–6 bullets describing automation opportunities relevant to
home-service businesses.

SECTION 3: Your Top 3 Automation Wins
Write three wins in this format:

Win 1 — Short, outcome-focused title
What This Fixes:
- 2–4 bullets

What This Does For You:
- 3–4 bullets describing benefits like time saved, more booked jobs, less stress.

What’s Included:
- 3–5 bullets describing simple, easy-to-understand automation actions
  (for example: automatic follow-up, instant replies, reminders, scheduling flows).

Then repeat the same structure for:
Win 2 — ...
Win 3 — ...

SECTION 4: Your Automation Scorecard (0–100)
Give a clear, fair score from 0–100.

Then write 4–6 bullets describing:
- Strengths they already have
- Weaknesses that hurt performance
- What the score means in everyday language
- What is most important to fix first

SECTION 5: Your 30-Day Action Plan
Break into weekly sections:

Week 1 — Stabilize
Write 3–4 simple bullets.

Week 2 — Increase Booked Jobs
Write 3–4 bullets.

Week 3 — Improve Customer Experience
Write 3–4 bullets.

Week 4 — Optimize and Prepare to Scale
Write 3–4 bullets.

SECTION 6: Final Recommendations
Write 5–7 bullets giving clear, calm guidance:
- What to build first
- What will create the fastest improvements
- What not to worry about yet
- What they should come prepared with for a strategy call
- Where their biggest long-term opportunity is

END OF BLUEPRINT
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        blueprint_text = response.output[0].content[0].text.strip()
        print("Blueprint length (chars):", len(blueprint_text), flush=True)

        # Simple "summary" = everything up through Section 2
        summary_section = blueprint_text
        marker = "SECTION 3:"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # --------- GENERATE PDF LOCALLY ----------
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # --------- UPLOAD PDF TO S3 ----------
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME env var is not set in Render")

        s3_key = f"blueprints/{pdf_filename}"

        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={
                "ContentType": "application/pdf",
                "ACL": "public-read",
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
    return "Apex Blueprint API (Render + S3, single-prompt cleaned version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

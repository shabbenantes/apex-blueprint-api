from flask import Flask, request, jsonify, send_from_directory, send_file
import os
import uuid
from io import BytesIO

from openai import OpenAI
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
import boto3

app = Flask(__name__)

# ----------------- OpenAI client -----------------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ----------------- AWS / S3 config -----------------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")  # change if you used another
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")            # e.g. "apex-blueprints-prod"

s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
)


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
# /run  – main endpoint used by GoHighLevel (Render)
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    Takes the contact + form answers, generates a blueprint,
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

    # ---------- 3-section blueprint prompt (the one you liked) ----------
    prompt = f"""
You are APEX AI, a business automation consultant for home service companies.
Your job is to create a clean, premium, easy-to-read AI Automation Blueprint
based on the owner's answers.

Write in simple, non-technical business language. Make it easy to scan.

Contact name: {name}
Business name: {business_name if business_name else "Not specified"}

Owner's raw answers:
{raw_form_text}

Structure your response EXACTLY like this:

# AI AUTOMATION BLUEPRINT

## 1. Quick Business Snapshot
- 3–6 short bullets that describe:
  - What type of business this appears to be
  - Their biggest pain points (rewrite clearly)
  - Where they are losing time and money
  - The main opportunity automation can unlock

## 2. Your Top 3 Automation Wins
For each win, follow this format:

### WIN 1 – [Short outcome-based title]
**What this fixes:**
- 2–4 bullets

**What this does for you:**
- 3–4 bullets

**What’s included:**
- 3–5 simple items (no tech terms)

### WIN 2 – [Title]
(same structure)

### WIN 3 – [Title]
(same structure)

Keep everything focused on outcomes: more booked jobs, fewer missed calls,
faster responses, better follow-up, and less stress.

## 3. 30-Day Game Plan
Break this into 4 weeks with 3–4 bullets each:

### Week 1 – Stabilize
### Week 2 – Increase Booked Jobs
### Week 3 – Improve Customer Experience
### Week 4 – Scale & Optimize

Keep each bullet short, clear, and non-technical.
Don't talk about “APIs” or “CRMs” — just describe what happens in the business.
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        # Full blueprint text
        blueprint_text = response.output[0].content[0].text

        # Summary = everything before "## 2. Your Top 3 Automation Wins"
        summary_section = blueprint_text
        marker = "## 2. Your Top 3 Automation Wins"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate a unique PDF file in /tmp
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # ---------- NEW: upload PDF to S3 so it doesn't disappear ----------
        s3_key = f"blueprints/{pdf_filename}"
        if S3_BUCKET:
            try:
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()

                s3_client.put_object(
                    Bucket=S3_BUCKET,
                    Key=s3_key,
                    Body=pdf_bytes,
                    ContentType="application/pdf",
                )
            except Exception as e:
                print("Error uploading PDF to S3:", e)
        else:
            print("WARNING: S3_BUCKET_NAME not set – PDFs only live in /tmp")

        # We'll keep using the same kind of URL as before, but now /pdf/<id>
        # will stream from S3 instead of /tmp.
        base_url = request.host_url.rstrip("/")
        pdf_url = f"{base_url}/pdf/{pdf_id}"

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,  # full document
                "summary": summary_section,   # quick overview section
                "pdf_url": pdf_url,           # link to the PDF via our /pdf route
                "name": name,
                "email": email,
                "business_name": business_name,
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


# --------------------------------------------------------------------
# /pdf/<id> – now streams from S3 (fallback to /tmp if needed)
# --------------------------------------------------------------------
@app.route("/pdf/<pdf_id>", methods=["GET"])
def serve_pdf(pdf_id):
    pdf_filename = f"blueprint_{pdf_id}.pdf"
    s3_key = f"blueprints/{pdf_filename}"

    # Preferred: load from S3 (persists across Render restarts)
    if S3_BUCKET:
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
            pdf_bytes = obj["Body"].read()
            return send_file(
                BytesIO(pdf_bytes),
                mimetype="application/pdf",
                download_name=pdf_filename,
            )
        except Exception as e:
            print("Error fetching PDF from S3:", e)

    # Fallback: try local /tmp (old behaviour)
    pdf_dir = "/tmp"
    return send_from_directory(pdf_dir, pdf_filename, mimetype="application/pdf")


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API (Render + S3) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

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


# --------------------------------------------------------------------
# PDF GENERATION
# --------------------------------------------------------------------
def generate_pdf(blueprint_text: str, pdf_path: str, name: str, business_name: str):
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0A1A2F"),
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
        alignment=TA_CENTER,
        spaceBefore=20,
        spaceAfter=4,
    )

    cta_body_style = ParagraphStyle(
        "CTABodyStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        alignment=TA_CENTER,
        leading=14,
        textColor=colors.HexColor("#222222"),
        spaceAfter=4,
    )

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
        title="AI Automation Blueprint",
        author="Apex Automation"
    )

    story = []

    # ------- COVER -------
    story.append(Paragraph("Apex Automation", title_style))
    story.append(Paragraph("AI Automation Blueprint for Your Service Business", tagline_style))

    owner_line = f"Prepared for: {name}"
    if business_name:
        owner_line += f"  •  Business: {business_name}"

    story.append(Paragraph(owner_line, small_label_style))
    story.append(Spacer(1, 18))

    story.append(
        Paragraph(
            "<para alignment='center'><font size=8 color='#CCCCCC'>────────────────────────────</font></para>",
            small_label_style,
        )
    )
    story.append(Spacer(1, 12))

    # INTRO
    story.append(
        Paragraph(
            "This blueprint outlines the top automation opportunities for your business based on your answers.",
            body_style,
        )
    )
    story.append(Spacer(1, 12))

    # ------- BODY -------
    lines = blueprint_text.splitlines()
    for raw in lines:
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        # Section detection
        if (
            line.startswith("SECTION")
            or line.startswith("Subsection")
            or line.startswith("WIN TITLE")
            or line.startswith("Week ")
            or line.startswith("TITLE:")
        ):
            story.append(Paragraph(line, heading_style))
        else:
            story.append(Paragraph(line, body_style))

    # ------- CTA -------
    story.append(Spacer(1, 20))
    story.append(Paragraph("Next Step: Book Your Automation Strategy Call", cta_heading_style))
    story.append(
        Paragraph(
            "We'll walk through your blueprint together and map out your fast-path automation plan.",
            cta_body_style,
        )
    )
    story.append(
        Paragraph(
            "Use the booking link in your email to pick a time.",
            cta_body_style,
        )
    )

    doc.build(story)


# --------------------------------------------------------------------
# SINGLE-PROMPT BLUEPRINT GENERATION
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():

    data = request.get_json(force=True) or {}

    contact = data.get("contact", {}) or data.get("contact_data", {})
    form = data.get("form_fields", {}) or data.get("form", {}) or {}

    name = contact.get("name") or contact.get("first_name") or "there"
    email = contact.get("email", "")
    business_name = form.get("business_name", "")

    # ---- CUSTOM FIELDS ----
    business_type = form.get("business_type", "")
    services = form.get("services", "")
    ideal_customer = form.get("ideal_customer", "")
    bottlenecks = form.get("bottlenecks", "")
    manual_tasks = form.get("manual_tasks", "")
    software = form.get("software", "")
    response_time = form.get("response_time", "")
    leads_per_week = form.get("leads_per_week", "")
    jobs_per_week = form.get("jobs_per_week", "")
    growth_goals = form.get("growth_goals", "")
    frustrations = form.get("frustrations", "")
    extra_notes = form.get("extra_notes", "")

    # ---- THINK + WRITE SUPER PROMPT ----
    prompt = f"""
You are APEX AI, a senior automation consultant for service businesses.

FIRST, THINK SILENTLY AND STRUCTURE THE INFORMATION (do not show your thinking):
- Identify the exact business type: {business_type}
- Services offered: {services}
- Ideal customer: {ideal_customer}
- Bottlenecks: {bottlenecks}
- Manual tasks they want automated: {manual_tasks}
- Software: {software}
- Lead response speed: {response_time}
- Lead volume: {leads_per_week}
- Job volume: {jobs_per_week}
- Growth goals: {growth_goals}
- Frustrations: {frustrations}
- Extra notes: {extra_notes}

Then determine:
- Their top 5 pain points
- Their highest-impact automation opportunities
- Where time/money is being lost
- The 3 most relevant automation wins for THEIR exact business model

ONLY AFTER THINKING, WRITE THE BLUEPRINT.

------------------------------------------------------------
TITLE: AI Automation Blueprint for {business_name}

Prepared for: {name}
Business Type: {business_type}

SECTION 1: Quick Snapshot
Write 5–7 bullets using ONLY their actual data.

SECTION 2: What You Told Me

Subsection: Your Goals
3–5 bullets from their goals.

Subsection: Your Challenges
3–6 bullets from their bottlenecks + frustrations.

Subsection: Where Time Is Being Lost
3–5 bullets from inefficiencies detected.

Subsection: Opportunities You’re Not Leveraging Yet
3–6 bullets relevant to their service type.

SECTION 3: Your Top 3 Automation Wins
For each win:

WIN TITLE
What This Fixes:
- 2–4 bullets

What This Does For You:
- 3–4 bullets

What’s Included:
- 3–5 bullets based on their business type

SECTION 4: Your Automation Scorecard (0–100)
Provide score + 4–6 bullets explaining strengths, weaknesses, and priorities.

SECTION 5: Your 30-Day Action Plan
Week 1 — Stabilize
Week 2 — Increase Booked Jobs
Week 3 — Improve Customer Experience
Week 4 — Optimize and Prepare to Scale

SECTION 6: Final Recommendations
Write 5–7 bullets guiding next steps.

END OF BLUEPRINT
"""

    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        blueprint_text = resp.output[0].content[0].text.strip()

        # Create PDF
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_path = f"/tmp/{pdf_filename}"

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # Upload to S3
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET_NAME is not set")

        s3_key = f"blueprints/{pdf_filename}"
        s3_client.upload_file(
            Filename=pdf_path,
            Bucket=S3_BUCKET,
            Key=s3_key,
            ExtraArgs={"ContentType": "application/pdf", "ACL": "public-read"},
        )

        pdf_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"

        return jsonify({
            "success": True,
            "blueprint": blueprint_text,
            "pdf_url": pdf_url,
            "name": name,
            "business_name": business_name
        })

    except Exception as e:
        print("Error:", e, flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API (Render + S3, THINK version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

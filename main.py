from flask import Flask, request, jsonify
import os
import uuid
import json  # NEW: to pass raw JSON to the model

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
# Make sure these are set in Render:
#   S3_BUCKET_NAME  = apex-blueprints-prod   (your bucket name)
#   S3_REGION       = us-east-2              (Ohio for you)
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_REGION = os.environ.get("S3_REGION", "us-east-2")

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
            "<para alignment='center'><font size=8 color='#CCCCCC'>────────────────────────────</font></para>",
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
    lines = blueprint_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        # Simple heading detection
        if (
            line.upper().startswith("TITLE:")
            or line.upper().startswith("SECTION ")
            or line.upper().startswith("WIN ")
            or line.upper().startswith("WEEK ")
            or line.upper().startswith("SUBSECTION:")
        ):
            story.append(Paragraph(line, heading_style))
        else:
            story.append(Paragraph(line, body_style))

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

    # ---- Log the raw payload to Render logs (helps if GHL changes shape) ----
    print("Incoming payload:", json.dumps(data, indent=2), flush=True)

    # Contact + form info (works with different GHL shapes)
    contact = data.get("contact", {}) or data.get("contact_data", {}) or {}

    # Try a few common places for form fields
    form_fields = (
        data.get("form_fields")
        or data.get("form")
        or data.get("form_submission", {}).get("form_fields")
        or {}
    )

    # Convenience values (based on your current field labels)
    name = (
        contact.get("full_name")
        or contact.get("first_name")
        or contact.get("firstName")
        or contact.get("name")
        or "there"
    )
    email = contact.get("email", "")

    business_name = (
        form_fields.get("Business Name")
        or form_fields.get("business_name")
        or ""
    )

    business_type = (
        form_fields.get("Business Type")
        or form_fields.get("business_type")
        or ""
    )

    services_offered = (
        form_fields.get("Services You Offer")
        or form_fields.get("services_offered")
        or ""
    )

    ideal_customer = (
        form_fields.get("Ideal Customer")
        or form_fields.get("ideal_customer")
        or ""
    )

    bottlenecks = (
        form_fields.get("Biggest Operational Bottlenecks")
        or form_fields.get("bottlenecks")
        or ""
    )

    manual_tasks = (
        form_fields.get("Manual Tasks You Want Automated")
        or form_fields.get("manual_tasks")
        or ""
    )

    current_software = (
        form_fields.get("Software You Currently Use")
        or form_fields.get("current_software")
        or ""
    )

    lead_response_time = (
        form_fields.get("Average Lead Response Time")
        or form_fields.get("lead_response_time")
        or ""
    )

    leads_per_week = (
        form_fields.get("Leads Per Week")
        or form_fields.get("leads_per_week")
        or ""
    )

    jobs_per_week = (
        form_fields.get("Jobs Per Week")
        or form_fields.get("jobs_per_week")
        or ""
    )

    growth_goals = (
        form_fields.get("Growth Goals (6–12 months)")
        or form_fields.get("growth_goals")
        or ""
    )

    frustrations = (
        form_fields.get("What Frustrates You Most")
        or form_fields.get("frustrations")
        or ""
    )

    extra_notes = (
        form_fields.get("Anything Else We Should Know")
        or form_fields.get("extra_notes")
        or ""
    )

    # Full raw JSON for the model to reference (this is the safety net)
    raw_json = json.dumps(data, indent=2, ensure_ascii=False)

    # --------- SINGLE PROMPT ----------
    prompt = f"""
You are APEX AI, a senior automation consultant who writes premium,
clear, confidence-building business blueprints for home-service owners.

Your job is to create a clean, structured, easy-to-read written blueprint
that feels clearly based on the owner's answers.

IMPORTANT:
- Only describe the business using information that appears in their answers / JSON.
- If something is missing, say "Not specified" instead of guessing.
- If a business type is given (like plumbing, HVAC, cleaning, roofing, etc.),
  use that exact wording instead of the generic term "home service".
- Speak directly to the owner as "you" and "your business".
- Use bullets more than long paragraphs.
- Do NOT mention AI, prompts, JSON, or that this was generated.

OWNER INFO (parsed fields):
- Owner name: {name}
- Business name: {business_name or "Not specified"}
- Business type: {business_type or "Not specified"}
- Services you offer: {services_offered or "Not specified"}
- Ideal customer: {ideal_customer or "Not specified"}
- Biggest operational bottlenecks: {bottlenecks or "Not specified"}
- Manual tasks you want automated: {manual_tasks or "Not specified"}
- Current software: {current_software or "Not specified"}
- Average lead response time: {lead_response_time or "Not specified"}
- Leads per week: {leads_per_week or "Not specified"}
- Jobs per week: {jobs_per_week or "Not specified"}
- Growth goals (6–12 months): {growth_goals or "Not specified"}
- What frustrates you most: {frustrations or "Not specified"}
- Extra notes: {extra_notes or "Not specified"}

RAW FORM DATA (JSON FROM GOHIGHLEVEL):
The following is the full JSON payload from the form submission.
Use this as the source of truth for the owner's answers. Look carefully
for any nested fields or custom fields and reflect them accurately in
the blueprint. Do not invent details that are not present.

{raw_json}

NOW WRITE THE BLUEPRINT USING THIS EXACT STRUCTURE:

TITLE: AI Automation Blueprint

Prepared for: {name}
Business: {business_name or "Not specified"}
Business type: {business_type or "Not specified"}

SECTION 1: Quick Snapshot
Write 4–6 short bullets describing:
- What type of business they run (use their exact business type or services if provided)
- Their main pain points and bottlenecks, using their language where possible
- Where time or money is being lost today
- The biggest opportunities for automation based on their answers
- Anything else that stands out as important from their JSON data

SECTION 2: What You Told Me
Rewrite their answers into the following labeled subsections:

Subsection: Your Goals
- 3–5 bullets summarizing their 6–12 month goals and priorities.

Subsection: Your Challenges
- 3–6 bullets summarizing the problems they described
  (capacity, leads, staffing, follow-up, software issues, etc.).

Subsection: Where Time Is Being Lost
- 3–5 bullets describing the manual tasks, delays, or bottlenecks.

Subsection: Opportunities You’re Not Using Yet
- 4–6 bullets describing automation opportunities that clearly
  connect to their specific situation.

SECTION 3: Your Top 3 Automation Wins
For each win, write:

WIN 1: [short, outcome-focused title]
What This Fixes:
- 2–4 bullets tied directly to their stated bottlenecks and frustrations.

What This Does For You:
- 3–4 bullets describing benefits (time saved, more booked jobs, fewer headaches).

What’s Included:
- 3–5 bullets describing simple, easy-to-understand automation actions
  (for example: automatic follow-up, instant replies, reminders, scheduling flows).

Repeat the same structure for WIN 2 and WIN 3.

SECTION 4: Your Automation Scorecard (0–100)
Give a clear, fair score from 0–100 based on how automated they seem
from their answers (do not assume they are fully manual if they mention tools).

Then write 4–6 bullets describing:
- Strengths they already have
- Weak spots that are slowing them down
- What the score means in everyday language
- What is most important to fix first

SECTION 5: Your 30-Day Action Plan
Break into weekly sections:

Week 1 — Stabilize the Business
- 3–4 bullets based on their current chaos and bottlenecks.

Week 2 — Capture and Convert More Leads
- 3–4 bullets focused on lead handling, follow-up, and booking.

Week 3 — Improve Customer Experience
- 3–4 bullets focused on communication, reminders, and reliability.

Week 4 — Optimize and Prepare to Scale
- 3–4 bullets focused on visibility, reporting, and tightening up automations.

SECTION 6: Final Recommendations
Write 5–7 bullets giving clear, calm guidance:
- What to build first for the fastest improvement
- What will move them toward their 6–12 month goals
- What they can safely ignore for now
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
                "ACL": "public-read",  # allow download by link
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
    return "Apex Blueprint API (Render + S3, JSON-aware single-prompt) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

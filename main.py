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
    # We’ll treat any line that starts with a known heading pattern as a heading.
    lines = blueprint_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        if (
            line.upper().startswith("TITLE:")
            or line.upper().startswith("SECTION ")
            or line.upper().startswith("SUBSECTION:")
            or line.upper().startswith("WIN ")
            or line.upper().startswith("WEEK ")
        ):
            story.append(Paragraph(line, heading_style))
        else:
            story.append(Paragraph(line.replace("\t", " "), body_style))

    # ------- CTA BLOCK AT END -------
    story.append(Spacer(1, 20))
    story.append(
        Paragraph(
            "Next Step: Book Your Automation Strategy Call", cta_heading_style
        )
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
# Helper: get field from multiple possible keys
# --------------------------------------------------------------------
def get_field(form_fields: dict, *names) -> str:
    """
    Try several possible keys (different labels / cases) and return
    the first non-empty value as a string. Falls back to "".
    """
    # Exact matches first
    for name in names:
        if name in form_fields and form_fields[name]:
            return str(form_fields[name])

    # Case-insensitive fallback
    lower_map = {k.lower(): v for k, v in form_fields.items()}
    for name in names:
        v = lower_map.get(name.lower())
        if v:
            return str(v)

    return ""


# --------------------------------------------------------------------
# /run – SINGLE-PROMPT BLUEPRINT GENERATION (with locked facts)
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    Takes the contact + form answers, generates a blueprint in ONE AI call,
    generates a PDF, uploads it to S3, and returns everything as JSON.

    IMPORTANT CHANGE:
    - We build the business facts in code (no guessing)
    - We hard-code Title + Section 1
    - The model only writes Sections 2–6 and is not allowed to
      change the business type or invent a different industry.
    """
    data = request.get_json(force=True) or {}

    # Contact + form info
    contact = data.get("contact", {}) or data.get("contact_data", {})
    form_fields = data.get("form_fields", {}) or data.get("form", {}) or {}

    name = (
        contact.get("first_name")
        or contact.get("firstName")
        or contact.get("name")
        or contact.get("full_name")
        or "there"
    )
    email = contact.get("email", "")

    # ---- Intake fields (using your current labels) ----
    # Adjust/add keys here if you rename any custom fields in GHL.
    business_name = get_field(
        form_fields, "Business Name", "business_name", "Company Name"
    )
    business_type = get_field(
        form_fields, "Business Type", "business_type", "Industry"
    )
    services_offered = get_field(
        form_fields, "Services You Offer", "Services Offered", "services_offered"
    )
    ideal_customer = get_field(
        form_fields, "Ideal Customer", "ideal_customer", "Ideal Customer Experience"
    )
    bottlenecks = get_field(
        form_fields,
        "Biggest Operational Bottlenecks",
        "Time Loss Areas",
        "Where Things Slow Down",
    )
    manual_tasks = get_field(
        form_fields,
        "Manual Tasks You Want Automated",
        "Desired Automations",
        "What Would You Love To Stop Doing Manually",
    )
    current_software = get_field(
        form_fields, "Software You Currently Use", "Current Software"
    )
    avg_response_time = get_field(
        form_fields,
        "Average Lead Response Time",
        "Lead Response Speed",
    )
    leads_per_week = get_field(form_fields, "Leads Per Week")
    jobs_per_week = get_field(form_fields, "Jobs Per Week")
    growth_goals = get_field(
        form_fields,
        "Growth Goals (6–12 months)",
        "Main Goals",
    )
    frustrations = get_field(
        form_fields,
        "What Frustrates You Most",
        "Biggest Frustration",
    )
    extra_notes = get_field(
        form_fields,
        "Anything Else We Should Know",
        "Additional Notes",
        "Notes",
    )

    # --------- FACTS BLOCK (NO GUESSING) ----------
    facts_block = f"""
Business name: {business_name or 'Not provided'}
Business type: {business_type or 'Not specified'}
Services offered: {services_offered or 'Not specified'}
Ideal customer: {ideal_customer or 'Not specified'}
Biggest operational bottlenecks: {bottlenecks or 'Not specified'}
Manual tasks you want automated: {manual_tasks or 'Not specified'}
Current software: {current_software or 'Not specified'}
Average lead response time: {avg_response_time or 'Not specified'}
Leads per week: {leads_per_week or 'Not specified'}
Jobs per week: {jobs_per_week or 'Not specified'}
Growth goals (6–12 months): {growth_goals or 'Not specified'}
What frustrates you most: {frustrations or 'Not specified'}
Anything else we should know: {extra_notes or 'Not specified'}
""".strip()

    # --------- PRE-BUILT TITLE + SECTION 1 ----------
    safe_business_label = business_name or "your business"
    safe_type_label = business_type or "home service"
    safe_ideal_label = ideal_customer or "Not specified"

    section1_text = f"""TITLE: AI Automation Blueprint

Prepared for: {name}
Business: {safe_business_label}
Business type: {safe_type_label}

SECTION 1: Quick Snapshot

This blueprint is based on the following facts about your business:

- Business type: {safe_type_label}
- Services you offer: {services_offered or 'Not specified'}
- Ideal customer: {safe_ideal_label}
- Biggest operational bottlenecks: {bottlenecks or 'Not specified'}
- Manual tasks you want automated: {manual_tasks or 'Not specified'}
- Current software: {current_software or 'Not specified'}
- Average lead response time: {avg_response_time or 'Not specified'}
- Leads per week: {leads_per_week or 'Not specified'}
- Jobs per week: {jobs_per_week or 'Not specified'}
- Growth goals (6–12 months): {growth_goals or 'Not specified'}
- What frustrates you most: {frustrations or 'Not specified'}
"""

    # --------- SINGLE PROMPT (model writes Sections 2–6 only) ----------
    prompt = f"""
You are APEX AI, a senior automation consultant who writes premium,
clear, confidence-building business blueprints for home-service owners.

Your job is to write SECTIONS 2–6 of an automation blueprint.
SECTION 1 has already been written for you.

FACTS ABOUT THE BUSINESS (DO NOT CHANGE THESE):

{facts_block}

HARD RULES (VERY IMPORTANT):
- You must treat this as a {safe_type_label} business.
- Do NOT change the industry. If the business type says "Plumbing",
  always describe it as a plumbing business, not cleaning or anything else.
- Do NOT say "likely", "appears to", or "seems to" about the business type.
- Do NOT invent different services or tools that were not provided.
- If something is missing in the facts, say "Not provided" or "Not specified"
  instead of guessing.
- Use simple business language only (no technical jargon).
- Speak directly to the owner using "you" and "your business".
- Prefer short paragraphs and bullet points.
- Do NOT use markdown headings (#, ##, ###). Use the plain text headings
  shown below (SECTION X, Subsection: ...).
- Do NOT mention AI, prompts, or that this text was generated.

You will now write ONLY the following sections in this exact order.
Start your answer with:

SECTION 2: What You Told Me

STRUCTURE TO FOLLOW:

SECTION 2: What You Told Me
Summarize their answers into these labeled subsections:

Subsection: Your Goals
- 3–5 bullets summarizing growth goals and what they want to improve.

Subsection: Your Challenges
- 3–6 bullets summarizing the main problems and bottlenecks.

Subsection: Where Time Is Being Lost
- 3–5 bullets describing where time is being wasted or where work
  falls through the cracks.

Subsection: Opportunities You’re Not Using Yet
- 3–6 bullets describing automation opportunities that fit the facts
  above and a {safe_type_label} business.

SECTION 3: Your Top 3 Automation Wins
For each win, include:

WIN 1: Short outcome-focused title
What This Fixes:
- 2–4 bullets tied directly to their bottlenecks and frustrations.

What This Does For You:
- 3–4 bullets on benefits (time saved, more booked jobs, fewer headaches).

What’s Included:
- 3–5 bullets in simple language explaining what the automation does
  (for example: automatic follow-up, instant replies, reminders, scheduling flows).

WIN 2: ...
WIN 3: ...
Follow the same pattern.

SECTION 4: Your Automation Scorecard (0–100)
- Give a clear score from 0–100 that matches the facts.
- Then write 4–6 bullets explaining:
  - Where they are already doing well
  - Where they are weak
  - What this score means in everyday language
  - What is most important to fix first

SECTION 5: Your 30-Day Action Plan
Break the next 30 days into 4 weeks, using the business facts:

Week 1 — Stabilize the Business
- 3–4 bullets.

Week 2 — Capture and Convert More Leads
- 3–4 bullets.

Week 3 — Improve Customer Experience
- 3–4 bullets.

Week 4 — Optimize and Prepare to Scale
- 3–4 bullets.

Keep everything non-technical and concrete.

SECTION 6: Final Recommendations
Write 5–7 bullets with clear guidance:
- What to focus on first
- What will create the fastest improvements
- What they can safely ignore for now
- What to have ready before an automation strategy call
- Where their biggest long-term opportunity is, based on their facts

Remember:
- Do NOT rewrite Section 1.
- Do NOT change the business type.
- Start your answer with "SECTION 2: What You Told Me".
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        body_text = response.output[0].content[0].text.strip()
        # Combine our fixed Section 1 with the AI-written sections
        blueprint_text = f"{section1_text.strip()}\n\n{body_text}".strip()

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
                "ACL": "public-read",  # allow download by link (since your bucket has ACLs enabled)
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
                "business_type": business_type,
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
    return "Apex Blueprint API (Render + S3, locked-facts version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

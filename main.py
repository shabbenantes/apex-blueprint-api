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

    # NEW: include business name + owner when possible
    if business_name and name:
        owner_line = f"Prepared for: {business_name} – {name}"
    elif business_name:
        owner_line = f"Prepared for: {business_name}"
    else:
        owner_line = f"Prepared for: {name or 'Your Business Owner'}"

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
    lines = blueprint_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

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
# /run – SINGLE-PROMPT BLUEPRINT GENERATION (MORE TAILORED)
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
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

    # Pull each custom field explicitly (using your labels)
    services_offered = form_fields.get("services_offered", "")  # Services Offered
    lead_response_speed = form_fields.get("lead_response_speed", "")  # Lead Response Speed
    leads_per_week = form_fields.get("leads_per_week", "")  # Leads Per Week
    jobs_per_week = form_fields.get("jobs_per_week", "")  # Jobs Per Week
    contact_methods = form_fields.get("customer_contact_methods", "")  # Customer Contact Methods
    biggest_frustration = form_fields.get("biggest_frustration", "")  # Biggest Frustration
    time_loss_areas = form_fields.get("time_loss_areas", "")  # Time Loss Areas
    main_goals = form_fields.get("main_goals", "")  # Main Goals
    desired_automations = form_fields.get("desired_automations", "")  # Desired Automations
    current_software = form_fields.get("current_software", "")  # Current Software
    ideal_experience = form_fields.get("ideal_customer_experience", "")  # Ideal Customer Experience

    # --------- SINGLE PROMPT (more literal + no guessing) ----------
    prompt = f"""
You are APEX AI, a senior automation consultant who writes premium,
clear, confidence-building business blueprints for home-service owners.

Your goal: create a clean, structured, easy-to-read written blueprint
that will later be inserted into a Google Docs template and exported as
a polished PDF.

ABSOLUTE RULES (VERY IMPORTANT):
- Use ONLY the information given below about this business.
- DO NOT invent details that are not clearly supported by the answers.
- DO NOT say “likely”, “seems”, “appears”, or anything that sounds uncertain.
- State things as facts based on what they wrote.
- If something is not mentioned, simply don’t talk about it.
- Reuse their own words where helpful, but clean up grammar and make it clearer.
- Keep every section short, concrete, and easy to scan.
- Use simple business language (no tech jargon like “CRM”, “APIs”, “backend”).
- Do NOT use markdown symbols (#, ##, *, bullets with hyphens, etc.).
- Do NOT include emojis.
- Speak directly to the owner using “you” and “your business”.
- Maintain the same structure every time so the PDF layout stays consistent.

------------------------------------------------------------
INPUT DATA FROM INTAKE FORM

Owner name: {name}
Business name: {business_name}

Services offered:
{services_offered}

Lead response speed (how fast they respond now):
{lead_response_speed}

Leads per week:
{leads_per_week}

Jobs per week:
{jobs_per_week}

Customer contact methods:
{contact_methods}

Biggest frustration:
{biggest_frustration}

Where time is being lost:
{time_loss_areas}

Main goals for the next 6–12 months:
{main_goals}

What they’d love to stop doing manually (desired automations):
{desired_automations}

Current software/tools:
{current_software}

Ideal customer experience:
{ideal_experience}
------------------------------------------------------------

NOW WRITE THE BLUEPRINT USING THIS EXACT STRUCTURE
(plain text headings, no markdown):

TITLE: AI Automation Blueprint

SECTION 1: Quick Snapshot
Write 4–6 short bullets that:
- Describe what type of business they run using their services and volume (leads/jobs per week).
- Call out their biggest frustration and main goals in your own clear words.
- Describe where they are losing time, using their “time loss areas” and contact methods.
- Highlight the main opportunities for automation based on their desired automations and current situation.

Every bullet in this section MUST be clearly connected to one or more of:
services offered, leads per week, jobs per week, biggest frustration,
time loss areas, desired automations, ideal customer experience.

SECTION 2: What You Told Me
Rewrite their answers into the following labeled subsections:

Subsection: Your Goals
Use ONLY their “main goals” and anything related from other answers.
Summarize into 3–5 bullets in clear, simple language.

Subsection: Your Challenges
Use mainly “biggest frustration”, “time loss areas”, and anything that
sounds like a problem in their answers.
Write 3–6 bullets that feel very specific to them.

Subsection: Where Time Is Being Lost
Use ONLY their “time loss areas”, lead response speed, contact methods,
and anything that adds detail.
Write 3–5 bullets that describe where time, focus, or money is being wasted.

Subsection: Opportunities You’re Not Leveraging Yet
Use their “desired automations”, “ideal customer experience”, and
“current software”.
Write 3–6 bullets that show clear, practical opportunities for automation
in their exact situation.

SECTION 3: Your Top 3 Automation Wins
Create three “wins” that are obviously based on their answers.

For each win, write:

WIN 1 – Short outcome-focused title
What This Fixes:
- 2–4 bullets describing the specific problems this win addresses,
  based on their challenges and time loss areas.

What This Does For You:
- 3–4 bullets describing the benefits in plain language
  (time saved, fewer headaches, more booked jobs, better experience).

What’s Included:
- 3–5 bullets describing what the automation actually DOES day to day,
  using their desired automations and contact methods (e.g. faster replies,
  automatic reminders, better tracking).

Repeat this structure for WIN 2 and WIN 3.
Each win should feel different and cover a different cluster of problems.

SECTION 4: Your Automation Scorecard (0–100)
Give a score between 0 and 100 that fits their current level.
Base it on:
- How manual their communication and scheduling are.
- Whether they’re using any software already.
- Their goals vs. where they are now.

Then write 4–6 bullets explaining:
- What they’re already doing well.
- Where they are weak or at risk.
- What this score means in simple language.
- What is most important to fix first.

SECTION 5: Your 30-Day Action Plan
Break into weekly sections:

Week 1 — Stabilize The Day-To-Day
Write 3–4 bullets focused on fixing the worst leaks first
(response speed, missed calls/messages, basic tracking).

Week 2 — Increase Booked Jobs
Write 3–4 bullets focused on follow-up, reminders, and
making it easier for people to book jobs.

Week 3 — Improve Customer Experience
Write 3–4 bullets focused on communication, updates,
and matching their “ideal customer experience”.

Week 4 — Optimize and Prepare to Scale
Write 3–4 bullets focused on small improvements, better visibility,
and getting ready to handle more volume without more chaos.

SECTION 6: Final Recommendations
Write 5–7 short bullets that:
- Tell them what to focus on first.
- Highlight the simple wins that will have the biggest impact.
- Reassure them they don’t need to fix everything at once.
- Suggest what they should have ready before a strategy call
  (examples: logins, examples of messages, simple numbers).
- Point out their biggest long-term opportunity based on their goals.

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
    return "Apex Blueprint API (Render + S3, tailored single-prompt version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

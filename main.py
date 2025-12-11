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

    # Show business name on the cover if we have it
    if business_name:
        cover_subtitle = f"AI Automation Blueprint for {business_name}"
    else:
        cover_subtitle = "AI Automation Blueprint for Your Service Business"

    story.append(Paragraph(cover_subtitle, tagline_style))

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

        # Treat obvious section labels as headings
        upper = line.upper()
        if (
            upper.startswith("TITLE:")
            or upper.startswith("SECTION ")
            or upper.startswith("WIN ")
            or upper.startswith("WEEK ")
            or upper.startswith("SUBSECTION:")
        ):
            story.append(Paragraph(line, heading_style))
        else:
            story.append(Paragraph(line, body_style))

    # ------- CTA BLOCK AT END -------
    story.append(Spacer(1, 20))
    story.append(
        Paragraph(
            "Next Step: Book Your Automation Strategy Call",
            cta_heading_style,
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
# /run – TWO-STEP (THINK → WRITE) BLUEPRINT GENERATION
# --------------------------------------------------------------------
@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    Called by your automation system when the form is submitted.
    1) Takes the contact + form answers.
    2) First AI call: turns answers into a structured brief (no guessing).
    3) Second AI call: turns the brief into the final blueprint.
    4) Generates a PDF, uploads it to S3, and returns JSON.
    """
    data = request.get_json(force=True) or {}

    # Contact + form info
    contact = data.get("contact", {}) or data.get("contact_data", {})
    form_fields = data.get("form_fields", {}) or data.get("form", {}) or {}

    # Basic contact details
    name = (
        contact.get("first_name")
        or contact.get("firstName")
        or contact.get("name")
        or form_fields.get("Full Name")
        or "there"
    )
    email = contact.get("email") or form_fields.get("Email") or ""
    phone = contact.get("phone") or form_fields.get("Phone") or ""

    # Explicitly pull your 12 questions (falling back to generic keys just in case)
    business_name = (
        form_fields.get("Business Name")
        or form_fields.get("business_name")
        or ""
    )
    services_offered = (
        form_fields.get("Services Offered")
        or form_fields.get("services_offered")
        or ""
    )
    lead_response_speed = (
        form_fields.get("Lead Response Speed")
        or form_fields.get("lead_response_speed")
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
    contact_methods = (
        form_fields.get("Customer Contact Methods")
        or form_fields.get("customer_contact_methods")
        or ""
    )
    biggest_frustration = (
        form_fields.get("Biggest Frustration")
        or form_fields.get("biggest_frustration")
        or ""
    )
    time_loss_areas = (
        form_fields.get("Time Loss Areas")
        or form_fields.get("time_loss_areas")
        or ""
    )
    main_goals = (
        form_fields.get("Main Goals")
        or form_fields.get("main_goals")
        or ""
    )
    desired_automations = (
        form_fields.get("Desired Automations")
        or form_fields.get("desired_automations")
        or ""
    )
    current_software = (
        form_fields.get("Current Software")
        or form_fields.get("current_software")
        or ""
    )
    ideal_customer_experience = (
        form_fields.get("Ideal Customer Experience")
        or form_fields.get("ideal_customer_experience")
        or ""
    )

    # Also keep a raw dump for debugging if needed
    raw_form_text_lines = [f"{k}: {v}" for k, v in form_fields.items()]
    raw_form_text = "\n".join(raw_form_text_lines) if raw_form_text_lines else "N/A"

    # --------- STEP 1: THINK – CREATE A STRUCTURED BRIEF ----------
    think_prompt = f"""
You are APEX AI, a senior automation consultant for home-service businesses.

You will be given raw intake answers from a blueprint form.
Your job in THIS STEP is ONLY to create a clear, structured BRIEF.
Another AI call will later write the final blueprint from this brief.

DO NOT write the final blueprint.
DO NOT add generic assumptions.
DO NOT say "likely", "probably", or "appears to".
If something is not mentioned, say "Not mentioned" instead of guessing.

Use the exact wording from the owner wherever it helps.
You can lightly clean typos, but keep their meaning.

-------------------------
INTAKE ANSWERS
Owner Name: {name}
Email: {email}
Phone: {phone}

Business Name: {business_name}
Services Offered: {services_offered}
Lead Response Speed: {lead_response_speed}
Leads Per Week: {leads_per_week}
Jobs Per Week: {jobs_per_week}
Customer Contact Methods: {contact_methods}
Biggest Frustration: {biggest_frustration}
Time Loss Areas: {time_loss_areas}
Main Goals: {main_goals}
Desired Automations: {desired_automations}
Current Software: {current_software}
Ideal Customer Experience: {ideal_customer_experience}

Raw Form Dump (for extra context):
{raw_form_text}
-------------------------

Now create a structured brief using EXACTLY this format:

BUSINESS SUMMARY
- One sentence describing what the business does, based ONLY on "Services Offered" and "Business Name".
- Bullet about typical customer type, ONLY if it is clearly stated.
- Bullet about current lead + job volume using the actual numbers (Leads Per Week, Jobs Per Week).

GOALS
- 3–6 bullets summarizing "Main Goals" in plain English.
- If goals are vague, clarify them gently but stay close to their wording.

PAIN POINTS
- 3–6 bullets combining "Biggest Frustration" and anything relevant from "Time Loss Areas".
- Use their own phrases in quotes where helpful.

TIME LOSS AREAS
- 3–5 bullets that clearly describe where time is being wasted, based ONLY on their answers.

DESIRED AUTOMATIONS
- 3–6 bullets.
- If they say they are "not sure" or "whatever you recommend",
  write bullets that make reasonable suggestions based on their PAIN POINTS and TIME LOSS AREAS
  (for example: missed calls, slow follow-up, manual scheduling).
- Make it VERY clear these are recommendations, not things they already have.

CUSTOMER EXPERIENCE
- 3–5 bullets summarizing "Ideal Customer Experience".
- Reference contact methods and response speed if mentioned.

OTHER NOTES
- Any relevant details from "Current Software" or other fields.
- If a field is empty, mention "Current Software: Not mentioned" or similar.
"""

    try:
        think_response = client.responses.create(
            model="gpt-4.1-mini",
            input=think_prompt,
        )
        brief_text = think_response.output[0].content[0].text.strip()
        print("Brief length (chars):", len(brief_text), flush=True)

        # --------- STEP 2: WRITE – USE BRIEF TO CREATE BLUEPRINT ----------
        write_prompt = f"""
You are APEX AI, a senior automation consultant.

You will be given a structured BRIEF about a home-service business.
Using ONLY that brief, write a clear, premium-feeling AI Automation Blueprint.

RULES:
- Do NOT invent facts that are not in the brief.
- Do NOT say "likely", "probably", or "appears to".
- Use the business name and owner’s situation so it feels personal.
- Use simple business language (no tech jargon).
- Prefer bullets over long paragraphs.
- Speak directly to the owner using "you" and "your business".
- Never mention AI, prompts, surveys, or that this was generated.
- Keep the structure identical every time so it works well as a template.

-------------------------
BRIEF
{brief_text}
-------------------------

Now write the blueprint in this exact structure and labels:

TITLE: AI Automation Blueprint for {business_name if business_name else "Your Service Business"}

SECTION 1: Quick Snapshot
- 4–6 bullets that summarize:
  - What the business does
  - The main pain points
  - Where they are losing time or money
  - The biggest opportunities for automation
  - Any clear numbers from the brief (like leads/jobs per week)

SECTION 2: What You Told Me

Subsection: Your Goals
- 3–5 bullets directly reflecting the GOALS section of the brief.

Subsection: Your Challenges
- 3–6 bullets summarizing the PAIN POINTS.

Subsection: Where Time Is Being Lost
- 3–5 bullets summarizing the TIME LOSS AREAS.

Subsection: Opportunities You’re Not Leveraging Yet
- 3–6 bullets that connect their PAIN POINTS and TIME LOSS AREAS
  to the DESIRED AUTOMATIONS recommendations in the brief.

SECTION 3: Your Top 3 Automation Wins

For each win, write:

WIN 1: [Short outcome-focused title]
What This Fixes:
- 2–4 bullets

What This Does For You:
- 3–4 bullets with benefits (more booked jobs, fewer missed calls, time back, less stress).

What’s Included:
- 3–5 bullets describing simple, easy-to-understand automation actions,
  clearly based on their situation (e.g. missed calls, manual scheduling, no-shows).

Then write WIN 2 and WIN 3 in the same style, focusing on different improvements from the brief.

SECTION 4: Your Automation Scorecard (0–100)
- Start with: "Your current automation score: X/100" with a reasonable
  score based ONLY on the brief (do not say how you calculated it).
- Then 4–6 bullets explaining:
  - Where they are strong
  - Where they are weak
  - What this score means in plain English
  - What is most important to fix first

SECTION 5: Your 30-Day Action Plan

Week 1 — Stabilize
- 3–4 bullets focused on stopping the biggest leaks (e.g. missed calls, response times).

Week 2 — Increase Booked Jobs
- 3–4 bullets focused on follow-up and converting more leads to jobs.

Week 3 — Improve Customer Experience
- 3–4 bullets focused on communication, updates, and reliability.

Week 4 — Optimize and Prepare to Scale
- 3–4 bullets focused on reporting, consistency, and light optimization.

SECTION 6: Final Recommendations
- 5–7 bullets of clear guidance:
  - What to implement first
  - What will bring the fastest improvements
  - What they don’t need to worry about yet
  - What to bring to a strategy call (examples: key processes, access to current tools)
  - Where their biggest long-term opportunity is

END OF BLUEPRINT
"""

        write_response = client.responses.create(
            model="gpt-4.1-mini",
            input=write_prompt,
        )
        blueprint_text = write_response.output[0].content[0].text.strip()
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
                "phone": phone,
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
    return "Apex Blueprint API (Render + S3, two-step version) is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

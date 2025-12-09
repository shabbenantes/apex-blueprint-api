
from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
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
# MAIN ENDPOINT (single prompt, stable)
# --------------------------------------------------------------------
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

    # 🔥 AI Business Blueprint prompt (fixed, no illegal { } inside)
    prompt = f"""
You are APEX AI, a business automation consultant for home service companies.
Your job is to create a clean, premium, easy-to-read AI Automation Blueprint
based on the owner’s answers.

Your writing must follow these rules:
- Use SIMPLE business language (no jargon: no “CRM”, no “API”, no “backend”)
- Be extremely clear
- Be structured and visually clean
- Be written like a professional consultant
- Be outcome-focused: more booked jobs, fewer missed calls, faster response, less stress
- Make the owner feel understood
- Make the blueprint feel valuable, but NOT overwhelming
- Do NOT give step-by-step instructions
- Do NOT give tool setup instructions
- Do NOT refer to “the form” or “the user”
- Talk directly to the owner using “you” and “your business”
- Keep sections tight, clean, and easy to scan

---------------------------------------------------------
# AI AUTOMATION BLUEPRINT
Prepared for: {name}
Business: {business_name if business_name else "Not specified"}

---------------------------------------------------------
## 1. Your 1-Page Business Summary
(Keep this ultra clear, 3–6 bullets total)

Include:
- What type of business you appear to run
- Your biggest pain points (rewrite them in clear language)
- The biggest opportunities for automation
- What is costing you the most money right now
- What feels overwhelming or chaotic in your current process

Make this section feel like: “You understand me.”

---------------------------------------------------------
## 2. Your Top 3 Automation Wins
(Each win MUST be outcome-focused, simple, and powerful)

For each win, use this structure:

### WIN: Short, outcome-focused title
Examples: “Never Miss Another Call”, “Get Faster Booked Jobs”,
“Follow-Up That Never Stops”, “More Reviews on Autopilot”, etc.

**What this fixes in your business:**
- 2–4 bullets describing the specific business problem this automation solves
- Use simple, real-world language

**What this does for you:**
- 3–4 bullets describing the benefits (time saved, more booked jobs, fewer headaches)

**What’s included in this win:**
- 3–5 items described in plain English
  Examples: “Instant text replies”, “Lead follow-up messages”,
  “Automatic reminders”, “After-hours call handling”

Do NOT describe how to build any automation.
Just describe what it does and why it matters.

---------------------------------------------------------
## 3. Your Automation Scorecard (0–100)

Give the business a simple “automation maturity score” based on the answers.
Explain:
- Where they are strong
- Where they are weak
- What this score means in plain English

---------------------------------------------------------
## 4. Your 30-Day Game Plan
(Each week: 3–4 simple bullets)

### Week 1 — Stabilize the Business
- Fix the biggest leaks first (missed calls, slow response, lost leads)
- Get one automation live quickly
- Give the owner a quick win

### Week 2 — Increase Booked Jobs
- Add follow-up messages
- Reduce no-shows
- Improve new lead response

### Week 3 — Build Customer Experience
- Improve review flow
- Improve rebooking
- Add simple customer updates or reminders

### Week 4 — Scale and Optimize
- Add additional automations that support growth
- Improve reporting and visibility
- Prep for monthly maintenance

Keep each bullet SIMPLE and non-technical.

---------------------------------------------------------
## 5. What You Told Me
Rewrite the owner’s answers in clean categories:

### Your Goals
- Summarize their top goals in fresh language

### Your Challenges
- Summarize the problems they described

### Where You’re Losing Time
- Explain in clear, simple terms

### Opportunities You’re Not Taking Advantage Of
- Show them the value they’re leaving on the table

Make this section feel like a mirror: “Yes, that IS my situation.”

---------------------------------------------------------
## 6. Final Recommendations
Give 4–6 clear bullets such as:

- “Start with Win #1 — it will bring the fastest return.”
- “You don’t need to fix everything at once — follow the 30-day plan.”
- “Your biggest opportunity is improving ____.”
- “Here’s what to have ready before an automation strategy call.”

DO NOT sell anything directly.
Just create clarity and confidence.

---------------------------------------------------------

STYLE REQUIREMENTS:
- Clean, crisp, consultant tone
- Short sentences
- Lots of spacing
- Bullet points preferred over paragraphs
- No fluff
- No AI-sounding text
- No technical explanations
- No tool names unless absolutely necessary
- Must feel PREMIUM, calm, and high-trust

Owner's raw answers:
{raw_form_text}
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        # Full blueprint text
        blueprint_text = response.output[0].content[0].text

        # Try to carve out a shorter "summary" section for email previews:
        # everything before "## 2. Your Top 3 Automation Wins"
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

        # Build a URL that your automation system can use
        base_url = request.host_url.rstrip("/")
        pdf_url = f"{base_url}/pdf/{pdf_id}"

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,   # full document
                "summary": summary_section,    # quick overview section
                "pdf_url": pdf_url,            # link to the PDF
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
# PDF SERVE + HEALTHCHECK
# --------------------------------------------------------------------
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

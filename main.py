
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


def generate_pdf(blueprint_text: str, pdf_path: str, name: str, business_name: str):
    """
    Turn the blueprint text into a clean, branded PDF.
    """
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#000000"),
        spaceAfter=12,
    )

    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=12,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
        spaceAfter=18,
    )

    heading_style = ParagraphStyle(
        "HeadingStyle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=colors.HexColor("#000000"),
        spaceBefore=12,
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
        title="AI Business Blueprint",
        author="Apex Automation",
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )

    story = []

    # Header / cover
    story.append(Paragraph("Apex Automation", title_style))
    story.append(
        Paragraph("AI Business Blueprint for Your Service Business", subtitle_style)
    )

    owner_line = f"For: {name if name else 'Your Business Owner'}"
    if business_name:
        owner_line += f"  |  Business: {business_name}"

    story.append(Paragraph(owner_line, body_style))
    story.append(Spacer(1, 18))

    # Intro block
    story.append(
        Paragraph(
            "This document outlines where your business is currently leaking time and money, and the simplest automation wins to fix it over the next 30 days.",
            body_style,
        )
    )
    story.append(Spacer(1, 12))

    # Split the blueprint text into chunks by double line breaks
    sections = blueprint_text.split("\n\n")

    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue

        # Simple handling: if a line starts with '#', treat it as a heading
        if stripped.startswith("# "):
            # Main heading
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 12))
            story.append(Paragraph(heading_text, heading_style))

        elif stripped.startswith("## "):
            heading_text = stripped.lstrip("# ").strip()
            story.append(Spacer(1, 8))
            story.append(Paragraph(heading_text, heading_style))

        else:
            # Regular paragraph
            # Replace simple bullets with nicer paragraphs if needed
            # We'll just render them as text here
            story.append(Paragraph(stripped.replace("\n", "<br/>"), body_style))

    # Final CTA
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

    # 🔥 AI Business Blueprint prompt
    prompt = f"""
You are APEX AI, an expert AI automation consultant for small service businesses.

Your job is to write a **short, clear, easy-to-read AI Business Blueprint** for this owner based on their answers.

Contact name: {name}
Contact email: {email}
Business name: {business_name if business_name else "Not specified"}

Raw form answers from the owner:
{raw_form_text}

Write the blueprint using this structure and formatting:

# AI Business Blueprint

## 1. Quick Summary (2–4 short bullets)
- What kind of business this appears to be
- Their biggest pain points or time-wasters (in your own words)
- The biggest opportunities for AI/automation in their business

Keep this section very simple and written in plain English.

---

## 2. Top 3 Automation Wins (High Impact, Simple to Start)
For each win, follow this format:

### Win: Short, outcome-focused title
**What to automate:**  
- 1–3 bullets describing what part of their business to automate (calls, follow-up, scheduling, missed calls, estimates, etc.)

**Why it matters:**  
- 1–3 bullets explaining how this helps them (time saved, fewer dropped leads, less chaos, more revenue)

**Suggested tools or approach:**  
- Name a few non-technical options: e.g. customer contact/lead system (like GoHighLevel), simple call/text workflows, AI assistant (ChatGPT), simple automations.

Write all of this specifically for THEIR business type, based on their answers.

---

## 3. Suggested AI Stack for Your Business
List 5–8 simple items, in bullets, such as:
- AI assistant (for writing replies, messages, and basic content)
- Customer contact + pipeline automations (for leads, follow-ups, and reminders)
- Call/text workflows (missed call → text back, appointment reminders)
- Simple reporting or dashboard tools
- Any industry-specific tools that fit what they described

Explain each in one plain sentence (“This helps you…”).

---

## 4. 30-Day Action Plan
Break the next 30 days into weeks with **realistic, beginner-friendly steps**:

### Week 1 – Foundation
- Pick and set up your core tools (customer contact system / basic automations)
- Get 1 quick win live (like missed call → text)

### Week 2 – Expansion
- Add 1–2 more automations that remove manual work they mentioned
- Start using AI to help write messages/emails

### Week 3 – Optimization
- Review what’s working
- Adjust any automations that feel annoying or confusing
- Add one simple reporting or tracking view

### Week 4 – Scale & Next Ideas
- Add follow-up sequences for leads or customers
- Plan one “next level” automation (something deeper, but still doable)

Tailor the bullets under each week to THEIR business and what they said in the form.

---

## 5. Notes From Your Answers
Summarize their answers back to them in a clean way, under small headings like:
- Goals
- Current challenges
- Where you’re losing time
- Other details you mentioned

Do NOT just copy their answers; rewrite them so they feel understood.

---

## 6. Final Recommendations
End with 3–5 bullet points, such as:
- Which automation win they should start with first
- What to have ready before an automation strategy call
- A reminder that they don’t need to do everything at once

Formatting rules:
- Use clear headings (H1, H2, H3) and bullet points.
- Make it read like a finished, well-edited document you’d send to a client.
- Talk directly to the owner (“you”, “your business”), not “the user” or “the form”.
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        # Full blueprint text
        blueprint_text = response.output[0].content[0].text

        # Try to carve out a shorter "summary" section for email previews:
        # everything before "## 2. Top 3 Automation Wins"
        summary_section = blueprint_text
        marker = "## 2. Top 3 Automation Wins"
        if marker in blueprint_text:
            summary_section = blueprint_text.split(marker, 1)[0].strip()

        # Generate a unique PDF file in /tmp
        pdf_id = uuid.uuid4().hex
        pdf_filename = f"blueprint_{pdf_id}.pdf"
        pdf_dir = "/tmp"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        generate_pdf(blueprint_text, pdf_path, name, business_name)

        # Build a URL that your automation system can use
        # request.host_url gives something like "https://your-app.onrender.com/"
        base_url = request.host_url.rstrip("/")
        pdf_url = f"{base_url}/pdf/{pdf_id}"

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,      # full document
                "summary": summary_section,       # quick overview section
                "pdf_url": pdf_url,               # link to the PDF
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

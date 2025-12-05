from flask import Flask, request, jsonify
import os
from openai import OpenAI

app = Flask(__name__)

# OpenAI client using env var (set OPENAI_API_KEY in Render)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


@app.route("/run", methods=["POST"])
def run_blueprint():
    """
    This endpoint is called by GoHighLevel via webhook
    when your 4-question form is submitted.
    It expects the standard GHL event JSON (contact + form fields),
    generates a polished blueprint, and returns it as JSON.
    """
    data = request.get_json(force=True) or {}

    # Try to pull out contact info from typical GHL payload shapes
    contact = data.get("contact", {}) or data.get("contact_data", {})
    form_fields = data.get("form_fields", {}) or data.get("form", {}) or {}

    # Fallbacks so it never crashes if a key is missing
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

    # 🔥 This is the new, clean prompt
    prompt = f"""
You are an expert AI Automation & Operations consultant.
You write client-ready reports that are clear, well formatted, and easy to skim.

You are preparing a **30-Day AI Automation Blueprint** for this business owner.
Use professional but friendly language and fix any grammar or wording issues.

Contact name: {name}
Contact email: {email}
Business name: {business_name if business_name else "Not specified"}

Raw form answers from the owner:
{raw_form_text}

Write the blueprint using this structure and formatting:

# 30-Day AI Automation Blueprint

## 1. Quick Snapshot (3–5 bullets)
- What kind of business this appears to be
- Their biggest bottlenecks or pain points (in your own words)
- Any obvious opportunities for automation

## 2. Top 3–5 Quick Wins (Next 30 Days)
For each quick win, use this format:
- **Win #{n}: Short, outcome-focused title**
  - What to automate:
  - Tools to use (name specific tools like Zapier, Make, ChatGPT, CRMs, etc. when relevant):
  - Where it plugs into their current workflow:
  - Expected benefit (time saved, errors reduced, more leads, etc.):

Keep this section very actionable and specific.

## 3. Longer-Term Automation Opportunities (60–180 Days)
Give 3–5 ideas for deeper automations (systems, dashboards, multi-step workflows).
For each one:
- Name of the opportunity
- What it would look like in their business
- Why it matters / upside

## 4. Suggested Tech Stack & Integrations
List specific tools and integrations you recommend for this business based on their answers.
Group them under headings like:
- CRM & Contact Management
- Lead Generation & Follow-Up
- Operations & Internal Processes
- Reporting & Dashboards

## 5. Implementation Roadmap (Next 30 Days)
Break the next 30 days into weeks:
- Week 1:
- Week 2:
- Week 3:
- Week 4:

Under each week, list 3–5 concrete tasks that someone could actually check off.

## 6. Prep for Our Automation Strategy Call
End with a short section that:
- Summarizes what you plan to focus on first with them
- Lists 3–4 questions you’ll ask on the call
- Encourages them to bring logins or examples (screenshots, reports, email sequences, etc.)

Formatting rules:
- Use clear headings (H1, H2, H3) and bullet points.
- Make it read like a finished, well-edited document you’d send to a paying client.
- Do NOT talk about “the form” or “questions above” – talk directly to the business owner (“you”, “your business”).
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        # Extract text from the new Responses API format
        blueprint_text = response.output[0].content[0].text

        return jsonify(
            {
                "success": True,
                "blueprint": blueprint_text,
                "name": name,
                "email": email,
                "business_name": business_name,
            }
        )

    except Exception as e:
        # Log the error and return a safe message
        print("Error generating blueprint:", e)
        return jsonify(
            {
                "success": False,
                "error": str(e),
            }
        ), 500


@app.route("/", methods=["GET"])
def healthcheck():
    return "Apex Blueprint API is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

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

       # 🔥 New simpler AI Business Blueprint prompt
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
- Name a few non-technical options: e.g. CRM automation (like GoHighLevel), Zapier/Make, AI assistant (ChatGPT), simple call/text workflows.

Write all of this specifically for THEIR business type, based on their answers.

---

## 3. Suggested AI Stack for Your Business
List 5–8 simple items, in bullets, such as:
- AI assistant (for writing replies, messages, and basic content)
- CRM + pipeline automations (for leads, follow-ups, and reminders)
- Call/text workflows (missed call → text back, appointment reminders)
- Simple reporting or dashboard tools
- Any industry-specific tools that fit what they described

Explain each in one plain sentence (“This helps you…”).

---

## 4. 30-Day Action Plan
Break the next 30 days into weeks with **realistic, beginner-friendly steps**:

### Week 1 – Foundation
- Pick and set up your core tools (CRM / basic automations)
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

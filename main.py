from flask import Flask, request, jsonify
import os
from openai import OpenAI

app = Flask(__name__)

# OpenAI client using env var
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


@app.route("/run", methods=["POST"])
def run_blueprint():
    data = request.get_json(force=True)

    contact = data.get("contact", {})
    form_fields = data.get("form_fields", {})

    name = contact.get("firstName", "there")
    business = form_fields.get("business_name") or form_fields.get("Business Name") or "their business"

    prompt = f"""
    You are an AI Automation consultant.

    Create a clear, easy-to-read AI Automation Blueprint for this business.

    Contact name: {name}
    Business: {business}
    Raw form data: {form_fields}

    Include sections:
    1. Quick Summary (2–3 sentences)
    2. Top 3 Automation Wins (each with: what to automate, tools to use, est. time saved per week)
    3. Implementation Plan (30-day roadmap broken into weekly steps)
    4. Extra Ideas (optional, 3–5 bullet points)
    Use friendly, simple language.
    """

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    blueprint = response.choices[0].message.content.strip()

    # This is what HighLevel will see in the webhook response
    return jsonify({
        "status": "ok",
        "blueprint": blueprint
    })
    

if __name__ == "__main__":
    # for local testing only; Render will use gunicorn
    app.run(host="0.0.0.0", port=5000)

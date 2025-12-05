from flask import Flask, request, jsonify
import openai
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
import markdown
from datetime import datetime

app = Flask(__name__)

# --------------------------
# CONFIG
# --------------------------

OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"

# Google Service Account JSON (store as environment variable in Render)
GOOGLE_CREDS = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/documents", 
            "https://www.googleapis.com/auth/drive"]
)

docs_service = build("docs", "v1", credentials=GOOGLE_CREDS)
drive_service = build("drive", "v3", credentials=GOOGLE_CREDS)

openai.api_key = OPENAI_API_KEY

# --------------------------
# BLUEPRINT PROMPT
# --------------------------

BLUEPRINT_PROMPT = """YOU PASTE THE NEW PROMPT HERE EXACTLY AS I GAVE IT"""

# --------------------------
# GOOGLE DOC CREATION
# --------------------------

def create_google_doc(title, markdown_text, client_email):
    # Create empty Doc
    doc = docs_service.documents().create(
        body={"title": title}
    ).execute()
    
    doc_id = doc["documentId"]

    # Convert markdown → HTML → Google Docs insertable text
    html = markdown.markdown(markdown_text)

    # Insert into document
    requests_body = {
        "requests": [{
            "insertText": {
                "location": {"index": 1},
                "text": markdown_text
            }
        }]
    }

    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body=requests_body
    ).execute()

    # Share with the client
    drive_service.permissions().create(
        fileId=doc_id,
        body={
            "type": "user",
            "role": "writer",
            "emailAddress": client_email
        },
        sendNotificationEmail=True
    ).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"

# --------------------------
# OPENAI REQUEST
# --------------------------

def generate_blueprint(form_data):
    response = openai.ChatCompletion.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": BLUEPRINT_PROMPT},
            {"role": "user", "content": str(form_data)}
        ]
    )

    result = response.choices[0].message["content"]

    return result


# --------------------------
# API ROUTE FOR GOHIGHLEVEL
# --------------------------

@app.route("/generate-blueprint", methods=["POST"])
def handle_form():
    data = request.json

    client_name = data.get("firstName", "Business Owner")
    business_name = data.get("businessName", "Your Business")
    client_email = data.get("email")

    # 1. Generate blueprint text from OpenAI
    blueprint = generate_blueprint(data)

    # 2. Create Google Doc
    doc_url = create_google_doc(
        title=f"Apex Blueprint - {business_name}",
        markdown_text=blueprint,
        client_email=client_email
    )

    # 3. Return URL for logging
    return jsonify({
        "status": "success",
        "doc_url": doc_url
    })


@app.route("/", methods=["GET"])
def health_check():
    return "Apex server running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

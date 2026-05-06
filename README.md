# BMC Webhook — Deployment Guide

## What this does
Receives a POST from Zapier when a client posts a link in the community.
Finds that client's Lifecycle folder in Google Drive, navigates to
"6. Community Posts", and saves the file there automatically.

Supports: Google Drive links, Dropbox links, MP3 links, Noteflight (logged).

---

## Files needed
- app.py — the webhook server
- requirements.txt — Python dependencies
- credentials.json — Google service account key (DO NOT commit to GitHub)

---

## Step 1 — Share the Client Lifecycle Content folder with the service account

In Google Drive, share this folder with Editor access:
  bmc-webhook@bmc-automation-495519.iam.gserviceaccount.com

Folder: Client Lifecycle Content
ID: 1JbvYsh8KfUVEikSndaaSYgsfCRvq6wqW

---

## Step 2 — Deploy to Render (free)

1. Create a GitHub repo, push these files (NOT credentials.json)
2. Go to render.com → New Web Service → connect repo
3. Add environment variable:
   GOOGLE_CREDENTIALS_JSON = (paste the full contents of credentials.json)
4. Deploy

Then update app.py line 12 to read from env:
  import json
  creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
  creds = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)

---

## Step 3 — Update Zapier

In the existing Zap, add a Webhooks by Zapier step BEFORE the Asana step:
- Method: POST
- URL: https://your-render-url.onrender.com/webhook
- Payload:
  {
    "person": [Person field from Google Sheets step],
    "body": [Data Body from trigger],
    "post_title": [Data Title from trigger]
  }

---

## Webhook payload format

POST /webhook
{
  "person": "Alex Smith [Exevot]",
  "body": "Here is my track https://drive.google.com/file/d/xxxxx/view",
  "post_title": "Week 3 update"
}

Response:
{
  "status": "success",
  "person": "Alex Smith [Exevot]",
  "client_folder": "Alex Smith [Exevot] Lifecycle C...",
  "destination": "6. Community Posts",
  "links_processed": 1,
  "results": [...]
}

---

## Testing locally

pip install -r requirements.txt
cp bmc-automation-495519-3cd481d73918.json credentials.json
python app.py

Then send a test POST:
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{"person":"Alex Smith [Exevot]","body":"Check this out https://drive.google.com/file/d/TEST/view","post_title":"Test post"}'

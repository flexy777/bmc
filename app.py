from flask import Flask, request, jsonify
import re
import os
import io
import json
import requests
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# ── Google Drive setup ────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_LIFECYCLE_FOLDER_ID = "1JbvYsh8KfUVEikSndaaSYgsfCRvq6wqW"
COMMUNITY_POSTS_FOLDER_NAME = "6. Community Posts"
SCREENSHOTS_FOLDER_NAME = "SCREENSHOTS"
PAGEPIXELS_API_KEY = "vIpwjc7PWJTJVoKV5qRTJKNf-S9xlInEjPQDCWBfWzo"

# ── PagePixels screenshot ─────────────────────────────────────────────────────
def take_screenshot(url):
    """Take a full-page screenshot of a URL using PagePixels. Returns image bytes or None."""
    try:
        resp = requests.get(
            "https://pagepixels.com/app/screenshots",
            params={
                "access_token": PAGEPIXELS_API_KEY,
                "url": url,
                "full_page": "true",
                "format": "png",
            },
            timeout=60
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Screenshot failed for {url}: {e}")
        return None

# ── Google Drive helpers ──────────────────────────────────────────────────────
def find_folder(service, parent_id, name_contains):
    """Find a subfolder whose name contains the given string."""
    results = service.files().list(
        q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    for folder in results.get("files", []):
        if name_contains.lower() in folder["name"].lower():
            return folder["id"], folder["name"]
    return None, None

def find_or_create_folder(service, parent_id, folder_name):
    """Find a folder by exact name or create it."""
    results = service.files().list(
        q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"]
    folder = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True
    ).execute()
    return folder["id"]

def upload_to_drive(service, folder_id, filename, file_bytes, mime_type):
    """Upload bytes as a file into a Drive folder."""
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
    file = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, name, webViewLink",
        supportsAllDrives=True
    ).execute()
    return file

# ── Link helpers ──────────────────────────────────────────────────────────────
def extract_links(text):
    return re.findall(r'https?://[^\s\'"<>\]]+', text)

# ── Main webhook ──────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    person     = data.get("person", "").strip()      # "Ness Savelkoul [Dr.Ness]"
    body       = data.get("body", "").strip()         # post content
    post_title = data.get("post_title", "untitled")  # post title
    post_url   = data.get("post_url", "").strip()     # community post URL

    if not person:
        return jsonify({"status": "error", "message": "Missing 'person' field"}), 400
    if not post_url:
        return jsonify({"status": "error", "message": "Missing 'post_url' field"}), 400

    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M")
    # Use first name only for folder search
    clean_name = person.split("[")[0].strip()
    first_name = clean_name.split()[0] if clean_name else ""
    if not first_name:
        return jsonify({"status": "skipped", "message": "No person found — likely a test post, skipping."}), 200

    try:
        service = get_drive_service()

        # 1. Find client lifecycle folder
        client_folder_id, client_folder_name = find_folder(
            service, CLIENT_LIFECYCLE_FOLDER_ID, first_name
        )
        if not client_folder_id:
            return jsonify({"status": "error", "message": f"No lifecycle folder found for '{person}'"}), 404

        # 2. Get destination folders
        community_folder_id  = find_or_create_folder(service, client_folder_id, COMMUNITY_POSTS_FOLDER_NAME)
        screenshots_folder_id = find_or_create_folder(service, client_folder_id, SCREENSHOTS_FOLDER_NAME)

        results = []

        # ── STEP A: Always screenshot the community post itself ───────────────
        post_screenshot = take_screenshot(post_url)
        if post_screenshot:
            safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', post_title)[:40]
            filename = f"{timestamp}_{safe_title}_post.png"
            uploaded = upload_to_drive(service, community_folder_id, filename, post_screenshot, "image/png")
            results.append({
                "step": "post_screenshot",
                "status": "success",
                "saved_to": "6. Community Posts",
                "file": uploaded.get("webViewLink")
            })
        else:
            results.append({"step": "post_screenshot", "status": "failed", "url": post_url})

        # ── STEP B: Check for links in the post body ──────────────────────────
        links = extract_links(body)
        if not links:
            return jsonify({
                "status": "success",
                "person": person,
                "client_folder": client_folder_name,
                "message": "Post screenshot saved. No links found — stopping here.",
                "results": results
            })

        # ── STEP C: Screenshot each link → save to SCREENSHOTS ────────────────
        for url in links:
            img_bytes = take_screenshot(url)
            if img_bytes:
                label = re.sub(r'https?://', '', url).split("/")[0].replace(".", "_")[:30]
                filename = f"{timestamp}_{label}_link.png"
                uploaded = upload_to_drive(service, screenshots_folder_id, filename, img_bytes, "image/png")
                results.append({
                    "step": "link_screenshot",
                    "status": "success",
                    "url": url,
                    "saved_to": "SCREENSHOTS",
                    "file": uploaded.get("webViewLink")
                })
            else:
                results.append({"step": "link_screenshot", "status": "failed", "url": url})

        return jsonify({
            "status": "success",
            "person": person,
            "client_folder": client_folder_name,
            "links_found": len(links),
            "results": results
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
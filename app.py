from flask import Flask, request, jsonify
import re
import os
import io
import requests
import tempfile
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

app = Flask(__name__)

# ── Google Drive setup ──────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    import json
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

# ── Folder IDs ───────────────────────────────────────────────────────────────
CLIENT_LIFECYCLE_FOLDER_ID = "1JbvYsh8KfUVEikSndaaSYgsfCRvq6wqW"  # Client Lifecycle Content
COMMUNITY_POSTS_FOLDER_NAME = "6. Community Posts"
SCREENSHOTS_FOLDER_NAME = "SCREENSHOTS"

# ── PagePixels config ─────────────────────────────────────────────────────────
PAGEPIXELS_API_KEY = "vIpwjc7PWJTJVoKV5qRTJKNf-S9xlInEjPQDCWBfWzo"

# ── Link detection ───────────────────────────────────────────────────────────
def extract_links(text):
    url_pattern = r'https?://[^\s\'"<>]+'
    return re.findall(url_pattern, text)

def detect_link_type(url):
    if "drive.google.com" in url:
        return "google_drive"
    elif "dropbox.com" in url:
        return "dropbox"
    elif "noteflight.com" in url:
        return "noteflight"
    elif url.endswith(".mp3") or "mp3" in url.lower():
        return "mp3"
    else:
        return "unknown"

# ── Google Drive helpers ─────────────────────────────────────────────────────
def find_folder(service, parent_id, name_contains):
    """Find a subfolder inside parent_id whose name contains name_contains."""
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    folders = results.get("files", [])
    for folder in folders:
        if name_contains.lower() in folder["name"].lower():
            return folder["id"], folder["name"]
    return None, None

def find_or_create_folder(service, parent_id, folder_name):
    """Find a folder by exact name, or create it if missing."""
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"name='{folder_name}' and trashed=false"
    )
    results = service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"]
    # Create it
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    return folder["id"]

def upload_to_drive(service, folder_id, filename, file_bytes, mime_type):
    """Upload bytes as a file into folder_id."""
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
    meta = {"name": filename, "parents": [folder_id]}
    file = service.files().create(
        body=meta, media_body=media, fields="id, name, webViewLink",
        supportsAllDrives=True
    ).execute()
    return file

def copy_google_drive_file(service, file_id, dest_folder_id, new_name):
    """Copy a Google Drive file into dest_folder_id."""
    body = {"name": new_name, "parents": [dest_folder_id]}
    copied = service.files().copy(
        fileId=file_id, body=body, fields="id, name, webViewLink",
        supportsAllDrives=True
    ).execute()
    return copied

def extract_gdrive_file_id(url):
    """Extract file/folder ID from a Google Drive URL."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# ── Per-link handlers ────────────────────────────────────────────────────────
def handle_google_drive(service, url, dest_folder_id, timestamp):
    file_id = extract_gdrive_file_id(url)
    if not file_id:
        return {"status": "error", "message": "Could not extract Google Drive file ID"}
    try:
        meta = service.files().get(
            fileId=file_id, fields="name, mimeType", supportsAllDrives=True
        ).execute()
        new_name = f"{timestamp}_{meta['name']}"
        copied = copy_google_drive_file(service, file_id, dest_folder_id, new_name)
        return {"status": "success", "type": "google_drive", "file": copied}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def handle_dropbox(service, url, dest_folder_id, timestamp):
    # Convert Dropbox share URL to direct download
    direct_url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com")
    direct_url = re.sub(r'[?&]dl=\d', '', direct_url)
    try:
        resp = requests.get(direct_url, timeout=30)
        resp.raise_for_status()
        # Guess filename from URL or headers
        filename = url.split("/")[-1].split("?")[0] or "dropbox_file"
        filename = f"{timestamp}_{filename}"
        mime_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        uploaded = upload_to_drive(service, dest_folder_id, filename, resp.content, mime_type)
        return {"status": "success", "type": "dropbox", "file": uploaded}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def handle_mp3(service, url, dest_folder_id, timestamp):
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        filename = url.split("/")[-1].split("?")[0] or "audio.mp3"
        filename = f"{timestamp}_{filename}"
        uploaded = upload_to_drive(service, dest_folder_id, filename, resp.content, "audio/mpeg")
        return {"status": "success", "type": "mp3", "file": uploaded}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def take_screenshot(url, timestamp, label="screenshot"):
    """Use PagePixels API to screenshot a URL and return the image bytes."""
    try:
        api_url = "https://pagepixels.com/app/screenshots"
        params = {
            "access_token": PAGEPIXELS_API_KEY,
            "url": url,
            "full_page": "true",
            "format": "png",
        }
        resp = requests.get(api_url, params=params, timeout=60)
        resp.raise_for_status()
        filename = f"{timestamp}_{label}.png"
        return resp.content, filename
    except Exception as e:
        return None, str(e)

def handle_noteflight(service, url, screenshots_folder_id, timestamp):
    """Screenshot the Noteflight page and save to SCREENSHOTS folder."""
    img_bytes, filename = take_screenshot(url, timestamp, label="noteflight")
    if img_bytes:
        uploaded = upload_to_drive(service, screenshots_folder_id, filename, img_bytes, "image/png")
        return {"status": "success", "type": "noteflight", "file": uploaded, "saved_to": "SCREENSHOTS"}
    else:
        return {"status": "error", "type": "noteflight", "message": filename}

def handle_unknown_with_screenshot(service, url, screenshots_folder_id, timestamp):
    """Screenshot any unknown link and save to SCREENSHOTS folder."""
    label = re.sub(r'https?://', '', url).split("/")[0].replace(".", "_")[:30]
    img_bytes, filename = take_screenshot(url, timestamp, label=label)
    if img_bytes:
        uploaded = upload_to_drive(service, screenshots_folder_id, filename, img_bytes, "image/png")
        return {"status": "success", "type": "screenshot", "file": uploaded, "saved_to": "SCREENSHOTS"}
    else:
        return {"status": "error", "type": "unknown", "message": filename}

def save_link_log(service, dest_folder_id, person, links_results, post_title, timestamp):
    """Save a text log of all processed links into the folder."""
    lines = [
        f"Post: {post_title}",
        f"Person: {person}",
        f"Date: {timestamp}",
        "",
        "Links processed:",
    ]
    for r in links_results:
        lines.append(f"  - [{r.get('type','?')}] {r.get('status','?')} — {r.get('url', r.get('file', {}).get('webViewLink', ''))}")
    content = "\n".join(lines).encode("utf-8")
    upload_to_drive(
        service, dest_folder_id,
        f"{timestamp}_link_log.txt", content, "text/plain"
    )

# ── Main webhook route ───────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    person      = data.get("person", "").strip()       # e.g. "Alex Smith [Exevot]"
    body        = data.get("body", "").strip()         # post content with links
    post_title  = data.get("post_title", "untitled")

    if not person:
        return jsonify({"status": "error", "message": "Missing 'person' field"}), 400

    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M")

    try:
        service = get_drive_service()

        # 1. Find the client's Lifecycle folder
        client_folder_id, client_folder_name = find_folder(
            service, CLIENT_LIFECYCLE_FOLDER_ID, person.split("[")[0].strip()
        )
        if not client_folder_id:
            return jsonify({
                "status": "error",
                "message": f"No lifecycle folder found for '{person}'"
            }), 404

        # 2. Find or create destination folders inside client folder
        community_folder_id = find_or_create_folder(
            service, client_folder_id, COMMUNITY_POSTS_FOLDER_NAME
        )
        screenshots_folder_id = find_or_create_folder(
            service, client_folder_id, SCREENSHOTS_FOLDER_NAME
        )

        # 3. Extract and process all links from the post body
        links = extract_links(body)
        if not links:
            return jsonify({
                "status": "no_links",
                "message": "No links found in post body",
                "person": person
            })

        results = []
        for url in links:
            link_type = detect_link_type(url)
            if link_type == "google_drive":
                result = handle_google_drive(service, url, community_folder_id, timestamp)
            elif link_type == "dropbox":
                result = handle_dropbox(service, url, community_folder_id, timestamp)
            elif link_type == "mp3":
                result = handle_mp3(service, url, community_folder_id, timestamp)
            elif link_type == "noteflight":
                result = handle_noteflight(service, url, screenshots_folder_id, timestamp)
            else:
                result = handle_unknown_with_screenshot(service, url, screenshots_folder_id, timestamp)
            result["url"] = url
            results.append(result)

        # 4. Save a log file of everything
        save_link_log(service, community_folder_id, person, results, post_title, timestamp)

        return jsonify({
            "status": "success",
            "person": person,
            "client_folder": client_folder_name,
            "destination": COMMUNITY_POSTS_FOLDER_NAME,
            "links_processed": len(links),
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
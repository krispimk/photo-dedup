import os
import json
import sqlite3
import base64
import time
import threading
from datetime import datetime
from flask import (
    Flask, render_template, redirect, request,
    session, jsonify, url_for, Response,
)
import requests
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Allow OAuth over HTTP in local dev ──────────────────────────
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
THRESHOLD_SECONDS = int(os.environ.get("THRESHOLD_SECONDS", "10"))
REDIRECT_URI = "http://localhost:5000/auth/callback"
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]
DB_PATH = os.path.join(os.path.dirname(__file__), "photos.db")


# ── Database ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS photos (
                id           TEXT PRIMARY KEY,
                filename     TEXT,
                creation_time TEXT,
                base_url     TEXT,
                product_url  TEXT,
                width        INTEGER,
                height       INTEGER,
                mime_type    TEXT,
                synced_at    INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ct ON photos(creation_time);
        """)


init_db()


# ── Background scan state ───────────────────────────────────────

_scan: dict = {"status": "idle", "fetched": 0, "message": "", "error": None}
_scan_lock = threading.Lock()


# ── Google OAuth helpers ────────────────────────────────────────

def _make_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def _get_credentials():
    token_data = session.get("token")
    if not token_data:
        return None
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            session["token"] = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
            }
        except Exception:
            return None
    return creds


# ── Photo fetching (runs in background thread) ──────────────────

def _fetch_all_photos(token: str) -> None:
    global _scan
    BASE = "https://photoslibrary.googleapis.com/v1/mediaItems"
    headers = {"Authorization": f"Bearer {token}"}
    page_token = None
    fetched = 0

    with get_db() as conn:
        while True:
            params: dict = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = requests.get(BASE, headers=headers, params=params, timeout=30)
            except requests.RequestException as exc:
                with _scan_lock:
                    _scan["status"] = "error"
                    _scan["error"] = str(exc)
                return

            if resp.status_code != 200:
                with _scan_lock:
                    _scan["status"] = "error"
                    _scan["error"] = f"API {resp.status_code}: {resp.text[:300]}"
                return

            data = resp.json()
            items = data.get("mediaItems", [])

            now = int(time.time())
            for item in items:
                meta = item.get("mediaMetadata", {})
                if "photo" not in meta:  # skip videos
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO photos
                       (id, filename, creation_time, base_url, product_url,
                        width, height, mime_type, synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        item["id"],
                        item.get("filename", ""),
                        meta.get("creationTime", ""),
                        item.get("baseUrl", ""),
                        item.get("productUrl", ""),
                        int(meta.get("width", 0) or 0),
                        int(meta.get("height", 0) or 0),
                        item.get("mimeType", "image/jpeg"),
                        now,
                    ),
                )
                fetched += 1

            conn.commit()

            with _scan_lock:
                _scan["fetched"] = fetched
                _scan["message"] = f"Fetched {fetched:,} photos…"

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    with _scan_lock:
        _scan["status"] = "done"
        _scan["fetched"] = fetched
        _scan["message"] = f"Complete — {fetched:,} photos indexed."


# ── Grouping algorithm ──────────────────────────────────────────

def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find_groups(threshold: int = THRESHOLD_SECONDS):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM photos WHERE creation_time != '' ORDER BY creation_time"
        ).fetchall()

    if not rows:
        return []

    photos = [dict(r) for r in rows]
    groups: list[list[dict]] = []
    cur: list[dict] = [photos[0]]

    for photo in photos[1:]:
        try:
            diff = abs(
                (_parse_ts(photo["creation_time"]) - _parse_ts(cur[-1]["creation_time"])).total_seconds()
            )
        except (ValueError, AttributeError):
            diff = float("inf")

        if diff <= threshold:
            cur.append(photo)
        else:
            if len(cur) >= 2:
                groups.append(cur)
            cur = [photo]

    if len(cur) >= 2:
        groups.append(cur)

    return groups


# ── AI analysis ─────────────────────────────────────────────────

def _fresh_base_url(photo_id: str, token: str) -> str:
    """Re-fetch a media item to get a non-expired base URL."""
    resp = requests.get(
        f"https://photoslibrary.googleapis.com/v1/mediaItems/{photo_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json().get("baseUrl", "")
    return ""


def analyze_group(group: list[dict], token: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    images = []
    for photo in group:
        base_url = _fresh_base_url(photo["id"], token)
        if not base_url:
            continue
        url = base_url + "=w900-h900"
        try:
            img_resp = requests.get(url, timeout=30)
            img_resp.raise_for_status()
        except requests.RequestException:
            continue
        images.append(
            {
                "b64": base64.standard_b64encode(img_resp.content).decode(),
                "mime": photo.get("mime_type", "image/jpeg"),
                "id": photo["id"],
                "filename": photo["filename"],
                "product_url": photo["product_url"],
            }
        )

    if len(images) < 2:
        return {"error": "Could not download enough photos for comparison."}

    content: list = []
    for i, img in enumerate(images, 1):
        content.append({"type": "text", "text": f"**Photo {i}** — {img['filename']}"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["mime"],
                    "data": img["b64"],
                },
            }
        )

    n = len(images)
    content.append(
        {
            "type": "text",
            "text": f"""You are a professional photo editor reviewing {n} near-duplicate photos taken within seconds of each other.

For EACH photo (1 through {n}), assess:
- **sharpness**: focus quality, motion blur
- **exposure**: well-exposed / over / under
- **composition**: framing, subject positioning
- **subject**: eyes open, expression, posing
- **score**: integer 1–10

Then state which single photo to KEEP and which to DELETE.

Respond ONLY with valid JSON — no markdown fences, no extra text:
{{
  "assessments": [
    {{"photo": 1, "sharpness": "...", "exposure": "...", "composition": "...", "subject": "...", "score": 8, "notes": "one sentence"}},
    ...
  ],
  "winner": 1,
  "reason": "one sentence explaining why photo N wins",
  "delete": [2, 3]
}}""",
        }
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if model ignores the instruction
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Could not parse AI response.", "raw": raw}

    result["photo_ids"] = [img["id"] for img in images]
    result["photo_filenames"] = [img["filename"] for img in images]
    result["product_urls"] = [img["product_url"] for img in images]
    return result


# ── Routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", authenticated=bool(session.get("token")))


@app.route("/auth/google")
def auth_google():
    flow = _make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    # Newer google-auth-oauthlib adds a PKCE code_verifier automatically.
    # We must carry it to the callback because we re-create the Flow object there.
    session["pkce_verifier"] = getattr(flow, "code_verifier", None)
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    # Validate state ourselves to prevent CSRF
    returned_state = request.args.get("state", "")
    saved_state = session.pop("oauth_state", None)
    if not saved_state or returned_state != saved_state:
        return "State mismatch — possible CSRF. Please try signing in again.", 400

    code = request.args.get("code")
    if not code:
        return "Missing authorization code.", 400

    # Exchange code for tokens directly, including PKCE verifier if present.
    # We bypass requests-oauthlib here to avoid state/verifier round-trip issues.
    payload = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    verifier = session.pop("pkce_verifier", None)
    if verifier:
        payload["code_verifier"] = verifier

    resp = requests.post("https://oauth2.googleapis.com/token", data=payload, timeout=15)
    if resp.status_code != 200:
        return f"Token exchange failed: {resp.text}", 500

    tokens = resp.json()
    session["token"] = {
        "token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
    }
    return redirect(url_for("index"))


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/scan/start", methods=["POST"])
def scan_start():
    global _scan
    if not session.get("token"):
        return jsonify({"error": "Not authenticated"}), 401
    with _scan_lock:
        if _scan["status"] == "running":
            return jsonify({"error": "Scan already running"}), 409
        _scan = {"status": "running", "fetched": 0, "message": "Starting…", "error": None}

    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "Auth expired — please sign in again"}), 401

    threading.Thread(target=_fetch_all_photos, args=(creds.token,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scan/status")
def scan_status():
    with _scan_lock:
        return jsonify(dict(_scan))


@app.route("/api/db/stats")
def db_stats():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        last = conn.execute("SELECT MAX(synced_at) FROM photos").fetchone()[0]
    return jsonify(
        {
            "photo_count": count,
            "last_sync": datetime.fromtimestamp(last).strftime("%b %d %Y, %H:%M")
            if last
            else None,
        }
    )


@app.route("/api/groups")
def api_groups():
    if not session.get("token"):
        return jsonify({"error": "Not authenticated"}), 401
    threshold = int(request.args.get("threshold", THRESHOLD_SECONDS))
    groups = find_groups(threshold)

    summary = []
    for i, group in enumerate(groups):
        try:
            span = round(
                (_parse_ts(group[-1]["creation_time"]) - _parse_ts(group[0]["creation_time"])).total_seconds(),
                1,
            )
        except Exception:
            span = 0
        summary.append(
            {
                "group_id": i,
                "count": len(group),
                "span_seconds": span,
                "taken_at": group[0]["creation_time"],
                "photos": [
                    {
                        "id": p["id"],
                        "filename": p["filename"],
                        "product_url": p["product_url"],
                        "creation_time": p["creation_time"],
                        "width": p["width"],
                        "height": p["height"],
                        "thumb_url": f"/api/thumb/{p['id']}",
                    }
                    for p in group
                ],
            }
        )

    return jsonify({"groups": summary, "total": len(summary), "threshold": threshold})


@app.route("/api/thumb/<photo_id>")
def get_thumb(photo_id: str):
    """Proxy thumbnail through the server to avoid CORS issues."""
    creds = _get_credentials()
    if not creds:
        return "", 401
    base_url = _fresh_base_url(photo_id, creds.token)
    if not base_url:
        return "", 404
    img = requests.get(base_url + "=w400-h400", timeout=30)
    if img.status_code != 200:
        return "", 404
    return Response(img.content, content_type=img.headers.get("Content-Type", "image/jpeg"))


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "Not authenticated"}), 401

    body = request.json or {}
    group_id = body.get("group_id")
    threshold = int(body.get("threshold", THRESHOLD_SECONDS))

    groups = find_groups(threshold)
    if group_id is None or not (0 <= group_id < len(groups)):
        return jsonify({"error": "Invalid group_id"}), 400

    result = analyze_group(groups[group_id], creds.token)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="localhost", debug=True, port=5000)

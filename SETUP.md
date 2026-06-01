# Photo Dedup — Setup Guide

## 1. Google Cloud credentials (5 min)

1. Go to https://console.cloud.google.com/ and create a new project (or pick one).
2. **Enable the API**: APIs & Services → Library → search "Photos Library API" → Enable.
3. **OAuth consent screen**: APIs & Services → OAuth consent screen
   - User type: External
   - App name: anything (e.g. "Photo Dedup")
   - Add your Gmail address as a **Test user** (bottom of the screen)
   - Save
4. **Create credentials**: APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:5000/auth/callback`
   - Click Create — copy the Client ID and Client Secret

## 2. Anthropic API key

Get one at https://console.anthropic.com/ → API Keys → Create key.

## 3. Configure the app

```bash
cp .env.example .env
```

Edit `.env` and fill in:
```
GOOGLE_CLIENT_ID=<paste client id>
GOOGLE_CLIENT_SECRET=<paste client secret>
ANTHROPIC_API_KEY=sk-ant-...
THRESHOLD_SECONDS=10   # seconds between photos to consider a burst
FLASK_SECRET_KEY=any-random-string-here
```

## 4. Run

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

## How it works

| Step | What happens |
|------|-------------|
| **Sync** | Fetches all photo metadata (timestamps, filenames) from Google Photos and caches it locally in `photos.db`. No pixel data is stored. |
| **Group** | Sorts by timestamp and groups consecutive photos that fall within your chosen burst window (default 10 s). |
| **Analyze** | For each group you click "Analyze with AI", the app downloads resized versions, sends them to Claude, and gets per-photo scores (sharpness, exposure, composition, subject quality) plus a keeper recommendation. |
| **Delete** | The Google Photos API is read-only, so deletion links open the photo directly in Google Photos for you to delete manually. |

## Notes

- The sync can take several minutes for large libraries. Progress is shown in real time.
- Base URLs from Google Photos expire after ~60 min; the app always fetches a fresh URL before downloading for AI analysis.
- Re-running "Sync library" refreshes all cached metadata.
- You can adjust the burst window with the slider at any time without re-syncing.

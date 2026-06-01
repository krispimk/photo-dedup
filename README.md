# Photo Dedup

Stop paying for Google Photos storage you don't need. Photo Dedup finds burst-mode near-duplicates in your library and uses Claude AI to pick the sharpest, best-exposed keeper — so you know exactly which shots to delete.

> **Read-only and safe.** The app never deletes anything automatically. It opens photos directly in Google Photos so you stay in control.

---

## How it works

| Step | What happens |
|------|-------------|
| **① Sync** | Fetches all photo metadata (timestamps, filenames) from Google Photos and caches it locally in `photos.db`. No originals are stored. |
| **② Group** | Sorts by timestamp and groups consecutive photos taken within a configurable burst window (default: 10 seconds). |
| **③ Rank** | For any group you choose, Claude downloads resized versions and scores each photo on sharpness, exposure, composition, and subject quality — then recommends one to keep. |
| **④ Delete** | Links open the suggested deletions directly in Google Photos for one-click manual removal. |

---

## Requirements

- Python 3.10+
- A [Google Cloud project](https://console.cloud.google.com/) with the **Photos Library API** enabled
- An [Anthropic API key](https://console.anthropic.com/)

---

## Quick start

```bash
git clone https://github.com/your-username/photo-dedup.git
cd photo-dedup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys (see SETUP.md)
python app.py
```

Open **http://localhost:5000** in your browser.

See [SETUP.md](SETUP.md) for the full Google OAuth setup walkthrough (takes about 5 minutes).

---

## Configuration

All configuration lives in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | — | OAuth client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | — | OAuth client secret |
| `ANTHROPIC_API_KEY` | — | Your Anthropic API key |
| `FLASK_SECRET_KEY` | `dev-secret-change-me` | Random string for session signing |
| `THRESHOLD_SECONDS` | `10` | Max seconds between photos to be grouped as a burst |

The burst window can also be adjusted live with the slider in the UI without re-syncing.

---

## Tech stack

- **Backend:** Python / Flask, SQLite (metadata cache), Google Photos Library API
- **AI:** Claude (`claude-sonnet-4-6`) via the Anthropic SDK — vision + structured JSON output
- **Frontend:** Single-page vanilla JS, Tailwind CSS

---

## Limitations

- **Manual deletion only.** The Google Photos Library API is read-only, so the app cannot delete photos on your behalf. It provides direct links to open each photo in Google Photos.
- **Burst grouping, not perceptual hashing.** Photos are grouped by timestamp proximity, not pixel similarity. Two identical shots taken far apart in time won't be grouped.
- **API costs.** Each "Analyze with AI" click sends images to the Anthropic API. Costs are small (a few cents per group) but not zero.
- **Large libraries take time.** The initial sync can take several minutes. Progress is shown in real time and survives page refreshes.

---

## Contributing

Contributions are welcome! Some areas where help would be valuable:

- **Batch analysis** — analyze all groups at once with a queue and rate limiting
- **Better grouping** — perceptual hashing or embedding-based similarity as an alternative to timestamp-only grouping
- **Export / report** — generate a summary of recommended deletions before acting
- **Storage savings estimate** — calculate how many MB/GB would be freed
- **Tests** — unit tests for the grouping algorithm and API helpers

To contribute:
1. Fork the repo and create a feature branch
2. Make your changes
3. Open a pull request with a clear description of what you changed and why

Please keep PRs focused — one feature or fix per PR makes review much easier.

---

## License

MIT

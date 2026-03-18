# Tracker

A web dashboard that shows per-user RunPod spend using actual billing data. Filters pods by the naming convention `<project>_<user>_<description>` and attributes costs per user — including terminated pods.

## Setup

### Prerequisites

- Python 3.10+
- A RunPod API key
- Postgres (optional locally, required on Railway for persistence)

### Local development

```bash
pip install -r requirements.txt
RUNPOD_API_KEY=your_key_here python app.py
```

Works without Postgres — just won't persist pod metadata for terminated pod attribution.

To run with Postgres locally:
```bash
DATABASE_URL=postgresql://user:pass@localhost/tracker RUNPOD_API_KEY=xxx python app.py
```

Visit `http://localhost:8000`. Filter by user with `/?user=erick`.

## Deploy to Railway

1. Push this repo to GitHub
2. Create a new Railway project from the GitHub repo
3. Add a **Postgres** plugin to the project (one click — Railway injects `DATABASE_URL` automatically)
4. Set environment variables:
   - `RUNPOD_API_KEY` — your RunPod API key
   - `APP_PASSWORD` — password to protect the dashboard
   - `SECRET_KEY` — any random string (keeps sessions alive across deploys)
5. Deploy and visit the generated `.up.railway.app` URL

The first page load creates the database tables and backfills ~31 days of billing history.

## How it works

- Uses the **RunPod billing API** (`/v1/billing/pods`) for actual daily charges — not estimates
- Queries the GraphQL API for current pod status (running/stopped, GPU, uptime)
- **Persists pod metadata to Postgres** so terminated pods retain their name and user attribution
- Parses pod names (`<project>_<user>_<rest>`) to attribute costs per user
- Stores billing records for long-term history beyond the ~31 days the API retains
- Supports browsing prior months via dropdown
- Spend projection chart with power law fit and per-user budget line ($5k/user)
- Auto-refreshes every 5 minutes

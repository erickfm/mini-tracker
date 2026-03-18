# Mini Tracker

A lightweight web dashboard that shows per-user RunPod spend for the current month. Filters pods by the naming convention `<project>-<user>-<id>` and calculates GPU compute + storage costs.

## Setup

### Prerequisites

- Python 3.10+
- A RunPod API key

### Get your RunPod API key

1. Go to [RunPod Settings](https://www.runpod.io/console/user/settings)
2. Scroll to **API Keys**
3. Click **Create API Key** (or copy an existing one)
4. Copy the key — you'll need it as an environment variable

### Local development

```bash
pip install -r requirements.txt
RUNPOD_API_KEY=your_key_here python app.py
```

Visit `http://localhost:8000`. Filter by user with `/?user=erick`.

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [Railway](https://railway.com) and create a new project from the GitHub repo
3. In the Railway project settings, add the environment variable:
   - `RUNPOD_API_KEY` = your RunPod API key
4. Railway auto-detects the Python app and deploys it — no further config needed
5. Visit the generated `.up.railway.app` URL

## How it works

- Queries the RunPod GraphQL API for all pods in the org
- Parses pod names (`<project>-<user>-<id>`) to attribute costs per user
- Calculates GPU compute cost from uptime and hourly rate
- Calculates storage costs (container disk + volume disk) prorated to current date
- Stopped pods use a higher container storage rate ($0.20/GB/mo vs $0.10/GB/mo)
- Auto-refreshes every 5 minutes

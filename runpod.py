import calendar
import datetime
import json
import time
import requests
from collections import defaultdict

GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_BASE = "https://rest.runpod.io/v1"

PODS_QUERY = """
query {
  myself {
    pods {
      id
      name
      desiredStatus
      costPerHr
      gpuCount
      machine {
        gpuDisplayName
      }
      runtime {
        uptimeInSeconds
      }
    }
  }
}
"""

BUDGET_PER_USER = 5000


class RunPodAPIError(Exception):
    pass


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def fetch_pods(api_key):
    """Fetch current pods from the GraphQL API (for live status info)."""
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": PODS_QUERY},
            headers=_headers(api_key),
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RunPodAPIError(f"Failed to reach RunPod API: {e}")

    data = resp.json()
    if "errors" in data:
        raise RunPodAPIError(f"RunPod API error: {data['errors']}")

    try:
        return data["data"]["myself"]["pods"]
    except (KeyError, TypeError):
        raise RunPodAPIError("Unexpected API response structure.")


def fetch_billing(api_key):
    """Fetch pod billing history from the REST API."""
    try:
        resp = requests.get(
            f"{REST_BASE}/billing/pods",
            headers=_headers(api_key),
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RunPodAPIError(f"Failed to reach RunPod billing API: {e}")
    return resp.json()


def parse_pod_user(pod_name):
    """Extract the user segment from a pod name like '<project>_<user>_<rest>'
    or '<project>-<user>-<rest>'.

    Splits on '_' first (dominant convention), falls back to '-'.
    Returns the user string, or None if the name doesn't match.
    """
    if "_" in pod_name:
        parts = pod_name.split("_")
    else:
        parts = pod_name.split("-")
    if len(parts) >= 3:
        return parts[1]
    return None


def _sync_to_db(pods, billing):
    """Persist API data to Postgres. Fails silently if DB unavailable."""
    try:
        from db import DATABASE_URL, upsert_pods, upsert_billing, log_sync
        if not DATABASE_URL:
            return
        t0 = time.time()
        pods_n = upsert_pods(pods, parse_pod_user)
        billing_n = upsert_billing(billing)
        duration = int((time.time() - t0) * 1000)
        log_sync(pods_n, billing_n, duration)
    except Exception as e:
        print(f"DB sync failed: {e}")


def _get_pod_info_from_db():
    """Load all known pods from DB. Returns empty dict if DB unavailable."""
    try:
        from db import DATABASE_URL, get_all_known_pods
        if not DATABASE_URL:
            return {}
        return get_all_known_pods()
    except Exception as e:
        print(f"DB pod lookup failed: {e}")
        return {}


def _get_billing_from_db(year_month):
    """Load billing records for a month from DB."""
    try:
        from db import DATABASE_URL, get_billing_for_month
        if not DATABASE_URL:
            return None
        return get_billing_for_month(year_month)
    except Exception as e:
        print(f"DB billing lookup failed: {e}")
        return None


def _get_available_months_from_db():
    """Get available months from DB."""
    try:
        from db import DATABASE_URL, get_available_months
        if not DATABASE_URL:
            return None
        return get_available_months()
    except Exception as e:
        print(f"DB months lookup failed: {e}")
        return None


def get_spend_report(api_key, user=None, month=None):
    """Build a spend report using actual billing data.

    month: 'YYYY-MM' string, defaults to current month.
    """
    pods = fetch_pods(api_key)
    billing = fetch_billing(api_key)

    # Persist to DB (non-blocking on failure)
    _sync_to_db(pods, billing)

    # Build pod_info: DB first (historical), then overlay live API data
    db_pods = _get_pod_info_from_db()
    pod_info = {}

    # Load historical pods from DB (terminated pods get resolved here)
    for pod_id, info in db_pods.items():
        pod_info[pod_id] = {
            "name": info["name"],
            "user": info["user_name"],
            "status": "Terminated",
            "gpu_name": info["gpu_name"],
            "gpu_count": info["gpu_count"],
            "cost_per_hr": 0,
            "uptime_hours": 0,
        }

    # Overlay live API data (takes precedence for current pods)
    for p in pods:
        pod_info[p["id"]] = {
            "name": p.get("name", ""),
            "user": parse_pod_user(p.get("name", "")),
            "status": "Running" if p.get("desiredStatus") == "RUNNING" else "Stopped",
            "gpu_name": (p.get("machine") or {}).get("gpuDisplayName", "N/A"),
            "gpu_count": p.get("gpuCount", 1),
            "cost_per_hr": p.get("costPerHr") or 0,
            "uptime_hours": round(((p.get("runtime") or {}).get("uptimeInSeconds") or 0) / 3600, 1),
        }

    today = datetime.date.today()
    if month:
        target_year, target_month = int(month[:4]), int(month[5:7])
    else:
        target_year, target_month = today.year, today.month

    target_prefix = f"{target_year}-{target_month:02d}"
    is_current_month = (target_year == today.year and target_month == today.month)
    days_in_month = calendar.monthrange(target_year, target_month)[1]
    days_elapsed = today.day if is_current_month else days_in_month
    month_label = datetime.date(target_year, target_month, 1).strftime("%B %Y")

    # For past months, try DB first (has longer history than API)
    if not is_current_month:
        db_billing = _get_billing_from_db(target_prefix)
        if db_billing is not None:
            billing = db_billing

    # Aggregate billing by pod for the target month
    pod_spend = defaultdict(lambda: {"amount": 0, "time_billed_ms": 0, "disk_billed_gb": 0})
    for record in billing:
        if not record["time"].startswith(target_prefix):
            continue
        pid = record["podId"]
        pod_spend[pid]["amount"] += record.get("amount", 0)
        pod_spend[pid]["time_billed_ms"] += record.get("timeBilledMs", 0)
        pod_spend[pid]["disk_billed_gb"] += record.get("diskSpaceBilledGB", 0)

    # Build enriched pod list
    all_users_set = set()
    enriched = []
    terminated_spend = 0
    terminated_count = 0

    for pid, spend in pod_spend.items():
        info = pod_info.get(pid)
        if info and info.get("user"):
            # Pod has a known name and user (from API or DB)
            pod_user = info["user"]
            all_users_set.add(pod_user)

            if user and pod_user != user:
                continue

            enriched.append({
                "id": pid,
                "name": info["name"],
                "user": pod_user,
                "status": info["status"],
                "gpu_name": info["gpu_name"],
                "gpu_count": info["gpu_count"],
                "cost_per_hr": round(info["cost_per_hr"], 2),
                "uptime_hours": info["uptime_hours"],
                "total_cost": round(spend["amount"], 2),
                "long_running": info["status"] == "Running" and info["uptime_hours"] > 24,
            })
        else:
            # Truly unknown pod (terminated before we started tracking)
            terminated_spend += spend["amount"]
            terminated_count += 1

    # Gather users from current pods not in billing (newly created)
    for pid, info in pod_info.items():
        pod_user = info.get("user")
        if pod_user:
            all_users_set.add(pod_user)

    all_users = sorted(all_users_set)

    # Sort: running first, then stopped, then terminated
    status_order = {"Running": 0, "Stopped": 1, "Terminated": 2}
    running_pods = sorted(
        [p for p in enriched if p["status"] == "Running"],
        key=lambda p: -p["total_cost"],
    )
    stopped_pods = sorted(
        [p for p in enriched if p["status"] != "Running"],
        key=lambda p: (status_order.get(p["status"], 9), -p["total_cost"]),
    )

    total_spend = sum(p["total_cost"] for p in enriched)
    burn_per_hr = sum(p["cost_per_hr"] for p in running_pods)

    # Weekly spend
    weekly_spend = 0
    if is_current_month:
        weekday = today.weekday()
        week_start = today - datetime.timedelta(days=weekday)
        if week_start.month < today.month or week_start.year < today.year:
            week_start = today.replace(day=1)
        week_label = f"{week_start.strftime('%b %d')} – {today.strftime('%b %d')}"

        for record in billing:
            if not record["time"].startswith(target_prefix):
                continue
            record_date = record["time"][:10]
            if record_date >= week_start.isoformat() and record_date <= today.isoformat():
                pid = record["podId"]
                info = pod_info.get(pid)
                if info and info.get("user"):
                    if user and info["user"] != user:
                        continue
                    weekly_spend += record.get("amount", 0)
    else:
        week_label = None

    budget = BUDGET_PER_USER * len(all_users) if not user else BUDGET_PER_USER
    projection = build_projection(total_spend, burn_per_hr, days_elapsed, days_in_month, budget) if is_current_month else None

    # Available months: DB (long history) or API fallback
    db_months = _get_available_months_from_db()
    if db_months:
        available_months = db_months
    else:
        available_months = sorted(set(r["time"][:7] for r in billing), reverse=True)

    return {
        "user": user or "All Users",
        "month_label": month_label,
        "month_value": target_prefix,
        "is_current_month": is_current_month,
        "week_label": week_label,
        "running_pods": running_pods,
        "stopped_pods": stopped_pods,
        "total_spend": round(total_spend, 2),
        "weekly_spend": round(weekly_spend, 2),
        "burn_per_hr": round(burn_per_hr, 2),
        "terminated_spend": round(terminated_spend, 2),
        "terminated_count": terminated_count,
        "all_users": all_users,
        "available_months": available_months,
        "projection": projection,
    }


def build_projection(total_spend, burn_per_hr, days_elapsed, days_in_month, budget):
    """Build chart data for spend projection using a power law fit."""
    if days_elapsed <= 0 or total_spend <= 0 or burn_per_hr <= 0:
        return None

    daily_burn = burn_per_hr * 24
    t = days_elapsed

    b = max(0.5, min(3.0, (daily_burn * t) / total_spend))
    a = total_spend / (t ** b) if t > 0 else 0

    labels = list(range(1, days_in_month + 1))
    projected = [round(a * (d ** b), 2) for d in labels]
    budget_line = [budget] * len(labels)
    eom_projected = round(a * (days_in_month ** b), 2)

    return {
        "labels_json": json.dumps(labels),
        "projected_json": json.dumps(projected),
        "budget_json": json.dumps(budget_line),
        "days_elapsed": days_elapsed,
        "eom_projected": eom_projected,
        "budget": budget,
        "over_budget": eom_projected > budget,
    }

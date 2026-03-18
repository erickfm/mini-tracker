import calendar
import datetime
import requests

RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"

PODS_QUERY = """
query {
  myself {
    pods {
      id
      name
      runtime {
        uptimeInSeconds
        gpus {
          gpuUtilPercent
        }
      }
      desiredStatus
      costPerHr
      gpuCount
      machine {
        gpuDisplayName
      }
      volumeInGb
      containerDiskInGb
    }
  }
}
"""

# Storage rates (per GB per month)
STORAGE_RATE_RUNNING = 0.10
STORAGE_RATE_STOPPED = 0.20
VOLUME_RATE = 0.10


class RunPodAPIError(Exception):
    pass


def fetch_pods(api_key):
    """Fetch all pods from the RunPod GraphQL API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            RUNPOD_GRAPHQL_URL,
            json={"query": PODS_QUERY},
            headers=headers,
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


def parse_pod_user(pod_name):
    """Extract the user segment from a pod name like '<project>-<user>-<rest>'
    or '<project>_<user>_<rest>'.

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


def get_unique_users(pods):
    """Return sorted list of unique user names parsed from pod names."""
    users = set()
    for pod in pods:
        user = parse_pod_user(pod.get("name", ""))
        if user:
            users.add(user)
    return sorted(users)


def calculate_pod_cost(pod, days_elapsed, days_in_month):
    """Calculate costs for a single pod and return an enriched dict."""
    name = pod.get("name", "unnamed")
    user = parse_pod_user(name) or "unknown"
    is_running = pod.get("desiredStatus") == "RUNNING"

    # GPU info
    machine = pod.get("machine") or {}
    gpu_name = machine.get("gpuDisplayName", "N/A")
    gpu_count = pod.get("gpuCount", 1)

    # Uptime
    runtime = pod.get("runtime") or {}
    uptime_seconds = runtime.get("uptimeInSeconds") or 0
    uptime_hours = uptime_seconds / 3600

    # Cost per hour (already reflects gpu count per spec)
    cost_per_hr = pod.get("costPerHr") or 0

    # Compute cost
    compute_cost = uptime_hours * cost_per_hr if is_running else 0

    # Storage costs (prorated)
    prorate = days_elapsed / days_in_month if days_in_month > 0 else 0
    container_gb = pod.get("containerDiskInGb") or 0
    volume_gb = pod.get("volumeInGb") or 0

    container_rate = STORAGE_RATE_RUNNING if is_running else STORAGE_RATE_STOPPED
    container_storage_cost = container_gb * container_rate * prorate
    volume_storage_cost = volume_gb * VOLUME_RATE * prorate
    storage_cost = container_storage_cost + volume_storage_cost

    total_cost = compute_cost + storage_cost

    return {
        "name": name,
        "user": user,
        "status": "Running" if is_running else "Stopped",
        "gpu_name": gpu_name,
        "gpu_count": gpu_count,
        "uptime_hours": round(uptime_hours, 1),
        "cost_per_hr": round(cost_per_hr, 2),
        "compute_cost": round(compute_cost, 2),
        "storage_cost": round(storage_cost, 2),
        "total_cost": round(total_cost, 2),
    }


def _weekly_cost(pod_data, days_in_week, days_in_month):
    """Estimate a pod's cost for the current week (Mon-Sun).

    For running pods: prorate compute by (days_in_week / uptime_days) if
    uptime is longer than the week, otherwise use full compute.
    Storage is prorated to the week days.
    """
    # Storage prorated to this week
    monthly_storage = pod_data["storage_cost"]
    # Reverse the monthly prorate, then re-prorate to the week
    if days_in_month > 0:
        weekly_storage = (monthly_storage / max(pod_data.get("_days_elapsed", 1), 1)) * days_in_week
    else:
        weekly_storage = 0

    # Compute: if uptime_hours fits within the week, use it directly.
    # Otherwise prorate.
    uptime_hours = pod_data["uptime_hours"]
    week_hours = days_in_week * 24
    if uptime_hours <= week_hours:
        weekly_compute = pod_data["compute_cost"]
    else:
        weekly_compute = pod_data["compute_cost"] * (week_hours / uptime_hours) if uptime_hours > 0 else 0

    return round(weekly_compute + weekly_storage, 2)


def get_spend_report(api_key, user=None):
    """Build a full spend report, optionally filtered by user."""
    pods = fetch_pods(api_key)
    all_users = get_unique_users(pods)

    today = datetime.date.today()
    days_elapsed = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_label = today.strftime("%B %Y")

    # Current week: Monday=0 .. Sunday=6
    weekday = today.weekday()  # 0=Mon
    week_start = today - datetime.timedelta(days=weekday)
    # Clamp to start of month
    if week_start.month < today.month or week_start.year < today.year:
        week_start = today.replace(day=1)
    days_in_week = (today - week_start).days + 1  # inclusive of today
    week_label = f"{week_start.strftime('%b %d')} – {today.strftime('%b %d')}"

    enriched = []
    for pod in pods:
        pod_data = calculate_pod_cost(pod, days_elapsed, days_in_month)
        pod_data["_days_elapsed"] = days_elapsed
        if user and pod_data["user"] != user:
            continue
        pod_data["weekly_cost"] = _weekly_cost(pod_data, days_in_week, days_in_month)
        enriched.append(pod_data)

    running_pods = sorted(
        [p for p in enriched if p["status"] == "Running"],
        key=lambda p: -p["total_cost"],
    )
    stopped_pods = sorted(
        [p for p in enriched if p["status"] != "Running"],
        key=lambda p: -p["total_cost"],
    )

    total_compute = sum(p["compute_cost"] for p in enriched)
    total_storage = sum(p["storage_cost"] for p in enriched)
    total_spend = sum(p["total_cost"] for p in enriched)
    burn_per_hr = sum(p["cost_per_hr"] for p in running_pods)
    weekly_spend = sum(p["weekly_cost"] for p in enriched)

    return {
        "user": user or "All Users",
        "month_label": month_label,
        "week_label": week_label,
        "running_pods": running_pods,
        "stopped_pods": stopped_pods,
        "total_compute": round(total_compute, 2),
        "total_storage": round(total_storage, 2),
        "total_spend": round(total_spend, 2),
        "weekly_spend": round(weekly_spend, 2),
        "burn_per_hr": round(burn_per_hr, 2),
        "all_users": all_users,
    }

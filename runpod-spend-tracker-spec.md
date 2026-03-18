# RunPod Per-User Spend Tracker

## Overview

A CLI tool that calculates an individual user's running monthly spend on RunPod by filtering pods based on a naming convention and aggregating their costs via the RunPod API.

## Problem

RunPod's Billing Explorer only shows org-level spend. We need per-user visibility into monthly costs. All pods in the org follow the naming convention `<project>-<user>-<id>` (e.g., `gr-erick-1`), so we can attribute costs by parsing pod names.

## Pod Naming Convention

```
<project>-<user>-<id>
```

Examples:
- `gr-erick-1`
- `ml-erick-2`
- `gr-sarah-1`

The `<user>` segment (second token when splitting on `-`) is the key for filtering.

## Requirements

### Core

1. **Authenticate** with the RunPod GraphQL API using an API key (read from `RUNPOD_API_KEY` env var).
2. **Fetch all pods** for the org (both running and stopped).
3. **Filter pods** to only those belonging to a given user by parsing the pod name (`name.split('-')[1] === user`).
4. **Calculate cost** for each matching pod:
   - GPU compute cost: `runtime_in_seconds × gpu_cost_per_second`
   - Storage cost: container disk + volume disk, using RunPod's per-GB rates
5. **Output a summary** showing:
   - Total spend for the current calendar month
   - Per-pod breakdown (pod name, GPU type, uptime, compute cost, storage cost)
   - Grand total

### CLI Interface

```
runpod-spend --user erick
```

| Flag | Description | Default |
|---|---|---|
| `--user` | Username to filter pods by (required) | — |
| `--month` | Month to report on (YYYY-MM) | current month |
| `--format` | Output format: `table`, `json`, `csv` | `table` |
| `--include-stopped` | Include stopped pods (storage-only costs) | `true` |

### Output Example (table)

```
RunPod Spend Report — erick — March 2026
═══════════════════════════════════════════════════════════════

Pod Name         GPU          Uptime (hrs)   Compute    Storage    Total
─────────────────────────────────────────────────────────────────────────
gr-erick-1       A100 80GB    142.3          $185.99    $12.40     $198.39
ml-erick-2       RTX 4090      38.7           $30.96     $5.00      $35.96
gr-erick-3       (stopped)      —              —         $8.20       $8.20
─────────────────────────────────────────────────────────────────────────
TOTAL                                        $216.95    $25.60     $242.55
```

## RunPod API

- **Endpoint:** `https://api.runpod.io/graphql`
- **Auth:** `Authorization: Bearer <RUNPOD_API_KEY>`

### Key Query — Get All Pods

```graphql
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
```

> Note: Verify the exact schema fields against RunPod's current GraphQL introspection. Fields may have shifted since this was written.

### Cost Calculation Logic

```
gpu_compute_cost = (uptimeInSeconds / 3600) × costPerHr

container_storage_cost = containerDiskInGb × $0.10/GB/month (prorated)
                         — if stopped: containerDiskInGb × $0.20/GB/month (prorated)

volume_storage_cost = volumeInGb × $0.10/GB/month (prorated)

pod_total = gpu_compute_cost + container_storage_cost + volume_storage_cost
```

Prorate storage to the number of days elapsed in the billing month.

## Tech Stack

- **Language:** Python 3.10+
- **HTTP:** `httpx` or `requests`
- **CLI:** `click` or `argparse`
- **Output:** `rich` for table formatting (nice to have, not required)

## Edge Cases to Handle

- Pod name doesn't follow the naming convention (skip it, don't crash)
- Pod has no runtime data yet (newly created)
- API key is missing or invalid (clear error message)
- User has zero matching pods (friendly "no pods found" message)
- Stopped pods with lingering storage costs
- Pods with multiple GPUs (`costPerHr` should already reflect this, but verify)

## Out of Scope

- Historical billing reconciliation against RunPod invoices
- Serverless endpoint costs
- Network volume costs (separate from pod volumes)
- Automated scheduling or cron — this is a manual CLI tool

## Nice to Haves (stretch)

- `--watch` flag that refreshes every N seconds showing live running cost
- Breakdown by project (first segment of pod name)
- Warn if any pod has been running for more than 24 hours

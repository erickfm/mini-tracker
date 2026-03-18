import os
import time
import psycopg2
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pods (
                    pod_id      TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    user_name   TEXT,
                    gpu_name    TEXT,
                    gpu_count   SMALLINT DEFAULT 1,
                    first_seen  TIMESTAMPTZ DEFAULT now(),
                    last_seen   TIMESTAMPTZ DEFAULT now(),
                    last_status TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pods_user_name ON pods (user_name);

                CREATE TABLE IF NOT EXISTS billing_records (
                    id                   BIGSERIAL PRIMARY KEY,
                    pod_id               TEXT NOT NULL REFERENCES pods(pod_id),
                    billing_date         DATE NOT NULL,
                    amount               NUMERIC(10,4) NOT NULL,
                    time_billed_ms       BIGINT DEFAULT 0,
                    disk_space_billed_gb NUMERIC(10,4) DEFAULT 0,
                    raw_time             TEXT NOT NULL,
                    created_at           TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (pod_id, raw_time)
                );

                CREATE INDEX IF NOT EXISTS idx_billing_date ON billing_records (billing_date);
                CREATE INDEX IF NOT EXISTS idx_billing_pod_date ON billing_records (pod_id, billing_date);

                CREATE TABLE IF NOT EXISTS sync_log (
                    id               SERIAL PRIMARY KEY,
                    synced_at        TIMESTAMPTZ DEFAULT now(),
                    pods_upserted    INTEGER DEFAULT 0,
                    records_upserted INTEGER DEFAULT 0,
                    duration_ms      INTEGER
                );
            """)


def upsert_pods(pods, parse_user_fn):
    """Upsert pod metadata from the GraphQL API response.

    parse_user_fn: function that extracts user from pod name (injected to avoid circular import).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            for p in pods:
                name = p.get("name", "")
                user_name = parse_user_fn(name)
                gpu_name = (p.get("machine") or {}).get("gpuDisplayName", "N/A")
                gpu_count = p.get("gpuCount", 1)
                status = "Running" if p.get("desiredStatus") == "RUNNING" else "Stopped"
                cur.execute("""
                    INSERT INTO pods (pod_id, name, user_name, gpu_name, gpu_count, last_status, first_seen, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (pod_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        user_name = EXCLUDED.user_name,
                        gpu_name = EXCLUDED.gpu_name,
                        gpu_count = EXCLUDED.gpu_count,
                        last_status = EXCLUDED.last_status,
                        last_seen = now()
                """, (p["id"], name, user_name, gpu_name, gpu_count, status))
    return len(pods)


def upsert_billing(records):
    """Upsert billing records. Creates stub pod entries for unknown pod IDs."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            for r in records:
                pod_id = r["podId"]
                # Ensure pod exists (stub for unknown/terminated pods)
                cur.execute("""
                    INSERT INTO pods (pod_id, name, user_name, last_status)
                    VALUES (%s, %s, NULL, 'Terminated')
                    ON CONFLICT (pod_id) DO NOTHING
                """, (pod_id, f"unknown-{pod_id[:8]}"))

                billing_date = r["time"][:10]
                cur.execute("""
                    INSERT INTO billing_records (pod_id, billing_date, amount, time_billed_ms, disk_space_billed_gb, raw_time)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (pod_id, raw_time) DO UPDATE SET
                        amount = EXCLUDED.amount,
                        time_billed_ms = EXCLUDED.time_billed_ms,
                        disk_space_billed_gb = EXCLUDED.disk_space_billed_gb
                """, (
                    pod_id, billing_date,
                    r.get("amount", 0), r.get("timeBilledMs", 0),
                    r.get("diskSpaceBilledGB", 0), r["time"],
                ))
    return len(records)


def get_all_known_pods():
    """Return all known pods as {pod_id: {name, user_name, gpu_name, gpu_count, last_status}}."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pod_id, name, user_name, gpu_name, gpu_count, last_status FROM pods")
            rows = cur.fetchall()
    return {
        row[0]: {
            "name": row[1],
            "user_name": row[2],
            "gpu_name": row[3] or "N/A",
            "gpu_count": row[4] or 1,
            "last_status": row[5],
        }
        for row in rows
    }


def get_billing_for_month(year_month):
    """Return billing records for a YYYY-MM month, shaped like the REST API response."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pod_id, billing_date, amount, time_billed_ms, disk_space_billed_gb, raw_time
                FROM billing_records
                WHERE to_char(billing_date, 'YYYY-MM') = %s
            """, (year_month,))
            rows = cur.fetchall()
    return [
        {
            "podId": row[0],
            "time": row[5],
            "amount": float(row[2]),
            "timeBilledMs": row[3],
            "diskSpaceBilledGB": float(row[4]),
        }
        for row in rows
    ]


def get_available_months():
    """Return sorted list of YYYY-MM strings that have billing data, newest first."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT to_char(billing_date, 'YYYY-MM') as ym
                FROM billing_records
                ORDER BY ym DESC
            """)
            return [row[0] for row in cur.fetchall()]


def log_sync(pods_upserted, records_upserted, duration_ms):
    """Record a sync event."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sync_log (pods_upserted, records_upserted, duration_ms)
                VALUES (%s, %s, %s)
            """, (pods_upserted, records_upserted, duration_ms))

import os
import secrets
import threading
import time
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for
from runpod import get_spend_report, fetch_pods, fetch_billing, _sync_to_db, RunPodAPIError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Initialize database if configured
from db import DATABASE_URL, init_db
if DATABASE_URL:
    try:
        init_db()
    except Exception as e:
        print(f"DB init failed (will run without persistence): {e}")


# Background sync thread — runs every 5 minutes regardless of page visits
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", 300))  # seconds

def _background_sync():
    api_key = os.environ.get("RUNPOD_API_KEY")
    while True:
        time.sleep(SYNC_INTERVAL)
        if not api_key or not DATABASE_URL:
            continue
        try:
            pods = fetch_pods(api_key)
            billing = fetch_billing(api_key)
            _sync_to_db(pods, billing)
            print(f"Background sync: {len(pods)} pods, {len(billing)} billing records")
        except Exception as e:
            print(f"Background sync failed: {e}")

if DATABASE_URL and os.environ.get("RUNPOD_API_KEY"):
    sync_thread = threading.Thread(target=_background_sync, daemon=True)
    sync_thread.start()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        app_password = os.environ.get("APP_PASSWORD")
        if app_password and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    app_password = os.environ.get("APP_PASSWORD")
    if not app_password:
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), app_password):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        return render_template("error.html", message="RUNPOD_API_KEY environment variable is not set."), 500

    user = request.args.get("user") or None
    month = request.args.get("month") or None

    try:
        report = get_spend_report(api_key, user=user, month=month)
    except RunPodAPIError as e:
        return render_template("error.html", message=str(e)), 502

    return render_template("dashboard.html", report=report, selected_user=user, selected_month=month)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

import os
import secrets
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for
from runpod import get_spend_report, RunPodAPIError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))


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

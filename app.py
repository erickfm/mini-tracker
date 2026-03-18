import os
from flask import Flask, render_template, request
from runpod import get_spend_report, RunPodAPIError

app = Flask(__name__)


@app.route("/")
def dashboard():
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        return render_template("error.html", message="RUNPOD_API_KEY environment variable is not set."), 500

    user = request.args.get("user") or None
    try:
        report = get_spend_report(api_key, user=user)
    except RunPodAPIError as e:
        return render_template("error.html", message=str(e)), 502

    return render_template("dashboard.html", report=report, selected_user=user)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)

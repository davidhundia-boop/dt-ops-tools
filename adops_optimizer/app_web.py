"""
Campaign Optimization Tool — Web app.
Runs locally (Flask). No deployment. Open http://127.0.0.1:5000 in your browser.
"""

import os
import tempfile
import uuid
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename

from optimizer import run_optimization, col_letter_to_idx

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

# In-memory store for report downloads: download_id -> bytes
_report_store = {}


def _validate_request(files, form):
    """Return (None, None) if valid, else (None, error_message)."""
    if "internal_file" not in files or not files["internal_file"].filename:
        return None, "Please upload the Internal Campaign Data (.xlsx) file."
    if "advertiser_file" not in files or not files["advertiser_file"].filename:
        return None, "Please upload the Advertiser Performance Report (.csv) file."
    d7 = (form.get("d7_col") or "").strip().upper()
    if len(d7) != 1 or not d7.isalpha():
        return None, "ROI D7 Column Letter must be a single letter A–Z."
    d2nd = (form.get("d2nd_col") or "").strip().upper()
    if len(d2nd) != 1 or not d2nd.isalpha():
        return None, "ROI D2nd Column Letter must be a single letter A–Z."
    try:
        kpi_d7 = float(form.get("kpi_d7", 0))
        kpi_d2nd = float(form.get("kpi_d2nd", 0))
        weight_main = float(form.get("weight_main", 80)) / 100.0
        weight_secondary = float(form.get("weight_secondary", 20)) / 100.0
    except (TypeError, ValueError):
        return None, "KPI targets and weights must be numbers."
    if kpi_d7 <= 0:
        return None, "D7 KPI Target must be greater than 0."
    if kpi_d2nd <= 0:
        return None, "D2nd KPI Target must be greater than 0."
    if abs(weight_main + weight_secondary - 1.0) > 0.01:
        weight_main, weight_secondary = 0.80, 0.20
    return (d7, d2nd, kpi_d7, kpi_d2nd, weight_main, weight_secondary), None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    files = request.files
    form = request.form
    validated, err = _validate_request(files, form)
    if err:
        return jsonify({"error": err}), 400
    d7_col, d2nd_col, kpi_d7, kpi_d2nd, weight_main, weight_secondary = validated

    internal_file = files["internal_file"]
    advertiser_file = files["advertiser_file"]
    if not internal_file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Internal file must be .xlsx"}), 400
    if not advertiser_file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Advertiser file must be .csv"}), 400

    try:
        kpi_col_d7_idx = col_letter_to_idx(d7_col)
        kpi_col_d2nd_idx = col_letter_to_idx(d2nd_col)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    internal_path = None
    advertiser_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
            f.write(internal_file.read())
            internal_path = f.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(advertiser_file.read())
            advertiser_path = f.name

        output_bytes, summary = run_optimization(
            internal_file=internal_path,
            advertiser_file=advertiser_path,
            kpi_col_d7_idx=kpi_col_d7_idx,
            kpi_col_d2nd_idx=kpi_col_d2nd_idx,
            kpi_d7_pct=kpi_d7,
            kpi_d2nd_pct=kpi_d2nd,
            weight_main=weight_main,
            weight_secondary=weight_secondary,
        )
        report_bytes = output_bytes.getvalue()
        download_id = str(uuid.uuid4())
        _report_store[download_id] = report_bytes
        # Keep last 10 reports to avoid unbounded growth
        if len(_report_store) > 10:
            for k in list(_report_store.keys())[:-10]:
                del _report_store[k]

        return jsonify({
            "summary": {
                "total_rows": summary.get("total_rows", 0),
                "rows_actioned": summary.get("rows_actioned", 0),
                "rows_disregarded": summary.get("rows_disregarded", 0),
                "rows_with_cap": summary.get("rows_with_cap", 0),
                "roi_d2nd_col": summary.get("roi_d2nd_col", ""),
                "action_breakdown": summary.get("action_breakdown") or {},
                "segment_breakdown": summary.get("segment_breakdown") or {},
            },
            "download_id": download_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if internal_path and os.path.isfile(internal_path):
            try:
                os.unlink(internal_path)
            except OSError:
                pass
        if advertiser_path and os.path.isfile(advertiser_path):
            try:
                os.unlink(advertiser_path)
            except OSError:
                pass


@app.route("/download/<download_id>")
def download(download_id):
    if download_id not in _report_store:
        return "Report not found or expired.", 404
    from io import BytesIO
    buf = BytesIO(_report_store.pop(download_id, b""))
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="optimization_output.xlsx",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

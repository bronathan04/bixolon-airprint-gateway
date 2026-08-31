import ipaddress
import json
import os
import socket
import subprocess
import tempfile

from flask import Flask, flash, redirect, render_template, request, url_for

CONFIG_PATH = "/config/config.json"

QUEUES = {
    "dhl": {
        "cups_name": "bixolon",
        "ppd_path": "/opt/cups/bixolon-xd5-43t-label.ppd",
        "filter_path": "/usr/lib/cups/filter/bixolonzpl",
        "name_field": "dhl_airprint_name",
        "default_name": "Shipping Label Printer",
        "label": "DHL label (autoscale + crop)",
    },
    "plain": {
        "cups_name": "bixolon-plain",
        "ppd_path": "/opt/cups/bixolon-xd5-43t-plain.ppd",
        "filter_path": "/usr/lib/cups/filter/bixolonplain",
        "name_field": "plain_airprint_name",
        "default_name": "Label Printer (Scale to Fit)",
        "label": "Generic (scale whole page to fit)",
    },
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bixolon-airprint-gateway")

DEFAULTS = {"printer_ip": "", "printer_port": 9100}
DEFAULTS.update({q["name_field"]: q["default_name"] for q in QUEUES.values()})


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        merged = dict(DEFAULTS)
        merged.update(cfg)
        return merged
    return dict(DEFAULTS)


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def apply_cups_queue(queue_key, cfg):
    if not cfg["printer_ip"]:
        return
    q = QUEUES[queue_key]
    device_uri = f"socket://{cfg['printer_ip']}:{cfg['printer_port']}"
    subprocess.run(
        [
            "lpadmin", "-p", q["cups_name"],
            "-E",
            "-v", device_uri,
            "-P", q["ppd_path"],
            "-D", cfg[q["name_field"]],
            "-o", "printer-is-shared=true",
        ],
        check=True,
    )
    subprocess.run(["cupsenable", q["cups_name"]], check=False)
    subprocess.run(["cupsaccept", q["cups_name"]], check=False)


def apply_all_queues(cfg):
    for key in QUEUES:
        apply_cups_queue(key, cfg)


# Re-apply saved config on container (re)start so both queues survive restarts.
_startup_cfg = load_config()
if _startup_cfg["printer_ip"]:
    try:
        apply_all_queues(_startup_cfg)
    except Exception:
        pass


def valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    return render_template("index.html", cfg=cfg, queues=QUEUES)


@app.route("/save", methods=["POST"])
def save():
    ip = request.form.get("printer_ip", "").strip()
    port = request.form.get("printer_port", "9100").strip()

    if not valid_ip(ip):
        flash("That doesn't look like a valid IP address.", "error")
        return redirect(url_for("index"))
    try:
        port = int(port)
    except ValueError:
        flash("Port must be a number.", "error")
        return redirect(url_for("index"))

    cfg = {"printer_ip": ip, "printer_port": port}
    for q in QUEUES.values():
        cfg[q["name_field"]] = request.form.get(q["name_field"], "").strip() or q["default_name"]

    save_config(cfg)
    try:
        apply_all_queues(cfg)
    except subprocess.CalledProcessError as e:
        flash(f"Saved, but CUPS setup failed: {e}", "error")
        return redirect(url_for("index"))

    names = " and ".join(f'"{cfg[q["name_field"]]}"' for q in QUEUES.values())
    flash(f"Saved. Both printers should appear in AirPrint as {names} within a minute or two.",
          "success")
    return redirect(url_for("index"))


@app.route("/test-connect", methods=["POST"])
def test_connect():
    cfg = load_config()
    if not cfg["printer_ip"]:
        flash("Set a printer IP first.", "error")
        return redirect(url_for("index"))
    try:
        with socket.create_connection((cfg["printer_ip"], cfg["printer_port"]), timeout=4):
            flash(f"Connected to {cfg['printer_ip']}:{cfg['printer_port']} ✓", "success")
    except OSError as e:
        flash(f"Could not connect to {cfg['printer_ip']}:{cfg['printer_port']}: {e}", "error")
    return redirect(url_for("index"))


@app.route("/test-print", methods=["POST"])
def test_print():
    cfg = load_config()
    if not cfg["printer_ip"]:
        flash("Set a printer IP first.", "error")
        return redirect(url_for("index"))
    # Plain ZPL text fields, not a graphic -- the printer auto-senses ZPL
    # vs SBPL per job, and small text-only ZPL jobs have printed reliably
    # in testing. Only large ^GF graphic fields lock the printer up (see
    # bixolonzpl / _common.py), so this one test label is fine to leave as
    # ZPL rather than guessing at untested native SBPL text syntax.
    zpl = (
        b"^XA"
        b"^PW1200^LL1800"
        b"^FO50,50^GB1100,1700,4^FS"
        b"^FO100,100^A0N,60,60^FDBixolon AirPrint Gateway^FS"
        b"^FO100,200^A0N,40,40^FDTest label - if this printed,^FS"
        b"^FO100,250^A0N,40,40^FDthe network path is working.^FS"
        b"^XZ"
    )
    try:
        with socket.create_connection((cfg["printer_ip"], cfg["printer_port"]), timeout=5) as s:
            s.sendall(zpl)
        flash("Test label sent.", "success")
    except OSError as e:
        flash(f"Failed to send test label: {e}", "error")
    return redirect(url_for("index"))


@app.route("/test-pdf/<queue_key>", methods=["POST"])
def test_pdf(queue_key):
    if queue_key not in QUEUES:
        flash("Unknown queue.", "error")
        return redirect(url_for("index"))
    cfg = load_config()
    if not cfg["printer_ip"]:
        flash("Set a printer IP first.", "error")
        return redirect(url_for("index"))
    upload = request.files.get("pdf")
    if not upload or upload.filename == "":
        flash("Choose a PDF first.", "error")
        return redirect(url_for("index"))

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        upload.save(tmp.name)
        tmp_path = tmp.name

    filter_path = QUEUES[queue_key]["filter_path"]
    try:
        result = subprocess.run(
            [filter_path, "1", "test", "test", "1", "", tmp_path],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            flash(f"Conversion failed: {result.stderr.decode(errors='replace')[:500]}", "error")
            return redirect(url_for("index"))
        with socket.create_connection((cfg["printer_ip"], cfg["printer_port"]), timeout=5) as s:
            s.sendall(result.stdout)
        flash(f"PDF converted ({QUEUES[queue_key]['label']}) and sent to the printer.", "success")
    except subprocess.TimeoutExpired:
        flash("Conversion timed out.", "error")
    except OSError as e:
        flash(f"Converted OK but failed to send to printer: {e}", "error")
    finally:
        os.unlink(tmp_path)

    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

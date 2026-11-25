"""Per-site employee push: the receiving endpoint, and the setup .bat an admin runs on the site's server."""
import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from core import sync
from core.db import db
from core.models import Site
from core.security import superadmin_required

bp = Blueprint("site_sync", __name__)
CLIENT = Path(__file__).parent / "site_client.bat"


def _hash(key):
    # Keys are 256-bit random, so a plain fast hash is enough (no password stretching needed).
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------- receiving end
@bp.post("/api/sites/<int:site_id>/employees")
def push_employees(site_id):
    site = db.session.get(Site, site_id)
    scheme, _, key = request.headers.get("Authorization", "").partition(" ")
    if not (site and site.sync_key_hash and scheme.lower() == "bearer"
            and hmac.compare_digest(site.sync_key_hash, _hash(key.strip()))):
        return jsonify(error="Invalid site or key."), 401
    if not site.is_active:
        return jsonify(error="This site is inactive."), 403
    try:
        records, companies = _parse(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    result = sync.upsert(records, company_map=companies, only_site=site)
    site.last_push_at = datetime.now()
    site.last_push_result = (f"{result['received']} received: {result['inserted']} added, {result['updated']} updated, "
                             f"{result['kept_manual'] + result['kept_staff']} preserved, "
                             f"{result['other_site']} of other sites skipped")
    db.session.commit()
    return jsonify(result)


def _parse(data):
    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list) or not data["feeds"]:
        raise ValueError("Expected JSON with a non-empty 'feeds' list.")
    companies = data.get("companies") or {}
    if not isinstance(companies, dict):
        raise ValueError("'companies' must be an object.")
    records, sources = [], current_app.config["SOURCE_VIEWS"]
    for feed in data["feeds"]:
        if not isinstance(feed, dict) or not isinstance(feed.get("view"), str) or feed["view"] not in sources:
            raise ValueError("Unknown view. Expected one of: " + ", ".join(sources))
        header, rows = feed.get("header"), feed.get("rows")
        if not isinstance(header, list) or not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            raise ValueError("Each feed needs a 'header' list and a 'rows' list of lists.")
        records += sync.normalize(header, rows, sources[feed["view"]])
    # associates first so staff wins when the same ID is in both
    records.sort(key=lambda r: sync.SOURCE_PRIORITY.get(r["source"], 9), reverse=True)
    return records, {str(k): str(v) for k, v in companies.items() if v}


# ---------------------------------------------------------------- client .bat
def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _lan_ip():
    try:  # UDP connect sends nothing, it only makes the OS pick the outgoing interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return ""
    return "" if _loopback(ip) or ip == "0.0.0.0" else ip


def detect_endpoint():
    """Address the admin is using right now; the LAN IP when that is localhost; "" when neither works."""
    url = urlsplit(request.url_root)
    host = url.hostname or ""
    if host and not _loopback(host):
        return request.url_root.rstrip("/")
    host = _lan_ip()
    return f"{url.scheme}://{host}{f':{url.port}' if url.port else ''}{url.path.rstrip('/')}" if host else ""


def clean_endpoint(raw):
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "http://" + raw
    raw = re.sub(r"/api/sites/.*$", "", raw).rstrip("/")  # tolerate a pasted full endpoint
    # Strict on purpose: the value is written into a script.
    if not re.fullmatch(r"https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:\d{1,5})?(/[\w./-]*)?", raw, re.ASCII):
        raise ValueError("Enter the Docket address as an IP or domain, e.g. http://10.0.0.5:5050 or https://docket.example.com")
    if urlsplit(raw).port == 0:  # .port itself raises ValueError above 65535
        raise ValueError("Port out of range.")
    return raw


def build_bat(site, base_url, key):
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", site.code).strip("-") or str(site.id)
    staff, associates = current_app.config["SOURCE_VIEWS"]
    text = CLIENT.read_text(encoding="ascii")
    for token, value in {
        "__SITE__": re.sub(r"[^A-Za-z0-9 .()_-]+", " ", str(site)).strip(),  # free text going into a script
        "__TASK__": f"DocketSync-{slug}", "__BASE_URL__": base_url, "__API_PATH__": f"/api/sites/{site.id}/employees",
        "__KEY__": key, "__STAFF_VIEW__": staff, "__ASSOCIATE_VIEW__": associates,
        "__COLUMNS__": ",".join(sorted({sync._key(n) for names in sync.COLUMNS.values() for n in names})),
    }.items():
        text = text.replace(token, value)
    return f"docket-sync-{slug}.bat", text.replace("\n", "\r\n").encode("ascii")


@bp.route("/admin/sites/<int:site_id>/client", methods=["GET", "POST"])
@superadmin_required
def client(site_id):
    site = db.session.get(Site, site_id) or abort(404)
    if request.method == "POST":
        try:
            base_url = clean_endpoint(request.form.get("endpoint"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("site_sync.client", site_id=site.id))
        key = secrets.token_urlsafe(32)
        filename, content = build_bat(site, base_url, key)
        site.sync_key_hash = _hash(key)  # only the hash is kept; the key exists only inside the download
        db.session.commit()
        return Response(content, mimetype="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})
    return render_template("admin/site_client.html", site=site, endpoint=detect_endpoint())

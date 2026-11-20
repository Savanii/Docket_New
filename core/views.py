from datetime import datetime

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import or_

from core import approvals
from core.access import visible_apps
from core.db import db
from core.models import Employee, Notification
from core.notify import unread
from core.registry import APPS

bp = Blueprint("main", __name__)
OPEN_STATUSES = {"pending", "returned", "approved", "arranged"}  # requests still moving


@bp.get("/")
def home():
    apps = visible_apps(g.user)
    # Red counts = things to act on + unread updates ("approval needed" notes are already counted as tasks).
    badges = {app.code: (app.badge(g.user) if app.badge else 0) + unread(g.user, app.code).filter_by(kind="update").count()
              for app in apps}
    tasks_badge = len(approvals.pending_for(g.user)) + unread(g.user).filter_by(kind="update").count()
    return render_template("home.html", apps=apps, badges=badges, task_count=tasks_badge,
                           banner=current_app.config["BANNER_TEXT"], banner_url=current_app.config["BANNER_URL"])


def _my_requests():
    rows = []
    for app in visible_apps(g.user):
        if app.user_requests:
            rows += [(app, rec) for rec in app.user_requests(g.user)]
    rows.sort(key=lambda r: r[1].updated_at, reverse=True)
    return rows


@bp.get("/tasks")
def tasks():
    """Requests & Tasks: approvals waiting on me, my own requests, and updates."""
    tab = request.args.get("tab", "pending")
    pending = []
    for task in approvals.pending_for(g.user):
        app = APPS.get(task.app_code)
        record = app.approval_record(task.ref_id) if app and app.approval_record else None
        if record is not None:
            pending.append((task, app, record))
    mine = _my_requests()
    status = request.args.get("status", "open")
    shown = [r for r in mine if status == "all" or (r[1].status in OPEN_STATUSES) == (status == "open")]
    unread_refs = {(n.app_code, n.ref_id) for n in unread(g.user)}
    notes = Notification.query.filter_by(employee_id=g.user.employee_id).order_by(Notification.id.desc()).limit(100).all()
    return render_template("tasks.html", tab=tab, pending=pending, mine=shown, status=status,
                           open_count=len([r for r in mine if r[1].status in OPEN_STATUSES]),
                           unread_refs=unread_refs, notes=notes, updates_count=unread(g.user).count())


@bp.get("/notifications/<int:note_id>")
def open_notification(note_id):
    note = db.session.get(Notification, note_id)
    if note is None or note.employee_id != g.user.employee_id:
        abort(404)
    note.read_at = note.read_at or datetime.now()
    db.session.commit()
    return redirect(note.url if note.url.startswith("/") else url_for("main.tasks", tab="updates"))


@bp.post("/notifications/read-all")
def read_all():
    unread(g.user).update({"read_at": datetime.now()})
    db.session.commit()
    return redirect(request.referrer or url_for("main.tasks", tab="updates"))


@bp.get("/api/employees")
def search_employees():
    """Employee picker suggestions."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    query = Employee.query.filter(
        Employee.status == "Active",
        or_(Employee.name.ilike(like), Employee.employee_id.ilike(like),
            Employee.designation.ilike(like), Employee.department.ilike(like)),
    )
    if g.get("site_id") and not request.args.get("all_sites"):
        query = query.filter(Employee.site_id == g.site_id)
    people = query.order_by(Employee.name).limit(15).all()
    return jsonify([p.to_brief() for p in people])


@bp.get("/people/<emp_id>")
def person(emp_id):
    emp = db.session.get(Employee, emp_id) or abort(404)
    reports = Employee.query.filter_by(reporting_manager_id=emp.employee_id, status="Active").order_by(Employee.name).all()
    return render_template("person.html", emp=emp, reports=reports)


@bp.post("/switch-site/<int:site_id>")
def switch_site(site_id):
    from flask import session
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    from core.models import Site
    target = db.session.get(Site, site_id)
    if target and target.is_active:
        session["active_site_id"] = target.id
    ref = request.referrer
    if ref:
        parsed = urlparse(ref)
        qs = parse_qs(parsed.query)
        if "site" in qs:
            qs.pop("site")
            new_query = urlencode(qs, doseq=True)
            ref = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    return redirect(ref or url_for("main.home"))


# ------------------------------------------------------------------ Email Action & Approval API
@bp.route("/approvals/email-action/<token>", methods=["GET", "POST"])
def email_approval_action(token):
    from core.models import ApprovalTask
    from flask import session
    try:
        task, actor, action = approvals.verify_action_token(token)
    except approvals.ApprovalError as exc:
        return render_template("approvals/email_action.html", error=str(exc)), 400

    # Auto-authenticate the approver via valid cryptographic token
    session["emp_id"] = actor.employee_id
    if actor.site_id:
        session["active_site_id"] = actor.site_id
    g.user = actor

    record = approvals._record(task)
    req_no = getattr(record, "req_no", record.approval_title)
    applicant = getattr(record, "applicant", None)
    app_name = applicant.name if applicant else "Employee"
    app_dept = getattr(record, "department", None) or (applicant.department if applicant else "") or "-"
    app_desig = (applicant.designation if applicant else "") or "-"

    base_url = request.host_url.rstrip("/")
    view_url = f"{base_url}{approvals._url(record)}"

    if task.status != "pending":
        return render_template(
            "approvals/email_action.html",
            already_decided=True,
            task=task,
            req_no=req_no,
            view_url=view_url,
        )

    # Requisition info for display
    visit_type = getattr(record, "visit_type", "")
    purpose = getattr(record, "purpose", "")
    guests_list = getattr(record, "guests", [])
    guest_count = len(guests_list) if guests_list else 0
    first_arr = getattr(record, "first_arrival", None)
    last_dep = getattr(record, "last_departure", None)
    arr_str = first_arr.strftime("%d %b %Y %H:%M") if first_arr else "-"
    dep_str = last_dep.strftime("%d %b %Y %H:%M") if last_dep else "-"
    dates_str = f"{arr_str} to {dep_str} ({guest_count} guest{'s' if guest_count != 1 else ''})" if guest_count else ""

    req_items = []
    if getattr(record, "need_guest_house", False): req_items.append("Guest House")
    if getattr(record, "need_hotel", False): req_items.append("Hotel")
    if getattr(record, "need_vehicle", False): req_items.append("Vehicle")
    if getattr(record, "need_food", False): req_items.append("Food")
    if getattr(record, "need_ticket", False): req_items.append("Tickets")
    req_str = ", ".join(req_items) if req_items else ""

    earlier_approved = [t for t in ApprovalTask.query.filter_by(app_code=task.app_code, ref_id=task.ref_id, round=task.round).all()
                        if t.status == "approved" and t.acted_by and t.position < task.position]
    earlier_info = ", ".join(f"{t.label} ({t.acted_by.name})" for t in earlier_approved) if earlier_approved else ""

    if request.method == "POST":
        remarks = request.form.get("remarks", "").strip()
        try:
            outcome = approvals.act(task, actor, action, remarks=remarks)
            if task.app_code == "guest_house":
                from core.models import log_activity
                log_activity("guest_house", record.id, actor, f"Email action: {action.title()} at {task.label}", remarks)
            db.session.commit()
            next_step = ""
            if outcome == "pending":
                next_task = ApprovalTask.query.filter_by(app_code=task.app_code, ref_id=task.ref_id, round=task.round, status="pending").first()
                if next_task:
                    next_step = f"Requisition has moved to: {next_task.label}."
            elif outcome == "approved":
                next_step = "Requisition has been fully approved and forwarded to the Administration Desk."

            return render_template(
                "approvals/email_action.html",
                success=True,
                outcome=outcome,
                req_no=req_no,
                next_step=next_step,
                view_url=view_url,
            )
        except approvals.ApprovalError as exc:
            return render_template("approvals/email_action.html", error=str(exc)), 400

    return render_template(
        "approvals/email_action.html",
        pending=True,
        task=task,
        actor=actor,
        action=action,
        req_no=req_no,
        applicant_name=app_name,
        applicant_dept=app_dept,
        applicant_desig=app_desig,
        visit_type=visit_type,
        purpose=purpose,
        dates_str=dates_str,
        req_str=req_str,
        earlier_info=earlier_info,
        view_url=view_url,
    )


@bp.post("/api/approvals/decide")
def api_approval_decide():
    """
    JSON / Form API for approving or rejecting a requisition.
    Accepts:
    - token: secure token from email
    OR (when logged in):
    - task_id: ID of the approval task
    - action: "approve" or "reject"
    - remarks: optional for approve, required for reject
    """
    from core.models import ApprovalTask
    data = request.get_json(silent=True) or request.form.to_dict()
    token = data.get("token")
    remarks = data.get("remarks", "").strip()
    action = data.get("action")
    task_id = data.get("task_id")

    if token:
        try:
            task, actor, token_action = approvals.verify_action_token(token)
            action = action or token_action
        except approvals.ApprovalError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    else:
        if not g.get("user"):
            return jsonify({"ok": False, "error": "Authentication or token required."}), 401
        actor = g.user
        task = db.session.get(ApprovalTask, task_id)
        if not task:
            return jsonify({"ok": False, "error": "Approval task not found."}), 404

    if not action or action not in ("approve", "reject", "return"):
        return jsonify({"ok": False, "error": "Valid action (approve/reject/return) is required."}), 400

    try:
        outcome = approvals.act(task, actor, action, remarks=remarks)
        record = approvals._record(task)
        if task.app_code == "guest_house":
            from core.models import log_activity
            log_activity("guest_house", record.id, actor, f"API action: {action.title()} at {task.label}", remarks)
        db.session.commit()
        req_no = getattr(record, "req_no", record.approval_title)
        return jsonify({
            "ok": True,
            "outcome": outcome,
            "task_id": task.id,
            "req_no": req_no,
            "message": f"Requisition {req_no} has been {outcome} successfully.",
        })
    except approvals.ApprovalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


"""Employee master import, ported from Docket's sync_service.

Same rules as Docket:
- column names are matched loosely ("EMPLOYEE  NAME", "EMPLOYEE_NAME", "Name" all work)
- emails lower-cased; a blank email from the source never wipes one saved in the portal
- department "IT" is renamed "Information Technology"
- staff records win over associates when the same ID appears in both
Addition: a reporting manager column is picked up when the source has one.
Instead of purging employees missing from the feed, they can be marked Inactive (keeps history intact).
"""
import csv
import io
import re
from datetime import datetime

from core.db import db
from core.models import Employee, Site

COLUMNS = {
    "employee_id": ("EMPLOYEE ID", "EMPID", "EMP ID", "EMP CODE", "EMPLOYEE CODE"),
    "name": ("EMPLOYEE NAME", "NAME"),
    "designation": ("DESIGNATION",),
    "department": ("DEPARTMENT", "DEPT"),
    "email": ("EMAIL ID", "EMAIL", "OFFICIAL EMAIL"),
    "status": ("EMPLOYEE STATUS", "STATUS"),
    "phone": ("CONTACT NO", "MOBILE", "MOBILE NO", "PHONE"),
    "gender": ("GENDER",),
    "category": ("CATEGORY",),
    "company_name": ("COMPANY NAME", "COMPANY", "COMPANY_NAME", "ORGANIZATION", "ORGANISATION", "COMP NAME"),
    "manager_id": ("REPORTING MANAGER ID", "REPORTING MANAGER CODE", "MANAGER ID", "RM ID",
                   "REPORTING MANAGER", "REPORTING TO", "REPORTING OFFICER"),
    "site_code": ("SITE", "SITE CODE", "PLANT", "PLANT CODE", "LOCATION CODE", "COMPANY CODE", "UNIT"),
}
DEPARTMENT_RENAMES = {"IT": "Information Technology"}
SOURCE_PRIORITY = {"manual": -1, "staff": 0, "associates": 1, "upload": 2}


def _key(name):
    return re.sub(r"[\s_.]+", "", str(name or "").upper())


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():  # Excel turns 4050163 into 4050163.0
        value = int(value)
    text = str(value).strip()
    return text or None


def normalize(header, rows, source):
    """Map raw rows from any source to employee dicts."""
    keys = [_key(h) for h in header]
    idx = {}
    for field, names in COLUMNS.items():
        for name in names:
            if _key(name) in keys:
                idx[field] = keys.index(_key(name))
                break
    if "employee_id" not in idx:
        raise ValueError("No employee ID column found. Expected one of: " + ", ".join(COLUMNS["employee_id"]))

    records = {}
    for row in rows:
        get = lambda f: _clean(row[idx[f]]) if f in idx and idx[f] < len(row) else None  # noqa: E731
        emp_id = get("employee_id")
        if not emp_id:
            continue
        dept = get("department")
        manager = get("manager_id")
        if manager:  # "Ravi Kumar (4050163)" -> "4050163"
            found = re.search(r"\(([^()]+)\)\s*$", manager)
            manager = found.group(1).strip() if found else manager
        records[emp_id] = {
            "employee_id": emp_id,
            "name": get("name") or f"Employee {emp_id}",
            "designation": get("designation"),
            "department": DEPARTMENT_RENAMES.get((dept or "").upper(), dept),
            "email": (get("email") or "").lower() or None,
            "status": get("status") or "Active",
            "phone": get("phone"),
            "gender": get("gender"),
            "category": get("category"),
            "manager_id": manager,
            "site_code": get("site_code"),
            "company_name": get("company_name"),
            "source": source,
        }
    return list(records.values())


def fetch_company_mapping(odbc):
    """Map of employee_id -> Company Name from Mantra dbo.view_Transaction_Report."""
    if not odbc:
        return {}
    import pyodbc
    try:
        with pyodbc.connect(odbc, timeout=15) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT [Employee ID], [Company Name] FROM dbo.view_Transaction_Report WHERE [Company Name] IS NOT NULL")
            return {str(r[0]).strip(): str(r[1]).strip() for r in cur.fetchall() if r[0] and r[1]}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to fetch company mapping from Mantra: %s", exc)
        return {}


def resolve_site_id(rec, company_name=None, default_site_id=None, sites_by_code=None, sites_by_name=None):
    """Resolve site_id from explicit site code, company name, or default_site_id."""
    c_name = (company_name or rec.get("company_name") or "").strip()
    s_code = str(rec.get("site_code") or "").strip()

    # 1. Match explicit site_code if given and valid
    if s_code and sites_by_code and s_code in sites_by_code:
        return sites_by_code[s_code]

    # 2. Match company name to known sites
    if c_name:
        c_upper = c_name.upper()
        if ("JAIGARH" in c_upper or "JAIGAD" in c_upper) and sites_by_code and "5110" in sites_by_code:
            return sites_by_code["5110"]
        if "DHARAMTAR" in c_upper and sites_by_code and "5130" in sites_by_code:
            return sites_by_code["5130"]
        if sites_by_name:
            for s_name, sid in sites_by_name.items():
                if s_name.upper() in c_upper or c_upper in s_name.upper():
                    return sid

    # 3. Fallback to default_site_id
    if default_site_id:
        return default_site_id

    return None


def upsert(records, company_map=None, default_site_id=None, deactivate_missing=False, deactivate_unselected=False,
           only_site=None):
    """Insert/update employees. Returns a stats dict. Caller commits.

    only_site: the records were pushed by that site, so they may only create or change its own people.
    """
    now = datetime.now()
    stats = {
        "received": len(records),
        "inserted": 0,
        "updated": 0,
        "kept_staff": 0,
        "kept_manual": 0,
        "managers_linked": 0,
        "deactivated": 0,
        "other_site": 0,
    }
    existing = {e.employee_id: e for e in Employee.query.all()}
    sites_by_code = {s.code: s.id for s in Site.query.all()}
    sites_by_name = {s.name: s.id for s in Site.query.all()}
    company_map = company_map or {}
    # Sites that push their own people: a feed from elsewhere must not move or deactivate those.
    push_sites = {s.id for s in Site.query.filter(Site.last_push_at.isnot(None))}

    if only_site:
        names = {only_site.name: only_site.id, **sites_by_name}  # own name first: wins when a company matches several

        def is_ours(rec):
            emp = existing.get(rec["employee_id"])
            return only_site.id == ((emp.site_id if emp else None) or resolve_site_id(
                rec, company_map.get(rec["employee_id"]), only_site.id, sites_by_code, names))

        records = [r for r in records if is_ours(r)]
        stats["other_site"] = stats["received"] - len(records)

    for rec in records:
        emp_id = rec["employee_id"]
        if emp_id in company_map and not rec.get("company_name"):
            rec["company_name"] = company_map[emp_id]

        emp = existing.get(emp_id)
        if emp is None:
            emp = Employee(employee_id=emp_id, created_at=now)
            db.session.add(emp)
            existing[emp.employee_id] = emp
            stats["inserted"] += 1
        elif emp.is_superadmin or emp.source == "manual" or emp.employee_id == "admin":
            stats["kept_manual"] += 1
            continue  # Admin or manually edited in portal stays as is!
        elif SOURCE_PRIORITY.get(emp.source, 9) < SOURCE_PRIORITY.get(rec["source"], 9):
            stats["kept_staff"] += 1  # e.g. associates feed must not overwrite a staff record
            continue
        else:
            stats["updated"] += 1

        for field in ("name", "status", "phone", "gender", "category", "source", "company_name"):
            val = rec.get(field)
            if val is not None and str(val).strip() != "":
                setattr(emp, field, val)
        if rec["designation"]:
            emp.designation = rec["designation"]
        if rec["department"]:
            emp.department = rec["department"]

        target_site = only_site.id if only_site else resolve_site_id(
            rec,
            company_name=emp.company_name,
            default_site_id=default_site_id,
            sites_by_code=sites_by_code,
            sites_by_name=sites_by_name,
        )
        if target_site and emp.site_id not in push_sites:
            emp.site_id = target_site

        emp.email = rec["email"] or emp.email
        emp.last_synced_at = now

    # Second pass: managers may appear later in the feed than their reports.
    db.session.flush()  # everyone must exist before anyone points at them (foreign key)
    for rec in records:
        emp = existing[rec["employee_id"]]
        if (
            rec["manager_id"]
            and not emp.manager_locked
            and not emp.is_superadmin
            and emp.source != "manual"
            and rec["manager_id"] in existing
            and rec["manager_id"] != emp.employee_id
            and emp.source == rec["source"]
        ):
            emp.reporting_manager_id = rec["manager_id"]
            stats["managers_linked"] += 1

    if deactivate_missing and records:
        seen = {r["employee_id"] for r in records}
        sources = {r["source"] for r in records}
        for emp in existing.values():
            if (emp.source in sources and emp.employee_id not in seen and emp.is_active and not emp.is_superadmin
                    and emp.site_id not in push_sites):
                emp.status = "Inactive"
                stats["deactivated"] += 1

    if deactivate_unselected and records:
        synced_sources = {r["source"] for r in records}
        for emp in existing.values():
            if (
                emp.source not in synced_sources
                and emp.source != "manual"
                and emp.site_id not in push_sites
                and emp.is_active
                and not emp.is_superadmin
                and emp.employee_id != "admin"
            ):
                emp.status = "Inactive"
                stats["deactivated"] += 1

    db.session.flush()
    return stats


def read_file(filename, stream):
    """(header, rows) from an uploaded .xlsx or .csv."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        sheet = load_workbook(stream, read_only=True, data_only=True).active
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    elif filename.lower().endswith(".csv"):
        text = stream.read().decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("Upload an .xlsx or .csv file.")
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    return rows[0], rows[1:]


def fetch_view(odbc, view, active_only=False):
    """(header, rows) from a SQL Server view (Mantra)."""
    import pyodbc  # optional dependency, only needed for live sync
    if not re.fullmatch(r"\w+", view):
        raise ValueError(f"Bad view name {view!r}")
    query = f"SELECT * FROM dbo.{view}" + (" WHERE [Employee Status] = 'Active'" if active_only else "")
    with pyodbc.connect(odbc, timeout=15) as conn:
        cur = conn.cursor()
        cur.execute(query)
        return [c[0].strip() for c in cur.description], [list(r) for r in cur.fetchall()]


def get_sync_config():
    """Retrieve saved sync configuration from AppSetting (app_code='core_sync')."""
    import json
    from core.models import AppSetting
    row = db.session.get(AppSetting, ("core_sync", "config"))
    if row and row.value:
        try:
            return json.loads(row.value)
        except Exception:
            pass
    return {
        "views": ["view_EmployeeMaster_Report_Staff"],
        "active_only": True,
        "deactivate_missing": False,
        "deactivate_unselected": False,
    }


def save_sync_config(views, active_only=True, deactivate_missing=False, deactivate_unselected=False):
    """Save sync configuration to AppSetting so it remains persistent across syncs."""
    import json
    from core.models import AppSetting
    row = db.session.get(AppSetting, ("core_sync", "config")) or AppSetting(app_code="core_sync", key="config")
    row.value = json.dumps({
        "views": list(views),
        "active_only": bool(active_only),
        "deactivate_missing": bool(deactivate_missing),
        "deactivate_unselected": bool(deactivate_unselected),
    })
    db.session.add(row)

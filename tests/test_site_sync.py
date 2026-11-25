"""Site push: the real downloaded .bat, against a fake Mantra, over real HTTP into the real endpoint."""
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from app import create_app
from core import site_sync, sync
from core.db import db
from core.models import Employee, Site

STAFF = "view_EmployeeMaster_Report_Staff"
HEADER = ["EMPLOYEE  ID", "EMPLOYEE NAME", "Department", "Email ID", "Employee Status", "Reporting Manager", "AADHAAR"]


@pytest.fixture()
def app(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}", "CSRF_ENABLED": True, "TESTING": True})
    with app.app_context():
        db.create_all()
        db.session.add_all([
            Site(id=1, code="A1", name="Alpha Port"), Site(id=2, code="B/2 x", name="Beta Port"),
            Employee(employee_id="admin", name="Admin", is_superadmin=True, source="manual"),
            Employee(employee_id="plain", name="Plain", site_id=1, source="manual"),
            Employee(employee_id="B9", name="Beta Boss", email="boss@x.in", site_id=2, source="staff"),
        ])
        db.session.commit()
    return app


def login(app, emp_id="admin"):
    c = app.test_client()
    with c.session_transaction() as s:
        s["emp_id"], s["_csrf"] = emp_id, "t"
    return c


def download(app, site_id=1, endpoint="http://10.1.2.3:5050"):
    r = login(app).post(f"/admin/sites/{site_id}/client", data={"_csrf": "t", "endpoint": endpoint})
    return r.data.decode("ascii") if r.mimetype == "application/octet-stream" else None


def key_of(bat):
    return re.search(r"^\$Key +?= '([\w-]{40,})'", bat, re.M)[1]


def post(app, key, rows, site_id=1, **extra):
    return app.test_client().post(f"/api/sites/{site_id}/employees", headers={"Authorization": f"Bearer {key}"},
                                  json={"feeds": [{"view": STAFF, "header": HEADER, "rows": rows}], **extra})


def row(emp_id, name="N", email="", status="Active", manager=""):
    return [emp_id, name, "IT", email, status, manager, "secret"]


def emp(app, emp_id):
    with app.app_context():
        return db.session.get(Employee, emp_id)


def test_bat_contents_and_input_checks(app):
    with app.app_context():
        db.session.get(Site, 2).name = "Beta'; calc \u2019 %PATH% #> & Port"
        db.session.commit()
    bat = download(app, site_id=2)
    assert "'http://10.1.2.3:5050'" in bat and "'/api/sites/2/employees'" in bat and "'DocketSync-B-2-x'" in bat
    assert not re.search(r"__[A-Z_]+__", bat) and "\r\n" in bat
    assert "$SiteName = 'Beta calc PATH Port (B 2 x)'" in re.sub(" +", " ", bat)  # a site name can't break out of the script
    with app.app_context():
        assert db.session.get(Site, 2).sync_key_hash == site_sync._hash(key_of(bat))  # plain key is not stored
    for bad in ("", "ftp://x", "http://x\nevil", "http://x'; calc", "http://u:p@x", "http://x:99999"):
        assert download(app, endpoint=bad) is None
    assert site_sync.clean_endpoint("docket.example.com:81/api/sites/4/employees") == "http://docket.example.com:81"
    assert login(app, "plain").get("/admin/sites/1/client").status_code == 403
    assert app.test_client().get("/admin/sites/1/client").status_code == 302


def test_create_site_leads_to_client_page_with_detected_address(app):
    c = login(app)
    r = c.post("/admin/sites", data={"_csrf": "t", "action": "create", "code": "C3", "name": "Gamma"})
    assert r.headers["Location"].endswith("/admin/sites/3/client")
    assert c.get("/admin/sites/3/client").status_code == 200
    with app.test_request_context(base_url="https://docket.example.com:8443"):
        assert site_sync.detect_endpoint() == "https://docket.example.com:8443"
    with app.test_request_context(base_url="http://127.0.0.1:5050"):
        assert "127.0.0.1" not in site_sync.detect_endpoint()
    assert c.get("/admin/sites").status_code == 200 and c.get("/admin/sync").status_code == 200


def test_key_is_required_and_rotates(app):
    key = key_of(download(app))
    assert post(app, key, [row("1")]).status_code == 200
    assert post(app, "wrong", [row("1")]).status_code == 401
    assert post(app, key, [row("1")], site_id=2).status_code == 401  # another site's endpoint
    assert app.test_client().post("/api/sites/1/employees", json={}).status_code == 401
    new = key_of(download(app))
    assert post(app, key, [row("1")]).status_code == 401 and post(app, new, [row("1")]).status_code == 200
    for body in ([], {}, {"feeds": [1]}, {"feeds": [{"view": "x"}]}, {"feeds": [{"view": STAFF, "header": ["x"], "rows": []}]}):
        r = app.test_client().post("/api/sites/1/employees", headers={"Authorization": f"Bearer {new}"}, json=body)
        assert r.status_code == 400
    with app.app_context():
        db.session.get(Site, 1).is_active = False
        db.session.commit()
    assert post(app, new, [row("1")]).status_code == 403


def test_upsert_stays_inside_own_site(app):
    key = key_of(download(app))
    r = post(app, key, [row("A1", "Asha", "asha@x.in", manager="A2"), row("A2", "Ravi"),
                        row("B9", "Hijack", "evil@x.in", manager="A2"), row("V1", "Visitor"), row("admin", "Hijack")],
             companies={"V1": "Beta Port Ltd"}).get_json()
    assert (r["inserted"], r["other_site"], r["kept_manual"]) == (2, 2, 1)
    assert emp(app, "A1").site_id == 1 and emp(app, "A1").reporting_manager_id == "A2"
    boss = emp(app, "B9")
    assert (boss.name, boss.email, boss.reporting_manager_id, boss.site_id) == ("Beta Boss", "boss@x.in", None, 2)
    assert emp(app, "V1") is None and emp(app, "admin").name == "Admin"

    # again: updates in place, blank email keeps the saved one, leavers go inactive
    r = post(app, key, [row("A1", "Asha K"), row("A2", "Ravi", status="Inactive")]).get_json()
    assert (r["inserted"], r["updated"]) == (0, 2)
    assert emp(app, "A1").name == "Asha K" and emp(app, "A1").email == "asha@x.in" and emp(app, "A2").status == "Inactive"

    # a pull from some other database must not deactivate or move people of a site that pushes
    with app.app_context():
        sync.upsert(sync.normalize(HEADER, [row("Z1"), row("A1", "Asha K")], "staff"), default_site_id=2,
                    deactivate_missing=True, deactivate_unselected=True)
        db.session.commit()
    assert emp(app, "A1").site_id == 1 and emp(app, "Z1").site_id == 2
    post(app, key, [row("A3")])
    with app.app_context():
        sync.upsert(sync.normalize(HEADER, [row("Z1")], "staff"), deactivate_missing=True)
        db.session.commit()
    assert emp(app, "A3").is_active and not emp(app, "B9").is_active  # B9's site never pushed: old behaviour


@pytest.mark.skipif(sys.platform != "win32", reason="the client is a Windows batch file")
def test_downloaded_client_end_to_end(app, tmp_path):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bat = tmp_path / "docket sync & co.bat"
        bat.write_bytes(download(app, endpoint=f"http://127.0.0.1:{server.port}").encode("ascii"))
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(Path(__file__).with_name("site_client_check.ps1")), str(bat)],
                             capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert "ALL OK" in out.stdout, out.stdout + out.stderr
        e = emp(app, "4050163")
        assert (e.name, e.email, e.site_id) == ("Asha \"O'Neil\" \u0906", "asha@x.in", 1)
        assert emp(app, "4050164").email is None

        # the batch half really hands over to the PowerShell half ("lib" loads the functions and stops)
        out = subprocess.run(f'cmd /s /c ""{bat}" lib"', capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert out.returncode == 0 and not (out.stdout + out.stderr).strip(), out.stdout + out.stderr
    finally:
        server.shutdown()

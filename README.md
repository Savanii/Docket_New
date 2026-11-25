# Docket (new)

Flask + SQLAlchemy + Alembic + SQLite. A platform with the employee master and hierarchy, plus apps that are written as code in `modules/`.

## Run
```
pip install -r requirements.txt
copy .env.example .env          # set SECRET_KEY; OTP_SHOW_ON_SCREEN=true for local dev
alembic upgrade head
flask --app app seed-demo       # optional demo data; admin / admin123
flask --app app create-admin    # real super admin
python app.py                   # listens on APP_HOST:APP_PORT from .env
pytest -q
```

## Layout
- `core/` is the platform: employees and sync, app access grants, app settings, the approval engine (one approval org chart per app), notifications, and the activity log.
- `modules/<app>/` holds one app with its own `models.py`, `views.py` and `templates/<app>/`. `__init__.py` exposes `APP = AppDef(...)`, which declares the app's extra permissions, settings, approval conditions and hooks. The app is mounted automatically at `/apps/<code>`.
- `migrations/` is a single Alembic history for everything. After adding models: `alembic revision --autogenerate -m "..."`, then `alembic upgrade head`.

## Employees from each site's Mantra
Administration → Sites → Sync client downloads one `.bat` for that site, with its key and the Docket address inside. Run it on the site server: it asks for the Mantra details and push times, tests one push, and creates a Task Scheduler job. Nothing else needs installing. The client posts to `POST /api/sites/<id>/employees` (`Authorization: Bearer <key>`), which upserts by employee ID. A key can only create or change its own site's people. To move a person to another site, use bulk assign on the Employees page. The client source is `core/site_client.bat`.

## Adding an app
Copy `modules/guest_house`, then change the code, models and views. Add an icon branch in `core/templates/_icons.html` and generate a migration. Then configure the app in Administration → Applications: who can use it, the app admins, master managers, report viewers, the approval chain and the settings.

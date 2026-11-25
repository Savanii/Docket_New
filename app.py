from pathlib import Path

from flask import Flask, redirect, url_for

from config import Config
from core import admin, auth, cli, registry, security, site_sync, views
from core.db import db


def create_app(overrides=None):
    app = Flask(__name__, template_folder="core/templates", static_folder="core/static")
    app.config.from_object(Config)
    app.config.update(overrides or {})
    app.jinja_env.auto_reload = True
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    Path(app.instance_path).mkdir(exist_ok=True)

    db.init_app(app)
    security.init_app(app)
    for bp in (auth.bp, views.bp, admin.bp, site_sync.bp):
        app.register_blueprint(bp)
    app.add_url_rule("/favicon.ico", "favicon", lambda: redirect(url_for("static", filename="img/logo.svg")))
    registry.discover(app)
    cli.init_app(app)
    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host=app.config["APP_HOST"], port=app.config["APP_PORT"], debug=True)

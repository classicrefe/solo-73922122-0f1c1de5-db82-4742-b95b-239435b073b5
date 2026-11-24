"""录音分账核定应用包。"""
from __future__ import annotations

import os

from flask import Flask

from . import db, web


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        DATABASE=os.environ.get("APP_DB", os.path.join(app.instance_path, "settlement.db")),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    app.teardown_appcontext(db.close_db)
    app.register_blueprint(web.bp)

    @app.template_filter("versions_of")
    def versions_of(recording_id: int):
        return db.list_versions(recording_id)

    @app.cli.command("init-db")
    def init_db_command() -> None:
        db.init_db()
        print("database initialized:", app.config["DATABASE"])

    @app.cli.command("seed")
    def seed_command() -> None:
        from .seed import seed_demo_data

        db.init_db()
        seed_demo_data()
        print("demo data seeded")

    return app

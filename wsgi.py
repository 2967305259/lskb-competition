"""Prepared, read-only application loading for Gunicorn workers."""
from app import create_app, db
from migrations import assert_schema_current

app = create_app()
with app.app_context():
    assert_schema_current(db.engine)

"""Prepared, read-only application loading for Gunicorn workers."""
import os

from app import create_app, db
from migrations import assert_schema_current

app = create_app(os.environ.get('FLASK_ENV', 'production'))
with app.app_context():
    assert_schema_current(db.engine)

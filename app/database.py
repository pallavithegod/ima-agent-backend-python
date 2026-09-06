"""Compatibility shim: the sqlite3 implementation moved to SQLAlchemy in
app.db.repo. Existing imports (agent, remediation, main, tests) keep working."""

from .db.repo import (  # noqa: F401
    create_deployment,
    create_incident,
    get_deployment,
    get_incident,
    get_incident_by_deployment,
    init_database,
    list_deployments,
    list_incidents,
    update_incident,
)

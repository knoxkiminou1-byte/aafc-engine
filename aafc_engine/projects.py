"""Client project records for the AAFC business engine.

A project groups the commercial work for a client (proposal, active delivery,
maintenance). Projects optionally link to a website and/or an audit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import Store
from .websites import WebsiteError, get_website

PROJECT_STAGES = ["PROSPECT", "PROPOSAL", "ACTIVE", "DELIVERED", "MAINTENANCE", "CLOSED"]


def _now() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _load_projects(store: Store, client_id: str) -> list[dict[str, Any]]:
    """Read the client's project list (empty list when none exist yet)."""
    return store.read_json(client_id, "projects.json", default=[])


def _save_projects(store: Store, client_id: str, projects: list[dict[str, Any]]) -> None:
    """Atomically persist the client's project list."""
    store.write_json(client_id, projects, "projects.json")


def create_project(
    store: Store,
    client_id: str,
    name: str,
    website_id: str | None = None,
    audit_id: str | None = None,
    stage: str = "PROPOSAL",
) -> dict[str, Any]:
    """Create a project for a client.

    The stage must be one of PROJECT_STAGES. Raises ValueError on an invalid
    stage or a blank name, and KeyError when the client does not exist.

    ``website_id`` and ``audit_id``, when given, are validated: they must
    exist *and belong to this client* -- dangling or cross-client references
    are refused so every project stays traceable to its own customer.
    """
    if stage not in PROJECT_STAGES:
        raise ValueError(f"invalid project stage: {stage!r}; must be one of {PROJECT_STAGES}")
    if not store.client_exists(client_id):
        raise KeyError(f"client not found: {client_id}")
    if not name or not name.strip():
        raise ValueError("project name is required")
    if website_id is not None:
        try:
            get_website(store, client_id, website_id)
        except WebsiteError as exc:
            raise ValueError(
                f"website {website_id!r} does not exist for client {client_id!r}; "
                "refusing a dangling project reference"
            ) from exc
    if audit_id is not None:
        audit = store.read_json(client_id, "audits", f"{audit_id}.json", default=None)
        if not isinstance(audit, dict):
            raise ValueError(
                f"audit {audit_id!r} does not exist for client {client_id!r}; "
                "refusing a dangling project reference"
            )
    now = _now()
    project = {
        "id": store.new_id("prj_"),
        "client_id": client_id,
        "name": name.strip(),
        "website_id": website_id,
        "audit_id": audit_id,
        "stage": stage,
        "created_at": now,
        "updated_at": now,
    }
    store.append_json_list(client_id, project, "projects.json")
    return project


def get_project(store: Store, client_id: str, project_id: str) -> dict[str, Any]:
    """Return one project by id. Raises KeyError when it does not exist."""
    for project in _load_projects(store, client_id):
        if project.get("id") == project_id:
            return project
    raise KeyError(f"project not found: {project_id}")


def list_projects(store: Store, client_id: str) -> list[dict[str, Any]]:
    """Return all projects for a client, oldest first."""
    return _load_projects(store, client_id)


def set_project_stage(
    store: Store, client_id: str, project_id: str, stage: str
) -> dict[str, Any]:
    """Move a project to a new stage.

    The stage must be one of PROJECT_STAGES, else ValueError. Raises KeyError
    when the project does not exist.
    """
    if stage not in PROJECT_STAGES:
        raise ValueError(f"invalid project stage: {stage!r}; must be one of {PROJECT_STAGES}")
    projects = _load_projects(store, client_id)
    for project in projects:
        if project.get("id") == project_id:
            project["stage"] = stage
            project["updated_at"] = _now()
            _save_projects(store, client_id, projects)
            return project
    raise KeyError(f"project not found: {project_id}")

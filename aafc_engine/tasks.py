"""Work tasks for the AAFC business engine.

Tasks track individual units of work. A task may belong to a project and may
reference the audit finding it implements a fix for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import Store

TASK_STATES = ["TODO", "IN_PROGRESS", "DONE", "BLOCKED"]


def _now() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _load_tasks(store: Store, client_id: str) -> list[dict[str, Any]]:
    """Read the client's task list (empty list when none exist yet)."""
    return store.read_json(client_id, "tasks.json", default=[])


def _save_tasks(store: Store, client_id: str, tasks: list[dict[str, Any]]) -> None:
    """Atomically persist the client's task list."""
    store.write_json(client_id, tasks, "tasks.json")


def create_task(
    store: Store,
    client_id: str,
    project_id: str | None,
    title: str,
    finding_id: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Create a task for a client.

    project_id may be None (standalone task). When given, the project must
    exist, else ValueError. Raises ValueError on a blank title.
    """
    if not title or not str(title).strip():
        raise ValueError("task title is required")
    if project_id is not None:
        from aafc_engine.projects import get_project

        try:
            get_project(store, client_id, project_id)
        except KeyError as exc:
            raise ValueError(f"project not found: {project_id}") from exc
    task = {
        "id": store.new_id("tsk_"),
        "client_id": client_id,
        "project_id": project_id,
        "finding_id": finding_id,
        "title": str(title).strip(),
        "notes": notes or "",
        "status": "TODO",
        "created_at": _now(),
        "completed_at": None,
    }
    store.append_json_list(client_id, task, "tasks.json")
    return task


def get_task(store: Store, client_id: str, task_id: str) -> dict[str, Any]:
    """Return one task by id. Raises KeyError when it does not exist."""
    for task in _load_tasks(store, client_id):
        if task.get("id") == task_id:
            return task
    raise KeyError(f"task not found: {task_id}")


def list_tasks(
    store: Store, client_id: str, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Return tasks for a client, oldest first; optionally filtered by project."""
    tasks = _load_tasks(store, client_id)
    if project_id is not None:
        tasks = [t for t in tasks if t.get("project_id") == project_id]
    return tasks


def set_task_status(
    store: Store, client_id: str, task_id: str, status: str
) -> dict[str, Any]:
    """Set a task's status.

    Status must be one of TASK_STATES, else ValueError. Moving to DONE stamps
    completed_at; moving away from DONE clears it. Raises KeyError when the
    task does not exist.
    """
    if status not in TASK_STATES:
        raise ValueError(f"invalid task status: {status!r}; must be one of {TASK_STATES}")
    tasks = _load_tasks(store, client_id)
    for task in tasks:
        if task.get("id") == task_id:
            task["status"] = status
            if status == "DONE" and not task.get("completed_at"):
                task["completed_at"] = _now()
            elif status != "DONE":
                task["completed_at"] = None
            _save_tasks(store, client_id, tasks)
            return task
    raise KeyError(f"task not found: {task_id}")


def complete_task(
    store: Store, client_id: str, task_id: str, note: str = ""
) -> dict[str, Any]:
    """Mark a task DONE, stamping completed_at.

    An optional note is appended to the task's notes. Raises KeyError when the
    task does not exist.
    """
    tasks = _load_tasks(store, client_id)
    for task in tasks:
        if task.get("id") == task_id:
            task["status"] = "DONE"
            task["completed_at"] = _now()
            if note and note.strip():
                existing = (task.get("notes") or "").strip()
                task["notes"] = f"{existing}\n{note.strip()}".strip()
            _save_tasks(store, client_id, tasks)
            return task
    raise KeyError(f"task not found: {task_id}")

"""Client pipeline: ordered stages with controlled advancement.

Movement rules: forward exactly one stage at a time, or backward to any
earlier stage (rework). Skipping forward is rejected. Advancing never
touches the client record_type (no automatic lead -> client promotion).
"""

from __future__ import annotations

from typing import Any

from .store import Store, utc_now_iso

STAGES = [
    "LEAD",
    "INTAKE",
    "AUDIT",
    "REPORT",
    "CONVERSATION",
    "PROPOSAL",
    "PROJECT",
    "IMPLEMENTATION",
    "RE-AUDIT",
    "VERIFIED",
    "MAINTENANCE",
]

_PIPELINE_FILE = "pipeline.json"


class PipelineError(ValueError):
    """Raised for invalid stages or illegal stage transitions."""


def get_pipeline(store: Store, client_id: str) -> dict:
    """Get a client's pipeline record, creating it at LEAD on first call.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        The pipeline dict: ``{client_id, stage, history}``.
    """
    pipeline = store.read_json(client_id, _PIPELINE_FILE)
    if pipeline is None:
        pipeline = {
            "client_id": client_id,
            "stage": "LEAD",
            "history": [],
        }
        store.write_json(client_id, pipeline, _PIPELINE_FILE)
    return pipeline


def advance(store: Store, client_id: str, to_stage: str, note: str = "") -> dict:
    """Advance a client's pipeline to a new stage.

    Args:
        store: The Store.
        client_id: The client identifier.
        to_stage: Target stage; must be in STAGES.
        note: Optional note recorded in history.

    Returns:
        The updated pipeline dict.

    Raises:
        PipelineError: If ``to_stage`` is unknown, is the current stage,
            or skips forward more than one stage.
    """
    if to_stage not in STAGES:
        raise PipelineError(
            f"unknown stage {to_stage!r}; expected one of {STAGES}"
        )
    pipeline = get_pipeline(store, client_id)
    current = pipeline.get("stage", "LEAD")
    cur_idx = STAGES.index(current)
    new_idx = STAGES.index(to_stage)

    if new_idx == cur_idx:
        raise PipelineError(f"already at stage {current!r}")
    if new_idx > cur_idx + 1:
        raise PipelineError(
            f"cannot skip stages: {current!r} -> {to_stage!r} "
            f"(next allowed stage is {STAGES[cur_idx + 1]!r})"
        )
    # new_idx == cur_idx + 1: forward one stage. new_idx < cur_idx: rework.

    entry: dict[str, Any] = {
        "from": current,
        "to": to_stage,
        "at": utc_now_iso(),
        "note": note,
    }
    pipeline["history"].append(entry)
    pipeline["stage"] = to_stage
    # Deliberately never touches the client record_type.
    store.write_json(client_id, pipeline, _PIPELINE_FILE)
    return pipeline

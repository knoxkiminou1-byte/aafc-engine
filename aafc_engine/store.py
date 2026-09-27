"""Storage primitives for the AAFC business engine.

Per-client isolation: everything lives under ``data/clients/<client_id>/``.
All writes are atomic (temp file in the same directory + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """Base error for store failures."""


#: Client identifiers are always ``cl_`` + 12 hex chars (see ``new_id``).
#: The store rejects anything else at the trust boundary so a hostile or
#: mistyped ``client_id`` can never escape ``<root>/clients/``.
_CLIENT_ID_RE = re.compile(r"^cl_[0-9a-f]{12}$")


def _validate_client_id(client_id: str) -> None:
    """Reject anything that is not a bare ``cl_<hex>`` client identifier."""
    if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id):
        raise StoreError(f"invalid client id: {client_id!r}")


def _validate_parts(parts: tuple[str, ...]) -> None:
    """Reject path parts that could escape the client directory."""
    for part in parts:
        if (
            not isinstance(part, str)
            or not part
            or part in (".", "..")
            or "/" in part
            or "\\" in part
            or part.startswith("~")
        ):
            raise StoreError(f"invalid path part: {part!r}")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        ISO-8601 timestamp in UTC (e.g. ``2026-09-26T22:01:00+00:00``).
    """
    return datetime.now(timezone.utc).isoformat()


class Store:
    """JSON-file store rooted at a data directory.

    Args:
        root: Store root directory. Defaults to the ``AAFC_DATA_DIR``
            environment variable, else ``<repo>/data``.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            env_root = os.environ.get("AAFC_DATA_DIR")
            if env_root:
                root = env_root
            else:
                root = Path(__file__).resolve().parent.parent / "data"
        self.root = Path(root)

    def client_dir(self, client_id: str) -> Path:
        """Return the client's directory, creating it (and parents) if needed.

        Args:
            client_id: The client identifier (e.g. ``cl_a1b2c3d4e5f6``).

        Returns:
            Path to ``<root>/clients/<client_id>``.

        Raises:
            StoreError: If ``client_id`` is not a valid client identifier.
        """
        _validate_client_id(client_id)
        directory = self.root / "clients" / client_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _read_path(self, client_id: str, parts: tuple[str, ...]) -> Path:
        """Path for a read; does not create directories."""
        _validate_client_id(client_id)
        _validate_parts(parts)
        return self.root.joinpath("clients", client_id, *parts)

    def read_json(
        self, client_id: str, *parts: str, default: Any = None
    ) -> Any:
        """Read a JSON document from a client's directory.

        Args:
            client_id: The client identifier.
            *parts: Path parts under the client dir (e.g. ``"audits"``,
                ``f"{audit_id}.json"``).
            default: Returned when the file does not exist or is not valid JSON.

        Returns:
            The parsed JSON document, or ``default``.
        """
        path = self._read_path(client_id, parts)
        if not path.is_file():
            return default
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            return default

    def write_json(self, client_id: str, obj: Any, *parts: str) -> None:
        """Write a JSON document atomically.

        Writes to a temp file in the same directory, then ``os.replace``s it
        into place so a crash can never leave a half-written file.

        Args:
            client_id: The client identifier.
            obj: JSON-serializable object to write.
            *parts: Path parts under the client dir.

        Raises:
            StoreError: If the write fails.
        """
        directory = self.client_dir(client_id)
        _validate_parts(parts)
        path = directory.joinpath(*parts)
        if path.parent != directory:
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(obj, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StoreError(f"failed to write {path}: {exc}") from exc

    def append_json_list(self, client_id: str, item: Any, *parts: str) -> list[Any]:
        """Append ``item`` to a JSON list file (creating it as ``[]`` first).

        Args:
            client_id: The client identifier.
            item: Item to append.
            *parts: Path parts under the client dir.

        Returns:
            The full list after appending.
        """
        items = self.read_json(client_id, *parts, default=[])
        if not isinstance(items, list):
            raise StoreError(
                f"expected a JSON list at {parts}, found {type(items).__name__}"
            )
        items.append(item)
        self.write_json(client_id, items, *parts)
        return items

    def new_id(self, prefix: str) -> str:
        """Generate a new identifier: ``prefix`` + 12 hex chars.

        Args:
            prefix: ID prefix such as ``"cl_"``.

        Returns:
            A new unique-ish identifier.
        """
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    def client_exists(self, client_id: str) -> bool:
        """Return True if the client's directory exists.

        Args:
            client_id: The client identifier.
        """
        return (self.root / "clients" / client_id).is_dir()

    def list_client_ids(self) -> list[str]:
        """Return the sorted list of known client IDs.

        Returns:
            Sorted list of directory names under ``<root>/clients``.
        """
        clients_root = self.root / "clients"
        if not clients_root.is_dir():
            return []
        return sorted(
            entry.name for entry in clients_root.iterdir() if entry.is_dir()
        )

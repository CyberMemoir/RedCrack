"""Local source snapshots; never replace original text with a generated query."""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .export import write_json


class EvidenceArchive:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def save(self, kind: str, data: Any) -> dict:
        observed_at = datetime.now(UTC).isoformat()
        path = self.root / f"{kind}-{uuid.uuid4().hex}.json"
        write_json(path, {"kind": kind, "observed_at": observed_at, "data": data})
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "observed_at": observed_at,
            "locator": "/data",
            "kind": kind,
        }

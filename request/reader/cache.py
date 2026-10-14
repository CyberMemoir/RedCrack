"""Explicit-refresh cache; summaries and full details are stored separately."""

import hashlib
import json
from pathlib import Path

from .errors import ReaderError
from .export import write_json
from .models import MediaAsset, Note


class NoteCache:
    def __init__(self, profile: Path) -> None:
        self.root = profile / "reader-cache"

    def path(self, kind: str, key: str) -> Path:
        return self.root / kind / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    @staticmethod
    def restore(row: dict) -> Note:
        values = dict(row)
        values.pop("canonical_url", None)  # Derived identity, including older cache rows.
        values["media"] = [MediaAsset(**asset) for asset in values.get("media", [])]
        return Note(**values)

    def get(self, kind: str, key: str) -> list[Note] | None:
        path = self.path(kind, key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = []
            for row in data["notes"]:
                note = self.restore(row)
                note.cached = True
                result.append(note)
            return result
        except (TypeError, KeyError, ValueError) as exc:
            raise ReaderError("缓存损坏，请检查本地缓存；不会自动重新访问") from exc

    def put(self, kind: str, key: str, notes: list[Note]) -> None:
        write_json(self.path(kind, key), {"notes": [n.to_dict() for n in notes]})

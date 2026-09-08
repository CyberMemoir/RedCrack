from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Note


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _csv_cell(value: Any) -> Any:
    # 避免从网页导出的标题/正文被电子表格作为公式执行。
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_notes(
    notes: list[Note], output: Path, *, keyword: str = "", complete: bool = True
) -> None:
    write_json(
        output / "results.json",
        {
            "schema_version": 1,
            "keyword": keyword,
            "count": len(notes),
            "complete": complete,
            "notes": [note.to_dict() for note in notes],
        },
    )
    text = io.StringIO(newline="")
    fields = [
        "note_id",
        "title",
        "description",
        "kind",
        "author",
        "author_id",
        "url",
        "tags",
        "published_at",
        "likes",
        "collects",
        "comments",
        "shares",
        "media_count",
    ]
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    for note in notes:
        row = {key: getattr(note, key) for key in fields if hasattr(note, key)}
        row.update(note.stats)
        row["tags"] = ", ".join(note.tags)
        row["media_count"] = len(note.media)
        writer.writerow({key: _csv_cell(row.get(key, "")) for key in fields})
    atomic_write(output / "results.csv", "\ufeff" + text.getvalue())


def write_note(note: Note, directory: Path) -> None:
    write_json(directory / "note.json", note.to_dict())
    markdown = f"# {note.title or note.note_id}\n\n作者：{note.author}\n\n来源：{note.url}\n\n{note.description}\n"
    if note.tags:
        markdown += "\n" + " ".join(f"#{tag}" for tag in note.tags) + "\n"
    atomic_write(directory / "note.md", markdown)

"""Structural audit of saved evidence only; fields are witnesses, not API recipes."""

import json
import re
from pathlib import Path

from .family_bundle import sha256

FIELDS = re.compile(r"cursor|search.?id|session|has.?more|^page$|^user.?id$|^author_id$", re.I)
FILES = (
    "search-response*.json",
    "search-context*.json",
    "xhs-search*result.json",
    "page-state.json",
    "note.json",
    "results.json",
)


def witnesses(value, locator=""):
    if isinstance(value, dict):
        for key, child in value.items():
            at = locator + "/" + key.replace("~", "~0").replace("/", "~1")
            if FIELDS.search(key):
                yield {
                    "locator": at,
                    "field": key,
                    "value_type": type(child).__name__,
                    "nonempty": child is not None and child != "",
                }
            yield from witnesses(child, at)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from witnesses(child, locator + "/" + str(index))


def audit_archives(inputs):
    paths = set()
    for item in inputs:
        path = Path(item).resolve()
        if path.is_file():
            paths.add(path)
        else:
            for pattern in FILES:
                paths.update(
                    p
                    for p in path.rglob(pattern)
                    if not {"browser-profile", "offline-profile", "tool"}.intersection(p.parts)
                )
    files, authors = [], set()
    for path in sorted(paths):
        body = path.read_bytes()
        value = json.loads(body.decode("utf-8-sig"))
        fields = list(witnesses(value))
        kind = value.get("kind") if isinstance(value, dict) else None
        for field in fields:
            field["scope"] = (
                "comment_state_not_search"
                if "/comments/" in field["locator"].lower()
                else "normal_search_candidate"
                if kind in {"search-response", "search-context"}
                else "unclassified_saved_state"
            )
        if path.name in {"note.json", "results.json"} and isinstance(value, dict):
            for row in value.get("notes", [value]):
                author = row.get("author_id")
                if isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9_-]+", author):
                    authors.add(author)
        files.append(
            {
                "private_path": str(path),
                "sha256": sha256(body),
                "archive_kind": kind,
                "normal_search_response": kind == "search-response",
                "normal_request_context": kind == "search-context",
                "field_witnesses": fields,
            }
        )
    return {
        "schema_version": 1,
        "schema_name": "xhs-saved-reader-audit-v1",
        "files_audited": len(files),
        "normal_search_responses": sum(f["normal_search_response"] for f in files),
        "normal_request_contexts": sum(f["normal_request_context"] for f in files),
        "stable_author_ids": len(authors),
        "comment_cursor_witnesses": sum(
            w["field"] == "cursor" and w["scope"] == "comment_state_not_search"
            for f in files
            for w in f["field_witnesses"]
        ),
        "search_session_witnesses": sum(
            bool(re.search(r"search.?id|session", w["field"], re.I))
            and w["scope"] == "normal_search_candidate"
            for f in files
            for w in f["field_witnesses"]
        ),
        "restartable_history": "unsupported_no_verified_token_session_contract",
        "author_history": "unsupported_no_verified_list_entry",
        "historical_popularity": "not_established_by_search_or_current_metrics",
        "platform_requests": 0,
        "files": files,
    }

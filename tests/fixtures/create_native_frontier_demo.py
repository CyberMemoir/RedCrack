"""Source-shaped search + invented, explicitly fixture-only related pages. No network."""

import argparse
import json
from pathlib import Path

from request.reader.native_search import search_page
from tests.fixtures.create_frontier_demo import A, B, C, create, note


def create_native(root):
    create(root)
    path = root / "replay.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    request = {
        "keyword": "合成甲",
        "page": 1,
        "page_size": 20,
        "search_id": "synthetic-session",
        "sort": "general",
        "note_type": 0,
        "ext_flags": [],
        "geo": "",
        "image_formats": ["jpg", "webp", "avif"],
    }

    def response(note_id, more):
        return {
            "success": True,
            "data": {
                "items": [{"id": note_id, "note_card": {"note_id": note_id, "title": "合成甲"}}],
                "has_more": more,
            },
        }

    cursor = search_page("合成甲", request, response(A, True))["next_cursor"]
    data["searches"][:2] = [
        {
            "query": "合成甲",
            "cursor": None,
            "native_request": request,
            "native_response": response(A, True),
        },
        {
            "query": "合成甲",
            "cursor": cursor,
            "native_request": request | {"page": 2},
            "native_response": response(B, False),
        },
    ]
    data["related"] = [
        {
            "parent_note": A,
            "cursor": None,
            "notes": [note(B)],
            "next_cursor": "synthetic-related-page-2",
            "exhausted": False,
        },
        {
            "parent_note": A,
            "cursor": "synthetic-related-page-2",
            "notes": [note(C)],
            "next_cursor": None,
            "exhausted": True,
        },
        {
            "parent_note": B,
            "cursor": None,
            "notes": [note(A)],
            "next_cursor": None,
            "exhausted": True,
        },
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.directory.exists():
        raise SystemExit("Use a new demo directory")
    create_native(args.directory)
    print(
        json.dumps(
            {"sample_mode": "fixture", "config": str(args.directory.resolve() / "config.json")}
        )
    )

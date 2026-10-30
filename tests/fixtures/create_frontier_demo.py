"""Generate synthetic offline source/replay data. No real IDs, accounts or network."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

A, B, C = (f"{i:024x}" for i in (1, 2, 3))


def note(note_id, *, detail=False, body=""):
    url = "https://www.xiaohongshu.com/explore/" + note_id + "?xsec_token=synthetic-token"
    return {
        "note_id": note_id,
        "url": url,
        "title": "合成甲素材",
        "description": body,
        "content_scope": "detail" if detail else "search_card",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "published_at": None,
        "stats": {"likes": "123"},
        "access": {"url": url, "method": "navigate"},
    }


def source(root, index, title, description, *, replace_row=None, family=None):
    root = Path(root)
    data = root / "bili"
    data.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"title": title, "description": description}, ensure_ascii=False).encode()
    sha = hashlib.sha256(body).hexdigest()
    path = data / "objects" / sha[:2] / (sha + ".bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    record = {
        "platform": "bilibili",
        "kind": "video",
        "item_id": f"BV123456789{index}",
        "family_id": family,
        "observed_at": "2026-10-29T00:00:00+00:00",
        "published_at_raw": None,
        "evidence_sha": sha,
        "texts": [
            {"kind": "title", "text": title, "locator": "/title"},
            {"kind": "description", "text": description, "locator": "/description"},
        ],
        "explainer_discovery": {"status": "strong_candidate"},
    }
    with closing(sqlite3.connect(data / "collection.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS observations(record_json TEXT NOT NULL)")
        if replace_row:
            db.execute(
                "UPDATE observations SET record_json=? WHERE rowid=?",
                (json.dumps(record, ensure_ascii=False), replace_row),
            )
        else:
            db.execute(
                "INSERT INTO observations VALUES(?)", (json.dumps(record, ensure_ascii=False),)
            )
    return data / "collection.sqlite3"


def create(root):
    root = Path(root)
    source(
        root,
        0,
        "【梗百科】<em>合成甲</em>是什么梗",
        "明确引用 https://www.xiaohongshu.com/explore/" + A,
    )
    database = source(
        root,
        1,
        "合成甲是什么梗",
        "原作链接 https://www.xiaohongshu.com/explore/" + A,
        family="00000000-0000-4000-8000-000000000001",
    )
    replay = {
        "schema_name": "xhs-frontier-replay-v1",
        "sample_mode": "fixture",
        "searches": [
            {
                "query": "合成甲",
                "cursor": None,
                "next_cursor": "fixture-only-opaque-next",
                "notes": [note(A)],
                "exhausted": False,
            },
            {
                "query": "合成甲",
                "cursor": "fixture-only-opaque-next",
                "next_cursor": None,
                "notes": [note(B)],
                "exhausted": True,
            },
            {
                "query": "合成乙",
                "cursor": None,
                "next_cursor": None,
                "notes": [],
                "exhausted": True,
            },
            {
                "query": "合成丙",
                "cursor": None,
                "next_cursor": None,
                "notes": [note(C)],
                "exhausted": True,
            },
        ],
        "details": {
            A: note(
                A,
                detail=True,
                body="合成甲改图模板，用于回复。引用 https://www.xiaohongshu.com/explore/" + B,
            ),
            B: note(B, detail=True, body="未知关系材料"),
            C: {"outcome": "unavailable"},
        },
    }
    (root / "replay.json").write_text(
        json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    config = {
        "driver": "offline_replay",
        "state_dir": "frontier",
        "source_database": "bili/collection.sqlite3",
        "replay_file": "replay.json",
        "batch": 2,
        "source_batch": 1,
        "update_batch": 1,
        "max_depth": 1,
        "idle_seconds": 1,
    }
    (root / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return database


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.directory.exists():
        raise SystemExit("Use a new demo directory")
    create(args.directory)
    print(
        json.dumps(
            {"sample_mode": "fixture", "config": str(args.directory.resolve() / "config.json")}
        )
    )

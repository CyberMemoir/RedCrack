"""Read-only Bilibili candidate handoff, isolated from registered meme families."""

import json
import re
import sqlite3
from pathlib import Path

from .export import atomic_write, write_json
from .family_bundle import encoded, sha256


def pointer(document, locator):
    if not locator.startswith("/"):
        raise ValueError("Candidate evidence requires an original JSON pointer")
    for part in locator[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def candidate_bundle(database, output, limit=5):
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Candidate metadata handoff budget is 1..10")
    database, output = Path(database).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Use a new candidate bundle directory; previous snapshots stay immutable")
    if output.is_relative_to(database.parent):
        raise ValueError("Candidate output must be outside the source collector data directory")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT record_json FROM observations WHERE json_extract(record_json,'$.platform')='bilibili' AND json_extract(record_json,'$.explainer_discovery.status')='strong_candidate' ORDER BY rowid DESC LIMIT 200"
        ).fetchall()
    finally:
        connection.close()
    candidates, objects, seen = [], {}, set()
    for (raw,) in rows:
        record = json.loads(raw)
        bvid, evidence_sha = record["item_id"], record["evidence_sha"]
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not re.fullmatch(
            r"[a-f0-9]{64}", evidence_sha
        ):
            raise ValueError("Invalid candidate source identity or evidence hash")
        if bvid in seen:
            continue
        evidence = database.parent / "objects" / evidence_sha[:2] / (evidence_sha + ".bin")
        if evidence.stat().st_size > 4 * 1024**2:
            raise ValueError("Candidate evidence exceeds the bounded metadata budget")
        body = evidence.read_bytes()
        if sha256(body) != evidence_sha:
            raise ValueError("Saved Bilibili evidence hash changed")
        document = json.loads(body)
        texts = []
        for text in record["texts"]:
            if text["kind"] not in {"title", "description"}:
                continue
            if pointer(document, text["locator"]) != text["text"]:
                raise ValueError("Saved candidate original text/pointer changed")
            texts.append({"kind": text["kind"], "text": text["text"], "locator": text["locator"]})
        objects[evidence_sha] = body
        candidate_id = "bilibili:" + bvid
        refs = [
            {"platform": "bilibili", "work_id": candidate_id, "evidence_sha": evidence_sha, **text}
            for text in texts
        ]
        subject = record.get("analysis", {}).get("title_subject_candidate")
        query_candidates = (
            [{"text": subject, "basis": "saved_rule_subject_candidate", "evidence_refs": refs}]
            if isinstance(subject, str) and subject.strip()
            else []
        )
        candidates.append(
            {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "family_id": None,
                "registration": "unregistered_collection_candidate",
                "bvid": bvid,
                "url": "https://www.bilibili.com/video/" + bvid,
                "observed_at": record["observed_at"],
                "published_at_raw": record.get("published_at_raw"),
                "metrics": record.get("metrics", {}),
                "evidence_sha": evidence_sha,
                "texts": texts,
                "evidence_refs": refs,
                "query_candidates": query_candidates,
                "source_hints": [
                    {
                        "kind": "explicit_link",
                        "value": match.group(),
                        "evidence_sha": evidence_sha,
                        "locator": text["locator"],
                        "char_start": match.start(),
                        "char_end_exclusive": match.end(),
                        "status": "author_reference_not_confirmed_origin",
                    }
                    for text in texts
                    for match in re.finditer(r"https?://[^\s<>\"'，。；！？]+", text["text"])
                ],
                "roles": ["derivative", "usage"],
                "relation_status": "candidate_not_confirmed",
                "family_creation_authorized": False,
            }
        )
        seen.add(bvid)
        if len(candidates) == limit:
            break
    payload = b"".join(encoded(row) + b"\n" for row in candidates)
    manifest = {
        "schema_name": "xhs-candidate-anchor-bundle-v1",
        "schema_version": 1,
        "source_platform": "bilibili",
        "target_platform": "xiaohongshu",
        "candidate_count": len(candidates),
        "formal_family_count": 0,
        "records_sha256": sha256(payload),
        "objects": sorted(objects),
        "registration": "candidate_only_not_family_import",
        "family_creation_authorized": False,
        "platform_requests": 0,
    }
    for digest, body in objects.items():
        atomic_write(output / "objects" / (digest + ".bin"), body.decode("utf-8"))
    atomic_write(output / "candidates.jsonl", payload.decode("utf-8"))
    write_json(output / "manifest.json", manifest)
    return manifest


def candidate_cli(args):
    return candidate_bundle(args.bili_database, args.directory, args.limit)

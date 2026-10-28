"""Platform exchange adapter, not a second task scheduler or semantic classifier."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .analysis import analyze_archives, digest, input_files
from .export import atomic_write, write_json
from .models import NOTE_ID, Note, normalize_note_url, note_id_from_url

SCHEMA = "platform-collection-v1"
EXPLANATION = re.compile(
    r"(?:梗|表情包).{0,8}(?:科普|解说|解读|解析)|(?:科普|解说|解读|解析).{0,8}(?:梗|表情包)"
)
DEFINITION = re.compile(r"是指|意思是|指的是|源自|出自|来源于")
LINK = re.compile(r"https?://[^\s<>\"'，。；！？（）【】]+")


def identity(kind: str, entity_id: str) -> dict:
    return {"platform": "xiaohongshu", "entity_type": kind, "entity_id": entity_id}


def persist_rows(path: Path, additions: list[dict]) -> tuple[int, int]:
    previous = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if path.exists()
        else []
    )
    records = {digest(row): row for row in previous}
    before = len(records)
    records.update((digest(row), row) for row in additions)
    atomic_write(
        path,
        "".join(json.dumps(records[key], ensure_ascii=False) + "\n" for key in sorted(records)),
    )
    return len(records) - before, len(records)


def convert_note(row: dict, evidence_sha: str, locator: str, keyword: str):
    note_id = str(row.get("note_id", "")).lower()
    if not NOTE_ID.fullmatch(note_id):
        raise ValueError("归档缺少有效note ID")
    if row.get("url") and note_id_from_url(normalize_note_url(row["url"])) != note_id:
        raise ValueError("访问URL与归档note ID不一致")
    source = identity("note", note_id)
    observed_at = row.get("observed_at")
    common = {
        "schema_version": SCHEMA,
        "observed_at": observed_at,
        "evidence_sha": evidence_sha,
        "locator": locator,
    }
    access = row.get("access") or (
        {"url": row["url"], "method": "navigate"}
        if row.get("url") and parse_qs(urlsplit(row["url"]).query).get("xsec_token")
        else {"url": None, "method": "unavailable"}
    )
    access_id = digest([note_id, access])
    private_access = [{"access_ref": access_id, **source, "access": access}]
    node = {
        **common,
        **source,
        "canonical_url": Note(note_id).canonical_url,
        "title": row.get("title", ""),
        "body": row.get("description", ""),
        "author_id": row.get("author_id") or None,
        "published_at_raw": row.get("published_at"),
        "metrics": row.get("stats") or {},
        "content_type": row.get("kind", "unknown"),
        "content_scope": row.get("content_scope", "unknown"),
        "tags": row.get("tags") or [],
        "access_ref": access_id,
    }
    if keyword:
        node["discovered_via"] = {
            "entry_type": "search",
            "query": keyword,
            "layer": "rule_seed",
            "is_actual_search_log": False,
        }
    nodes, edges, frontier, media = [node], [], [], []
    if node["content_scope"] != "detail":
        frontier.append(
            {
                **source,
                "action": "detail",
                "canonical_url": node["canonical_url"],
                "access_ref": access_id,
                "execution_status": "access_missing"
                if access["method"] == "unavailable"
                else "pending",
            }
        )
    author_id = row.get("author_id")
    if author_id:
        author = identity("author", str(author_id))
        nodes.append(
            {
                **common,
                **author,
                "canonical_url": "https://www.xiaohongshu.com/user/profile/"
                + quote(str(author_id), safe=""),
                "name": row.get("author", ""),
            }
        )
        edges.append(
            {
                **common,
                "from": author,
                "to": source,
                "relation": "author_post",
                "locator": locator + "/author_id",
            }
        )
    for tag in row.get("tag_refs") or []:
        if not tag.get("id"):
            continue
        target = identity("tag", str(tag["id"]))
        nodes.append({**common, **target, "canonical_url": None, "name": tag.get("name", "")})
        edges.append(
            {
                **common,
                "from": source,
                "to": target,
                "relation": "tag_member",
                "locator": locator + "/tag_refs",
            }
        )
    for match in LINK.finditer(node["body"]):
        try:
            url = normalize_note_url(match.group())
        except ValueError:
            continue
        target_id = note_id_from_url(url)
        if not target_id or target_id == note_id:
            continue
        target = identity("note", target_id)
        target_access = {"url": url, "method": "navigate"}
        ref = digest([target_id, target_access])
        private_access.append({"access_ref": ref, **target, "access": target_access})
        nodes.append(
            {
                **common,
                **target,
                "canonical_url": Note(target_id).canonical_url,
                "content_scope": "reference_only",
                "access_ref": ref,
            }
        )
        edges.append(
            {
                **common,
                "from": source,
                "to": target,
                "relation": "links_to",
                "locator": locator + "/description",
                "char_start": match.start(),
                "char_end_exclusive": match.end(),
            }
        )
        frontier.append(
            {
                **target,
                "action": "detail",
                "canonical_url": Note(target_id).canonical_url,
                "access_ref": ref,
            }
        )
    # Only a rule candidate with proven detail scope can provide OCR/media inputs.
    # URLs stay private; this function does not download or decode any media.
    explanation_candidate = bool(EXPLANATION.search(node["title"] + "\n" + node["body"])) or (
        "什么梗" in node["title"] and bool(DEFINITION.search(node["body"]))
    )
    if node["content_scope"] == "detail" and explanation_candidate:
        assets = [
            (f"/media/{index}", asset["kind"], index, asset)
            for index, asset in enumerate(row.get("media") or [])
        ]
        assets.extend(
            (f"/subtitle_tracks/{index}", "subtitle", index, track)
            for index, track in enumerate(row.get("subtitle_tracks") or [])
        )
        for suffix, kind, index, asset in assets:
            asset_ref = digest([note_id, suffix, asset])
            private_access.append({"access_ref": asset_ref, **source, "asset": asset})
            media.append(
                {
                    **common,
                    **source,
                    "kind": kind,
                    "asset_index": index,
                    "candidate_role": "explanation_candidate",
                    "role_rule": "explicit_explanation_phrase_v1",
                    "access_ref": asset_ref,
                    "downloaded": False,
                    "locator": locator + suffix,
                    "ocr_status": "not_requested",
                }
            )
    # Public text retains ordinary provenance, but an embedded URL loses query tokens.
    node["body"] = LINK.sub(lambda m: public_text_url(m.group()), node["body"])
    return nodes, edges, frontier, media, private_access


def public_text_url(value: str) -> str:
    try:
        url = normalize_note_url(value)
        note_id = note_id_from_url(url)
        return Note(note_id).canonical_url if note_id else "[private share link]"
    except ValueError:
        return value


def public_record(value):
    if isinstance(value, str):
        return LINK.sub(lambda match: public_text_url(match.group()), value)
    if isinstance(value, dict):
        return {key: public_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_record(item) for item in value]
    return value


def export_queries(inputs: list[str], output: Path) -> tuple[int, int]:
    private = output / "private/query-analysis"
    analyze_archives(inputs, private)
    rows = []
    for line in (private / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        query = json.loads(line)
        evidence = query["evidence"][0]
        rows.append(
            {
                "schema_version": SCHEMA,
                "query_id": query["query_id"],
                "platform": "xiaohongshu",
                "source_id": query["source_id"],
                "raw_text": query["raw_text"],
                "raw_text_ref": query["query_id"],
                "layer": "real_expression",
                "source_kind": query["source_kind"],
                "is_actual_search_log": False,
                "rule_version": query["rule_version"],
                "observed_at": evidence["observed_at"],
                "evidence_sha": evidence["sha256"],
                "locator": evidence["locator"],
                "char_start": query["char_start"],
                "char_end_exclusive": query["char_end_exclusive"],
            }
        )
    return persist_rows(output / "queries.jsonl", public_record(rows))


def export_collection(
    inputs: list[str],
    output: str | Path,
    *,
    pause_record: str | None = None,
    tool_revision: str = "unknown",
) -> dict:
    output = Path(output).expanduser().resolve()
    files = input_files(inputs, output)
    resume_path = output / "resume.json"
    previous_resume = (
        json.loads(resume_path.read_text(encoding="utf-8")) if resume_path.exists() else {}
    )
    saved_pause = Path(pause_record) if pause_record else output / "private/pause.json"
    if pause_record and not saved_pause.exists():
        raise ValueError("指定的暂停记录不存在；不会按无暂停导出")
    blocked = json.loads(saved_pause.read_text(encoding="utf-8")) if saved_pause.exists() else None
    if blocked:
        if blocked.get("status") not in (
            "blocked_login",
            "blocked_verification",
            "blocked_rate_limit",
        ):
            raise ValueError("暂停记录必须提供明确blocked状态")
        write_json(output / "private/pause.json", blocked)
    nodes, edges, frontier, media, access, manifests = [], [], [], [], [], []
    for path in files:
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        data = json.loads(content.decode("utf-8-sig"))
        rows = data.get("notes") if "notes" in data else [data]
        if not isinstance(rows, list):
            raise ValueError("归档notes必须为列表")
        manifests.append({"path": str(path), "sha256": sha, "complete": data.get("complete")})
        for index, row in enumerate(rows):
            locator = f"/notes/{index}" if "notes" in data else ""
            n, e, f, m, a = convert_note(row, sha, locator, data.get("keyword", ""))
            nodes.extend(n)
            edges.extend(e)
            frontier.extend(f)
            media.extend(m)
            access.extend(a)
    new_nodes, total_nodes = persist_rows(output / "nodes.jsonl", public_record(nodes))
    new_edges, total_edges = persist_rows(output / "edges.jsonl", public_record(edges))
    persist_rows(output / "frontier.jsonl", public_record(frontier))
    persist_rows(output / "media_manifest.jsonl", public_record(media))
    persist_rows(output / "private/access.jsonl", access)
    write_json(output / "private/inputs.json", manifests)
    new_queries, total_queries = export_queries(inputs, output)
    capabilities = {
        "schema_version": SCHEMA,
        "tool_revision": tool_revision,
        "entries": [
            {
                "entry": name,
                "status": "unsupported"
                if name in ("comments", "platform_suggestions", "author_history")
                else "blocked"
                if blocked
                else "unverified",
                "validated_at": None,
                "reason": "not_implemented"
                if name in ("comments", "platform_suggestions", "author_history")
                else "unresolved_access_pause"
                if blocked
                else "offline_conversion_does_not_validate_platform_access",
            }
            for name in (
                "search",
                "account_recommendations",
                "note_detail",
                "tag_page",
                "related_notes",
                "author_history",
                "comments",
                "platform_suggestions",
            )
        ],
    }
    write_json(output / "capabilities.json", capabilities)
    summary = {
        "schema_version": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": blocked["status"] if blocked else "partial",
        "reason": "unresolved_access_pause"
        if blocked
        else "offline_export_only_no_live_collection",
        "offline_export_status": "success",
        "new_node_versions": new_nodes,
        "total_node_versions": total_nodes,
        "new_edges": new_edges,
        "total_edges": total_edges,
        "new_query_occurrences": new_queries,
        "total_query_occurrences": total_queries,
        "platform_requests": 0,
        "media_bytes": 0,
        "model_calls": 0,
        "browser_slot": "released",
        "resume": "rerun_same_inputs_is_idempotent",
    }
    write_json(output / "trial_summary.json", summary)
    write_json(
        output / "resume.json",
        {
            "schema_version": SCHEMA,
            "node_versions": total_nodes,
            "edges": total_edges,
            "seen_exported_identities": sorted(
                set(previous_resume.get("seen_exported_identities", []))
                | {(n["platform"] + ":" + n["entity_type"] + ":" + n["entity_id"]) for n in nodes}
            ),
            "input_hashes": sorted(
                set(previous_resume.get("input_hashes", [])) | {m["sha256"] for m in manifests}
            ),
        },
    )
    return summary

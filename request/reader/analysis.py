"""Offline, deterministic analysis of reader archives. Standard library only."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .export import _csv_cell, atomic_write, write_json
from .models import NOTE_ID, normalize_note_url, note_id_from_url

RULE_VERSION = "human-query-1"
METRICS = ("likes", "collects", "comments", "shares")
RULES = {
    "material_request": re.compile(
        r"(?:求|蹲|谁有|有没有|哪里|在哪|哪儿|怎么找|找不到|想找|想要).{0,60}"
        r"(?:表情包|原图|动图|素材|模板|出处|音源|背景音乐|BGM)",
        re.I,
    ),
    "name_question": re.compile(
        r"(?:这|那个|这个|它|表情包|梗).{0,40}(?:叫什么|叫啥|是什么梗|是啥梗)"
        r"|(?:什么|哪个|啥)(?:表情包|梗|背景音乐|BGM)",
        re.I,
    ),
    "usage_question": re.compile(
        r"(?:怎么|如何|什么场合|什么时候).{0,30}(?:用这个梗|用这个表情|用这张图|配文案)"
    ),
}
SENTENCES = re.compile(r"[^\n。！？!?；;]+[。！？!?；;]*")


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def metric_value(raw) -> dict:
    if raw is None or raw == "":
        return {"raw": raw, "value": None, "approximate": False, "status": "missing"}
    match = re.fullmatch(r"(\d+(?:,\d{3})*(?:\.\d+)?)([万亿kKwW]?)(\+?)", str(raw).strip())
    if not match:
        return {"raw": raw, "value": None, "approximate": False, "status": "unparsed"}
    number, unit, plus = match.groups()
    multiplier = {"": 1, "万": 10000, "亿": 100000000, "k": 1000, "w": 10000}[unit.lower()]
    value = Decimal(number.replace(",", "")) * multiplier
    if value != value.to_integral_value():
        return {"raw": raw, "value": None, "approximate": False, "status": "unparsed"}
    return {"raw": raw, "value": int(value), "approximate": bool(unit or plus), "status": "parsed"}


def observed_time(value: str | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observed_at 必须含时区；未知请保留null")
    return result.astimezone(UTC)


def input_files(inputs: list[str], output: Path) -> list[Path]:
    files = set()
    for value in inputs:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"输入不存在：{path}")
        candidates = (
            [path] if path.is_file() else [*path.rglob("results.json"), *path.rglob("note.json")]
        )
        files.update(p for p in candidates if not p.is_relative_to(output))
    if not files:
        raise ValueError("未找到results.json或note.json")
    return sorted(files)


def query_spans(text: str):
    for sentence in SENTENCES.finditer(text):
        raw = sentence.group()
        start = sentence.start() + len(raw) - len(raw.lstrip())
        end = sentence.end() - len(raw) + len(raw.rstrip())
        raw = text[start:end]
        matches = [name for name, pattern in RULES.items() if pattern.search(raw)]
        if matches:
            yield start, end, raw, matches


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row.get(key, "")) for key in fields})
    atomic_write(path, "\ufeff" + stream.getvalue())


def read_archives(files: list[Path], recorded_at: str):
    manifest, observations, queries, latest = [], {}, {}, {}
    rows_read = 0
    for path in files:
        content = path.read_bytes()
        source_hash = hashlib.sha256(content).hexdigest()
        data = json.loads(content.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"输入必须是笔记对象或包含notes列表的对象：{path}")
        rows = data.get("notes") if "notes" in data else [data]
        if not isinstance(rows, list):
            raise ValueError(f"notes必须是列表：{path}")
        manifest.append(
            {
                "path": str(path),
                "sha256": source_hash,
                "rows": len(rows),
                "complete": data.get("complete") if "notes" in data else None,
            }
        )
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not NOTE_ID.fullmatch(str(row.get("note_id", ""))):
                raise ValueError(f"无效笔记ID：{path} 行 {index}")
            note_id = row["note_id"].lower()
            if row.get("url") and note_id_from_url(normalize_note_url(row["url"])) != note_id:
                raise ValueError(f"笔记URL与ID不一致：{path} 行 {index}")
            for field in ("title", "description"):
                if not isinstance(row.get(field, ""), str):
                    raise ValueError(f"{field}必须为原文字符串：{path}")
            timestamp = row.get("observed_at")
            observed_time(timestamp)
            rows_read += 1
            locator = f"/notes/{index}" if "notes" in data else ""
            reference = {
                "path": str(path),
                "sha256": source_hash,
                "locator": locator,
                "observed_at": timestamp,
            }
            # Ignore cache flags and archive paths when deduplicating the same observation.
            snapshot = {
                key: row.get(key)
                for key in (
                    "title",
                    "description",
                    "kind",
                    "author",
                    "author_id",
                    "tags",
                    "stats",
                    "published_at",
                )
            }
            observation_id = digest([note_id, timestamp, snapshot])
            if observation_id not in observations:
                observations[observation_id] = {
                    "observation_id": observation_id,
                    "note_id": note_id,
                    "observed_at": timestamp,
                    "recorded_at": recorded_at,
                    "metrics": {
                        name: metric_value((row.get("stats") or {}).get(name)) for name in METRICS
                    },
                    "evidence": [],
                }
            observations[observation_id]["evidence"].append(reference)
            if note_id not in latest or observed_time(timestamp) > observed_time(
                latest[note_id]["observed_at"]
            ):
                latest[note_id] = {
                    "note_id": note_id,
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                    **snapshot,
                    "observed_at": timestamp,
                    "observation_id": observation_id,
                }
            for field, kind in (("title", "title"), ("description", "body")):
                text = row.get(field, "")
                for start, end, raw, matched in query_spans(text):
                    query_id = digest([RULE_VERSION, note_id, field, start, end, raw])
                    if query_id not in queries:
                        queries[query_id] = {
                            "query_id": query_id,
                            "platform": "xiaohongshu",
                            "source_id": note_id,
                            "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                            "raw_text": raw,
                            "normalized_text": re.sub(r"\s+", " ", raw),
                            "layer": "real_expression",
                            "source_kind": kind,
                            "is_actual_search_log": False,
                            "rule_version": RULE_VERSION,
                            "matched_rules": matched,
                            "context": text,
                            "char_start": start,
                            "char_end_exclusive": end,
                            "recorded_at": recorded_at,
                            "target_ids": [],
                            "evidence": [],
                        }
                    queries[query_id]["evidence"].append(
                        {**reference, "locator": locator + "/" + field}
                    )
    return manifest, observations, queries, latest, rows_read


def summarize(manifest, observations, queries, latest, rows_read, recorded_at):
    # Aggregate each note once. Never add successive cumulative counters together.
    metric_summary = []
    for name in METRICS:
        values = [observations[n["observation_id"]]["metrics"][name] for n in latest.values()]
        known = [v["value"] for v in values if v["value"] is not None]
        metric_summary.append(
            {
                "metric": name,
                "known_value_sum": sum(known) if known else None,
                "known_notes": len(known),
                "missing_notes": sum(v["status"] == "missing" for v in values),
                "unparsed_notes": sum(v["status"] == "unparsed" for v in values),
                "approximate_notes": sum(v["approximate"] for v in values),
            }
        )
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "recorded_at": recorded_at,
        "mode": "offline_rules",
        "model_calls": 0,
        "network_requests": 0,
        "input_files": len(manifest),
        "input_rows": rows_read,
        "unique_notes": len(latest),
        "unique_observations": len(observations),
        "duplicate_rows": rows_read - len(observations),
        "query_occurrences": len(queries),
        "distinct_query_texts": len({q["normalized_text"] for q in queries.values()}),
        "notes_with_queries": len({q["source_id"] for q in queries.values()}),
        "queries_by_source_kind": dict(Counter(q["source_kind"] for q in queries.values())),
        "queries_by_rule": dict(
            Counter(rule for q in queries.values() for rule in q["matched_rules"])
        ),
        "notes_by_kind": dict(Counter(n["kind"] or "unknown" for n in latest.values())),
        "notes_with_body": sum(bool(n["description"]) for n in latest.values()),
        "notes_with_author_id": sum(bool(n["author_id"]) for n in latest.values()),
        "notes_without_observation_time": sum(n["observed_at"] is None for n in latest.values()),
        "incomplete_input_files": sum(m["complete"] is False for m in manifest),
        "metrics": metric_summary,
        "selection": "latest_known_observed_at; equal_or_unknown_times_keep_first_input",
        "unsupported_sources": [
            "comments",
            "platform_suggestions",
            "image_audio_text_without_existing_text",
        ],
    }
    return summary


def export_analysis(output, observations, queries, latest, summary, manifest):
    for name, records in (("queries", queries), ("observations", observations)):
        atomic_write(
            output / f"{name}.jsonl",
            "".join(json.dumps(records[key], ensure_ascii=False) + "\n" for key in sorted(records)),
        )
    note_rows = [
        {
            **n,
            **{
                name: observations[n["observation_id"]]["metrics"][name]["value"]
                for name in METRICS
            },
        }
        for n in sorted(latest.values(), key=lambda n: n["note_id"])
    ]
    write_csv(
        output / "notes.csv",
        note_rows,
        [
            "note_id",
            "canonical_url",
            "title",
            "description",
            "author",
            "author_id",
            "kind",
            "published_at",
            "observed_at",
            *METRICS,
        ],
    )
    write_csv(
        output / "queries.csv",
        list(queries.values()),
        [
            "query_id",
            "source_id",
            "canonical_url",
            "raw_text",
            "source_kind",
            "layer",
            "char_start",
            "char_end_exclusive",
            "is_actual_search_log",
            "rule_version",
        ],
    )
    metric_summary = summary["metrics"]
    write_csv(output / "stats.csv", metric_summary, list(metric_summary[0]))
    write_json(output / "summary.json", summary)
    write_json(output / "inputs.json", {"recorded_at": summary["recorded_at"], "files": manifest})


def analyze_archives(inputs: list[str], output: str | Path) -> dict:
    output = Path(output).expanduser().resolve()
    files = input_files(inputs, output)
    recorded_at = datetime.now(UTC).isoformat()
    manifest, observations, queries, latest, rows_read = read_archives(files, recorded_at)
    summary = summarize(manifest, observations, queries, latest, rows_read, recorded_at)
    export_analysis(output, observations, queries, latest, summary, manifest)
    return summary

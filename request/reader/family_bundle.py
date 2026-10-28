"""Self-contained source-schema-1 bundles from saved reader observations; no network."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .export import atomic_write, write_json
from .models import NOTE_ID, Note

OUTCOMES = {"succeeded", "partial", "blocked_login", "blocked_verification", "failed"}
SENSITIVE = re.compile(
    r"xsec_token|authorization|cookie\s*[:=]|https?://\S*[?&](?:token|sign|signature)=", re.I
)
PRIVATE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/(?:Users|home)/")
TASK_OUTCOME = {
    "complete": "succeeded",
    "partial": "partial",
    "interrupted": "partial",
    "verification_required": "blocked_verification",
    "login_required": "blocked_login",
    "network_failed": "failed",
    "failed": "failed",
}


def preserve_outcome(current, observed):
    order = {
        "succeeded": 0,
        "partial": 1,
        "failed": 2,
        "blocked_login": 3,
        "blocked_verification": 4,
    }
    return max((current, observed), key=order.__getitem__)


def encoded(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def publication_time(raw, basis, offset, proof):
    """Mirror the public time contract, with explicit units and no date guessing."""
    zone = timezone(timedelta(minutes=offset)) if offset is not None else None
    if basis in {"unix_seconds", "unix_milliseconds"}:
        if type(raw) not in (int, float):
            raise ValueError("Epoch publication time must be numeric")
        start = end = datetime.fromtimestamp(
            raw / (1000 if basis == "unix_milliseconds" else 1), UTC
        )
        precision = "millisecond" if basis == "unix_milliseconds" else "second"
    elif basis in {"year", "month", "date"}:
        if zone is None:
            raise ValueError("Calendar dates require explicit offset_minutes")
        start = datetime.strptime(
            str(raw), {"year": "%Y", "month": "%Y-%m", "date": "%Y-%m-%d"}[basis]
        ).replace(tzinfo=zone)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        if basis == "year":
            end = end.replace(month=12, day=31)
        elif basis == "month":
            end = end.replace(day=calendar.monthrange(start.year, start.month)[1])
        precision = "day" if basis == "date" else basis
    elif basis == "iso8601":
        match = re.search(r"[T ]\d{2}:\d{2}(?::\d{2}(?:\.(\d+))?)?", raw)
        if not match:
            raise ValueError("ISO publication time requires a time component")
        start = datetime.fromisoformat(raw)
        if start.utcoffset() is None:
            if zone is None:
                raise ValueError("Local publication time requires explicit offset_minutes")
            start = start.replace(tzinfo=zone)
        if re.search(r"[T ]\d{2}:\d{2}:\d{2}", raw):
            end, precision = start, "fractional_second" if match[1] else "second"
        else:
            end, precision = start + timedelta(minutes=1) - timedelta(microseconds=1), "minute"
    else:
        raise ValueError("Unsupported publication basis")
    result = {
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "precision": precision,
        "basis": basis,
        "source_evidence": proof,
    }
    if offset is not None:
        result["offset_minutes"] = offset
    return result


def source_rows(path: Path):
    body = path.read_bytes()
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in body.decode("utf-8-sig").splitlines() if line.strip()]
        incomplete = False
    else:
        data = json.loads(body.decode("utf-8-sig"))
        rows = data.get("notes", [data])
        incomplete = data.get("complete") is False
    return rows, incomplete


def build_bundle(
    inputs,
    output,
    *,
    source_id,
    outcome,
    sample_mode,
    publication_basis=None,
    offset_minutes=None,
    pause_record=None,
    action=None,
):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", source_id):
        raise ValueError("source_id must be a stable non-account identifier")
    if outcome not in OUTCOMES or sample_mode not in {"fixture", "historical", "live"}:
        raise ValueError("Explicit reader outcome and sample mode are required")
    if pause_record:
        pause = json.loads(Path(pause_record).read_text(encoding="utf-8"))
        if pause.get("status") not in {"blocked_login", "blocked_verification"}:
            raise ValueError("Unsupported pause status; do not relabel it success")
        outcome = pause["status"]
    if action is not None and (
        SENSITIVE.search(encoded(action).decode()) or PRIVATE_PATH.search(encoded(action).decode())
    ):
        raise ValueError("Action provenance contains private access data or paths")
    records, objects, gaps = {}, {}, {}

    def gap(reason):
        gaps[reason] = gaps.get(reason, 0) + 1

    for value in inputs:
        path = Path(value).resolve()
        files = (
            sorted([*path.rglob("note.json"), *path.rglob("results.json")])
            if path.is_dir()
            else [path]
        )
        for file in files:
            rows, incomplete = source_rows(file)
            task_path = file.parent / "task.json"
            if task_path.exists():
                task = json.loads(task_path.read_text(encoding="utf-8"))
                if task.get("status") in TASK_OUTCOME:
                    outcome = preserve_outcome(outcome, TASK_OUTCOME[task["status"]])
            if incomplete and outcome == "succeeded":
                outcome = "partial"
            for row in rows:
                if row.get("entity_type", "note") != "note":
                    continue
                note_id = row.get("note_id", row.get("entity_id", ""))
                if not isinstance(note_id, str) or not NOTE_ID.fullmatch(note_id):
                    raise ValueError("Expected a stable 24-hex note ID")
                note_id = note_id.lower()
                observed = row.get("observed_at")
                if observed is None:
                    gap("missing_observed_at_not_fabricated")
                    continue
                if datetime.fromisoformat(observed).utcoffset() is None:
                    raise ValueError("Observation time requires an explicit timezone")
                view = {
                    "view_kind": "allowlisted_reader_note_v1",
                    "note_id": note_id,
                    "observed_at": observed,
                }
                texts = []
                for kind, key in (
                    ("title", "title"),
                    ("description", "description" if "description" in row else "body"),
                ):
                    text = row.get(key, "")
                    if not isinstance(text, str):
                        raise ValueError("Source text must be a string")
                    if SENSITIVE.search(text) or PRIVATE_PATH.search(text):
                        gap("sensitive_text_field_omitted")
                        continue
                    view[kind] = text
                    texts.append({"kind": kind, "text": text, "locator": "/" + kind})
                raw_date = row.get("published_at", row.get("published_at_raw"))
                if type(raw_date) not in {int, float, str, type(None)}:
                    raise ValueError("Publication date must be a scalar raw value")
                if isinstance(raw_date, str) and (
                    SENSITIVE.search(raw_date) or PRIVATE_PATH.search(raw_date)
                ):
                    raise ValueError("Publication date contains private access data")
                view["published_at_raw"] = raw_date
                author = row.get("author_id") or None
                if author is not None and (
                    not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", author)
                ):
                    raise ValueError("author_id must be a stable ID, not a name or URL")
                view["author_id"] = author
                metrics = {
                    key: value
                    for key, value in (row.get("stats") or row.get("metrics") or {}).items()
                    if key in {"views", "likes", "collects", "comments", "shares"}
                }
                if any(
                    type(value) not in {int, float, str, type(None)}
                    or (
                        isinstance(value, str)
                        and (SENSITIVE.search(value) or PRIVATE_PATH.search(value))
                    )
                    for value in metrics.values()
                ):
                    raise ValueError(
                        "Metrics must be original scalar observations without access data"
                    )
                if metrics:
                    view["metrics"] = metrics
                body = encoded(view)
                evidence = sha256(body)
                objects[evidence] = body
                record = {
                    "schema_version": 1,
                    "platform": "xiaohongshu",
                    "kind": "note",
                    "item_id": note_id,
                    "url": Note(note_id).canonical_url,
                    "observed_at": observed,
                    "author_id": author,
                    "published_at_raw": raw_date,
                    "evidence_sha": evidence,
                    "texts": texts,
                    "is_actual_search_log": False,
                    "publication_time_status": "unknown",
                }
                specification = row.get("publication_time", {})
                if metrics:
                    record["metrics"], record["metrics_observed_at"] = metrics, observed
                if row.get("content_scope"):
                    record["content_scope"] = row["content_scope"]
                if row.get("access_ref"):
                    if not re.fullmatch(r"[a-f0-9]{64}", row["access_ref"]):
                        raise ValueError("access_ref must be a private mapping SHA256")
                    record["access_ref"] = row["access_ref"]
                basis = specification.get("basis", publication_basis)
                offset = specification.get("offset_minutes", offset_minutes)
                if raw_date is not None and basis:
                    proof = {
                        "evidence_sha": evidence,
                        "locator": "/published_at_raw",
                        "raw_value": raw_date,
                    }
                    record["normalized_published_time"] = publication_time(
                        raw_date, basis, offset, proof
                    )
                    record["publication_time_status"] = "explicit_source_basis"
                elif raw_date is not None:
                    gap("publication_basis_unknown")
                records[sha256(encoded(record))] = record
    rows = [records[key] for key in sorted(records)]
    payload = b"".join(encoded(row) + b"\n" for row in rows)
    if len(rows) > 1000 or len(payload) > 16 * 1024**2:
        raise ValueError("Bundle exceeds source-contract batch budget")
    if gaps and outcome == "succeeded":
        outcome = "partial"
    manifest = {
        "schema_name": "family-source-bundle-v1",
        "schema_version": 1,
        "platform": "xiaohongshu",
        "source_id": source_id,
        "outcome": outcome,
        "records_sha256": sha256(payload),
        "objects": sorted(objects),
        "sample_mode": sample_mode,
        "record_count": len(rows),
        "gaps": gaps,
        "evidence_view": "allowlisted_original_text_no_access_parameters",
    }
    if action is not None:
        manifest["action"] = action
    output = Path(output).resolve()
    expected = {"manifest.json": encoded(manifest) + b"\n", "records.jsonl": payload}
    expected.update({"objects/" + sha + ".bin": body for sha, body in objects.items()})
    if output.exists():
        actual = {
            str(p.relative_to(output)).replace("\\", "/"): p.read_bytes()
            for p in output.rglob("*")
            if p.is_file()
        }
        if actual != expected:
            raise ValueError("Existing bundle differs; choose a new output directory")
        return manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".family-bundle-", dir=output.parent))
    try:
        for name, body in expected.items():
            atomic_write(temporary / name, body.decode("utf-8"))
        temporary.replace(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def bundle_cli(args):
    if args.report and Path(args.report).resolve().is_relative_to(Path(args.bundle).resolve()):
        raise ValueError("Producer report must be outside the immutable bundle")
    manifest = build_bundle(
        args.input,
        args.bundle,
        source_id=args.source_id,
        outcome=args.outcome,
        sample_mode=args.sample_mode,
        publication_basis=args.publication_basis,
        offset_minutes=args.offset_minutes,
        pause_record=args.pause_record,
    )
    if args.report:
        write_json(Path(args.report), manifest)
    return manifest

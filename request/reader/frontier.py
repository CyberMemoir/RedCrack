"""Single-worker, metadata-only candidate frontier. No central Store or model dependency."""

from __future__ import annotations

import asyncio
import html
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .candidate_anchor import pointer
from .export import atomic_write, write_json
from .family_bundle import encoded, sha256
from .models import Note, normalize_note_url, note_id_from_url

LINK = re.compile(r"https?://[^\s<>\"'，。；！？（）【】]+")
SUBJECT = re.compile(r"(.{2,48}?)(?:是什么梗|是啥梗|这个梗是什么意思)")
DERIVATIVE = re.compile(r"二创|改词|改编|混剪|剪辑|翻唱|填词|改图|配音")
USAGE = re.compile(r"用来|用于|配文|模板|表情包|拿来|回复")


def clean(text):
    text = html.unescape(re.sub(r"</?em\b[^>]*>", "", text, flags=re.I))
    return re.sub(r"^\s*(?:【(?:梗百科|梗科普|梗解析|网络梗|科普)】\s*)+", "", text).strip()


def query_clues(record):
    clues = []
    for field in record["texts"]:
        if field["kind"] not in {"title", "description"}:
            continue
        transformed = clean(field["text"])
        proof = {
            "evidence_sha": record["evidence_sha"],
            "locator": field["locator"],
            "original_text": field["text"],
            "cleaned_text": transformed,
            "rule": "strip_em_unescape_known_heading_v1",
        }
        if field["kind"] == "title":
            match = SUBJECT.search(transformed)
            if match:
                clues.append(
                    {
                        "query": match[1].strip(' “"「」'),
                        "roles": ["derivative", "usage"],
                        "proof": proof,
                        "rule": "title_subject_candidate_v1",
                    }
                )
        for match in re.finditer(
            r"(?:原句(?:是|为)?|出自|源自|原作)[:：\s]*[“「《\"]([^”」》\"\n]{2,60})[”」》\"]",
            transformed,
        ):
            clues.append(
                {
                    "query": match[1],
                    "roles": ["source"],
                    "proof": proof,
                    "rule": "explicit_quoted_reference_v1",
                }
            )
    return clues[:3]


class Frontier:
    def __init__(self, root):
        self.stop_requested = None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_versions(source_row INTEGER,hash TEXT,PRIMARY KEY(source_row,hash));
                CREATE TABLE IF NOT EXISTS anchors(id TEXT PRIMARY KEY,bvid TEXT NOT NULL,family_id TEXT,record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,cursor TEXT NOT NULL,epoch TEXT NOT NULL,lane TEXT NOT NULL,priority INTEGER NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,next_cursor TEXT,reason TEXT);
                CREATE TABLE IF NOT EXISTS paths(id TEXT PRIMARY KEY,job TEXT NOT NULL,anchor TEXT NOT NULL,proof TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cards(id TEXT,sha TEXT,record TEXT NOT NULL,PRIMARY KEY(id,sha));
                CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY,sha TEXT NOT NULL,record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS access(ref TEXT PRIMARY KEY,note_id TEXT NOT NULL,record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS roles(path TEXT PRIMARY KEY,work TEXT NOT NULL,roles TEXT NOT NULL,evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,record TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "frontier.sqlite3", timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def archive(self, body):
        digest = sha256(body)
        path = self.root / "objects" / (digest + ".bin")
        if path.exists():
            if sha256(path.read_bytes()) != digest:
                raise ValueError("Local evidence object changed; do not silently repair")
        else:
            atomic_write(path, body.decode("utf-8"))
        return digest

    @staticmethod
    def put(db, key, value):
        db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded(value).decode()),
        )

    def stop(self, reason):
        with self.db() as db:
            self.put(db, "stopped", reason)

    def request_stop(self, reason):
        self.stop_requested = reason

    def poll_stop(self):
        if self.stop_requested or (self.root / "STOP").exists():
            self.stop(self.stop_requested or "STOP_file")
        return self.get("stopped")

    def resume(self, reason):
        if not reason.strip():
            raise ValueError("Explicit operator resume reason required")
        if (self.root / "STOP").exists():
            raise ValueError("Remove the operator STOP file before an explicitly authorized resume")
        with self.db() as db:
            self.put(db, "stopped", None)
            self.put(db, "resume_reason", reason)

    def recover(self):
        with self.db() as db:
            return db.execute(
                "UPDATE jobs SET state='uncertain',reason='unknown_inflight_no_automatic_retry' WHERE state='inflight'"
            ).rowcount

    def resolve(self, job, decision, reason):
        if decision not in {"retry", "abandon"} or not reason.strip():
            raise ValueError("Explicit uncertain-job decision and reason required")
        with self.db() as db:
            changed = db.execute(
                "UPDATE jobs SET state=?,reason=? WHERE id=? AND state='uncertain'",
                ("queued" if decision == "retry" else "abandoned", reason, job),
            ).rowcount
        if not changed:
            raise ValueError("Only uncertain jobs can be explicitly resolved")

    def job(self, db, kind, target, proof, anchor, *, cursor=None, epoch="0", lane="head", depth=0):
        proof = proof | {"depth": depth}
        identity = sha256(encoded([kind, target, cursor, epoch]))
        db.execute(
            "INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,'queued',?,NULL,NULL)",
            (
                identity,
                kind,
                target,
                encoded(cursor).decode(),
                str(epoch),
                lane,
                0 if kind == "detail" else 1,
                encoded({"depth": depth}).decode(),
            ),
        )
        path = sha256(encoded([identity, anchor, proof]))
        db.execute(
            "INSERT OR IGNORE INTO paths VALUES(?,?,?,?)",
            (path, identity, anchor, encoded(proof).decode()),
        )
        if kind == "detail":
            work = db.execute("SELECT sha,record FROM works WHERE id=?", (target,)).fetchone()
            if work:
                db.execute(
                    "UPDATE jobs SET state='done',reason='existing_unique_detail' WHERE id=? AND state='queued'",
                    (identity,),
                )
                note = json.loads(work["record"])
                self.role(db, path, target, note, proof)
                if depth < self.get("max_depth", 1):
                    for match in list(LINK.finditer(note.get("description", "")))[:2]:
                        self.enqueue_link(
                            db,
                            match.group(),
                            anchor,
                            {
                                "roles": ["unknown"],
                                "rule": "explicit_note_reference_v1",
                                "parent_note": target,
                                "detail_sha": work["sha"],
                                "locator": "/description",
                                "char_start": match.start(),
                                "char_end_exclusive": match.end(),
                            },
                            depth=depth + 1,
                            lane="history",
                        )
        return identity

    def access_ref(self, db, note_id, access):
        if access.get("url") and note_id_from_url(normalize_note_url(access["url"])) != note_id:
            raise ValueError("Private access belongs to a different note")
        entry = {"note_id": note_id, "access": access}
        ref = sha256(encoded(entry))
        db.execute(
            "INSERT OR IGNORE INTO access VALUES(?,?,?)", (ref, note_id, encoded(entry).decode())
        )
        return ref

    def enqueue_link(self, db, value, anchor, proof, *, depth=0, lane="head"):
        try:
            url = normalize_note_url(value)
            note_id = note_id_from_url(url)
        except ValueError:
            return None
        if not note_id:
            return None  # No invented short-link resolution.
        ref = self.access_ref(db, note_id, {"url": url, "method": "navigate"})
        return self.job(
            db, "detail", note_id, proof | {"access_ref": ref}, anchor, depth=depth, lane=lane
        )

    def ingest(self, database, *, new_limit=20, update_limit=5):
        if (
            type(new_limit) is not int
            or not 1 <= new_limit <= 200
            or type(update_limit) is not int
            or not 0 <= update_limit <= 50
        ):
            raise ValueError("Incremental source batches must be bounded positive row counts")
        database = Path(database).resolve()
        if self.root.is_relative_to(database.parent):
            raise ValueError("Frontier must not write inside source collector data")
        source = str(database)
        if self.get("source_database", source) != source:
            raise ValueError("Use a new frontier for a different Bilibili source")
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            high, update = self.get("new_watermark", 0), self.get("update_watermark", 0)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(explainer_catalog_works)")}
            table = (
                "explainer_catalog_works"
                if {"record_json", "classification_json"} <= columns
                else "observations"
            )
            previous_table = self.get("source_table", "observations" if high else table)
            if previous_table != table:
                raise ValueError(
                    "Source table changed; use a new frontier instead of reusing its watermark"
                )
            assessment = "classification_json" if table == "explainer_catalog_works" else "NULL"
            rows = conn.execute(
                f"SELECT rowid,record_json,{assessment} FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?",
                (high, new_limit),
            ).fetchall()
            old = conn.execute(
                f"SELECT rowid,record_json,{assessment} FROM {table} WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                (update, high, update_limit),
            ).fetchall()
        finally:
            conn.close()
        added = changed = 0
        with self.db() as db:
            for rowid, raw, classification in [*rows, *old]:
                classification = json.loads(classification) if classification else None
                digest = sha256(encoded([raw, classification]))
                if db.execute(
                    "SELECT 1 FROM source_versions WHERE source_row=? AND hash=?", (rowid, digest)
                ).fetchone():
                    continue
                record = json.loads(raw)
                if record.get("platform") == "bilibili" and (
                    classification.get("explanation", {}).get("level") == "explicit_candidate"
                    if classification is not None
                    else record.get("explainer_discovery", {}).get("status") == "strong_candidate"
                ):
                    bvid = record["item_id"]
                    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                        raise ValueError("Invalid source BVID")
                    sha = record["evidence_sha"]
                    if not re.fullmatch(r"[a-f0-9]{64}", sha):
                        raise ValueError("Invalid source evidence hash")
                    obj = database.parent / "objects" / sha[:2] / (sha + ".bin")
                    if obj.stat().st_size > 4 * 1024**2:
                        raise ValueError("Source object exceeds bounded JSON evidence budget")
                    body = obj.read_bytes()
                    if sha256(body) != sha:
                        raise ValueError("Source evidence changed")
                    document = json.loads(body)
                    for field in record["texts"]:
                        if (
                            field["kind"] in {"title", "description"}
                            and pointer(document, field["locator"]) != field["text"]
                        ):
                            raise ValueError("Source original text/pointer changed")
                    self.archive(body)
                    anchor = "bilibili:" + bvid
                    previous = db.execute("SELECT 1 FROM anchors WHERE id=?", (anchor,)).fetchone()
                    added += not bool(previous)
                    changed += bool(previous)
                    family = record.get("family_id")
                    if family is not None:
                        uuid.UUID(family)
                    db.execute(
                        "INSERT INTO anchors VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record,family_id=excluded.family_id",
                        (
                            anchor,
                            bvid,
                            family,
                            encoded(
                                record | {"saved_source_classification": classification}
                            ).decode(),
                        ),
                    )
                    clues = query_clues(record)
                    for clue in clues:
                        self.job(db, "search", clue["query"], clue, anchor, epoch=digest)
                    for field in record["texts"]:
                        if field["kind"] in {"title", "description"}:
                            for match in list(LINK.finditer(field["text"]))[:3]:
                                self.enqueue_link(
                                    db,
                                    match.group(),
                                    anchor,
                                    {
                                        "roles": ["source"],
                                        "rule": "explicit_bili_link_v1",
                                        "evidence_sha": sha,
                                        "locator": field["locator"],
                                        "char_start": match.start(),
                                        "char_end_exclusive": match.end(),
                                    },
                                )
                db.execute("INSERT INTO source_versions VALUES(?,?)", (rowid, digest))
            self.put(db, "source_database", source)
            self.put(db, "source_table", table)
            self.put(db, "new_watermark", rows[-1][0] if rows else high)
            self.put(db, "update_watermark", old[-1][0] if old and old[-1][0] < high else 0)
        return {
            "source_rows_new": len(rows),
            "source_table": table,
            "source_rows_rechecked": len(old),
            "anchors_new": added,
            "anchors_updated": changed,
            "new_watermark": self.get("new_watermark"),
            "update_watermark": self.get("update_watermark"),
        }

    def role(self, db, path, work, note, proof):
        fields = {"/title": note.get("title", ""), "/description": note.get("description", "")}
        target = proof.get("query")
        target_hits = [
            {"locator": loc, "text": target}
            for loc, text in fields.items()
            if target and target in text
        ]
        detail_sha = db.execute("SELECT sha FROM works WHERE id=?", (work,)).fetchone()[0]
        roles, evidence = (
            [],
            {
                "status": "candidate_not_confirmed",
                "origin_confirmed": False,
                "detail_sha": detail_sha,
                "target": target_hits,
                "actions": [],
            },
        )
        if "source" in proof.get("roles", []) and proof.get("rule") in {
            "explicit_bili_link_v1",
            "explicit_quoted_reference_v1",
        }:
            roles.append("source")
        for name, pattern in (("derivative", DERIVATIVE), ("usage", USAGE)):
            actions = [
                {
                    "locator": loc,
                    "text": m.group(),
                    "char_start": m.start(),
                    "char_end_exclusive": m.end(),
                }
                for loc, text in fields.items()
                for m in pattern.finditer(text)
            ]
            if target_hits and actions:
                roles.append(name)
                evidence["actions"].extend(actions)
        db.execute(
            "INSERT OR REPLACE INTO roles VALUES(?,?,?,?)",
            (path, work, encoded(roles or ["unknown"]).decode(), encoded(evidence).decode()),
        )

    def choose(self):
        lane = self.get("next_lane", "head")
        with self.db() as db:
            row = db.execute(
                "SELECT rowid,* FROM jobs WHERE state='queued' ORDER BY CASE lane WHEN ? THEN 0 ELSE 1 END,priority,rowid LIMIT 1",
                (lane,),
            ).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='inflight' WHERE id=?", (row["id"],))
                self.put(db, "next_lane", "history" if row["lane"] == "head" else "head")
        return dict(row) if row else None

    def finish(self, job, result, stats, max_depth):
        with self.db() as db:
            paths = list(db.execute("SELECT * FROM paths WHERE job=?", (job["id"],)))
            outcome = result.get("outcome", "ok")
            if outcome != "ok":
                state = (
                    "blocked"
                    if outcome.startswith("blocked_")
                    else "unavailable"
                    if outcome == "unavailable"
                    else "unsupported"
                )
                db.execute(
                    "UPDATE jobs SET state=?,reason=? WHERE id=?", (state, outcome, job["id"])
                )
                stats[outcome] = stats.get(outcome, 0) + 1
                if state == "blocked":
                    self.put(db, "platform_stop", outcome)
                return
            if job["kind"] == "search":
                for row in result["notes"]:
                    if row.get("content_scope") != "search_card":
                        raise ValueError("Search result must remain search_card, not a full detail")
                    note_id = row["note_id"]
                    if note_id_from_url(normalize_note_url(row["url"])) != note_id:
                        raise ValueError("Search-card ID/URL mismatch")
                    digest = self.archive(encoded(row))
                    stats["search_cards"] += 1
                    ref = self.access_ref(
                        db, note_id, row.get("access") or {"url": row["url"], "method": "navigate"}
                    )
                    normalized = {
                        k: v
                        for k, v in row.items()
                        if k not in {"url", "access", "media", "subtitle_tracks", "evidence"}
                    } | {"url": Note(note_id).canonical_url, "access_ref": ref}
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO cards VALUES(?,?,?)",
                        (note_id, digest, encoded(normalized).decode()),
                    )
                    stats["duplicates"] += inserted.rowcount == 0
                    for path in paths:
                        proof = json.loads(path["proof"])
                        self.job(
                            db,
                            "detail",
                            note_id,
                            proof
                            | {"access_ref": ref, "discovered_by": job["id"], "card_sha": digest},
                            path["anchor"],
                            lane=job["lane"],
                        )
                if not result["notes"]:
                    stats["no_match"] += 1
                next_cursor = result.get("next_cursor")
                if next_cursor is not None:
                    if encoded(next_cursor).decode() == job["cursor"]:
                        db.execute(
                            "UPDATE jobs SET state='stalled',reason='cursor_did_not_advance' WHERE id=?",
                            (job["id"],),
                        )
                        stats["stalled"] += 1
                        return
                    next_id = sha256(encoded(["search", job["target"], next_cursor, job["epoch"]]))
                    previous = db.execute(
                        "SELECT state FROM jobs WHERE id=?", (next_id,)
                    ).fetchone()
                    if previous and previous[0] in {"done", "stalled", "uncertain"}:
                        db.execute(
                            "UPDATE jobs SET state='stalled',reason='cursor_cycle_or_uncertain_target' WHERE id=?",
                            (job["id"],),
                        )
                        stats["stalled"] += 1
                        return
                    for path in paths:
                        self.job(
                            db,
                            "search",
                            job["target"],
                            json.loads(path["proof"]),
                            path["anchor"],
                            cursor=next_cursor,
                            epoch=job["epoch"],
                            lane="history",
                        )
                stats["exhausted"] += bool(result.get("exhausted"))
                if result.get("continuation_unsupported"):
                    stats["unsupported_cursor"] = stats.get("unsupported_cursor", 0) + 1
            else:
                row = result["note"]
                if row.get("note_id") != job["target"] or row.get("content_scope") != "detail":
                    raise ValueError("Full detail must match the exact requested note ID")
                if note_id_from_url(normalize_note_url(row["url"])) != job["target"]:
                    raise ValueError("Detail access URL belongs to another note")
                digest = self.archive(encoded(row))
                ref = self.access_ref(
                    db,
                    row["note_id"],
                    row.get("access") or {"url": row["url"], "method": "navigate"},
                )
                row = {
                    k: v
                    for k, v in row.items()
                    if k not in {"url", "access", "media", "subtitle_tracks", "evidence"}
                } | {"url": Note(row["note_id"]).canonical_url, "access_ref": ref}
                exists = db.execute("SELECT 1 FROM works WHERE id=?", (job["target"],)).fetchone()
                if exists:
                    stats["duplicates"] += 1
                else:
                    stats["unique_details"] += 1
                db.execute(
                    "INSERT INTO works VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET sha=excluded.sha,record=excluded.record",
                    (job["target"], digest, encoded(row).decode()),
                )
                depth = json.loads(job["payload"])["depth"]
                for path in paths:
                    proof = json.loads(path["proof"])
                    self.role(db, path["id"], job["target"], row, proof)
                    path_depth = proof.get("depth", depth)
                    if path_depth < max_depth:
                        for match in list(LINK.finditer(row.get("description", "")))[:2]:
                            self.enqueue_link(
                                db,
                                match.group(),
                                path["anchor"],
                                {
                                    "roles": ["unknown"],
                                    "rule": "explicit_note_reference_v1",
                                    "parent_note": job["target"],
                                    "detail_sha": digest,
                                    "locator": "/description",
                                    "char_start": match.start(),
                                    "char_end_exclusive": match.end(),
                                },
                                depth=path_depth + 1,
                                lane="history",
                            )
            db.execute(
                "UPDATE jobs SET state='done',next_cursor=?,reason=? WHERE id=?",
                (
                    encoded(result.get("next_cursor")).decode(),
                    "continuation_unsupported"
                    if result.get("continuation_unsupported")
                    else "complete_bounded_step",
                    job["id"],
                ),
            )

    async def round(
        self, driver, *, max_jobs=5, source=None, source_batch=20, update_batch=5, max_depth=1
    ):
        with self.db() as db:
            prior_roles = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT j.value,count(*) FROM roles,json_each(roles.roles) j GROUP BY j.value"
                )
            }
        stats = {
            "worker_pid": self.get("worker_pid"),
            "mode": driver.mode,
            "platform_requests": 0,
            "driver_calls": 0,
            "search_cards": 0,
            "unique_details": 0,
            "duplicates": 0,
            "no_match": 0,
            "exhausted": 0,
            "stalled": 0,
            "jobs": [],
            "related_native": "unsupported",
            "at": time.time(),
        }
        with self.db() as db:
            self.put(db, "max_depth", max_depth)
        if self.poll_stop() or self.get("platform_stop"):
            stats["status"] = "stopped"
        else:
            if source:
                try:
                    stats.update(
                        self.ingest(source, new_limit=source_batch, update_limit=update_batch)
                    )
                except Exception as exc:
                    self.stop("source_ingest_fault_requires_review")
                    stats["status"], stats["fault"] = "stopped_uncertain", type(exc).__name__
            for _ in range(max_jobs):
                if self.poll_stop() or self.get("platform_stop"):
                    break
                job = self.choose()
                if not job:
                    break
                try:
                    with self.db() as db:
                        accesses = [
                            json.loads(r[0])
                            for r in db.execute(
                                "SELECT a.record FROM access a JOIN paths p ON json_extract(p.proof,'$.access_ref')=a.ref WHERE p.job=?",
                                (job["id"],),
                            )
                        ]
                    task = asyncio.create_task(driver.read(job, accesses))
                    while not task.done():
                        await asyncio.wait({task}, timeout=0.1)
                        if self.poll_stop():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            raise asyncio.CancelledError
                    result = await task
                    stats["driver_calls"] += 1
                    count = result.get("platform_requests", 0)
                    stats["platform_requests"] = (
                        stats["platform_requests"] + count
                        if count is not None and stats["platform_requests"] is not None
                        else None
                    )
                    self.finish(job, result, stats, max_depth)
                except BaseException as exc:
                    with self.db() as db:
                        db.execute(
                            "UPDATE jobs SET state='uncertain',reason=? WHERE id=?",
                            (type(exc).__name__, job["id"]),
                        )
                    stats["status"], stats["fault"] = "stopped_uncertain", type(exc).__name__
                    self.stop(
                        "worker_interrupted"
                        if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt)
                        else "code_fault_requires_review"
                    )
                    break
                stats["jobs"].append(
                    {
                        "id": job["id"],
                        "kind": job["kind"],
                        "lane": job["lane"],
                        "cursor": json.loads(job["cursor"]),
                    }
                )
            stats.setdefault(
                "status", "platform_blocked" if self.get("platform_stop") else "round_complete"
            )
        with self.db() as db:
            stats["frontier"] = {
                r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM jobs GROUP BY state")
            }
            stats["roles"] = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT j.value,count(*) FROM roles,json_each(roles.roles) j GROUP BY j.value"
                )
            }
            stats["new_role_paths"] = {
                key: max(0, value - prior_roles.get(key, 0))
                for key, value in stats["roles"].items()
            }
            stats["role_unique_works"] = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT j.value,count(DISTINCT work) FROM roles,json_each(roles.roles) j GROUP BY j.value"
                )
            }
            stats["paths"] = db.execute("SELECT count(*) FROM paths").fetchone()[0]
            db.execute("INSERT INTO receipts(record) VALUES(?)", (encoded(stats).decode(),))
            receipt_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        write_json(self.root / "receipts" / f"{receipt_id:08d}.json", stats)
        write_json(
            self.root / "state.json",
            stats
            | {
                "pid": self.get("worker_pid"),
                "stopped": self.get("stopped"),
                "platform_stop": self.get("platform_stop"),
                "new_watermark": self.get("new_watermark", 0),
                "update_watermark": self.get("update_watermark", 0),
            },
        )
        return stats

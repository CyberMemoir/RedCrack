"""Durable action handoff for normal reader heads and offline cursor fixtures.

This is a platform adapter spool, not the central family/task scheduler. Production
search cursor replay remains unsupported; fixture tokens are never sent online.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .export import atomic_write, write_json
from .family_bundle import OUTCOMES, build_bundle, encoded, sha256

PURPOSES = {"find_independent_uses", "find_original_reference", "find_explainer"}
TERMINAL = {"stopped", "cancelled", "blocked_login", "blocked_verification", "gap"}


@contextmanager
def worker_lock(root):
    """OS-released lock: a killed worker cannot strand a stale ownership file."""
    path = root / "worker.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_action(action):
    if (
        action.get("action_type") not in {"search", "detail"}
        or action.get("target_platform") != "xiaohongshu"
    ):
        raise ValueError("This adapter accepts Xiaohongshu search or explicit detail actions")
    if not isinstance(action.get("family_id"), str):
        raise ValueError("A registered family UUID is required; candidate bundles remain separate")
    uuid.UUID(action["family_id"])
    if (
        action.get("purpose") not in PURPOSES
        or not isinstance(action.get("query"), str)
        or not action["query"].strip()
    ):
        raise ValueError("A supported purpose and nonempty query are required")
    if not isinstance(action.get("evidence_refs"), list) or not action["evidence_refs"]:
        raise ValueError("Action must retain nonempty evidence_refs")
    if action.get("query_layer") != "rule_seed" or action.get("is_actual_search_log") is not False:
        raise ValueError("Rule actions are rule_seed, not actual search logs")
    result = {
        key: action[key]
        for key in (
            "action_type",
            "target_platform",
            "family_id",
            "purpose",
            "query",
            "evidence_refs",
            "query_layer",
            "is_actual_search_log",
        )
    } | {"anchors": action.get("anchors", []), "priority": action.get("priority", 0)}
    if "xhs_anchor" in action:
        result["xhs_anchor"] = action["xhs_anchor"]
    return result


class FamilyActions:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, mode TEXT NOT NULL,
                    fixture_sha TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
                    epoch INTEGER NOT NULL DEFAULT 0, cursor TEXT NOT NULL DEFAULT 'null');
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, recipe TEXT NOT NULL, epoch INTEGER NOT NULL,
                    state TEXT NOT NULL, cursor TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(recipe,epoch));
                CREATE TABLE IF NOT EXISTS pages (
                    execution TEXT NOT NULL, cursor TEXT NOT NULL, bundle TEXT NOT NULL,
                    next_cursor TEXT NOT NULL, outcome TEXT NOT NULL,
                    PRIMARY KEY(execution,cursor));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "actions.sqlite3", timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, action, fixture, mode):
        action = validate_action(action)
        if action["action_type"] != "search":
            raise ValueError("Page fixtures only support search; direct detail uses normal reader")
        if mode not in {"head", "history"}:
            raise ValueError("Expected head or history mode")
        body = Path(fixture).read_bytes()
        data = json.loads(body)
        if (
            data.get("schema_name") != "xhs-search-pages-fixture-v1"
            or data.get("sample_mode") != "fixture"
        ):
            raise ValueError("Only explicitly labelled offline page fixtures are supported")
        cursors = set()
        for page in data["pages"]:
            cursor, next_cursor = page["cursor"], page["next_cursor"]
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ValueError("Fixture cursor must be an opaque string or null")
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
                raise ValueError("Fixture next_cursor must be an opaque string or null")
            key = encoded(cursor)
            if key in cursors or page["outcome"] not in OUTCOMES:
                raise ValueError("Duplicate cursor or invalid reader outcome")
            cursors.add(key)
        return self.save_recipe(action, body, mode)

    def save_recipe(self, action, body, mode):
        fixture_sha = sha256(body)
        snapshot = self.root / "fixtures" / (fixture_sha + ".json")
        atomic_write(snapshot, body.decode("utf-8"))
        recipe = sha256(encoded({"action": action, "mode": mode, "fixture": fixture_sha}))
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO recipes(id,action,mode,fixture_sha,state) VALUES(?,?,?,?,'ready')",
                (recipe, encoded(action).decode(), mode, fixture_sha),
            )
        return self.status(recipe)

    def status(self, recipe):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recipes WHERE id=?", (recipe,)).fetchone()
            if row is None:
                raise ValueError("Unknown platform recipe")
            result = dict(row)
            result["action"] = json.loads(result["action"])
            result["cursor"] = json.loads(result["cursor"])
            result["executions"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM executions WHERE recipe=? ORDER BY epoch", (recipe,)
                )
            ]
            result["bundles"] = [
                dict(r)
                for r in db.execute(
                    "SELECT p.* FROM pages p JOIN executions e ON p.execution=e.id WHERE e.recipe=? ORDER BY e.epoch,p.rowid",
                    (recipe,),
                )
            ]
            return result

    def stop(self, recipe, reason, *, cancel=False):
        if not reason.strip():
            raise ValueError("A persistent stop/cancel reason is required")
        self.status(recipe)
        with self.connect() as db:
            db.execute(
                "UPDATE recipes SET state=?,reason=? WHERE id=? AND state!='cancelled'",
                ("cancelled" if cancel else "stopped", reason, recipe),
            )
            db.execute(
                "UPDATE executions SET state='interrupted' WHERE recipe=? AND state IN ('running','partial')",
                (recipe,),
            )
        return self.status(recipe)

    def refresh(self, recipe):
        with worker_lock(self.root):
            current = self.status(recipe)
            if current["mode"] != "head" or current["state"] != "awaiting_refresh":
                raise ValueError("Only completed head runs can refresh; stops/cancels never resume")
            with self.connect() as db:
                db.execute(
                    "UPDATE recipes SET epoch=epoch+1,cursor='null',state='ready',reason=NULL WHERE id=? AND state='awaiting_refresh'",
                    (recipe,),
                )
        return self.status(recipe)

    def _commit_page(self, recipe, execution, cursor, next_cursor, bundle, outcome, exhausted):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?)",
                (execution, cursor, str(bundle.relative_to(self.root)), next_cursor, outcome),
            )
            current = db.execute("SELECT state,mode FROM recipes WHERE id=?", (recipe,)).fetchone()
            state = current["state"]
            if state not in TERMINAL:
                if outcome.startswith("blocked_"):
                    state = outcome
                elif outcome == "failed":
                    state = "gap"
                elif current["mode"] == "head":
                    state = "awaiting_refresh"
                elif exhausted:
                    state = "completed" if outcome == "succeeded" else "gap"
                else:
                    state = "ready"
            db.execute(
                "UPDATE recipes SET cursor=?,state=? WHERE id=?", (next_cursor, state, recipe)
            )
            db.execute(
                "UPDATE executions SET cursor=?,pages=pages+1,state=? WHERE id=?",
                (
                    next_cursor,
                    "interrupted" if state in {"stopped", "cancelled"} else outcome,
                    execution,
                ),
            )

    def run(self, recipe, max_pages=1):
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError("Offline page budget must be 1..10")
        with worker_lock(self.root):
            current = self.status(recipe)
            if current["state"] != "ready":
                return current
            body = (self.root / "fixtures" / (current["fixture_sha"] + ".json")).read_bytes()
            if sha256(body) != current["fixture_sha"]:
                raise ValueError("Saved fixture evidence changed")
            data = json.loads(body)
            pages = {encoded(p["cursor"]).decode(): p for p in data["pages"]}
            execution = sha256(encoded({"recipe": recipe, "epoch": current["epoch"]}))
            with self.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO executions(id,recipe,epoch,state,cursor) VALUES(?,?,?,'running',?)",
                    (execution, recipe, current["epoch"], encoded(current["cursor"]).decode()),
                )
                db.execute("UPDATE executions SET state='running' WHERE id=?", (execution,))
            try:
                for _ in range(max_pages):
                    current = self.status(recipe)
                    if current["state"] != "ready":
                        break
                    cursor = encoded(current["cursor"]).decode()
                    with self.connect() as db:
                        visited = db.execute(
                            "SELECT 1 FROM pages WHERE execution=? AND cursor=?",
                            (execution, cursor),
                        ).fetchone()
                    if visited:
                        with self.connect() as db:
                            db.execute(
                                "UPDATE recipes SET state='gap',reason='cursor_cycle' WHERE id=?",
                                (recipe,),
                            )
                        break
                    if cursor not in pages:
                        with self.connect() as db:
                            db.execute(
                                "UPDATE recipes SET state='gap',reason='missing_fixture_cursor' WHERE id=?",
                                (recipe,),
                            )
                            db.execute(
                                "UPDATE executions SET state='failed' WHERE id=?", (execution,)
                            )
                        break
                    page = pages[cursor]
                    folder = self.root / "runs" / execution / sha256(cursor.encode())
                    source = folder / "results.json"
                    write_json(
                        source, {"notes": page["notes"], "complete": page["outcome"] == "succeeded"}
                    )
                    bundle = folder / "bundle"
                    manifest = build_bundle(
                        [source],
                        bundle,
                        source_id="xhs-action-" + recipe,
                        outcome=page["outcome"],
                        sample_mode="fixture",
                        action=current["action"]
                        | {"execution_id": execution, "mode": current["mode"]},
                    )
                    next_cursor = encoded(page["next_cursor"]).decode()
                    # Commit cursor only after the immutable, verifiable package exists.
                    self._commit_page(
                        recipe,
                        execution,
                        cursor,
                        next_cursor,
                        bundle,
                        manifest["outcome"],
                        page["next_cursor"] is None,
                    )
                    if self.status(recipe)["state"] != "ready":
                        break
                    if next_cursor == cursor:
                        with self.connect() as db:
                            db.execute(
                                "UPDATE recipes SET state='gap',reason='cursor_did_not_advance' WHERE id=?",
                                (recipe,),
                            )
                        break
            except KeyboardInterrupt:
                self.stop(recipe, "worker_interrupted")
                raise
        return self.status(recipe)


async def action_cli(args):
    engine = FamilyActions(args.state_dir)
    if args.operation == "enqueue":
        if args.reader_profile:
            from .family_reader import enqueue_reader

            return enqueue_reader(
                engine, json.loads(Path(args.action).read_text(encoding="utf-8")), args
            )
        return engine.enqueue(
            json.loads(Path(args.action).read_text(encoding="utf-8")), args.fixture, args.mode
        )
    if args.operation == "run":

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            current = engine.status(args.recipe_id)
            descriptor = json.loads(
                (engine.root / "fixtures" / (current["fixture_sha"] + ".json")).read_text(
                    encoding="utf-8"
                )
            )
            if descriptor.get("schema_name") == "xhs-normal-reader-job-v1":
                from .family_reader import run_reader

                return await run_reader(engine, args.recipe_id)
            return engine.run(args.recipe_id, args.max_pages)
        finally:
            signal.signal(signal.SIGTERM, previous)
    if args.operation == "refresh":
        return engine.refresh(args.recipe_id)
    if args.operation in {"stop", "cancel"}:
        return engine.stop(args.recipe_id, args.reason, cancel=args.operation == "cancel")
    return engine.status(args.recipe_id)

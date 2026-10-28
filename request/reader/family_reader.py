"""Bounded normal reader execution behind the existing durable family action CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .access import AccessPause
from .browser import XHSReader
from .cache import NoteCache
from .errors import LoginRequiredError, ReaderError, VerificationRequiredError
from .export import write_json
from .family_actions import TERMINAL, validate_action, worker_lock
from .family_bundle import build_bundle, encoded, sha256
from .models import Note, normalize_note_url, note_id_from_url


def enqueue_reader(engine, action, args):
    action = validate_action(action)
    if args.mode != "head":
        raise ValueError(
            "Real history unsupported: no evidenced restartable search/session cursor contract"
        )
    if action["action_type"] == "detail":
        note_id = note_id_from_url(normalize_note_url(action["query"]))
        if not note_id or action["query"] != Note(note_id).canonical_url or args.detail_limit < 1:
            raise ValueError(
                "Direct detail requires an explicit canonical note URL and detail budget"
            )
    if (
        not 1 <= args.limit <= 5
        or not 0 <= args.detail_limit <= args.limit
        or not 0 <= args.max_scrolls <= 2
    ):
        raise ValueError("Reader head budget: limit 1..5, details 0..limit, scrolls 0..2")
    if args.chrome_data_dir and not args.connect_existing:
        raise ValueError("chrome-data-dir requires connect-existing")
    descriptor = {
        "schema_name": "xhs-normal-reader-job-v1",
        "profile": str(Path(args.reader_profile).expanduser().resolve()),
        "connect_existing": args.connect_existing,
        "chrome_data_dir": args.chrome_data_dir,
        "limit": args.limit,
        "detail_limit": args.detail_limit,
        "max_scrolls": args.max_scrolls,
    }
    source_ref = action.get("xhs_anchor", {}).get("source_access_ref")
    if source_ref:
        mapping = json.loads(
            (Path(args.action).resolve().parent / "private/source-access.json").read_text(
                encoding="utf-8"
            )
        )
        entry = mapping[source_ref]
        if (
            sha256(encoded(entry)) != source_ref
            or entry["note_id"] != note_id_from_url(action["query"])
            or note_id_from_url(normalize_note_url(entry["access"]["url"])) != entry["note_id"]
        ):
            raise ValueError(
                "Explicit source access reference has changed or belongs to another note"
            )
        descriptor["source_access"] = entry
    return engine.save_recipe(action, encoded(descriptor), "head")


async def controlled(awaitable, engine, recipe):
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            if engine.status(recipe)["state"] in TERMINAL:
                raise asyncio.CancelledError
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def bind_access(row, access_map):
    note_id = row["note_id"]
    if note_id_from_url(row["url"]) != note_id:
        raise ReaderError("Search observation URL does not match note ID")
    access = row.get("access") or {"url": row["url"], "method": "navigate"}
    if access.get("url") and note_id_from_url(access["url"]) != note_id:
        raise ReaderError("Private access URL does not match note ID")
    ref = sha256(encoded({"note_id": note_id, "access": access}))
    access_map[ref] = {"note_id": note_id, "access": access}
    return row | {"access_ref": ref}


async def run_reader(engine, recipe, *, reader_factory=XHSReader, sample_mode="live"):
    with worker_lock(engine.root):
        current = engine.status(recipe)
        if current["state"] != "ready":
            return current
        body = (engine.root / "fixtures" / (current["fixture_sha"] + ".json")).read_bytes()
        if sha256(body) != current["fixture_sha"]:
            raise ValueError("Reader descriptor changed")
        spec = json.loads(body)
        execution = sha256(encoded({"recipe": recipe, "epoch": current["epoch"]}))
        folder = engine.root / "runs" / execution / "reader-head"
        checkpoint = folder / "private/checkpoint.json"
        state = (
            json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint.exists()
            else {
                "search_complete": False,
                "cards": [],
                "notes": {},
                "detail_done": [],
                "access_map": {},
                "finished": False,
                "outcome": "partial",
                "end_reason": None,
            }
        )

        def save():
            write_json(checkpoint, state)
            write_json(folder / "private/access.json", state["access_map"])

        with engine.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO executions(id,recipe,epoch,state,cursor) VALUES(?,?,?,'running','null')",
                (execution, recipe, current["epoch"]),
            )
            db.execute("UPDATE executions SET state='running' WHERE id=?", (execution,))
        if not state["finished"]:
            reader = None
            try:
                # No reader construction, browser connection or cache bypass before this gate.
                AccessPause(Path(spec["profile"])).check()
                reader = reader_factory(
                    profile_dir=spec["profile"],
                    connect_existing=spec["connect_existing"],
                    chrome_data_dir=spec["chrome_data_dir"],
                    refresh=True,
                    evidence_dir=folder / "private/evidence",
                )
                async with reader:
                    if current["action"]["action_type"] == "detail":
                        wanted = note_id_from_url(current["action"]["query"])
                        if wanted not in state["detail_done"]:
                            if spec.get("source_access"):
                                entry = spec["source_access"]
                                reader.cache.put(
                                    "access",
                                    wanted,
                                    [
                                        Note(
                                            wanted,
                                            url=entry["access"]["url"],
                                            access=entry["access"],
                                        )
                                    ],
                                )
                            detail = await controlled(
                                reader.get_note(current["action"]["query"]), engine, recipe
                            )
                            if detail.note_id != wanted or detail.content_scope != "detail":
                                raise ReaderError("Explicit reference detail ID/scope mismatch")
                            state["notes"][wanted] = bind_access(
                                detail.to_dict(), state["access_map"]
                            )
                            state["detail_done"].append(wanted)
                            state["search_complete"], state["end_reason"] = (
                                True,
                                "explicit_reference_complete",
                            )
                            save()
                    elif not state["search_complete"]:

                        def progress(notes):
                            state["cards"] = [
                                bind_access(n.to_dict(), state["access_map"]) for n in notes
                            ]
                            state["notes"].update((r["note_id"], r) for r in state["cards"])
                            save()

                        notes = await controlled(
                            reader.search(
                                current["action"]["query"],
                                limit=spec["limit"],
                                max_scrolls=spec["max_scrolls"],
                                on_progress=progress,
                            ),
                            engine,
                            recipe,
                        )
                        progress(notes)
                        state["search_complete"] = True
                        state["end_reason"] = reader.list_end_reason
                        save()
                    for card in state["cards"][: spec["detail_limit"]]:
                        if card["note_id"] in state["detail_done"]:
                            continue
                        entry = state["access_map"][card["access_ref"]]
                        if entry["note_id"] != card["note_id"] or card["access_ref"] != sha256(
                            encoded(entry)
                        ):
                            raise ReaderError("Saved private access identity or hash changed")
                        if bind_access(card, {})["access_ref"] != card["access_ref"]:
                            raise ReaderError(
                                "Saved card access no longer matches its private reference"
                            )
                        reader.cache.put(
                            "access",
                            card["note_id"],
                            [
                                NoteCache.restore(
                                    {k: v for k, v in card.items() if k != "access_ref"}
                                )
                            ],
                        )
                        detail = await controlled(reader.get_note(card["url"]), engine, recipe)
                        if detail.note_id != card["note_id"] or detail.content_scope != "detail":
                            raise ReaderError(
                                "Reader returned a different ID or incomplete detail scope"
                            )
                        state["notes"][detail.note_id] = bind_access(
                            detail.to_dict(), state["access_map"]
                        )
                        state["detail_done"].append(detail.note_id)
                        save()
                    state["outcome"] = (
                        "succeeded"
                        if state["end_reason"] in {"entry_exhausted", "explicit_reference_complete"}
                        else "partial"
                    )
            except VerificationRequiredError:
                state["outcome"], state["end_reason"] = (
                    "blocked_verification",
                    "reader_verification_stop",
                )
            except LoginRequiredError:
                state["outcome"], state["end_reason"] = "blocked_login", "reader_login_stop"
            except (asyncio.CancelledError, KeyboardInterrupt):
                engine.stop(recipe, "worker_interrupted_or_stopped")
                state["outcome"], state["end_reason"] = "partial", "worker_stopped"
            except (ReaderError, OSError, ValueError) as exc:
                state["outcome"], state["end_reason"] = "failed", type(exc).__name__
            state["finished"] = True
            save()
        source = folder / "results.json"
        write_json(
            source,
            {"notes": list(state["notes"].values()), "complete": state["outcome"] == "succeeded"},
        )
        bundle = folder / "bundle"
        manifest = build_bundle(
            [source],
            bundle,
            source_id="xhs-action-" + recipe,
            outcome=state["outcome"],
            sample_mode=sample_mode,
            action=current["action"] | {"execution_id": execution, "mode": "head"},
        )
        engine._commit_page(recipe, execution, "null", "null", bundle, manifest["outcome"], True)
        with engine.connect() as db:
            db.execute(
                "UPDATE recipes SET reason=? WHERE id=? AND state NOT IN ('stopped','cancelled')",
                (state["end_reason"], recipe),
            )
        return engine.status(recipe)

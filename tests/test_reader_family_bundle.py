"""Catch real contract risks: time-unit drift, evidence leakage and lost checkpoints."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from request.reader.family_actions import FamilyActions
from request.reader.family_bundle import build_bundle, sha256

FIXTURES = Path(__file__).parent / "fixtures"


class FamilyBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pages = json.loads((FIXTURES / "family_pages.json").read_text(encoding="utf-8"))
        self.action = json.loads((FIXTURES / "family_action.json").read_text(encoding="utf-8"))

    def source(self, notes):
        path = self.root / "note-input.json"
        path.write_text(json.dumps({"notes": notes}, ensure_ascii=False), encoding="utf-8")
        return path

    def export(self, source, name="bundle", **kwargs):
        return build_bundle(
            [source],
            self.root / name,
            source_id="fixture-xhs",
            outcome="succeeded",
            sample_mode="fixture",
            **kwargs,
        )

    def test_exact_text_sha_explicit_seconds_milliseconds_date_and_unknown(self):
        source = self.source([n for page in self.pages["pages"] for n in page["notes"]])
        manifest = self.export(source)
        directory = self.root / "bundle"
        payload = (directory / "records.jsonl").read_bytes()
        self.assertEqual(sha256(payload), manifest["records_sha256"])
        records = {r["item_id"]: r for r in map(json.loads, payload.splitlines())}
        for record in records.values():
            raw = (directory / "objects" / (record["evidence_sha"] + ".bin")).read_bytes()
            self.assertEqual(sha256(raw), record["evidence_sha"])
            document = json.loads(raw)
            for text in record["texts"]:
                self.assertEqual(document[text["locator"][1:]], text["text"])
        first, second, third, fourth = [records[f"{i:024x}"] for i in range(1, 5)]
        self.assertEqual(
            first["normalized_published_time"]["start_at"],
            second["normalized_published_time"]["start_at"],
        )
        self.assertEqual(second["normalized_published_time"]["precision"], "millisecond")
        self.assertEqual(third["normalized_published_time"]["precision"], "day")
        self.assertEqual(
            third["normalized_published_time"]["source_evidence"]["raw_value"], "2020-01-02"
        )
        self.assertNotIn("normalized_published_time", fourth)
        before = {str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        self.assertEqual(manifest, self.export(source))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        )
        (directory / "records.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.export(source)

    def test_no_guessed_observation_date_or_private_access_fields(self):
        notes = copy.deepcopy(self.pages["pages"][0]["notes"])
        notes[0]["observed_at"] = None
        notes[1].pop("publication_time")
        notes[1]["access"] = {"cookie": "secret", "url": "https://cdn.invalid?token=secret"}
        notes[1]["description"] = (
            "https://www.xiaohongshu.com/explore/000000000000000000000001?xsec_token=secret"
        )
        manifest = self.export(self.source(notes))
        self.assertEqual(manifest["outcome"], "partial")
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["gaps"]["missing_observed_at_not_fabricated"], 1)
        for path in (self.root / "bundle").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"secret", path.read_bytes())
        record = json.loads((self.root / "bundle/records.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("normalized_published_time", record)

    def test_pause_overrides_success_without_changing_pause(self):
        pause = self.root / "pause.json"
        content = b'{"status":"blocked_verification","reason":"historical warning"}'
        pause.write_bytes(content)
        manifest = self.export(self.source([]), pause_record=pause)
        self.assertEqual(manifest["outcome"], "blocked_verification")
        self.assertEqual(pause.read_bytes(), content)

    def test_reader_task_stop_is_not_overridden_by_requested_success(self):
        source = self.source([])
        (source.parent / "task.json").write_text(
            '{"status":"verification_required"}', encoding="utf-8"
        )
        self.assertEqual(self.export(source)["outcome"], "blocked_verification")

    def test_history_uses_opaque_saved_cursor_and_restart_reuses_same_execution(self):
        engine = FamilyActions(self.root / "state")
        recipe = engine.enqueue(self.action, FIXTURES / "family_pages.json", "history")["id"]
        first = engine.run(recipe)
        self.assertEqual(first["cursor"], "fixture:opaque-next-token")
        self.assertEqual(first["executions"][0]["state"], "partial")
        resumed = FamilyActions(self.root / "state").run(recipe)
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(len(resumed["executions"]), 1)
        self.assertEqual(len(resumed["bundles"]), 2)
        self.assertEqual(resumed, FamilyActions(self.root / "state").run(recipe))

    def test_crash_between_bundle_publish_and_cursor_commit_replays_without_duplicates(self):
        engine = FamilyActions(self.root / "state")
        recipe = engine.enqueue(self.action, FIXTURES / "family_pages.json", "history")["id"]
        with patch.object(
            engine, "_commit_page", side_effect=RuntimeError("simulated process loss")
        ):
            with self.assertRaises(RuntimeError):
                engine.run(recipe)
        self.assertIsNone(engine.status(recipe)["cursor"])
        bundles = list((engine.root / "runs").glob("*/*/bundle/manifest.json"))
        self.assertEqual(len(bundles), 1)
        before = bundles[0].read_bytes()
        recovered = FamilyActions(engine.root).run(recipe)
        self.assertEqual(bundles[0].read_bytes(), before)
        self.assertEqual(len(recovered["bundles"]), 1)
        self.assertEqual(recovered["executions"][0]["pages"], 1)

    def test_head_refresh_changes_execution_and_cancel_survives_enqueue_restart(self):
        engine = FamilyActions(self.root / "state")
        recipe = engine.enqueue(self.action, FIXTURES / "family_pages.json", "head")["id"]
        first = engine.run(recipe)
        engine.refresh(recipe)
        second = FamilyActions(engine.root).run(recipe)
        self.assertNotEqual(first["executions"][0]["id"], second["executions"][1]["id"])
        engine.stop(recipe, "user cancelled", cancel=True)
        restarted = FamilyActions(engine.root)
        self.assertEqual(
            restarted.enqueue(self.action, FIXTURES / "family_pages.json", "head")["state"],
            "cancelled",
        )
        self.assertEqual(len(restarted.run(recipe)["executions"]), 2)
        with self.assertRaises(ValueError):
            restarted.refresh(recipe)

    def test_blocked_and_persistent_stop_cannot_refresh_or_continue(self):
        self.pages["pages"][0]["outcome"] = "blocked_verification"
        path = self.root / "blocked.json"
        path.write_text(json.dumps(self.pages, ensure_ascii=False), encoding="utf-8")
        engine = FamilyActions(self.root / "state")
        recipe = engine.enqueue(self.action, path, "history")["id"]
        blocked = engine.run(recipe, 2)
        self.assertEqual(blocked["state"], "blocked_verification")
        self.assertEqual(len(FamilyActions(engine.root).run(recipe)["bundles"]), 1)
        other = engine.enqueue(self.action, FIXTURES / "family_pages.json", "history")["id"]
        engine.stop(other, "stop before run")
        self.assertEqual(FamilyActions(engine.root).run(other)["executions"], [])

    def test_concurrent_stop_after_bundle_publish_is_not_overwritten_by_checkpoint(self):
        engine = FamilyActions(self.root / "state")
        recipe = engine.enqueue(self.action, FIXTURES / "family_pages.json", "history")["id"]
        original = engine._commit_page

        def stop_then_commit(*args):
            FamilyActions(engine.root).stop(recipe, "stop during output")
            original(*args)

        with patch.object(engine, "_commit_page", side_effect=stop_then_commit):
            result = engine.run(recipe, 2)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["executions"][0]["state"], "interrupted")
        self.assertEqual(len(result["bundles"]), 1)


if __name__ == "__main__":
    unittest.main()

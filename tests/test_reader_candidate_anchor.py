import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from request.reader.candidate_anchor import candidate_bundle
from request.reader.family_actions import validate_action


class CandidateAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "bili-data"
        self.data.mkdir()
        self.database = self.data / "collection.sqlite3"
        body = json.dumps(
            {"data": {"title": "合成表达是什么梗", "description": "原样简介"}}, ensure_ascii=False
        ).encode()
        self.sha = hashlib.sha256(body).hexdigest()
        obj = self.data / "objects" / self.sha[:2] / (self.sha + ".bin")
        obj.parent.mkdir(parents=True)
        obj.write_bytes(body)
        row = {
            "platform": "bilibili",
            "item_id": "BV1234567890",
            "evidence_sha": self.sha,
            "observed_at": "2026-10-29T00:00:00+00:00",
            "published_at_raw": None,
            "texts": [{"kind": "title", "text": "合成表达是什么梗", "locator": "/data/title"}],
            "analysis": {"title_subject_candidate": "合成表达"},
            "explainer_discovery": {"status": "strong_candidate"},
        }
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE observations(record_json TEXT)")
            db.executemany(
                "INSERT INTO observations VALUES(?)", [(json.dumps(row),), (json.dumps(row),)]
            )

    def test_reads_metadata_without_modifying_source_or_creating_family(self):
        before = self.database.read_bytes()
        out = self.root / "candidate"
        result = candidate_bundle(self.database, out, 5)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["formal_family_count"], 0)
        row = json.loads((out / "candidates.jsonl").read_text(encoding="utf-8"))
        self.assertIsNone(row["family_id"])
        self.assertEqual(row["candidate_id"], "bilibili:BV1234567890")
        self.assertEqual(row["evidence_refs"][0]["locator"], "/data/title")
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(
            hashlib.sha256((out / "objects" / (self.sha + ".bin")).read_bytes()).hexdigest(),
            self.sha,
        )

    def test_changed_raw_text_is_rejected_before_any_package_write(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            row = json.loads(
                db.execute("SELECT record_json FROM observations LIMIT 1").fetchone()[0]
            )
            row["texts"][0]["text"] = "伪造标题"
            db.execute("UPDATE observations SET record_json=?", (json.dumps(row),))
        out = self.root / "candidate"
        with self.assertRaisesRegex(ValueError, "pointer changed"):
            candidate_bundle(self.database, out)
        self.assertFalse(out.exists())

    def test_output_cannot_be_inside_live_collector_data(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            candidate_bundle(self.database, self.data / "candidate")

    def test_unregistered_candidate_cannot_be_enqueued_as_a_formal_family(self):
        with self.assertRaisesRegex(ValueError, "registered family"):
            validate_action(
                {
                    "action_type": "search",
                    "target_platform": "xiaohongshu",
                    "family_id": None,
                    "candidate_id": "bilibili:BV1234567890",
                    "purpose": "find_independent_uses",
                    "query": "合成表达",
                    "evidence_refs": [{"fixture": True}],
                    "query_layer": "rule_seed",
                    "is_actual_search_log": False,
                }
            )

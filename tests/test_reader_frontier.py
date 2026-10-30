import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from request.reader.family_actions import worker_lock
from request.reader.frontier import Frontier, clean
from request.reader.frontier_driver import NormalReaderDriver, ReplayDriver
from tests.fixtures.create_frontier_demo import A, C, create, source


class FrontierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = create(self.root)
        self.engine = Frontier(self.root / "frontier")
        self.driver = ReplayDriver(self.root / "replay.json")

    async def step(self, **kwargs):
        return await self.engine.round(
            self.driver, source=self.database, source_batch=1, update_batch=1, max_jobs=2, **kwargs
        )

    async def test_multi_round_ingest_cursor_fairness_unique_details_and_multiple_paths(self):
        first = await self.step()
        self.assertEqual([j["lane"] for j in first["jobs"]], ["head", "history"])
        await self.step()
        third = await self.step()
        self.assertGreaterEqual(third["duplicates"], 1)
        with self.engine.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM anchors").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 2)
            self.assertEqual(
                db.execute(
                    "SELECT count(DISTINCT anchor) FROM paths p JOIN jobs j ON p.job=j.id WHERE j.target=?",
                    (A,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM anchors WHERE family_id IS NOT NULL").fetchone()[
                    0
                ],
                1,
            )
            roles = [
                json.loads(r[0]) for r in db.execute("SELECT roles FROM roles WHERE work=?", (A,))
            ]
            self.assertTrue(any("derivative" in r and "usage" in r for r in roles))
            self.assertNotIn(
                "synthetic-token",
                db.execute("SELECT record FROM works WHERE id=?", (A,)).fetchone()[0],
            )
        calls = [call for call in self.driver.calls if call[0] == "detail" and call[1] == A]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(c[2] == "fixture-only-opaque-next" for c in self.driver.calls))
        self.assertEqual(clean("【梗百科】<em>合成甲</em>是什么梗"), "合成甲是什么梗")

    async def test_new_rows_and_in_place_updates_are_bounded_and_unavailable_is_not_detail(self):
        for _ in range(3):
            await self.step()
        source(self.root, 2, "合成丙是什么梗", "明确链接 https://www.xiaohongshu.com/explore/" + C)
        receipt = await self.step()
        self.assertEqual(receipt["anchors_new"], 1)
        source(self.root, 0, "合成乙是什么梗", "新描述", replace_row=1)
        updates = []
        for _ in range(5):
            updates.append(await self.step())
        self.assertTrue(any(r.get("anchors_updated", 0) for r in updates))
        self.assertTrue(any(r["no_match"] for r in updates))
        self.assertTrue(all(r["source_rows_rechecked"] <= 1 for r in updates))
        with self.engine.db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM works WHERE id=?", (C,)).fetchone())
            self.assertEqual(
                db.execute(
                    "SELECT state FROM jobs WHERE target=? AND kind='detail'", (C,)
                ).fetchone()[0],
                "unavailable",
            )
        with closing(sqlite3.connect(self.database)) as db, db:
            row = json.loads(
                db.execute("SELECT record_json FROM observations WHERE rowid=1").fetchone()[0]
            )
            row["observed_at"] = "2026-10-30T00:00:00+00:00"
            db.execute("UPDATE observations SET record_json=? WHERE rowid=1", (json.dumps(row),))
        for _ in range(4):
            await self.step()
        with self.engine.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(DISTINCT epoch) FROM jobs WHERE kind='search' AND target='合成乙'"
                ).fetchone()[0],
                2,
            )

    async def test_stop_restart_and_unknown_inflight_never_blindly_retry(self):
        self.engine.ingest(self.database, new_limit=1, update_limit=1)
        job = self.engine.choose()
        restarted = Frontier(self.engine.root)
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(restarted.recover(), 0)
        await restarted.round(self.driver, max_jobs=1)
        with restarted.db() as db:
            self.assertEqual(
                db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0],
                "uncertain",
            )
        restarted.stop("user_stop")
        count = len(self.driver.calls)
        receipt = await Frontier(restarted.root).round(self.driver)
        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(len(self.driver.calls), count)

    async def test_account_pause_is_checked_before_any_normal_reader_construction(self):
        profile = self.root / "profile"
        profile.mkdir()
        pause = profile / "reader-access-pause.json"
        pause.write_text('{"reason":"device limit user report"}', encoding="utf-8")
        before = pause.read_bytes()
        factory = Mock(side_effect=AssertionError("no browser"))
        driver = NormalReaderDriver(profile, self.root / "evidence", reader_factory=factory)
        self.engine.ingest(self.database, new_limit=1, update_limit=1)
        receipt = await self.engine.round(driver, max_jobs=2)
        self.assertEqual(receipt["status"], "platform_blocked")
        self.assertEqual(receipt["platform_requests"], 0)
        factory.assert_not_called()
        self.assertEqual(pause.read_bytes(), before)

    async def test_wrong_detail_id_stops_as_uncertain_and_preserves_no_false_work(self):
        self.driver.data["details"][A]["note_id"] = C
        receipt = await self.step()
        self.assertEqual(receipt["status"], "stopped_uncertain")
        self.assertEqual(receipt["fault"], "ValueError")
        with self.engine.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 0)

    async def test_single_worker_lock_excludes_a_second_instance(self):
        with worker_lock(self.engine.root), self.assertRaises(OSError):
            with worker_lock(self.engine.root):
                pass

    async def test_saved_catalog_classification_updates_are_consumed_without_reclassification(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            raw = db.execute("SELECT record_json FROM observations LIMIT 1").fetchone()[0]
            db.execute(
                "CREATE TABLE explainer_catalog_works(record_json TEXT,classification_json TEXT)"
            )
            db.execute(
                "INSERT INTO explainer_catalog_works VALUES(?,?)",
                (raw, '{"explanation":{"level":"not_indicated"}}'),
            )
        first = self.engine.ingest(self.database, new_limit=1, update_limit=1)
        self.assertEqual(first["source_table"], "explainer_catalog_works")
        self.assertEqual(first["anchors_new"], 0)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "UPDATE explainer_catalog_works SET classification_json=?",
                ('{"explanation":{"level":"explicit_candidate"}}',),
            )
        second = self.engine.ingest(self.database, new_limit=1, update_limit=1)
        self.assertEqual(second["anchors_new"], 1)

    async def test_stagnant_replay_cursor_is_recorded_instead_of_looping(self):
        self.driver.data["searches"][1]["next_cursor"] = "fixture-only-opaque-next"
        receipts = [await self.step() for _ in range(4)]
        self.assertTrue(any(r["stalled"] for r in receipts))
        with self.engine.db() as db:
            self.assertGreater(
                db.execute("SELECT count(*) FROM jobs WHERE state='stalled'").fetchone()[0], 0
            )

"""Synthetic payloads exercise source-backed logic, never claim live witnesses."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from request.reader.frontier import Frontier
from request.reader.frontier_driver import NativeSearchDriver, ReplayDriver
from request.reader.native_search import search_page, search_request
from tests.fixtures.create_frontier_demo import A, B, C, create, note


def body(page=1):
    return {
        "keyword": "合成甲",
        "page": page,
        "page_size": 20,
        "search_id": "synthetic-session",
        "sort": "general",
        "note_type": 0,
        "ext_flags": [],
        "geo": "",
        "image_formats": ["jpg", "webp", "avif"],
        "captured_extra": {"filter": "keep-me"},
    }


def payload(ids, more):
    return {
        "success": True,
        "data": {
            "items": [{"id": i, "note_card": {"note_id": i, "title": "合成甲"}} for i in ids],
            "has_more": more,
        },
    }


class NativeFrontierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = create(self.root)
        self.engine = Frontier(self.root / "frontier")
        self.driver = ReplayDriver(self.root / "replay.json")

    async def test_native_parameters_persist_between_rounds_and_stop_restart(self):
        first = search_page("合成甲", body(), payload([A], True))
        self.assertEqual(first["next_cursor"]["request_body"], body(2))
        self.assertEqual(body()["page"], 1)
        self.driver.data["searches"] = [
            {
                "query": "合成甲",
                "cursor": None,
                "native_request": body(),
                "native_response": payload([A], True),
            },
            {
                "query": "合成甲",
                "cursor": first["next_cursor"],
                "native_request": body(2),
                "native_response": payload([B], False),
            },
        ]
        with self.engine.db() as db:
            self.engine.job(db, "search", "合成甲", {"roles": ["unknown"]}, "anchor")
        receipt = await self.engine.round(self.driver, max_jobs=1)
        self.assertEqual(receipt["platform_requests"], 0)
        restarted = Frontier(self.engine.root)
        (restarted.root / "STOP").write_text("operator", encoding="utf-8")
        self.assertEqual((await restarted.round(self.driver))["status"], "stopped")
        (restarted.root / "STOP").unlink()
        self.assertEqual((await Frontier(restarted.root).round(self.driver))["status"], "stopped")
        restarted.resume("explicit offline test resume")
        for _ in range(3):
            await restarted.round(self.driver, max_jobs=1)
        self.assertEqual(
            [c[2] for c in self.driver.calls if c[0] == "search"], [None, first["next_cursor"]]
        )
        with restarted.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT count(*) FROM jobs WHERE reason='entry_exhausted'").fetchone()[
                    0
                ],
                1,
            )

    async def test_repeated_page_and_empty_no_progress_terminate_even_if_page_advances(self):
        for empty, expected in ((False, "repeated_page"), (True, "no_progress_empty_page")):
            engine = Frontier(self.root / str(empty))
            first = search_page("合成甲", body(), payload([A], True))
            self.driver.data["searches"] = [
                {
                    "query": "合成甲",
                    "cursor": None,
                    "native_request": body(),
                    "native_response": payload([A], True),
                },
                {
                    "query": "合成甲",
                    "cursor": first["next_cursor"],
                    "native_request": body(2),
                    "native_response": payload([] if empty else [A], True),
                },
            ]
            with engine.db() as db:
                engine.job(db, "search", "合成甲", {}, "anchor")
            for _ in range(3):
                await engine.round(self.driver, max_jobs=1)
            with engine.db() as db:
                self.assertEqual(
                    db.execute("SELECT reason FROM jobs WHERE state='stalled'").fetchone()[0],
                    expected,
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM jobs WHERE kind='search'").fetchone()[0], 2
                )

    async def test_related_multiple_parents_and_late_anchor_reuses_detail(self):
        self.driver.related_enabled = True
        self.driver.data["details"][A] = note(A, detail=True)
        self.driver.data["details"][C] = note(C, detail=True)
        self.driver.data["related"] = [
            {
                "parent_note": parent,
                "cursor": None,
                "notes": [note(B)],
                "next_cursor": None,
                "exhausted": True,
            }
            for parent in (A, C)
        ]
        with self.engine.db() as db:
            for parent in (A, C):
                self.engine.job(db, "detail", parent, {}, "anchor-1")
        for _ in range(5):
            await self.engine.round(self.driver, max_jobs=1)
        # A new BVID path arrives after both detail and related page are cached.
        with self.engine.db() as db:
            self.engine.job(db, "detail", A, {}, "anchor-2")
        for _ in range(2):
            await self.engine.round(self.driver, max_jobs=1)
        with self.engine.db() as db:
            proofs = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT proof FROM paths p JOIN jobs j ON p.job=j.id WHERE j.kind='detail' AND j.target=?",
                    (B,),
                )
            ]
            self.assertEqual({p["parent_note"] for p in proofs}, {A, C})
            self.assertEqual(
                db.execute(
                    "SELECT count(DISTINCT anchor) FROM paths p JOIN jobs j ON p.job=j.id WHERE j.target=? AND j.kind='detail'",
                    (B,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 3)
        self.assertEqual(sum(c[:2] == ("detail", B) for c in self.driver.calls), 1)
        self.assertEqual(sum(c[:2] == ("related", A) for c in self.driver.calls), 1)

    async def test_related_parent_mismatch_is_uncertain_without_creating_child_work(self):
        self.driver.related_enabled = True
        self.driver.data["related"] = [
            {"parent_note": A, "cursor": None, "notes": [note(B)], "exhausted": True}
        ]

        # Deliberately wrong parent emitted by an adapter, not a matching replay lookup.
        async def wrong(job, accesses):
            return {"parent_note": C, "notes": [note(B)], "platform_requests": 0}

        self.driver.read = wrong
        with self.engine.db() as db:
            self.engine.job(db, "related", A, {}, "anchor")
        receipt = await self.engine.round(self.driver, max_jobs=1)
        self.assertEqual(receipt["status"], "stopped_uncertain")
        with self.engine.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 0)

    async def test_late_related_path_reaches_cached_second_page_without_refetch(self):
        self.driver.related_enabled = True
        self.driver.data["related"] = [
            {
                "parent_note": A,
                "cursor": None,
                "notes": [note(B)],
                "next_cursor": "synthetic-related-page-2",
                "exhausted": False,
            },
            {
                "parent_note": A,
                "cursor": "synthetic-related-page-2",
                "notes": [note(C)],
                "next_cursor": None,
                "exhausted": True,
            },
        ]
        self.driver.data["details"][C] = note(C, detail=True)
        with self.engine.db() as db:
            self.engine.job(db, "related", A, {}, "anchor-1", depth=1)
        for _ in range(4):
            await self.engine.round(self.driver, max_jobs=1)
        count = len(self.driver.calls)
        with self.engine.db() as db:
            self.engine.job(db, "related", A, {}, "anchor-2", depth=1)
        await self.engine.round(self.driver, max_jobs=1)
        self.assertEqual(len(self.driver.calls), count)
        with self.engine.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(DISTINCT anchor) FROM paths p JOIN jobs j ON j.id=p.job WHERE j.kind='detail' AND j.target=?",
                    (C,),
                ).fetchone()[0],
                2,
            )

    async def test_native_adapter_pause_gate_and_request_response_receipts(self):
        transport = AsyncMock(return_value=(200, payload([A], False)))
        profile = self.root / "profile"
        profile.mkdir()
        pause = profile / "reader-access-pause.json"
        pause.write_text('{"reason":"existing device limit"}', encoding="utf-8")
        before = pause.read_bytes()
        adapter = NativeSearchDriver(
            profile,
            self.root / "evidence",
            transport=transport,
            seeds={"合成甲": body()},
            permit_live=True,
        )
        job = {"kind": "search", "target": "合成甲", "cursor": "null"}
        self.assertEqual((await adapter.read(job, []))["platform_requests"], 0)
        transport.assert_not_called()
        self.assertEqual(before, pause.read_bytes())
        # Separate fresh synthetic profile; do not clear even the test pause.
        adapter = NativeSearchDriver(
            self.root / "fresh",
            self.root / "evidence",
            transport=transport,
            seeds={"合成甲": body()},
            permit_live=True,
        )
        with patch(
            "request.reader.frontier_driver.ProfileRuntime.before", new_callable=AsyncMock
        ) as budget:
            result = await adapter.read(job, [])
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["platform_requests"], 1)  # injected logical transport call only
        budget.assert_awaited_once()
        self.assertEqual(len(list((self.root / "evidence").glob("*.json"))), 2)

    def test_unknown_contract_and_oversize_page_are_rejected(self):
        for invalid in (
            body() | {"search_id": ""},
            body() | {"page_size": 21},
            body() | {"page": "2"},
        ):
            with self.assertRaises(ValueError):
                search_request("合成甲", invalid)
        with self.assertRaises(ValueError):
            search_page("合成甲", body(), {"data": {"items": []}})

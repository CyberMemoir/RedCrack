"""Normal-reader wiring verified offline, without declaring live account recovery."""

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from request.reader.browser import SEARCH_PATH, XHSReader
from request.reader.family_actions import FamilyActions
from request.reader.family_reader import enqueue_reader, run_reader
from request.reader.models import Note
from request.reader.reader_audit import audit_archives

ID = "000000000000000000000001"
OTHER = "000000000000000000000002"
ACTION = json.loads(
    (Path(__file__).parent / "fixtures/family_action.json").read_text(encoding="utf-8")
)


class FamilyReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = FamilyActions(self.root / "state")
        self.args = argparse.Namespace(
            mode="head",
            reader_profile=str(self.root / "profile"),
            connect_existing=True,
            chrome_data_dir=None,
            limit=2,
            detail_limit=1,
            max_scrolls=0,
        )

    def enqueue(self):
        return enqueue_reader(self.engine, ACTION, self.args)["id"]

    def card(self, note_id=ID):
        url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=fixture-private-token"
        return Note(
            note_id,
            title="合成甲表达是什么梗",
            url=url,
            observed_at="2026-01-02T00:00:00+00:00",
            content_scope="search_card",
            access={
                "url": url,
                "method": "navigate",
                "source_url": "https://www.xiaohongshu.com/search_result?keyword=fixture",
            },
        )

    def fake_search(self, cards):
        async def search(reader, query, **kwargs):
            self.assertEqual(query, ACTION["query"])
            self.assertEqual(kwargs["max_scrolls"], 0)
            reader.list_end_reason = "limit"
            kwargs["on_progress"](cards)
            return cards

        return search

    def records(self, result):
        bundle = self.engine.root / result["bundles"][0]["bundle"]
        return [
            json.loads(line)
            for line in (bundle / "records.jsonl").read_text(encoding="utf-8").splitlines()
        ], bundle

    async def test_calls_normal_search_and_same_id_detail_preserving_private_reference(self):
        recipe = self.enqueue()
        card = self.card()
        detail = self.card()
        detail.description, detail.content_scope = "原样详情正文", "detail"
        with (
            patch.object(XHSReader, "search", self.fake_search([card])),
            patch.object(XHSReader, "get_note", AsyncMock(return_value=detail)) as get,
        ):
            result = await run_reader(self.engine, recipe, sample_mode="fixture")
        get.assert_awaited_once_with(card.url)
        records, bundle = self.records(result)
        self.assertEqual(records[0]["content_scope"], "detail")
        self.assertEqual(records[0]["texts"][1]["text"], "原样详情正文")
        private = json.loads((bundle.parent / "private/access.json").read_text(encoding="utf-8"))
        self.assertEqual(private[records[0]["access_ref"]]["note_id"], ID)
        for path in bundle.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"fixture-private-token", path.read_bytes())
        self.assertEqual(result["state"], "awaiting_refresh")

    async def test_different_id_detail_fails_without_promoting_card_scope(self):
        recipe = self.enqueue()
        wrong = self.card(OTHER)
        wrong.content_scope = "detail"
        with (
            patch.object(XHSReader, "search", self.fake_search([self.card()])),
            patch.object(XHSReader, "get_note", AsyncMock(return_value=wrong)),
        ):
            result = await run_reader(self.engine, recipe, sample_mode="fixture")
        records, _ = self.records(result)
        self.assertEqual(result["state"], "gap")
        self.assertEqual(result["executions"][0]["state"], "failed")
        self.assertEqual(records[0]["item_id"], ID)
        self.assertEqual(records[0]["content_scope"], "search_card")

    async def test_existing_pause_stops_before_reader_construction(self):
        profile = Path(self.args.reader_profile)
        profile.mkdir()
        pause = profile / "reader-access-pause.json"
        pause.write_text('{"reason":"existing warning"}', encoding="utf-8")
        before = pause.read_bytes()
        recipe = self.enqueue()
        factory = Mock(side_effect=AssertionError("must not construct reader"))
        result = await run_reader(self.engine, recipe, reader_factory=factory)
        factory.assert_not_called()
        self.assertEqual(result["state"], "blocked_verification")
        self.assertEqual(self.records(result)[0], [])
        self.assertEqual(pause.read_bytes(), before)

    async def test_restart_after_detail_checkpoint_does_not_repeat_search_or_first_detail(self):
        self.args.detail_limit = 2
        recipe = self.enqueue()
        cards = [self.card(), self.card(OTHER)]
        for detail in cards:
            detail.content_scope = "detail"
        with (
            patch.object(XHSReader, "search", self.fake_search(cards)),
            patch.object(
                XHSReader,
                "get_note",
                AsyncMock(side_effect=[cards[0], RuntimeError("process loss")]),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await run_reader(self.engine, recipe, sample_mode="fixture")
        with (
            patch.object(
                XHSReader, "search", AsyncMock(side_effect=AssertionError("no repeat search"))
            ),
            patch.object(XHSReader, "get_note", AsyncMock(return_value=cards[1])) as get,
        ):
            result = await run_reader(
                FamilyActions(self.engine.root), recipe, sample_mode="fixture"
            )
        get.assert_awaited_once_with(cards[1].url)
        self.assertEqual(len(self.records(result)[0]), 2)
        self.assertEqual(len(result["executions"]), 1)

    async def test_real_history_is_rejected_before_any_reader(self):
        self.args.mode = "history"
        with self.assertRaisesRegex(ValueError, "history unsupported"):
            self.enqueue()

    async def test_persistent_cancel_reaches_inflight_detail_and_preserves_card(self):
        recipe = self.enqueue()
        entered = asyncio.Event()

        async def detail(reader, url):
            entered.set()
            await asyncio.Event().wait()

        async def cancel():
            await entered.wait()
            FamilyActions(self.engine.root).stop(recipe, "user cancellation", cancel=True)

        stopper = asyncio.create_task(cancel())
        with (
            patch.object(XHSReader, "search", self.fake_search([self.card()])),
            patch.object(XHSReader, "get_note", detail),
        ):
            result = await run_reader(self.engine, recipe, sample_mode="fixture")
        await stopper
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["executions"][0]["state"], "interrupted")
        self.assertEqual(self.records(result)[0][0]["content_scope"], "search_card")

    async def test_actual_request_context_is_saved_without_inventing_replay_fields(self):
        reader = XHSReader(self.root / "capture", evidence_dir=self.root / "evidence")
        response = AsyncMock()
        response.url, response.status = "https://www.xiaohongshu.com" + SEARCH_PATH, 200
        response.request.post_data_json = {
            "keyword": "fixture",
            "search_id": "synthetic-observed-id",
        }
        response.json.return_value = {"success": True, "data": {"items": [], "has_more": False}}
        reader._responses.put_nowait(response)
        try:
            await reader._payloads(SEARCH_PATH, keyword="fixture")
            contexts = list((self.root / "evidence").glob("search-context*.json"))
            context = json.loads(contexts[0].read_text(encoding="utf-8"))["data"]
            self.assertEqual(context["request_body"], response.request.post_data_json)
            audit = audit_archives([self.root / "evidence"])
            self.assertEqual(audit["normal_search_responses"], 1)
            self.assertEqual(audit["normal_request_contexts"], 1)
            self.assertTrue(audit["restartable_history"].startswith("unsupported"))
        finally:
            await reader.close()

    async def test_saved_comment_cursor_is_not_reported_as_search_history_token(self):
        path = self.root / "page-state.json"
        path.write_text(
            '{"details":{"fixture":{"comments":{"cursor":"opaque","hasMore":true}}}}',
            encoding="utf-8",
        )
        audit = audit_archives([path])
        self.assertEqual(audit["comment_cursor_witnesses"], 1)
        self.assertEqual(audit["normal_search_responses"], 0)
        self.assertEqual(audit["search_session_witnesses"], 0)


if __name__ == "__main__":
    unittest.main()

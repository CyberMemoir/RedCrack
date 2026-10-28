import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import xhs
from request.reader.browser import SEARCH_PATH, XHSReader
from request.reader.cache import NoteCache
from request.reader.errors import LoginRequiredError, NetworkReadError, VerificationRequiredError
from request.reader.evidence import EvidenceArchive
from request.reader.models import Note, parse_note

ID = "68add7d50000000000000001"
OTHER = "68add7d50000000000000002"


class SourceIdentityTests(unittest.TestCase):
    def test_missing_counts_and_mismatched_fallback_do_not_fabricate_identity(self):
        note = parse_note(
            {"note_id": ID.upper(), "interact_info": {"liked_count": 0}},
            fallback_url=f"https://www.xiaohongshu.com/explore/{OTHER}?xsec_token=wrong",
        )
        self.assertEqual(note.note_id, ID)
        self.assertEqual(note.url, note.canonical_url)
        self.assertEqual(note.stats["likes"], 0)
        self.assertIsNone(note.stats["shares"])

    def test_source_text_location_hash_and_observation_survive_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = {"note_id": ID, "desc": "这是啥表情包？\n求原图"}
            reference = EvidenceArchive(root / "evidence").save("detail-state", item)
            note = parse_note(item)
            note.evidence = [reference]
            note.observed_at = reference["observed_at"]
            cache = NoteCache(root)
            cache.put("detail", ID, [note])
            restored = cache.get("detail", ID)[0]
            saved = Path(restored.evidence[0]["path"])
            self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(), reference["sha256"])
            self.assertEqual(
                json.loads(saved.read_text(encoding="utf-8"))["data"]["desc"], note.description
            )
            self.assertEqual(restored.observed_at, note.observed_at)
            self.assertTrue(restored.cached)


class FailureStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_start_timeout_releases_profile_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(folder, timeout_ms=20)

            async def hang():
                await asyncio.Event().wait()

            with patch("playwright.async_api.async_playwright") as driver:
                driver.return_value.start = AsyncMock(side_effect=hang)
                with self.assertRaises(NetworkReadError):
                    await reader.start()
            self.assertIsNone(reader.runtime.handle)
            self.assertIsNone(reader.context)

    async def test_navigation_403_pauses_before_parsing_or_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(folder, interval=0)
            reader.page = AsyncMock()
            reader.page.url = "about:blank"
            response = AsyncMock()
            response.status = 403
            reader.page.goto.return_value = response
            reader._check_access = AsyncMock()
            try:
                with self.assertRaises(VerificationRequiredError):
                    await reader._goto(f"https://www.xiaohongshu.com/explore/{ID}", kind="detail")
                self.assertTrue(reader.access_pause.path.exists())
                self.assertEqual(reader.page.goto.await_count, 1)
            finally:
                await reader.close()

    async def test_response_body_timeout_stops_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(folder, timeout_ms=20)
            response = AsyncMock()
            response.url = "https://www.xiaohongshu.com" + SEARCH_PATH
            response.status = 200

            async def hang():
                await asyncio.Event().wait()

            response.json.side_effect = hang
            reader._responses.put_nowait(response)
            with self.assertRaises(NetworkReadError):
                await reader._payloads(SEARCH_PATH)
            self.assertIsNone(reader.context)

    async def test_failed_search_is_incomplete_and_has_terminal_task_state(self):
        with tempfile.TemporaryDirectory() as folder:
            args = xhs.build_parser().parse_args(["search", "线索", "-o", folder])
            reader = AsyncMock()
            reader.search.side_effect = LoginRequiredError("expired")
            with patch("xhs.XHSReader") as constructor, contextlib.redirect_stderr(io.StringIO()):
                constructor.return_value.__aenter__.return_value = reader
                with self.assertRaises(LoginRequiredError):
                    await xhs.run(args)
            task = json.loads((Path(folder) / "task.json").read_text(encoding="utf-8"))
            results = json.loads((Path(folder) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(task["status"], "login_required")
            self.assertEqual(task["exit_code"], 3)
            self.assertIsNotNone(task["finished_at"])
            self.assertFalse(results["complete"])
            self.assertEqual(len(list((Path(folder) / "tasks").glob("*.json"))), 1)

    async def test_cancelled_search_is_recorded_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            args = xhs.build_parser().parse_args(["search", "线索", "-o", folder])
            with patch("xhs._run", AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await xhs.run(args)
            task = json.loads((Path(folder) / "task.json").read_text(encoding="utf-8"))
            self.assertEqual((task["status"], task["exit_code"]), ("interrupted", 130))

    async def test_second_keyword_failure_preserves_first_keyword_results(self):
        with tempfile.TemporaryDirectory() as folder:
            queries = Path(folder) / "queries.txt"
            queries.write_text("第二个线索", encoding="utf-8")
            args = xhs.build_parser().parse_args(
                ["search", "第一个线索", "--queries-file", str(queries), "-o", folder]
            )
            reader = AsyncMock()
            reader.search.side_effect = [[Note(ID, title="已取得")], NetworkReadError("timeout")]
            with patch("xhs.XHSReader") as constructor, contextlib.redirect_stderr(io.StringIO()):
                constructor.return_value.__aenter__.return_value = reader
                with self.assertRaises(NetworkReadError):
                    await xhs.run(args)
            results = json.loads((Path(folder) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual([row["note_id"] for row in results["notes"]], [ID])
            self.assertFalse(results["complete"])

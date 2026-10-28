import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from request.reader.browser import FEED_PATH, SEARCH_PATH, XHSReader
from request.reader.collection import export_collection
from request.reader.errors import VerificationRequiredError
from request.reader.models import parse_note, parse_visible_card

ID = "68add7d50000000000000001"
OTHER = "68add7d50000000000000002"


class CollectionTests(unittest.TestCase):
    def test_card_prefers_same_identity_complete_url_and_preserves_source(self):
        note = parse_visible_card(
            {
                "url": f"https://www.xiaohongshu.com/explore/{ID}",
                "title": "卡片",
                "author": "作者",
                "links": [
                    f"https://www.xiaohongshu.com/explore/{OTHER}?xsec_token=wrong",
                    f"https://www.xiaohongshu.com/search_result/{ID}?xsec_token=correct&xsec_source=pc_search",
                ],
            },
            source_url="https://www.xiaohongshu.com/search_result?keyword=fixture",
        )
        self.assertIn("xsec_token=correct", note.url)
        self.assertNotIn("wrong", note.url)
        self.assertEqual(note.access["method"], "navigate")
        self.assertIn("keyword=fixture", note.access["source_url"])
        self.assertIn("/search_result/", note.access["url"])
        parsed = parse_note(
            {"note_id": ID, "xsec_token": "replacement"},
            fallback_url=note.url.replace("pc_search", "pc_feed"),
        )
        self.assertIn("xsec_source=pc_feed", parsed.url)

    def test_public_graph_private_access_and_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            note = {
                "note_id": ID,
                "url": f"https://www.xiaohongshu.com/explore/{ID}?xsec_token=private-token",
                "title": "这是什么梗",
                "description": f"来源 https://www.xiaohongshu.com/explore/{OTHER}?xsec_token=private-token",
                "author_id": "fixture-author",
                "author": "作者",
                "observed_at": "2026-10-28T00:00:00Z",
                "content_scope": "detail",
                "tag_refs": [{"id": "fixture-tag", "name": "示例"}],
                "media": [
                    {"kind": "video", "url": "https://cdn.invalid/video?token=private-token"}
                ],
                "subtitle_tracks": [
                    {"url": "https://cdn.invalid/subtitle?token=private-token", "language": "zh"}
                ],
            }
            source = root / "note.json"
            source.write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")
            first = export_collection([str(source)], root / "out")
            self.assertEqual(first["new_edges"], 3)
            self.assertEqual(first["new_query_occurrences"], 1)
            for name in ("nodes.jsonl", "edges.jsonl", "frontier.jsonl", "media_manifest.jsonl"):
                raw = (root / "out" / name).read_text(encoding="utf-8")
                self.assertNotIn("private-token", raw)
                self.assertNotIn("xsec_token", raw)
            self.assertIn(
                "private-token", (root / "out/private/access.jsonl").read_text(encoding="utf-8")
            )
            manifests = [
                json.loads(line)
                for line in (root / "out/media_manifest.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual({row["kind"] for row in manifests}, {"video", "subtitle"})
            second = export_collection([str(source)], root / "out")
            self.assertEqual((second["new_node_versions"], second["new_edges"]), (0, 0))
            self.assertEqual(second["new_query_occurrences"], 0)
            caps = json.loads((root / "out/capabilities.json").read_text(encoding="utf-8"))
            self.assertEqual(caps["entries"][0]["status"], "unverified")

    def test_search_covers_are_not_ocr_inputs_and_pause_survives_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "note.json"
            source.write_text(
                json.dumps(
                    {
                        "note_id": ID,
                        "title": "是什么梗",
                        "content_scope": "search_card",
                        "media": [{"kind": "image", "url": "https://cdn.invalid/cover"}],
                    }
                ),
                encoding="utf-8",
            )
            pause = root / "pause.json"
            pause.write_text(
                '{"status":"blocked_verification","reason":"device limit"}', encoding="utf-8"
            )
            result = export_collection([str(source)], root / "out", pause_record=str(pause))
            self.assertEqual(result["status"], "blocked_verification")
            self.assertEqual((root / "out/media_manifest.jsonl").read_text(), "")
            resumed = export_collection([str(source)], root / "out")
            self.assertEqual(resumed["status"], "blocked_verification")


class RestrictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_567_in_other_captured_endpoint_stops_before_discard(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(folder)
            response = AsyncMock()
            response.url = "https://www.xiaohongshu.com" + SEARCH_PATH
            response.status = 567
            reader._responses.put_nowait(response)
            try:
                with self.assertRaises(VerificationRequiredError):
                    await reader._payloads(FEED_PATH)
                self.assertTrue(reader.access_pause.path.exists())
            finally:
                await reader.close()

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from request.reader.errors import ReaderError
from request.reader.export import export_notes
from request.reader.models import Note, normalize_note_url, parse_note, parse_search_payload

FIXTURES = Path(__file__).parent / "fixtures"
ID = "68add7d50000000000000001"


class ModelTests(unittest.TestCase):
    def test_search_fixture(self):
        notes = parse_search_payload(json.loads((FIXTURES / "search.json").read_text()))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "周末咖啡散步")
        self.assertIn("fixture-search-token%3D", notes[0].url)
        self.assertEqual(notes[0].stats["likes"], "1.2万")
        self.assertEqual(notes[0].media, [])  # 搜索封面不冒充完整图片列表。

    def test_detail_fixture(self):
        item = json.loads((FIXTURES / "detail.json").read_text())["data"]["items"][0]
        note = parse_note(item)
        self.assertEqual(len(note.media), 2)
        self.assertEqual(note.media[1].url, "https://sns-img-bd.xhscdn.com/fixture-2")
        self.assertEqual(note.tags, ["咖啡", "城市漫步"])
        self.assertIn("第二行", note.description)

    def test_camel_case_state_and_video_quality(self):
        note = parse_note(
            {
                "note": {
                    "noteId": ID,
                    "type": "video",
                    "user": {"nickName": "author"},
                    "imageList": [{"urlDefault": "//sns-img-bd.xhscdn.com/cover"}],
                    "video": {
                        "media": {
                            "stream": {
                                "h264": [
                                    {
                                        "width": 640,
                                        "height": 480,
                                        "masterUrl": "http://sns-video-bd.xhscdn.com/low",
                                    },
                                    {
                                        "width": 1920,
                                        "height": 1080,
                                        "masterUrl": "https://sns-video-bd.xhscdn.com/high",
                                    },
                                ],
                                "h265": [{"master_url": "https://sns-video-bd.xhscdn.com/hevc"}],
                            }
                        }
                    },
                }
            }
        )
        self.assertEqual(note.author, "author")
        self.assertEqual(note.media[0].kind, "video")
        self.assertTrue(note.media[0].url.endswith("/high"))
        self.assertEqual(len(note.media), 2)

    def test_invalid_note(self):
        for item in ({}, {"id": "../../escape"}, {"note": "bad"}):
            with self.assertRaises(ReaderError):
                parse_note(item)

    def test_deduplicated_media(self):
        note = parse_note({"note_id": ID, "image_list": [{"url": "https://a.xhscdn.com/a"}] * 3})
        self.assertEqual(len(note.media), 1)

    def test_export_csv_unicode_and_formula_safety(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            export_notes(
                [Note(ID, title=" =SUM(1,2)", description="中文\n正文", tags=["咖啡"])],
                output,
                keyword="咖啡",
            )
            result = json.loads((output / "results.json").read_text())
            self.assertEqual(result["count"], 1)
            raw = (output / "results.csv").read_text(encoding="utf-8-sig")
            row = next(csv.DictReader(io.StringIO(raw)))
            self.assertTrue(row["title"].startswith("'"))
            self.assertEqual(row["description"], "中文\n正文")
            self.assertEqual(row["tags"], "咖啡")
            self.assertFalse(list(output.glob(".*.tmp")))


class URLTests(unittest.TestCase):
    def test_id(self):
        self.assertEqual(
            normalize_note_url(ID.upper()), f"https://www.xiaohongshu.com/explore/{ID}"
        )

    def test_share_text_and_token(self):
        result = normalize_note_url(
            f"分享笔记 https://www.xiaohongshu.com/discovery/item/{ID}?xsec_token=a%2Bb%3D&tracking=delete。复制打开"
        )
        self.assertIn("xsec_token=a%2Bb%3D", result)
        self.assertNotIn("tracking", result)

    def test_short_link(self):
        self.assertEqual(
            normalize_note_url("看看这个 http://xhslink.com/a/test，复制本条"),
            "https://xhslink.com/a/test",
        )

    def test_reject_invalid_links(self):
        for value in (
            "",
            "file:///etc/passwd",
            "https://xiaohongshu.com.evil.test/explore/" + ID,
            "https://evil.test/",
            "https://www.xiaohongshu.com/explore/../../foo",
            "https://user:pass@www.xiaohongshu.com/explore/" + ID,
            "https://www.xiaohongshu.com:9999/explore/" + ID,
            "https://www.xiaohongshu.com/user/profile/" + ID,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_note_url(value)

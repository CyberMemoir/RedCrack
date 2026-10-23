"""Expose existing page tracks without fetching or generating subtitle content."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from request.reader.browser import XHSReader
from request.reader.cache import NoteCache
from request.reader.models import Note


class SubtitleMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_and_cache_preserve_explicit_video_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            async with XHSReader(directory, refresh=True) as reader:
                reader.start = AsyncMock()
                reader._goto = AsyncMock()
                reader._clear_responses = lambda: None
                reader._read_note = AsyncMock(
                    return_value=Note("000000000000000000000001", kind="video")
                )
                reader.page = AsyncMock()
                tracks = [
                    {
                        "url": "https://v.xhscdn.com/subtitle.vtt",
                        "language": "zh",
                        "label": "subtitle",
                        "kind": "subtitles",
                    }
                ]
                reader.page.locator = Mock()
                reader.page.locator.return_value.evaluate_all = AsyncMock(return_value=tracks)
                note = await reader.get_note("000000000000000000000001")
                self.assertEqual(note.to_dict()["subtitle_tracks"], tracks)
                self.assertEqual(
                    NoteCache(Path(directory)).get("detail", note.note_id)[0].subtitle_tracks,
                    tracks,
                )
                reader.page = None

    async def test_no_observed_track_is_an_explicit_empty_list(self):
        self.assertEqual(Note("000000000000000000000001").to_dict()["subtitle_tracks"], [])


@unittest.skipUnless(os.environ.get("REDCRACK_BROWSER_TESTS") == "1", "offline browser opt-in")
class SubtitleBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_detail_page_exports_only_explicit_video_tracks(self):
        item = "000000000000000000000001"
        with tempfile.TemporaryDirectory() as directory:
            async with XHSReader(
                directory, headless=True, browser_channel="chromium", interval=0, timeout_ms=1500
            ) as reader:
                await reader.start()
                state = {
                    "user": {"loggedIn": True},
                    "note": {
                        "noteDetailMap": {
                            item: {"note": {"note_id": item, "title": "fixture", "type": "video"}}
                        }
                    },
                }
                html = (
                    '<video><track src="https://v.xhscdn.com/fixture.vtt" srclang="zh" label="fixture" kind="subtitles"></video><script>window.__INITIAL_STATE__='
                    + json.dumps(state)
                    + "</script>"
                )

                async def route(route):
                    if route.request.url.startswith(f"https://www.xiaohongshu.com/explore/{item}"):
                        await route.fulfill(content_type="text/html", body=html)
                    else:
                        await route.abort()

                await reader.context.route("**/*", route)
                note = await reader.get_note(item)
                self.assertEqual(
                    note.subtitle_tracks,
                    [
                        {
                            "url": "https://v.xhscdn.com/fixture.vtt",
                            "language": "zh",
                            "label": "fixture",
                            "kind": "subtitles",
                        }
                    ],
                )

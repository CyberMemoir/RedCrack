"""End reasons describe returned records, even on the final response page."""

import tempfile
import unittest
from unittest.mock import AsyncMock

from request.reader.browser import XHSReader


class ReaderLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_page_is_exhausted_only_if_all_records_are_returned(self):
        for limit, expected in (
            (1, "limit"),
            (3, "entry_exhausted"),
            (5, "entry_exhausted"),
        ):
            for kind in ("discover", "search"):
                with (
                    self.subTest(limit=limit, kind=kind),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    async with XHSReader(folder, interval=0, refresh=True) as reader:
                        reader.start = AsyncMock()
                        reader._goto = AsyncMock()
                        reader._check_access = AsyncMock()
                        reader._clear_responses = lambda: None
                        reader.page = AsyncMock()
                        reader.page.evaluate.return_value = []
                        payload = {
                            "data": {
                                "has_more": False,
                                "items": [
                                    {
                                        "id": f"{i:024x}",
                                        "note_card": {"title": f"title {i}"},
                                    }
                                    for i in (1, 2, 3)
                                ],
                            }
                        }
                        reader._payloads = AsyncMock(return_value=[payload])
                        reader._payload_evidence[id(payload)] = reader.evidence.save(
                            "fixture", payload
                        )
                        if kind == "discover":
                            notes = await reader.discover(limit=limit, max_scrolls=0)
                        else:
                            notes = await reader.search("fixture", limit=limit, max_scrolls=0)
                        self.assertEqual(len(notes), min(limit, 3))
                        self.assertEqual(reader.list_end_reason, expected)
                        reader.page = None

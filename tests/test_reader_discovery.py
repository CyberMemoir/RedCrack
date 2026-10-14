"""Normal recommendation feed, offline HTTP fixtures only."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from request.reader.browser import XHSReader
from request.reader.errors import VerificationRequiredError


@unittest.skipUnless(os.environ.get("REDCRACK_BROWSER_TESTS") == "1", "offline browser opt-in")
class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_preserves_evidence_and_reuses_separate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            async with XHSReader(
                folder, headless=True, browser_channel="chromium", interval=0, timeout_ms=1500
            ) as reader:
                await reader.start()
                calls = []
                fixture = json.loads(
                    (Path(__file__).parent / "fixtures/search.json").read_text(encoding="utf-8")
                )

                async def route(route):
                    parts = urlsplit(route.request.url)
                    calls.append(parts.path)
                    if parts.path == "/explore":
                        await route.fulfill(
                            content_type="text/html",
                            body="""
                            <script>window.__INITIAL_STATE__={user:{loggedIn:true}};
                            fetch('/api/sns/web/v1/homefeed',{method:'POST'});</script>""",
                        )
                    elif parts.path == "/api/sns/web/v1/homefeed":
                        await route.fulfill(json=fixture)
                    else:
                        await route.abort()

                await reader.context.route("**/*", route)
                notes = await reader.discover(limit=1, max_scrolls=0)
                self.assertEqual(len(notes), 1)
                self.assertIn("/data/items/0", notes[0].evidence[0]["locator"])
                first_time = notes[0].observed_at
                count = len(calls)
                cached = await reader.discover(limit=1, max_scrolls=0)
                self.assertTrue(cached[0].cached)
                self.assertEqual(cached[0].observed_at, first_time)
                self.assertEqual(len(calls), count)

    async def test_discovery_restriction_is_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            async with XHSReader(
                folder, headless=True, browser_channel="chromium", interval=0, timeout_ms=1500
            ) as reader:
                await reader.start()

                async def route(route):
                    if urlsplit(route.request.url).path == "/explore":
                        await route.fulfill(status=461, body="restriction")
                    else:
                        await route.abort()

                await reader.context.route("**/*", route)
                with self.assertRaises(VerificationRequiredError):
                    await reader.discover(limit=1, max_scrolls=0)
                with self.assertRaises(VerificationRequiredError):
                    await reader.discover(limit=1, max_scrolls=0)

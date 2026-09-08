"""真实 Chromium + 本地拦截响应，不访问小红书，不需要账号。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from request.reader.browser import XHSReader
from request.reader.errors import LoginRequiredError, ReaderError, VerificationRequiredError

FIXTURES = Path(__file__).parent / "fixtures"
ID = "68add7d50000000000000001"
ID2 = "68add7d50000000000000002"


@unittest.skipUnless(
    os.environ.get("REDCRACK_BROWSER_TESTS") == "1",
    "设置 REDCRACK_BROWSER_TESTS=1 启用浏览器集成测试",
)
class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.reader = XHSReader(
            self.folder.name,
            headless=True,
            browser_channel=os.environ.get("REDCRACK_TEST_BROWSER", "chromium"),
            timeout_ms=3000,
            interval=0.05,
        )
        await self.reader.start()
        self.mode = "search"
        self.pages_requested = []

        async def route_handler(route):
            parts = urlsplit(route.request.url)
            if parts.hostname == "xhslink.com":
                await route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body=f'<script>location.replace("https://www.xiaohongshu.com/explore/{ID}?xsec_token=fixture")</script>',
                )
                return
            if parts.path == "/api/sns/web/v1/search/notes":
                if self.mode == "verification":
                    await route.fulfill(status=461, json={"success": False})
                    return
                data = json.loads((FIXTURES / "search.json").read_text())
                body = route.request.post_data_json
                self.pages_requested.append(body["page"])
                if body["page"] > 1:
                    data["data"]["items"][0]["id"] = ID2
                    data["data"]["items"][0]["note_card"]["display_title"] = "第二页"
                    data["data"]["has_more"] = False
                if self.mode == "empty":
                    data["data"] = {"has_more": False, "items": []}
                await route.fulfill(json=data)
                return
            if parts.path == "/api/sns/web/v1/feed":
                await route.fulfill(json=json.loads((FIXTURES / "detail.json").read_text()))
                return
            if parts.path == "/search_result":
                keyword = parse_qs(parts.query)["keyword"][0]
                if self.mode == "login":
                    html = '<div class="login-container">请扫码登录</div>'
                elif self.mode == "guest":
                    html = '<input placeholder="登录探索更多内容">'
                elif self.mode == "timeout":
                    html = "<main>loading</main>"
                elif self.mode == "dom":
                    html = f'<section class="note-item"><a href="/explore/{ID}?xsec_token=dom-token"></a><span class="title">DOM 备用卡片</span><span class="author"><span class="name">作者</span></span></section>'
                else:
                    html = """<body style="height:10000px"><h1>Fixture Search</h1><script>
                    let page=0;
                    const fetchPage=()=>fetch('/api/sns/web/v1/search/notes', {
                        method:'POST',headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({keyword:KEYWORD,page:++page})});
                    fetchPage();
                    window.addEventListener('wheel',()=>{if(page<2) fetchPage();});
                    </script></body>""".replace("KEYWORD", json.dumps(keyword))
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)
                return
            if parts.path == "/explore":
                html = """<input placeholder="登录探索更多内容"
                    onclick="window.__INITIAL_STATE__={user:{loggedIn:true}}">
                    <script>window.__INITIAL_STATE__={user:{loggedIn:false}}</script>"""
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)
                return
            if parts.path.startswith("/explore/"):
                if self.mode == "state":
                    card = json.loads((FIXTURES / "detail.json").read_text())["data"]["items"][0][
                        "note_card"
                    ]
                    state = {"note": {"noteDetailMap": {ID: {"note": card}}}}
                    html = "<script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script>"
                else:
                    html = "<script>fetch('/api/sns/web/v1/feed',{method:'POST'});</script>"
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)
                return
            await route.abort()

        await self.reader.context.route("**/*", route_handler)

    async def asyncTearDown(self):
        await self.reader.close()
        self.folder.cleanup()

    async def test_search_paginates_and_preserves_token(self):
        checkpoints = []
        notes = await self.reader.search(
            "咖啡", limit=5, on_progress=lambda notes: checkpoints.append(len(notes))
        )
        self.assertEqual([n.note_id for n in notes], [ID, ID2])
        self.assertEqual(self.pages_requested, [1, 2])
        self.assertIn("xsec_token=", notes[0].url)
        self.assertEqual(checkpoints, [1, 2])

    async def test_limit_stops_early(self):
        notes = await self.reader.search("咖啡", limit=1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.pages_requested, [1])

    async def test_empty_is_success(self):
        self.mode = "empty"
        self.assertEqual(await self.reader.search("不存在"), [])

    async def test_dom_fallback(self):
        self.mode = "dom"
        notes = await self.reader.search("咖啡", limit=1)
        self.assertEqual(notes[0].title, "DOM 备用卡片")
        self.assertIn("dom-token", notes[0].url)

    async def test_login_gate(self):
        self.mode = "login"
        with self.assertRaises(LoginRequiredError):
            await self.reader.search("咖啡")

    async def test_guest_search_without_modal_requires_login(self):
        self.mode = "guest"
        with self.assertRaises(LoginRequiredError):
            await self.reader.search("咖啡")

    async def test_login_opens_current_guest_entry(self):
        self.reader.headless = False  # 浏览器仍为无头测试，模拟可交互登录流程。
        await self.reader.login(timeout_seconds=4)
        self.assertTrue((await self.reader._state())["loggedIn"])

    async def test_verification_stops(self):
        self.mode = "verification"
        with self.assertRaises(VerificationRequiredError):
            await self.reader.search("咖啡")

    async def test_no_data_is_not_empty_success(self):
        self.mode = "timeout"
        self.reader.timeout_ms = 500
        with self.assertRaises(ReaderError):
            await self.reader.search("咖啡")

    async def test_detail_from_response(self):
        note = await self.reader.get_note(
            f"https://www.xiaohongshu.com/explore/{ID}?xsec_token=original"
        )
        self.assertEqual(len(note.media), 2)
        self.assertIn("original", note.url)

    async def test_detail_from_initial_state(self):
        self.mode = "state"
        note = await self.reader.get_note(ID)
        self.assertEqual(note.title, "周末咖啡散步")
        self.assertEqual(len(note.media), 2)

    async def test_short_link_redirect(self):
        note = await self.reader.get_note("http://xhslink.com/a/fixture")
        self.assertEqual(note.note_id, ID)
        self.assertIn("xsec_token=fixture", note.url)

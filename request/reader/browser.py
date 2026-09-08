from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .errors import LoginRequiredError, ReaderError, VerificationRequiredError
from .models import (
    SHORT_HOSTS,
    Note,
    normalize_note_url,
    note_id_from_url,
    parse_note,
    parse_search_payload,
)

HOME_URL = "https://www.xiaohongshu.com/explore"
SEARCH_PATH = "/api/sns/web/v1/search/notes"
FEED_PATH = "/api/sns/web/v1/feed"

READ_STATE = """() => {
    const s = window.__INITIAL_STATE__ || {};
    const unwrap = v => v && (v.__v_isRef || v.__v_isShallow)
        ? (v.value === undefined ? v._value : v.value) : v;
    const note = unwrap(s.note) || {};
    const user = unwrap(s.user) || {};
    return JSON.parse(JSON.stringify({
        details: unwrap(note.noteDetailMap) || {},
        loggedIn: unwrap(user.loggedIn) === true || unwrap(user.isLogin) === true
    }));
}"""

READ_CARDS = """() => Array.from(document.querySelectorAll('section.note-item')).map(card => {
    const link = card.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
    if (!link) return null;
    const title = card.querySelector('.title');
    const author = card.querySelector('.author .name, .author-wrapper .name');
    return {url: link.href, title: title?.textContent?.trim() || '',
            author: author?.textContent?.trim() || ''};
}).filter(Boolean)"""


class XHSReader:
    """用独立持久化浏览器访问网页，读取网页自己的搜索/详情响应。"""

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        headless: bool = False,
        browser_channel: str = "auto",
        timeout_ms: int = 30_000,
        interval: float = 1.5,
    ) -> None:
        if timeout_ms <= 0 or interval < 0:
            raise ValueError("timeout 必须大于 0，interval 不能为负数")
        self.profile_dir = (
            Path(profile_dir or Path.home() / ".redcrack" / "xhs_reader_profile")
            .expanduser()
            .resolve()
        )
        self.headless = headless
        self.browser_channel = browser_channel
        self.timeout_ms = timeout_ms
        self.interval = interval
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self._responses: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> XHSReader:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self.context:
            return
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError as exc:
            raise ReaderError("请先安装依赖：pip install -r requirements-reader.txt") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = await async_playwright().start()
        channels = (
            ["chrome", None]
            if self.browser_channel == "auto"
            else [None if self.browser_channel == "chromium" else self.browser_channel]
        )
        errors = []
        for channel in channels:
            options = {
                "user_data_dir": str(self.profile_dir),
                "headless": self.headless,
                "locale": "zh-CN",
                "viewport": {"width": 1440, "height": 960},
            }
            if channel:
                options["channel"] = channel
            try:
                self.context = await self._playwright.chromium.launch_persistent_context(**options)
                break
            except Error as exc:
                errors.append(str(exc).splitlines()[0])
        if self.context is None:
            await self.close()
            raise ReaderError(
                "浏览器启动失败；可执行 python -m playwright install chromium，或检查 profile 是否被占用。"
                + " | ".join(errors)
            )
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("response", self._capture_response)

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = self.page = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    def _capture_response(self, response: Any) -> None:
        parts = urlsplit(response.url)
        if (parts.hostname or "").endswith(".xiaohongshu.com") and parts.path in {
            SEARCH_PATH,
            FEED_PATH,
        }:
            self._responses.put_nowait(response)

    def _clear_responses(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _payloads(self, path: str, *, keyword: str = "") -> list[dict]:
        payloads = []
        while not self._responses.empty():
            response = self._responses.get_nowait()
            if urlsplit(response.url).path != path:
                continue
            if keyword:
                try:
                    request_data = response.request.post_data_json or {}
                except (ValueError, json.JSONDecodeError):
                    request_data = {}
                if request_data.get("keyword") not in (None, keyword):
                    continue
            if response.status in (403, 429, 461, 471):
                raise VerificationRequiredError(
                    f"网页返回访问限制（HTTP {response.status}）；请在浏览器完成验证后重试"
                )
            if response.status == 401:
                raise LoginRequiredError("登录已失效，请运行 python xhs.py login")
            if response.status >= 400:
                raise ReaderError(f"小红书网页接口返回 HTTP {response.status}")
            try:
                payload = await response.json()
            except Exception as exc:
                raise ReaderError("网页接口没有返回可读取的 JSON") from exc
            if not isinstance(payload, dict):
                raise ReaderError("网页接口响应结构发生变化")
            if payload.get("success") is False:
                code = payload.get("code")
                if code in (-100, -100100, -101):
                    raise LoginRequiredError("请运行 python xhs.py login 更新登录状态")
                raise ReaderError(
                    f"网页接口失败：{payload.get('msg') or payload.get('message') or code}"
                )
            payloads.append(payload)
        return payloads

    async def _goto(self, url: str) -> None:
        from playwright.async_api import Error

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Error as exc:
            raise ReaderError("页面加载失败，请检查网络或增大 --timeout") from exc

    async def _state(self) -> dict:
        from playwright.async_api import Error

        try:
            return await self.page.evaluate(READ_STATE)
        except Error:
            return {}

    async def _check_access(self, *, require_login: bool = False) -> None:
        url = self.page.url.lower()
        if "/login" in url:
            raise LoginRequiredError("需要登录，请先运行 python xhs.py login")
        if any(marker in url for marker in ("/captcha", "/website-login/captcha", "/sorry")):
            raise VerificationRequiredError("页面要求验证，请在浏览器完成后重试")
        for selector in (".captcha-container", "#captcha-container", ".verify-container"):
            if await self.page.locator(selector).first.is_visible():
                raise VerificationRequiredError("页面要求验证，请在浏览器完成后重试")
        for selector in (".login-container", ".login-modal", ".login-box"):
            if await self.page.locator(selector).first.is_visible():
                raise LoginRequiredError("需要扫码登录，请先运行 python xhs.py login")
        if (
            require_login
            and await self.page.get_by_placeholder(
                "登录探索更多内容", exact=True
            ).first.is_visible()
        ):
            raise LoginRequiredError("搜索需要登录，请先运行 python xhs.py login")

    async def login(self, timeout_seconds: int = 300) -> None:
        if self.headless:
            raise LoginRequiredError("扫码登录需要有窗口模式，请去掉 --headless")
        if timeout_seconds <= 0:
            raise ValueError("登录等待时间必须大于 0")
        await self.start()
        await self._goto(HOME_URL)
        deadline = time.monotonic() + timeout_seconds
        prompted = False
        while time.monotonic() < deadline:
            state = await self._state()
            own_link = self.page.locator(
                '.side-bar a[href*="/user/profile/"], .user.side-bar-component a[href*="/user/profile/"]'
            )
            if state.get("loggedIn") or await own_link.first.is_visible():
                return
            # 等待页面初始化后再触发登录；新版访客页入口位于搜索框。
            if not prompted:
                for entry in (
                    self.page.get_by_text("登录", exact=True).first,
                    self.page.get_by_placeholder("登录探索更多内容", exact=True).first,
                ):
                    if await entry.is_visible():
                        await entry.click()
                        prompted = True
                        break
            await asyncio.sleep(1)
        raise LoginRequiredError(f"等待扫码登录超时（{timeout_seconds} 秒），可重新运行 login")

    async def search(
        self,
        keyword: str,
        *,
        limit: int = 20,
        max_scrolls: int = 30,
        on_progress: Callable[[list[Note]], None] | None = None,
    ) -> list[Note]:
        keyword = keyword.strip()
        if not keyword or limit <= 0 or max_scrolls < 0:
            raise ValueError("关键词不能为空，limit 必须大于 0，max-scrolls 不能为负数")
        await self.start()
        self._clear_responses()
        query = urlencode({"keyword": keyword, "source": "web_explore_feed"})
        await self._goto(f"https://www.xiaohongshu.com/search_result?{query}")
        found: dict[str, Note] = {}
        idle = scrolls = 0
        received = exhausted = False
        initial_deadline = time.monotonic() + self.timeout_ms / 1000
        while True:
            await asyncio.sleep(max(self.interval, 0.2))
            before = len(found)
            for payload in await self._payloads(SEARCH_PATH, keyword=keyword):
                received = True
                data = payload.get("data") or {}
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ReaderError("搜索接口数据结构发生变化，未找到 items 列表")
                exhausted = data.get("has_more", data.get("hasMore")) is False
                for note in parse_search_payload(payload):
                    found[note.note_id] = note
            await self._check_access(require_login=True)
            # API 结构变更时，仍可保留当前页面可见卡片及其完整访问令牌。
            for card in await self.page.evaluate(READ_CARDS):
                try:
                    url = normalize_note_url(card["url"])
                except ValueError:
                    continue
                note_id = note_id_from_url(url)
                if note_id and note_id not in found:
                    found[note_id] = Note(
                        note_id, title=card["title"], author=card["author"], url=url
                    )
            notes = list(found.values())[:limit]
            if on_progress and len(found) != before:
                on_progress(notes)
            if len(found) >= limit or exhausted:
                return notes
            if not found and not received:
                if await self.page.get_by_text("没有找到相关结果", exact=False).first.is_visible():
                    return []
                if time.monotonic() >= initial_deadline:
                    raise ReaderError(
                        "未读到搜索结果：请检查登录、网络或页面结构；本次已抓取结果会保留"
                    )
                continue
            idle = idle + 1 if len(found) == before else 0
            if idle >= 4 or scrolls >= max_scrolls:
                return notes
            await self.page.mouse.wheel(0, 1400)
            scrolls += 1

    async def get_note(self, value: str) -> Note:
        url = normalize_note_url(value)
        await self.start()
        self._clear_responses()
        await self._goto(url)
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            await self._check_access()
            current_url = self.page.url
            if urlsplit(current_url).hostname in SHORT_HOSTS:
                continue
            try:
                resolved = normalize_note_url(current_url)
            except ValueError as exc:
                raise ReaderError("笔记链接跳转到非笔记页面，可能已经删除或不可访问") from exc
            wanted = note_id_from_url(resolved) or note_id_from_url(url)
            # 保留原链接中的 token，即使网页路由移除了 query。
            source_url = (
                url
                if note_id_from_url(url) == wanted
                and parse_qs(urlsplit(url).query).get("xsec_token")
                else resolved
            )
            for payload in await self._payloads(FEED_PATH):
                for item in (payload.get("data") or {}).get("items") or []:
                    try:
                        note = parse_note(item, fallback_url=source_url)
                    except ReaderError:
                        continue
                    if note.note_id == wanted:
                        return note
            details = (await self._state()).get("details") or {}
            if isinstance(details, dict):
                for key, item in details.items():
                    if key != wanted and not key.startswith(wanted + "?"):
                        continue
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("note"), dict)
                        and item["note"]
                    ):
                        return parse_note(item, fallback_url=source_url)
            for message in ("当前笔记暂时无法浏览", "笔记已删除", "笔记不存在"):
                if await self.page.get_by_text(message, exact=False).first.is_visible():
                    raise ReaderError(message + "；请使用包含 xsec_token 的完整分享链接")
        raise ReaderError("未读取到笔记详情；请使用包含 xsec_token 的完整链接，或重新登录后重试")

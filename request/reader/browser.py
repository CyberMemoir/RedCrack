from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .access import AccessPause
from .cache import NoteCache
from .errors import LoginRequiredError, NetworkReadError, ReaderError
from .evidence import EvidenceArchive
from .existing import existing_chrome_endpoint
from .models import (
    NOTE_HOSTS,
    SHORT_HOSTS,
    Note,
    normalize_note_url,
    note_id_from_url,
    parse_note,
    parse_visible_card,
)
from .runtime import ProfileRuntime

HOME_URL = "https://www.xiaohongshu.com/explore"
SEARCH_PATH = "/api/sns/web/v1/search/notes"
FEED_PATH = "/api/sns/web/v1/feed"
HOMEFEED_PATH = "/api/sns/web/v1/homefeed"
WARNING_MARKERS = (
    "真人操作习惯",
    "操作过于频繁",
    "访问过于频繁",
    "异常访问",
    "非正常访问",
    "设备超限",
    "登录设备超",
    "设备数量已达",
)

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
    return {url: link.href,
            links: Array.from(card.querySelectorAll('a[href]')).map(a => a.href),
            title: title?.textContent?.trim() || '',
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
        interval: float = 15,
        refresh: bool = False,
        evidence_dir: str | Path | None = None,
        connect_existing: bool = False,
        chrome_data_dir: str | Path | None = None,
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
        if chrome_data_dir is not None and not connect_existing:
            raise ValueError("--chrome-data-dir 仅用于 --connect-existing")
        self.connect_existing = connect_existing
        self.chrome_data_dir = chrome_data_dir
        self._attached_browser: Any = None
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self._responses: asyncio.Queue = asyncio.Queue()
        self.access_pause = AccessPause(self.profile_dir)
        self.runtime = ProfileRuntime(self.profile_dir, interval)
        self.cache = NoteCache(self.profile_dir)
        self.refresh = refresh
        self.evidence = EvidenceArchive(Path(evidence_dir or self.profile_dir / "reader-evidence"))
        self._payload_evidence: dict[int, dict] = {}
        self.list_end_reason: str | None = None
        self._source_card_open = False

    async def __aenter__(self) -> XHSReader:
        self.access_pause.check()
        self.runtime.acquire()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        self.access_pause.check()
        self.runtime.acquire()
        if self.context:
            return
        endpoint = None
        if self.connect_existing:
            try:
                endpoint = existing_chrome_endpoint(self.chrome_data_dir)
            except (ReaderError, OSError):
                self.runtime.close()
                raise
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError as exc:
            self.runtime.close()
            raise ReaderError("请先安装依赖：pip install -r requirements-reader.txt") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._playwright = await asyncio.wait_for(
                async_playwright().start(), self.timeout_ms / 1000
            )
        except TimeoutError as exc:
            self.runtime.close()
            raise NetworkReadError("浏览器控制服务启动超时；未发起平台访问") from exc
        if self.connect_existing:
            try:
                print("等待Chrome连接许可：如出现弹窗，请点击允许。", file=sys.stderr, flush=True)
                self._attached_browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=self.timeout_ms
                )
                if not self._attached_browser.contexts:
                    raise ReaderError("已连接Chrome但没有可复用的浏览器会话")
                self.context = self._attached_browser.contexts[0]
                self.page = await self.context.new_page()
                self.page.set_default_timeout(self.timeout_ms)
                self.page.on("response", self._capture_response)
                self.runtime.event("existing_chrome_connected")
                return
            except Error as exc:
                await self.close()
                raise NetworkReadError("连接现有Chrome失败；请检查远程调试开关及连接授权") from exc
            except BaseException:
                await self.close()
                raise
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
                "timeout": self.timeout_ms,
            }
            if channel:
                options["channel"] = channel
                # The Playwright driver inherits its environment before launch options.
                # Locate installed Chrome when the host omits ProgramFiles, without
                # changing the user's process environment or browser profile.
                if os.name == "nt" and channel == "chrome" and "ProgramFiles" not in os.environ:
                    drive = os.environ.get("SystemDrive", "C:")
                    executable = Path(drive + "/Program Files/Google/Chrome/Application/chrome.exe")
                    if executable.is_file():
                        options["executable_path"] = str(executable)
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
            if self.connect_existing:
                if self.page:
                    await self.page.close()
            elif self.context:
                await self.context.close()
        finally:
            self.context = self.page = None
            self._attached_browser = None
            try:
                if self._playwright:
                    await self._playwright.stop()
            finally:
                self._playwright = None
                self.runtime.close()

    def _capture_response(self, response: Any) -> None:
        parts = urlsplit(response.url)
        if (parts.hostname or "").endswith(".xiaohongshu.com") and parts.path in {
            SEARCH_PATH,
            FEED_PATH,
            HOMEFEED_PATH,
        }:
            self._responses.put_nowait(response)

    def _clear_responses(self) -> None:
        self._payload_evidence.clear()
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _payloads(self, path: str, *, keyword: str = "") -> list[dict]:
        payloads = []
        while not self._responses.empty():
            response = self._responses.get_nowait()
            if response.status in (403, 412, 429, 461, 471, 567):
                self.evidence.save(
                    "access-restriction",
                    {"path": urlsplit(response.url).path, "http_status": response.status},
                )
                await self._stop_access(f"网页返回访问限制（HTTP {response.status}）")
            if urlsplit(response.url).path != path:
                continue
            if keyword:
                try:
                    request_data = response.request.post_data_json or {}
                except (ValueError, json.JSONDecodeError):
                    request_data = {}
                if request_data.get("keyword") not in (None, keyword):
                    continue
            if response.status == 401:
                raise LoginRequiredError("登录已失效，请运行 python xhs.py login")
            if response.status >= 400:
                raise NetworkReadError(f"小红书网页接口返回 HTTP {response.status}，停止当前任务")
            try:
                payload = await asyncio.wait_for(response.json(), self.timeout_ms / 1000)
            except TimeoutError as exc:
                raise NetworkReadError("网页响应读取超时，停止当前任务") from exc
            except Exception as exc:
                raise ReaderError("网页接口没有返回可读取的 JSON") from exc
            if not isinstance(payload, dict):
                raise ReaderError("网页接口响应结构发生变化")
            self._payload_evidence[id(payload)] = self.evidence.save(
                (
                    "discovery-response"
                    if path == HOMEFEED_PATH
                    else "search-response"
                    if path == SEARCH_PATH
                    else "detail-response"
                ),
                payload,
            )
            message = str(payload.get("msg") or payload.get("message") or "")
            for marker in WARNING_MARKERS:
                if marker in message:
                    await self._stop_access("网页行为提醒：" + marker)
            if payload.get("success") is False:
                code = payload.get("code")
                if code in (-100, -100100, -101):
                    raise LoginRequiredError("请运行 python xhs.py login 更新登录状态")
                raise ReaderError(
                    f"网页接口失败：{payload.get('msg') or payload.get('message') or code}"
                )
            payloads.append(payload)
        return payloads

    def _attach_evidence(self, note: Note, reference: dict, locator: str) -> Note:
        note.observed_at = reference["observed_at"]
        note.evidence = [{**reference, "locator": reference["locator"] + locator}]
        return note

    async def _goto(self, url: str, *, kind: str = "navigation") -> None:
        self.access_pause.check()
        from playwright.async_api import Error

        async def check_page() -> None:
            if self.page.url.startswith("https://www.xiaohongshu.com/"):
                try:
                    await self._check_access()
                except LoginRequiredError:
                    if kind != "login":
                        raise

        await check_page()
        for attempt in range(2):
            await self.runtime.before(kind, self.access_pause.check)
            await check_page()
            try:
                response = await self.page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                if response is not None:
                    status = response.status
                    if status in (403, 412, 429, 461, 471, 567):
                        self.evidence.save(
                            "access-restriction",
                            {"path": urlsplit(url).path, "http_status": status},
                        )
                        await self._stop_access(f"页面返回访问限制（HTTP {status}）")
                    if status == 401:
                        raise LoginRequiredError("页面要求登录，停止当前任务")
                    if status >= 400:
                        raise NetworkReadError(f"页面返回 HTTP {status}，停止当前任务")
                return
            except Error as exc:
                self.runtime.event("navigation_failed", kind=kind, attempt=attempt + 1)
                await self._check_access()
                for path in (SEARCH_PATH, FEED_PATH, HOMEFEED_PATH):
                    await self._payloads(path)
                if attempt or not any(
                    marker in str(exc)
                    for marker in ("net::ERR_CONNECTION", "net::ERR_NETWORK", "net::ERR_TIMED_OUT")
                ):
                    raise NetworkReadError("页面加载失败，已保存进度并停止访问") from exc
                await asyncio.sleep(max(self.interval, 15))

    async def _state(self) -> dict:
        from playwright.async_api import Error

        try:
            return await asyncio.wait_for(self.page.evaluate(READ_STATE), self.timeout_ms / 1000)
        except TimeoutError as exc:
            raise NetworkReadError("页面状态读取超时，停止当前任务") from exc
        except Error:
            return {}

    async def _check_access(self, *, require_login: bool = False) -> None:
        self.access_pause.check()
        # Only active UI warnings, not a note discussing these phrases.
        warnings = self.page.locator(
            '[role="dialog"], [role="alert"], .verify-container, .captcha-container, '
            ".risk-control-modal, .warning-modal, .el-message, .el-message-box"
        )
        for marker in WARNING_MARKERS:
            if await warnings.filter(has_text=marker).first.is_visible():
                await self._stop_access("页面行为提醒：" + marker)
        url = self.page.url.lower()
        if any(marker in url for marker in ("/captcha", "/website-login/captcha", "/sorry")):
            await self._stop_access("页面要求验证")
        if "/login" in url:
            raise LoginRequiredError("需要登录，请先运行 python xhs.py login")
        for selector in (".captcha-container", "#captcha-container", ".verify-container"):
            if await self.page.locator(selector).first.is_visible():
                await self._stop_access("页面要求验证")
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

    async def _stop_access(self, reason: str) -> None:
        self.runtime.event(
            "access_warning", reason=reason, attribution="unknown; warning may be delayed"
        )
        if self.page is not None:
            try:
                path = self.profile_dir / f"reader-warning-{time.time_ns()}.png"
                await self.page.screenshot(path=str(path), timeout=3000)
            except Exception:
                self.runtime.event("warning_screenshot_unavailable")
        self.access_pause.stop(reason)

    async def login(self, timeout_seconds: int = 300) -> None:
        if self.headless:
            raise LoginRequiredError("扫码登录需要有窗口模式，请去掉 --headless")
        if timeout_seconds <= 0:
            raise ValueError("登录等待时间必须大于 0")
        await self.start()
        await self._goto(HOME_URL, kind="login")
        deadline = time.monotonic() + timeout_seconds
        prompted = False
        while time.monotonic() < deadline:
            try:
                await self._check_access()
            except LoginRequiredError:
                pass  # Expected during login; verification and warnings still stop.
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
        limit: int = 5,
        max_scrolls: int = 2,
        on_progress: Callable[[list[Note]], None] | None = None,
    ) -> list[Note]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        query = urlencode({"keyword": keyword, "source": "web_explore_feed"})
        return await self._list_notes(
            f"https://www.xiaohongshu.com/search_result?{query}",
            SEARCH_PATH,
            cache_kind="search",
            keyword=keyword,
            limit=limit,
            max_scrolls=max_scrolls,
            on_progress=on_progress,
        )

    async def discover(
        self,
        *,
        limit: int = 5,
        max_scrolls: int = 2,
        on_progress: Callable[[list[Note]], None] | None = None,
    ) -> list[Note]:
        """Read this profile's normal recommendation feed, not a global hot chart."""
        return await self._list_notes(
            HOME_URL,
            HOMEFEED_PATH,
            cache_kind="discover",
            limit=limit,
            max_scrolls=max_scrolls,
            on_progress=on_progress,
        )

    async def _list_notes(
        self,
        url: str,
        response_path: str,
        *,
        cache_kind: str,
        keyword: str = "",
        limit: int = 5,
        max_scrolls: int = 2,
        on_progress: Callable[[list[Note]], None] | None = None,
    ) -> list[Note]:
        if limit <= 0 or not 0 <= max_scrolls <= 2:
            raise ValueError("limit 必须大于0，max-scrolls范围0–2")
        self.access_pause.check()
        self.runtime.acquire()
        cache_key = json.dumps([keyword, limit, max_scrolls], ensure_ascii=False)
        cached = None if self.refresh else self.cache.get(cache_kind, cache_key)
        if cached is not None:
            self.list_end_reason = "cached_bounded_result"
            self.runtime.event(cache_kind + "_cache_hit", count=len(cached))
            if on_progress:
                on_progress(cached)
            return cached
        await self.start()
        self._clear_responses()
        await self._goto(url, kind="search")
        found: dict[str, Note] = {}
        idle = scrolls = 0
        received = exhausted = False
        initial_deadline = time.monotonic() + self.timeout_ms / 1000
        page_deadline: float | None = None
        while True:
            await asyncio.sleep(0.3)
            before = len(found)
            for payload in await self._payloads(response_path, keyword=keyword):
                page_deadline = None
                received = True
                data = payload.get("data") or {}
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ReaderError("搜索接口数据结构发生变化，未找到 items 列表")
                exhausted = data.get("has_more", data.get("hasMore")) is False
                reference = self._payload_evidence[id(payload)]
                for index, item in enumerate(data["items"]):
                    if not isinstance(item, dict) or item.get("model_type", "note") not in (
                        "note",
                        "note_card",
                    ):
                        continue
                    try:
                        note = parse_note(item)
                    except ReaderError:
                        continue
                    if (
                        cache_kind == "search"
                        and "xsec_token=" in note.url
                        and "xsec_source=" not in note.url
                    ):
                        note.url += "&xsec_source=pc_search"
                    note.access = {
                        "url": note.url,
                        "source_url": self.page.url,
                        "method": "navigate",
                    }
                    note.content_scope = "search_card"
                    self.cache.put("access", note.note_id, [note])
                    found[note.note_id] = self._attach_evidence(
                        note, reference, f"/data/items/{index}"
                    )
            await self._check_access(require_login=True)
            # API 结构变更时，仍可保留当前页面可见卡片及其完整访问令牌。
            try:
                cards = await asyncio.wait_for(
                    self.page.evaluate(READ_CARDS), self.timeout_ms / 1000
                )
            except TimeoutError as exc:
                raise NetworkReadError("可见搜索卡片读取超时，停止当前任务") from exc
            for card in cards:
                try:
                    visible = parse_visible_card(card, source_url=self.page.url)
                except ValueError:
                    continue
                note_id = visible.note_id
                if note_id and note_id not in found:
                    found[note_id] = self._attach_evidence(
                        visible,
                        self.evidence.save("visible-card", card),
                        "",
                    )
                    self.cache.put("access", note_id, [found[note_id]])
                elif note_id in found and "xsec_token=" not in found[note_id].url:
                    found[note_id].url = visible.url
                    found[note_id].access = visible.access
                    self.cache.put("access", note_id, [found[note_id]])
            notes = list(found.values())[:limit]
            if on_progress and len(found) != before:
                on_progress(notes)
            if len(found) >= limit or exhausted:
                self.list_end_reason = (
                    "entry_exhausted" if exhausted and len(found) <= limit else "limit"
                )
                return self._save_list(cache_kind, cache_key, notes)
            if page_deadline is not None and len(found) == before:
                if time.monotonic() >= page_deadline:
                    raise NetworkReadError("搜索翻页等待超时，已有结果保留，未缓存为完整搜索")
                continue
            page_deadline = None
            if not found and not received:
                if (
                    cache_kind == "search"
                    and await self.page.get_by_text(
                        "没有找到相关结果", exact=False
                    ).first.is_visible()
                ):
                    return self._save_list(cache_kind, cache_key, [])
                if time.monotonic() >= initial_deadline:
                    raise NetworkReadError(
                        "未读到搜索结果：请检查登录、网络或页面结构；本次已抓取结果会保留"
                    )
                continue
            idle = idle + 1 if len(found) == before else 0
            if idle >= 2 or scrolls >= min(max_scrolls, 2):
                self.list_end_reason = "no_new_results" if idle >= 2 else "scroll_budget"
                return self._save_list(cache_kind, cache_key, notes)
            await self.runtime.before("scroll", self.access_pause.check)
            await self._check_access(require_login=True)
            await self.page.mouse.wheel(0, 1400)
            scrolls += 1
            page_deadline = time.monotonic() + self.timeout_ms / 1000

    def _save_list(self, kind: str, key: str, notes: list[Note]) -> list[Note]:
        self.cache.put(kind, key, notes)
        return notes

    async def get_note(self, value: str) -> Note:
        url = normalize_note_url(value)
        self.access_pause.check()
        self.runtime.acquire()
        cache_key = note_id_from_url(url) or url
        cached = None if self.refresh else self.cache.get("detail", cache_key)
        if cached:
            self.runtime.event("detail_cache_hit", note_id=cached[0].note_id)
            return cached[0]
        await self.start()
        self._clear_responses()
        access_rows = self.cache.get("access", cache_key)
        access = access_rows[0].access if access_rows else {}
        if "xsec_token=" not in url and access.get("method") == "source_card":
            await self._open_source_card(url, access)
        else:
            if access.get("url"):
                captured = normalize_note_url(access["url"])
                if note_id_from_url(captured) == cache_key and (
                    "xsec_token=" not in url or captured == url
                ):
                    url = access["url"]
            await self._goto(url, kind="detail")
        note = await self._read_note(url)
        note.content_scope = "detail"
        note.access = {
            "url": url,
            "source_url": access.get("source_url"),
            "method": access.get("method", "navigate"),
        }
        # Only explicit tracks attached to the normal page's video element.
        # Absence means not observed; never invoke OCR/ASR or infer a track URL.
        tracks = await self.page.locator("video track[src]").evaluate_all(
            "els => els.slice(0, 5).map(el => ({url: el.src, language: el.srclang || '', label: el.label || '', kind: el.kind || ''}))"
        )
        if not isinstance(tracks, list) or any(not isinstance(track, dict) for track in tracks):
            raise ReaderError("页面字幕轨道结构变化")
        note.subtitle_tracks = tracks
        self.cache.put("detail", note.note_id, [note])
        if cache_key != note.note_id:
            self.cache.put("detail", cache_key, [note])
        self.runtime.event("detail_saved", note_id=note.note_id)
        return note

    async def _open_source_card(self, url: str, access: dict) -> None:
        source = access.get("source_url", "")
        parts = urlsplit(source)
        if parts.hostname not in NOTE_HOSTS or parts.path not in ("/search_result", "/explore"):
            raise ReaderError("缺少有效来源页，不能把无访问参数的canonical URL当详情入口")
        if self.page.url != source or self._source_card_open:
            await self._goto(source, kind="search")
            self._source_card_open = False
        wanted = note_id_from_url(url)
        link = self.page.locator(f'section.note-item a[href*="/{wanted}"]').first
        from playwright.async_api import Error

        try:
            await link.wait_for(state="visible", timeout=self.timeout_ms)
        except Error as exc:
            await self._check_access(require_login=True)
            raise ReaderError("原来源页未显示目标笔记卡片，保留访问缺口，不猜其他链接") from exc
        await self.runtime.before("detail", self.access_pause.check)
        await self._check_access(require_login=True)
        await link.click(timeout=self.timeout_ms)
        self._source_card_open = True

    async def _read_note(self, url: str) -> Note:
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            await self._check_access()
            current_url = self.page.url
            if urlsplit(current_url).hostname in SHORT_HOSTS:
                continue
            try:
                resolved = normalize_note_url(current_url)
            except ValueError:
                resolved = ""
            wanted = note_id_from_url(url) or note_id_from_url(resolved)
            original_id = note_id_from_url(url)
            resolved_id = note_id_from_url(resolved)
            if original_id and resolved_id and resolved_id != original_id:
                raise ReaderError("笔记跳转后的 ID 与请求不一致，不关联到原来源")
            # 保留原链接中的 token，即使网页路由移除了 query。
            source_url = (
                url
                if note_id_from_url(url) == wanted
                and parse_qs(urlsplit(url).query).get("xsec_token")
                else resolved or url
            )
            for payload in await self._payloads(FEED_PATH):
                data = payload.get("data") or {}
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ReaderError("详情接口数据结构发生变化，未找到 items 列表")
                for index, item in enumerate(data["items"]):
                    if not isinstance(item, dict):
                        continue
                    try:
                        note = parse_note(item, fallback_url=source_url)
                    except ReaderError:
                        continue
                    if note.note_id == wanted:
                        return self._attach_evidence(
                            note, self._payload_evidence[id(payload)], f"/data/items/{index}"
                        )
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
                        note = parse_note(item, fallback_url=source_url)
                        if note.note_id != wanted:
                            raise ReaderError("页面详情 ID 与请求不一致，不关联到原来源")
                        return self._attach_evidence(
                            note, self.evidence.save("detail-state", item), ""
                        )
            for message in ("当前笔记暂时无法浏览", "笔记已删除", "笔记不存在"):
                if await self.page.get_by_text(message, exact=False).first.is_visible():
                    raise ReaderError(message + "；请使用包含 xsec_token 的完整分享链接")
            if not resolved and urlsplit(current_url).path not in ("/search_result", "/explore"):
                raise ReaderError("笔记链接跳转到非笔记页面，未取得匹配ID的详情")
        raise NetworkReadError("笔记详情等待超时；未取得详情，停止当前任务")

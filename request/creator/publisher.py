# ruff: noqa: BLE001, S110, S112
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self

# 页面元素会在 Vue 重渲染时短暂失效；轮询分支需要忽略这些瞬时异常。

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Locator, Page, Playwright


CREATOR_URL = "https://creator.xiaohongshu.com"
PUBLISH_URL = f"{CREATOR_URL}/publish/publish"
IMAGE_PUBLISH_URL = f"{PUBLISH_URL}?source=official&from=tab_switch&target=image"
VIDEO_PUBLISH_URL = f"{PUBLISH_URL}?source=official&from=tab_switch&target=video"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}

# 2026 年创作中心把发布按钮放进了 closed shadow root。导航前仅把这个
# Web Component 的 shadow root 改为 open，Playwright 才能点击内部真实按钮。
OPEN_PUBLISH_BUTTON_SHADOW = r"""
(() => {
    const original = Element.prototype.attachShadow;
    if (original.__redcrackPatched) return;

    function patched(init) {
        if (this && this.localName === "xhs-publish-btn") {
            init = Object.assign({}, init, {mode: "open"});
        }
        return original.call(this, init);
    }

    Object.defineProperty(patched, "__redcrackPatched", {value: true});
    Element.prototype.attachShadow = patched;
})();
"""


class PublishError(RuntimeError):
    """发布流程失败。"""


class LoginRequiredError(PublishError):
    """创作服务平台尚未登录。"""


@dataclass(slots=True)
class PublishResult:
    status: Literal["published", "ready"]
    kind: Literal["image", "video"]
    title: str
    media_count: int
    url: str
    message: str


def _normalize_tags(tags: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw).strip().lstrip("#").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result[:10]


def _validate_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValueError("标题不能为空")
    if len(title) > 20:
        raise ValueError(f"标题不能超过 20 个字符，当前为 {len(title)} 个字符")
    return title


def _validate_media_paths(
    paths: Sequence[str | Path], allowed_suffixes: set[str], media_name: str
) -> list[Path]:
    if not paths:
        raise ValueError(f"至少需要一个{media_name}文件")

    result: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{media_name}文件不存在: {path}")
        if path.suffix.lower() not in allowed_suffixes:
            supported = ", ".join(sorted(allowed_suffixes))
            raise ValueError(f"不支持的{media_name}格式: {path.suffix}；支持 {supported}")
        result.append(path)
    return result


class XHSCreatorPublisher:
    """通过小红书创作服务平台发布图文或视频笔记。

    浏览器使用独立的持久化 profile。首次以有头模式启动并扫码登录后，
    后续可复用登录状态执行 headless 发布。
    """

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        headless: bool = False,
        browser_channel: str = "auto",
        timeout_ms: int = 60_000,
    ) -> None:
        self.profile_dir = Path(
            profile_dir or Path.home() / ".redcrack" / "xhs_creator_profile"
        ).expanduser().resolve()
        self.headless = headless
        self.browser_channel = browser_channel
        self.timeout_ms = timeout_ms

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器尚未启动，请先调用 start()")
        return self._page

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if self._context is not None:
            return

        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "缺少 Playwright，请执行: pip install playwright"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()

        base_options: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "locale": "zh-CN",
            "viewport": {"width": 1440, "height": 960},
            "args": ["--disable-dev-shm-usage"],
        }

        channels: list[str | None]
        if self.browser_channel == "auto":
            channels = ["chrome", None]
        elif self.browser_channel in {"", "chromium"}:
            channels = [None]
        else:
            channels = [self.browser_channel]

        errors: list[str] = []
        for channel in channels:
            options = dict(base_options)
            if channel:
                options["channel"] = channel
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **options
                )
                break
            except Exception as exc:  # Playwright 不同版本异常类型不同
                errors.append(f"{channel or 'chromium'}: {exc}")

        if self._context is None:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError("无法启动浏览器：" + " | ".join(errors))

        self._context.set_default_timeout(self.timeout_ms)
        self._context.set_default_navigation_timeout(self.timeout_ms)
        await self._context.add_init_script(OPEN_PUBLISH_BUTTON_SHADOW)

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def login(self, timeout_seconds: int = 300) -> None:
        """打开创作中心并等待用户完成扫码登录。"""
        await self.start()
        if self.headless:
            raise LoginRequiredError("首次登录必须使用有窗口模式")

        await self._goto(IMAGE_PUBLISH_URL)
        if await self._is_logged_in():
            return

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await self._is_logged_in():
                await self._goto(IMAGE_PUBLISH_URL)
                if await self._is_logged_in():
                    return
            await asyncio.sleep(1)
        raise LoginRequiredError(f"等待扫码登录超时（{timeout_seconds} 秒）")

    async def ensure_login(self, timeout_seconds: int = 300) -> None:
        if await self._is_logged_in():
            return
        if self.headless:
            raise LoginRequiredError(
                "登录状态不存在或已过期；请先运行 `python publish.py login`"
            )
        await self.login(timeout_seconds=timeout_seconds)

    async def publish_image_note(
        self,
        *,
        title: str,
        content: str,
        images: Sequence[str | Path],
        tags: Iterable[str] | None = None,
        auto_publish: bool = True,
    ) -> PublishResult:
        """发布图文笔记；``auto_publish=False`` 时只填表不点击发布。"""
        title = _validate_title(title)
        image_paths = _validate_media_paths(images, IMAGE_SUFFIXES, "图片")
        normalized_tags = _normalize_tags(tags)

        await self.start()
        await self._goto(IMAGE_PUBLISH_URL)
        await self.ensure_login()
        await self._goto(IMAGE_PUBLISH_URL)
        await self._select_publish_tab("上传图文")
        await self._upload_images(image_paths)
        await self._fill_note_form(title, content, normalized_tags)

        if not auto_publish:
            return PublishResult(
                status="ready",
                kind="image",
                title=title,
                media_count=len(image_paths),
                url=self.page.url,
                message="图文表单已填写，尚未点击发布",
            )

        await self._click_publish_button()
        await self._wait_publish_success()
        return PublishResult(
            status="published",
            kind="image",
            title=title,
            media_count=len(image_paths),
            url=self.page.url,
            message="图文笔记已提交发布",
        )

    async def publish_video_note(
        self,
        *,
        title: str,
        content: str,
        video: str | Path,
        tags: Iterable[str] | None = None,
        auto_publish: bool = True,
    ) -> PublishResult:
        """发布单个视频笔记。"""
        title = _validate_title(title)
        video_path = _validate_media_paths([video], VIDEO_SUFFIXES, "视频")[0]
        normalized_tags = _normalize_tags(tags)

        await self.start()
        await self._goto(VIDEO_PUBLISH_URL)
        await self.ensure_login()
        await self._goto(VIDEO_PUBLISH_URL)
        await self._select_publish_tab("上传视频")

        file_input = await self._find_file_input("video", timeout_seconds=30)
        await file_input.set_input_files(str(video_path))
        await self._wait_for_form(timeout_seconds=180)
        await self._fill_note_form(title, content, normalized_tags)

        if not auto_publish:
            return PublishResult(
                status="ready",
                kind="video",
                title=title,
                media_count=1,
                url=self.page.url,
                message="视频表单已填写，尚未点击发布",
            )

        # 视频仍在处理时 submit-disabled 会保持 true，这里最长等待 10 分钟。
        await self._click_publish_button(timeout_seconds=600)
        await self._wait_publish_success(timeout_seconds=60)
        return PublishResult(
            status="published",
            kind="video",
            title=title,
            media_count=1,
            url=self.page.url,
            message="视频笔记已提交发布",
        )

    async def _goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            # 创作中心有长连接，networkidle 超时不代表页面不可用。
            pass

    async def _is_logged_in(self) -> bool:
        if self._page is None:
            return False
        current_url = self.page.url.lower()
        if "/login" in current_url or "redirectreason=401" in current_url:
            return False

        logged_in_markers = [
            "div.upload-content",
            "div.creator-tab",
            "input[type=file]",
            "xhs-publish-btn",
        ]
        for selector in logged_in_markers:
            locator = self.page.locator(selector)
            try:
                if await locator.count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _select_publish_tab(self, tab_name: str) -> None:
        # target=image/video 通常已经切好 TAB；仍保留点击逻辑以兼容重定向。
        await self._dismiss_popovers()
        tabs = self.page.locator("div.creator-tab")
        try:
            count = await tabs.count()
        except Exception:
            count = 0
        for index in range(count):
            tab = tabs.nth(index)
            try:
                if (await tab.inner_text()).strip() == tab_name and await tab.is_visible():
                    classes = (await tab.get_attribute("class")) or ""
                    if not re.search(r"(^|\s)(active|selected)(\s|$)", classes):
                        await tab.click()
                        await asyncio.sleep(0.8)
                    return
            except Exception:
                continue

    async def _dismiss_popovers(self) -> None:
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            await self.page.evaluate(
                """() => document.querySelectorAll('div.d-popover').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width && r.height) el.remove();
                })"""
            )
        except Exception:
            pass

    async def _find_file_input(
        self, kind: Literal["image", "video"], *, timeout_seconds: int
    ) -> Locator:
        deadline = time.monotonic() + timeout_seconds
        fallback: Locator | None = None
        while time.monotonic() < deadline:
            inputs = self.page.locator("input[type=file]")
            try:
                count = await inputs.count()
            except Exception:
                count = 0
            for index in range(count):
                candidate = inputs.nth(index)
                accept = ((await candidate.get_attribute("accept")) or "").lower()
                if fallback is None:
                    fallback = candidate
                if kind == "image" and (
                    "image/" in accept
                    or any(suffix in accept for suffix in IMAGE_SUFFIXES)
                ):
                    return candidate
                if kind == "video" and (
                    "video/" in accept
                    or any(suffix in accept for suffix in VIDEO_SUFFIXES)
                ):
                    return candidate
            if fallback is not None:
                return fallback
            await asyncio.sleep(0.25)
        raise PublishError(f"未找到{kind}文件上传输入框")

    async def _upload_images(self, paths: Sequence[Path]) -> None:
        for index, path in enumerate(paths, start=1):
            file_input = await self._find_file_input("image", timeout_seconds=30)
            await file_input.set_input_files(str(path))
            await self._wait_image_upload_count(index, timeout_seconds=60)
            await asyncio.sleep(0.5)
        await self._wait_for_form(timeout_seconds=30)

    async def _wait_image_upload_count(
        self, expected_count: int, *, timeout_seconds: int
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        preview_selectors = [
            ".img-preview-area .pr",
            ".img-preview-area .img-preview-item",
            ".image-list .image-item",
        ]
        while time.monotonic() < deadline:
            counts = []
            for selector in preview_selectors:
                try:
                    counts.append(await self.page.locator(selector).count())
                except Exception:
                    pass
            if counts and max(counts) >= expected_count:
                return
            await asyncio.sleep(0.5)
        raise PublishError(f"第 {expected_count} 张图片上传超时")

    async def _wait_for_form(self, *, timeout_seconds: int) -> None:
        await self._first_visible(
            [
                "div.d-input input",
                "#title-textarea",
                'input[placeholder*="标题"]',
                'textarea[placeholder*="标题"]',
            ],
            timeout_seconds=timeout_seconds,
            description="标题输入框",
        )

    async def _fill_note_form(
        self, title: str, content: str, tags: Sequence[str]
    ) -> None:
        title_input = await self._first_visible(
            [
                "div.d-input input",
                "#title-textarea",
                'input[placeholder*="标题"]',
                'textarea[placeholder*="标题"]',
            ],
            timeout_seconds=30,
            description="标题输入框",
        )
        await title_input.fill(title)

        editor = await self._find_content_editor(timeout_seconds=30)
        await editor.fill(content)
        await self._append_tags(editor, tags)
        await self._raise_form_validation_error()

    async def _find_content_editor(self, *, timeout_seconds: int) -> Locator:
        try:
            return await self._first_visible(
                [
                    'div[role="textbox"][contenteditable="true"]',
                    'div.tiptap[contenteditable="true"]',
                    "div.ql-editor",
                ],
                timeout_seconds=timeout_seconds,
                description="正文输入框",
            )
        except PublishError:
            placeholders = self.page.locator('p[data-placeholder*="输入正文描述"]')
            count = await placeholders.count()
            for index in range(count):
                current = placeholders.nth(index)
                for _ in range(5):
                    current = current.locator("xpath=..")
                    if (await current.get_attribute("contenteditable")) == "true":
                        return current
            raise

    async def _append_tags(self, editor: Locator, tags: Sequence[str]) -> None:
        if not tags:
            return

        await editor.click()
        await editor.evaluate(
            """el => {
                const range = document.createRange();
                range.selectNodeContents(el);
                range.collapse(false);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
        await self.page.keyboard.press("Enter")

        for tag in tags:
            await editor.type(f"#{tag}", delay=35)
            option = self.page.locator("#creator-editor-topic-container .item").first
            try:
                await option.wait_for(state="visible", timeout=1_500)
                await option.click()
            except Exception:
                await editor.type(" ")
            await asyncio.sleep(0.25)

    async def _raise_form_validation_error(self) -> None:
        selectors = [
            "div.title-container div.max_suffix",
            "div.edit-container div.length-error",
        ]
        for selector in selectors:
            locator = self.page.locator(selector)
            count = await locator.count()
            for index in range(count):
                item = locator.nth(index)
                if await item.is_visible():
                    text = (await item.inner_text()).strip()
                    if text:
                        raise PublishError(f"表单校验失败: {text}")

    async def _first_visible(
        self,
        selectors: Sequence[str],
        *,
        timeout_seconds: int,
        description: str,
    ) -> Locator:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    continue
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            await asyncio.sleep(0.25)
        raise PublishError(f"未找到{description}")

    async def _click_publish_button(self, *, timeout_seconds: int = 30) -> None:
        kind, button = await self._wait_publish_button(timeout_seconds=timeout_seconds)
        await button.scroll_into_view_if_needed()

        if kind == "widget":
            # init script 已把 xhs-publish-btn 的 closed shadow root 改为 open。
            inner_buttons = button.locator("button")
            count = await inner_buttons.count()
            for index in range(count):
                inner = inner_buttons.nth(index)
                text = (await inner.inner_text()).strip()
                if text.startswith("发布") and await inner.is_visible():
                    await inner.click()
                    return

            # 页面若在脚本注入前创建了组件，则按当前组件布局点击右侧 65% 位置。
            box = await button.bounding_box()
            if not box:
                raise PublishError("发布按钮没有可点击区域")
            await self.page.mouse.click(
                box["x"] + box["width"] * 0.65,
                box["y"] + box["height"] * 0.5,
            )
            return

        await button.click()

    async def _wait_publish_button(
        self, *, timeout_seconds: int
    ) -> tuple[Literal["widget", "button"], Locator]:
        deadline = time.monotonic() + timeout_seconds
        last_reason = ""
        while time.monotonic() < deadline:
            widgets = self.page.locator("xhs-publish-btn")
            try:
                count = await widgets.count()
            except Exception:
                count = 0
            for index in range(count):
                widget = widgets.nth(index)
                if not await widget.is_visible():
                    continue
                is_publish = await widget.get_attribute("is-publish")
                disabled = await widget.get_attribute("submit-disabled")
                loading = await widget.get_attribute("submit-loading")
                if is_publish == "false":
                    continue
                if disabled == "true":
                    last_reason = "新版发布按钮仍处于禁用状态"
                    continue
                if loading == "true":
                    last_reason = "新版发布按钮仍在处理中"
                    continue
                return "widget", widget

            old_selectors = [
                ".publish-page-publish-btn button.bg-red",
                "div.bottom button.submit",
                'button:has-text("发布")',
            ]
            for selector in old_selectors:
                buttons = self.page.locator(selector)
                try:
                    old_count = await buttons.count()
                except Exception:
                    continue
                for index in range(old_count):
                    button = buttons.nth(index)
                    if not await button.is_visible():
                        continue
                    if await button.is_disabled():
                        last_reason = "发布按钮仍处于禁用状态"
                        continue
                    if (await button.get_attribute("aria-disabled")) == "true":
                        last_reason = "发布按钮 aria-disabled=true"
                        continue
                    return "button", button
            await asyncio.sleep(0.5)

        raise PublishError(last_reason or "等待发布按钮超时")

    async def _wait_publish_success(self, *, timeout_seconds: int = 30) -> None:
        deadline = time.monotonic() + timeout_seconds
        success_pattern = re.compile(r"发布成功|提交成功|审核中")
        while time.monotonic() < deadline:
            if "/publish/publish" not in self.page.url:
                return
            success_text = self.page.get_by_text(success_pattern)
            try:
                count = await success_text.count()
                for index in range(min(count, 10)):
                    if await success_text.nth(index).is_visible():
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)

        screenshot = self.profile_dir / "last_publish_failure.png"
        try:
            await self.page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            screenshot = Path("（截图保存失败）")
        raise PublishError(
            "点击发布后未检测到成功状态；页面可能存在校验提示。"
            f"失败现场截图: {screenshot}"
        )

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from request.reader.browser import XHSReader
from request.reader.errors import ReaderError
from request.reader.existing import existing_chrome_endpoint


class ExistingChromeTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_and_disconnect_preserve_user_browser_and_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chrome_data = root / "chrome"
            chrome_data.mkdir()
            (chrome_data / "DevToolsActivePort").write_text(
                "12345\n/devtools/browser/test\n", encoding="utf-8"
            )
            reader = XHSReader(root / "budget", connect_existing=True, chrome_data_dir=chrome_data)
            driver = MagicMock()
            driver.stop = AsyncMock()
            browser = MagicMock()
            context = MagicMock()
            owned_page = MagicMock()
            owned_page.close = AsyncMock()
            context.close = AsyncMock()
            context.new_page = AsyncMock(return_value=owned_page)
            browser.close = AsyncMock()
            browser.contexts = [context]
            driver.chromium.connect_over_cdp = AsyncMock(return_value=browser)
            with patch("playwright.async_api.async_playwright") as start:
                start.return_value.start = AsyncMock(return_value=driver)
                async with reader:
                    await reader.start()
                    self.assertIs(reader.page, owned_page)
            driver.chromium.connect_over_cdp.assert_awaited_once_with(
                "ws://127.0.0.1:12345/devtools/browser/test", timeout=30000
            )
            driver.chromium.launch_persistent_context.assert_not_called()
            context.new_page.assert_awaited_once()
            owned_page.close.assert_awaited_once()
            context.close.assert_not_awaited()
            browser.close.assert_not_awaited()
            driver.stop.assert_awaited_once()
            self.assertIsNone(reader.runtime.handle)

    async def test_missing_user_approval_does_not_launch_another_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(
                Path(folder) / "budget", connect_existing=True, chrome_data_dir=folder
            )
            with patch("playwright.async_api.async_playwright") as start:
                with self.assertRaisesRegex(ReaderError, "chrome://inspect"):
                    await reader.start()
                start.assert_not_called()
            self.assertIsNone(reader.runtime.handle)

    async def test_bad_connection_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "DevToolsActivePort"
            path.write_text("12345\nhttps://other.invalid/\n", encoding="utf-8")
            with self.assertRaises(ReaderError):
                existing_chrome_endpoint(folder)

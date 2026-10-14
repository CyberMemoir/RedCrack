import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from request.reader.access import AccessPause
from request.reader.browser import SEARCH_PATH, XHSReader
from request.reader.errors import VerificationRequiredError


class AccessPauseTests(unittest.TestCase):
    def test_warning_survives_restart_until_explicit_acknowledgement(self):
        with tempfile.TemporaryDirectory() as folder:
            pause = AccessPause(Path(folder))
            with self.assertRaises(VerificationRequiredError):
                pause.stop("操作过于频繁")
            restarted = AccessPause(Path(folder))
            with self.assertRaises(VerificationRequiredError):
                restarted.check()
            restarted.acknowledge("人工处理页面提醒后检查")
            restarted.check()
            self.assertIn("操作过于频繁", (Path(folder)/"reader-access-history.jsonl").read_text(encoding="utf-8"))

    def test_empty_acknowledgement_keeps_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            pause = AccessPause(Path(folder))
            with self.assertRaises(VerificationRequiredError):
                pause.stop("访问提醒")
            with self.assertRaises(ValueError):
                pause.acknowledge(" ")
            with self.assertRaises(VerificationRequiredError):
                pause.check()


class ResponseWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_http_with_behavior_warning_also_pauses(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = XHSReader(folder)
            response = AsyncMock()
            response.url = "https://www.xiaohongshu.com" + SEARCH_PATH
            response.status = 200
            response.json.return_value = {"success": False, "msg": "与真人操作习惯不太一致"}
            reader._responses.put_nowait(response)
            try:
                with self.assertRaises(VerificationRequiredError):
                    await reader._payloads(SEARCH_PATH)
                with self.assertRaises(VerificationRequiredError):
                    reader.access_pause.check()
            finally:
                await reader.close()

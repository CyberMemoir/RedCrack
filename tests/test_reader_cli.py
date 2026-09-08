import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import xhs
from request.reader.errors import LoginRequiredError, ReaderError, VerificationRequiredError
from request.reader.models import Note

ID = "68add7d50000000000000001"


class CLITests(unittest.TestCase):
    def test_search_arguments(self):
        args = xhs.build_parser().parse_args(
            ["search", "咖啡", "--download", "--limit", "5", "--headless"]
        )
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.download)

    def test_reject_invalid_bounds(self):
        for option, value in (
            ("--limit", "0"),
            ("--max-scrolls", "-1"),
            ("--interval", "nan"),
            ("--interval", "inf"),
            ("--concurrency", "0"),
        ):
            with (
                self.subTest(option=option),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                xhs.build_parser().parse_args(["search", "咖啡", option, value])

    def test_collect_urls_from_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "links.txt").write_text(f"# comment\n{ID}\n\n", encoding="utf-8-sig")
            (path / "results.json").write_text(json.dumps({"notes": [{"url": ID}]}))
            args = argparse.Namespace(
                urls=[ID], file=str(path / "links.txt"), from_json=str(path / "results.json")
            )
            self.assertEqual(len(xhs.collect_urls(args)), 1)

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text('{"notes": ["not-a-dict"]}')
            with self.assertRaises(ValueError):
                xhs.collect_urls(argparse.Namespace(urls=[], file=None, from_json=str(path)))

    def test_missing_urls_fails_before_browser_start(self):
        with patch("xhs.XHSReader") as reader, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(xhs.main(["download"]), 2)
            reader.assert_not_called()

    def test_exit_codes(self):
        for error, code in (
            (LoginRequiredError("login"), 3),
            (VerificationRequiredError("verify"), 4),
            (ReaderError("fail"), 2),
        ):
            with (
                self.subTest(code=code),
                patch("xhs.run", AsyncMock(side_effect=error)),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(xhs.main(["search", "test"]), code)


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_batch_continues_after_note_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            args = xhs.build_parser().parse_args(
                ["download", ID, "--metadata-only", "--interval", "0", "-o", folder]
            )
            reader = AsyncMock()
            reader.get_note.side_effect = [
                ReaderError("not available"),
                Note(ID, title="成功", url=ID),
            ]
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = await xhs._download_batch(reader, ["first", "second"], args)
            self.assertEqual(code, 2)
            batch = json.loads((Path(folder) / "batch.json").read_text())
            self.assertTrue(batch["complete"])
            self.assertEqual([r["status"] for r in batch["attempts"]], ["failed", "metadata_only"])
            self.assertTrue((Path(folder) / ID / "note.md").exists())

    async def test_login_failure_checkpoints_and_stops_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            args = xhs.build_parser().parse_args(
                ["download", ID, "--metadata-only", "--interval", "0", "-o", folder]
            )
            reader = AsyncMock()
            reader.get_note.side_effect = LoginRequiredError("expired")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(LoginRequiredError):
                await xhs._download_batch(reader, ["first", "second"], args)
            self.assertEqual(reader.get_note.await_count, 1)
            batch = json.loads((Path(folder) / "batch.json").read_text())
            self.assertFalse(batch["complete"])
            self.assertEqual(batch["attempts"][0]["status"], "failed")

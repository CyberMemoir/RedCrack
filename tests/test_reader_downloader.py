import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from request.reader.downloader import MediaDownloader, media_extension, validate_media_url
from request.reader.errors import DownloadError
from request.reader.models import MediaAsset, Note

ID = "68add7d50000000000000001"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7k8AAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class MediaValidationTests(unittest.TestCase):
    def test_allow_cdn(self):
        self.assertEqual(
            validate_media_url("https://sns-img-bd.xhscdn.com/a"), "https://sns-img-bd.xhscdn.com/a"
        )

    def test_reject_foreign_urls(self):
        for url in (
            "http://127.0.0.1/a",
            "https://xhscdn.com.evil.test/a",
            "file:///tmp/a",
            "https://user:pass@xhscdn.com/a",
            "https://xhscdn.com:9000/a",
        ):
            with self.subTest(url=url), self.assertRaises(DownloadError):
                validate_media_url(url)

    def test_file_signatures(self):
        self.assertEqual(media_extension(PNG, "image"), ".png")
        self.assertEqual(media_extension(MP4, "video"), ".mp4")
        self.assertEqual(media_extension(b"RIFFxxxxWEBP", "image"), ".webp")
        with self.assertRaises(DownloadError):
            media_extension(b"<html>failed</html>", "image")


class DownloaderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.output = Path(self.folder.name)
        self.counts = {}

        async def handler(request):
            path = request.path
            self.counts[path] = self.counts.get(path, 0) + 1
            if path == "/redirect":
                raise web.HTTPFound("/image")
            if path == "/evil-redirect":
                raise web.HTTPFound("http://127.0.0.1/private")
            if path == "/html":
                return web.Response(text="<html>login required</html>", content_type="text/html")
            if path == "/fake-image":
                return web.Response(body=b'{"error":true}', content_type="image/png")
            if path == "/missing":
                raise web.HTTPNotFound()
            if path == "/flaky" and self.counts[path] == 1:
                raise web.HTTPServiceUnavailable()
            if path == "/video":
                return web.Response(body=MP4, content_type="video/mp4")
            if path == "/chunked":
                response = web.StreamResponse(headers={"Content-Type": "image/png"})
                await response.prepare(request)
                await response.write(PNG[:10])
                await response.write(PNG[10:])
                await response.write_eof()
                return response
            return web.Response(body=PNG, content_type="image/png")

        app = web.Application()
        app.router.add_get("/{name}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"

        def allow_fixture(url):
            if url.startswith(self.base + "/"):
                return url
            return validate_media_url(url)

        self.validator = patch(
            "request.reader.downloader.validate_media_url", side_effect=allow_fixture
        )
        self.validator.start()

    async def asyncTearDown(self):
        self.validator.stop()
        await self.runner.cleanup()
        self.folder.cleanup()

    def note(self, *paths):
        return Note(
            ID,
            title="下载测试",
            media=[
                MediaAsset("video" if path == "/video" else "image", self.base + path)
                for path in paths
            ],
        )

    async def test_download_stream_and_resume(self):
        async with MediaDownloader(self.output) as downloader:
            first = await downloader.download_note(self.note("/chunked", "/video"))
            self.assertEqual(first["status"], "complete")
            self.assertEqual((self.output / ID / "001-image.png").read_bytes(), PNG)
            self.assertEqual((self.output / ID / "002-video.mp4").read_bytes(), MP4)
            second = await downloader.download_note(self.note("/chunked", "/video"))
            self.assertTrue(all(f["status"] == "skipped" for f in second["files"]))
            self.assertEqual(self.counts["/chunked"], 1)

    async def test_tampered_file_is_replaced(self):
        async with MediaDownloader(self.output) as downloader:
            await downloader.download_note(self.note("/image"))
            (self.output / ID / "001-image.png").write_bytes(b"X" * len(PNG))
            result = await downloader.download_note(self.note("/image"))
            self.assertEqual(result["files"][0]["status"], "downloaded")
            self.assertEqual(self.counts["/image"], 2)

    async def test_changed_asset_not_skipped(self):
        async with MediaDownloader(self.output) as downloader:
            await downloader.download_note(self.note("/image"))
            result = await downloader.download_note(self.note("/other"))
            self.assertEqual(result["files"][0]["status"], "downloaded")

    async def test_redirect(self):
        async with MediaDownloader(self.output) as downloader:
            result = await downloader.download_note(self.note("/redirect", "/evil-redirect"))
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["files"][0]["status"], "downloaded")
            self.assertEqual(result["files"][1]["status"], "failed")

    async def test_errors_do_not_create_fake_media(self):
        async with MediaDownloader(self.output, retries=0) as downloader:
            result = await downloader.download_note(
                self.note("/image", "/html", "/fake-image", "/missing")
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(sum(f["status"] == "failed" for f in result["files"]), 3)
            self.assertEqual(len(list((self.output / ID).glob("*.png"))), 1)
            self.assertFalse(list((self.output / ID).glob("*.part")))
            manifest = json.loads((self.output / ID / "downloads.json").read_text())
            self.assertEqual(len(manifest["files"]), 4)

    async def test_size_limit_with_and_without_content_length(self):
        async with MediaDownloader(self.output, max_bytes=20) as downloader:
            result = await downloader.download_note(self.note("/image", "/chunked"))
            self.assertEqual(result["status"], "partial")
            self.assertTrue(all(f["status"] == "failed" for f in result["files"]))
            self.assertFalse(list((self.output / ID).glob("*.part")))

    async def test_retry_transient_failure(self):
        async with MediaDownloader(self.output, retries=1) as downloader:
            result = await downloader.download_note(self.note("/flaky"))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(self.counts["/flaky"], 2)

    async def test_metadata_without_media_is_not_download_success(self):
        async with MediaDownloader(self.output) as downloader:
            result = await downloader.download_note(self.note())
            self.assertEqual(result["status"], "partial")
            self.assertTrue((self.output / ID / "note.md").is_file())

    async def test_video_without_video_asset_is_partial(self):
        note = self.note("/image")
        note.kind = "video"
        async with MediaDownloader(self.output) as downloader:
            result = await downloader.download_note(note)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["errors"])

    async def test_reject_note_directory_symlink(self):
        with tempfile.TemporaryDirectory() as external:
            (self.output / ID).symlink_to(external, target_is_directory=True)
            async with MediaDownloader(self.output) as downloader:
                with self.assertRaises(DownloadError):
                    await downloader.download_note(self.note("/image"))

    async def test_corrupt_manifest_recovers(self):
        (self.output / ID).mkdir()
        (self.output / ID / "downloads.json").write_text("not json")
        async with MediaDownloader(self.output) as downloader:
            result = await downloader.download_note(self.note("/image"))
            self.assertEqual(result["status"], "complete")

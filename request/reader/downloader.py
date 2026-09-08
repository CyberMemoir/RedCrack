from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .errors import DownloadError
from .export import write_json, write_note
from .models import NOTE_ID, MediaAsset, Note

CDN_DOMAINS = ("xhscdn.com", "xiaohongshu.com")


def validate_media_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        parts.scheme not in ("https", "http")
        or parts.username
        or parts.password
        or parts.port not in (None, 80, 443)
    ):
        raise DownloadError("媒体地址协议、凭据或端口不受支持")
    if not any(host == domain or host.endswith("." + domain) for domain in CDN_DOMAINS):
        raise DownloadError("媒体地址不是小红书 CDN 域名")
    return url


def media_extension(header: bytes, kind: str) -> str:
    """按文件头识别格式，避免把 HTML/JSON 错误页当作素材保存。"""
    if kind == "image":
        if header.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return ".webp"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if header[4:8] == b"ftyp":
            if header[8:12] in (b"avif", b"avis"):
                return ".avif"
            if header[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
                return ".heic"
    elif kind == "video":
        if header[4:8] == b"ftyp":
            return ".mp4"
        if header.startswith(b"\x1a\x45\xdf\xa3"):
            return ".webm"
    raise DownloadError("下载内容不是受支持的图片/视频文件（可能是错误页或未支持的流媒体格式）")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _RetryableDownload(DownloadError):
    pass


class MediaDownloader:
    def __init__(
        self,
        output: str | Path,
        *,
        concurrency: int = 3,
        retries: int = 2,
        max_bytes: int = 512 * 1024 * 1024,
        timeout: float = 120,
    ) -> None:
        if concurrency < 1 or retries < 0 or max_bytes < 1 or timeout <= 0:
            raise ValueError("下载参数必须为正数，retries 允许为 0")
        self.output = Path(output).expanduser().resolve()
        self.retries = retries
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> MediaDownloader:
        self.output.mkdir(parents=True, exist_ok=True)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout, connect=20),
            headers={"Referer": "https://www.xiaohongshu.com/", "User-Agent": "Mozilla/5.0"},
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=True,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _fetch(self, asset: MediaAsset, directory: Path, index: int) -> dict:
        if self._session is None:
            raise RuntimeError("请通过 async with 使用 MediaDownloader")
        url = validate_media_url(asset.url)
        response = None
        temporary: str | None = None
        try:
            for _ in range(6):
                response = await self._session.get(url, allow_redirects=False)
                if response.status not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location")
                response.release()
                if not location:
                    raise DownloadError("媒体重定向缺少目标")
                from urllib.parse import urljoin

                url = validate_media_url(urljoin(url, location))
            else:
                raise DownloadError("媒体重定向次数过多")
            if response.status in (408, 429) or response.status >= 500:
                raise _RetryableDownload(f"媒体服务器暂不可用（HTTP {response.status}）")
            if response.status != 200:
                raise DownloadError(
                    f"媒体下载失败（HTTP {response.status}），可重新读取笔记以刷新媒体链接"
                )
            content_type = response.headers.get("Content-Type", "").lower()
            if any(x in content_type for x in ("text/", "json", "xml")):
                raise DownloadError("媒体服务器返回了文本错误页")
            if response.content_length is not None and response.content_length > self.max_bytes:
                raise DownloadError("媒体超过 --max-size-mb 限制")
            fd, temporary = tempfile.mkstemp(prefix=f".{index:03d}-", suffix=".part", dir=directory)
            digest = hashlib.sha256()
            length = 0
            prefix = bytearray()
            with os.fdopen(fd, "wb") as stream:
                async for chunk in response.content.iter_chunked(128 * 1024):
                    length += len(chunk)
                    if length > self.max_bytes:
                        raise DownloadError("媒体超过 --max-size-mb 限制")
                    if len(prefix) < 64:
                        prefix.extend(chunk[: 64 - len(prefix)])
                    digest.update(chunk)
                    stream.write(chunk)
            if length == 0:
                raise DownloadError("媒体服务器返回空文件")
            extension = media_extension(bytes(prefix), asset.kind)
            filename = f"{index:03d}-{asset.kind}{extension}"
            target = directory / filename
            if target.is_symlink():
                raise DownloadError("拒绝覆盖输出目录中的符号链接")
            os.replace(temporary, target)
            temporary = None
            return {
                "index": index,
                "kind": asset.kind,
                "url": asset.url,
                "file": filename,
                "size": length,
                "sha256": digest.hexdigest(),
                "status": "downloaded",
            }
        finally:
            if response is not None:
                response.release()
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    async def _download_asset(self, asset: MediaAsset, directory: Path, index: int) -> dict:
        async with self._semaphore:
            for attempt in range(self.retries + 1):
                try:
                    return await self._fetch(asset, directory, index)
                except (TimeoutError, _RetryableDownload, aiohttp.ClientError) as exc:
                    if attempt == self.retries:
                        raise DownloadError(
                            f"媒体下载在 {attempt + 1} 次尝试后失败（{type(exc).__name__}）"
                        ) from exc
                    await asyncio.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    @staticmethod
    async def _cached(record: dict, directory: Path, asset: MediaAsset) -> bool:
        name = record.get("file")
        if not isinstance(name, str) or Path(name).name != name or record.get("kind") != asset.kind:
            return False
        if urlsplit(record.get("url", "")).path != urlsplit(asset.url).path:
            return False
        path = directory / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            return False
        # URL 包含临时签名，刷新后变化不应使已完成素材重复下载。
        if path.stat().st_size != record.get("size"):
            return False
        return await asyncio.to_thread(file_sha256, path) == record.get("sha256")

    async def download_note(self, note: Note) -> dict:
        if not NOTE_ID.fullmatch(note.note_id):
            raise DownloadError("无效笔记 ID")
        directory = self.output / note.note_id
        if directory.is_symlink():
            raise DownloadError("笔记输出目录不能是符号链接")
        directory.mkdir(parents=True, exist_ok=True)
        write_note(note, directory)
        manifest_path = directory / "downloads.json"
        previous: dict[int, dict] = {}
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                previous = {
                    r["index"]: r
                    for r in data.get("files", [])
                    if isinstance(r, dict) and isinstance(r.get("index"), int)
                }
            except (ValueError, AttributeError, TypeError):
                previous = {}
        records: dict[int, dict] = {i: r for i, r in previous.items() if 1 <= i <= len(note.media)}
        processed: set[int] = set()
        errors = []
        if not note.media:
            errors.append("笔记详情没有可下载媒体")
        elif note.kind == "video" and not any(x.kind == "video" for x in note.media):
            errors.append("未读取到直链视频，只能下载现有图片；不支持仅提供 HLS/DASH 的视频")

        def snapshot() -> dict:
            failed = bool(errors) or any(r.get("status") == "failed" for r in records.values())
            complete = len(processed) == len(note.media)
            status = "partial" if failed else ("complete" if complete else "running")
            result = {
                "schema_version": 1,
                "note_id": note.note_id,
                "status": status,
                "errors": errors,
                "files": [records[k] for k in sorted(records)],
            }
            write_json(manifest_path, result)
            return result

        async def run(index: int, asset: MediaAsset) -> None:
            old = previous.get(index, {})
            try:
                if await self._cached(old, directory, asset):
                    records[index] = {**old, "url": asset.url, "status": "skipped"}
                else:
                    records[index] = await self._download_asset(asset, directory, index)
            except (DownloadError, OSError) as exc:
                records[index] = {
                    "index": index,
                    "kind": asset.kind,
                    "url": asset.url,
                    "status": "failed",
                    "error": str(exc),
                }
            processed.add(index)
            snapshot()

        snapshot()
        await asyncio.gather(*(run(i, asset) for i, asset in enumerate(note.media, 1)))
        return snapshot()

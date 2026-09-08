#!/usr/bin/env python3
"""RedCrack 小红书搜索、批量下载命令行。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from request.reader import LoginRequiredError, ReaderError, VerificationRequiredError, XHSReader
from request.reader.export import export_notes, write_json, write_note
from request.reader.models import Note, normalize_note_url


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须为非负有限数")
    return number


def _browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        default=str(Path.home() / ".redcrack" / "xhs_reader_profile"),
        help="独立浏览器登录目录",
    )
    parser.add_argument("--headless", action="store_true", help="登录后无窗口运行")
    parser.add_argument(
        "--browser-channel", choices=["auto", "chrome", "msedge", "chromium"], default="auto"
    )
    parser.add_argument("--timeout", type=positive_int, default=30, help="页面/结果等待超时（秒）")
    parser.add_argument(
        "--interval", type=nonnegative_float, default=1.5, help="滚动/读取笔记的间隔秒数"
    )


def _output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", "-o", default="downloads", help="输出目录（默认 downloads）")
    parser.add_argument(
        "--concurrency", type=positive_int, default=3, help="单篇笔记的媒体下载并发数"
    )
    parser.add_argument(
        "--retries", type=nonnegative_int, default=2, help="媒体下载失败后的重试次数"
    )
    parser.add_argument(
        "--max-size-mb", type=positive_int, default=512, help="单个媒体最大大小（MiB）"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RedCrack · 小红书自动搜索与下载", epilog="首次使用：python xhs.py login"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="打开浏览器扫码并保存登录状态")
    _browser_args(login)
    login.add_argument("--wait", type=positive_int, default=300, help="扫码等待秒数")

    search = sub.add_parser("search", help="搜索关键词，导出 JSON/CSV，可同时下载")
    _browser_args(search)
    _output_args(search)
    search.add_argument("keyword", help="搜索关键词")
    search.add_argument("--limit", type=positive_int, default=20, help="最多采集多少篇笔记")
    search.add_argument("--max-scrolls", type=nonnegative_int, default=30, help="最多向下滚动次数")
    search.add_argument("--download", action="store_true", help="搜索后读取详情并下载图片/视频")

    download = sub.add_parser("download", help="批量下载笔记链接或搜索结果")
    _browser_args(download)
    _output_args(download)
    download.add_argument("urls", nargs="*", help="笔记链接、ID 或带链接的分享文案（用引号包裹）")
    download.add_argument("--file", help="UTF-8 链接列表，每行一个，# 开头为注释")
    download.add_argument("--from-json", help="读取 search 导出的 results.json")
    download.add_argument(
        "--metadata-only", action="store_true", help="只保存完整正文/元数据，不下载媒体"
    )
    return parser


def collect_urls(args: argparse.Namespace) -> list[str]:
    values = list(args.urls)
    if args.file:
        values.extend(
            line.strip()
            for line in Path(args.file).expanduser().read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if args.from_json:
        data = json.loads(Path(args.from_json).expanduser().read_text(encoding="utf-8-sig"))
        rows = data.get("notes") if isinstance(data, dict) else data
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not row.get("url") for row in rows
        ):
            raise ValueError("--from-json 需要包含 notes 列表和每篇笔记的 url")
        values.extend(row["url"] for row in rows)
    if not values:
        raise ValueError("请提供笔记链接、--file 或 --from-json")
    return list(dict.fromkeys(normalize_note_url(value) for value in values))


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _download_batch(
    reader: XHSReader,
    urls: list[str],
    args: argparse.Namespace,
    *,
    initial: list[Note] | None = None,
) -> int:
    from request.reader.downloader import MediaDownloader

    output = Path(args.output).expanduser().resolve()
    notes = {note.note_id: note for note in initial or []}
    attempts: list[dict] = []
    handled: set[str] = set()
    keyword = getattr(args, "keyword", "")
    metadata_only = getattr(args, "metadata_only", False)

    def save(complete: bool = False) -> None:
        export_notes(list(notes.values()), output, keyword=keyword, complete=complete)
        write_json(
            output / "batch.json",
            {
                "schema_version": 1,
                "complete": complete,
                "requested": len(urls),
                "attempts": attempts,
            },
        )

    save()
    async with MediaDownloader(
        output,
        concurrency=args.concurrency,
        retries=args.retries,
        max_bytes=args.max_size_mb * 1024 * 1024,
    ) as downloader:
        for index, url in enumerate(urls, 1):
            progress(f"[{index}/{len(urls)}] 读取笔记…")
            try:
                if index > 1:
                    await asyncio.sleep(args.interval)
                note = await reader.get_note(url)
                if note.note_id in handled:
                    attempts.append({"url": url, "note_id": note.note_id, "status": "duplicate"})
                    continue
                notes[note.note_id] = note
                if metadata_only:
                    directory = output / note.note_id
                    if directory.is_symlink():
                        raise ReaderError("笔记输出目录不能是符号链接")
                    write_note(note, directory)
                    result = {"status": "metadata_only"}
                else:
                    result = await downloader.download_note(note)
                handled.add(note.note_id)
                attempts.append({"url": url, "note_id": note.note_id, "status": result["status"]})
                progress(f"  {note.title or note.note_id}: {result['status']}")
            except (LoginRequiredError, VerificationRequiredError) as exc:
                attempts.append({"url": url, "status": "failed", "error": str(exc)})
                raise
            except (ReaderError, ValueError, OSError) as exc:
                attempts.append({"url": url, "status": "failed", "error": str(exc)})
                progress(f"  失败：{exc}")
            finally:
                save()
    save(complete=True)
    failed = sum(attempt["status"] in ("failed", "partial") for attempt in attempts)
    print(
        json.dumps(
            {
                "status": "partial" if failed else "complete",
                "notes": len(handled),
                "failed": failed,
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 2 if failed else 0


async def run(args: argparse.Namespace) -> int:
    # 在启动浏览器之前验证输入。
    urls = collect_urls(args) if args.command == "download" else []
    if args.command == "search" and not args.keyword.strip():
        raise ValueError("搜索关键词不能为空")
    async with XHSReader(
        profile_dir=args.profile,
        headless=args.headless,
        browser_channel=args.browser_channel,
        timeout_ms=args.timeout * 1000,
        interval=args.interval,
    ) as reader:
        if args.command == "login":
            progress("请在打开的小红书窗口完成扫码登录…")
            await reader.login(timeout_seconds=args.wait)
            print(json.dumps({"status": "logged_in", "profile": args.profile}, ensure_ascii=False))
            return 0
        if args.command == "search":
            output = Path(args.output).expanduser().resolve()

            def checkpoint(notes: list[Note]) -> None:
                export_notes(notes, output, keyword=args.keyword, complete=False)
                progress(f"已采集 {len(notes)} / {args.limit} 篇")

            notes = await reader.search(
                args.keyword, limit=args.limit, max_scrolls=args.max_scrolls, on_progress=checkpoint
            )
            export_notes(notes, output, keyword=args.keyword)
            if not args.download or not notes:
                print(
                    json.dumps(
                        {"status": "complete", "count": len(notes), "output": str(output)},
                        ensure_ascii=False,
                    )
                )
                return 0
            return await _download_batch(reader, [note.url for note in notes], args, initial=notes)
        return await _download_batch(reader, urls, args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except LoginRequiredError as exc:
        progress(f"LOGIN_REQUIRED: {exc}")
        return 3
    except VerificationRequiredError as exc:
        progress(f"VERIFICATION_REQUIRED: {exc}")
        return 4
    except (ReaderError, ValueError, OSError) as exc:
        progress(f"FAILED: {exc}")
        return 2
    except KeyboardInterrupt:
        progress("已中断；已经完成的导出和媒体会保留，重新执行可跳过完整文件。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

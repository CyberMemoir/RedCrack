#!/usr/bin/env python3
"""RedCrack 小红书搜索、批量下载命令行。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from request.reader import LoginRequiredError, ReaderError, VerificationRequiredError, XHSReader
from request.reader.access import AccessPause
from request.reader.cache import NoteCache
from request.reader.errors import NetworkReadError
from request.reader.export import export_notes, write_json, write_note
from request.reader.models import Note, normalize_note_url, note_id_from_url


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
        "--interval",
        type=nonnegative_float,
        default=15,
        help="滚动/读取笔记的间隔秒数（非安全阈值）",
    )
    parser.add_argument("--refresh", action="store_true", help="明确刷新缓存，重新访问计入预算")
    parser.add_argument(
        "--connect-existing", action="store_true", help="经Chrome授权复用已打开的正常Chrome会话"
    )
    parser.add_argument(
        "--chrome-data-dir",
        help="现有Chrome数据根目录；Windows默认使用正常Chrome目录，只读连接入口",
    )


def _output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", "-o", default="downloads", help="输出目录（默认 downloads）")
    parser.add_argument(
        "--concurrency", type=positive_int, default=1, help="单篇笔记的媒体下载并发数"
    )
    parser.add_argument(
        "--retries", type=nonnegative_int, default=1, help="媒体下载失败后的重试次数（最多1）"
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
    search.add_argument("keyword", nargs="?", default="", help="搜索关键词")
    search.add_argument("--queries-file", help="有限关键词清单，每行一个；自动顺序处理")
    search.add_argument("--limit", type=positive_int, default=5, help="最多采集多少篇笔记")
    search.add_argument(
        "--max-scrolls", type=nonnegative_int, default=2, help="最多向下滚动次数，上限2"
    )
    search.add_argument("--download", action="store_true", help="搜索后读取详情并下载图片/视频")

    discover = sub.add_parser("discover", help="读取当前账号推荐页；不是全站热门榜")
    _browser_args(discover)
    _output_args(discover)
    discover.add_argument("--limit", type=positive_int, default=5)
    discover.add_argument("--max-scrolls", type=nonnegative_int, default=2)

    download = sub.add_parser("download", help="批量下载笔记链接或搜索结果")
    _browser_args(download)
    _output_args(download)
    download.add_argument("urls", nargs="*", help="笔记链接、ID 或带链接的分享文案（用引号包裹）")
    download.add_argument("--file", help="UTF-8 链接列表，每行一个，# 开头为注释")
    download.add_argument("--from-json", help="读取 search 导出的 results.json")
    download.add_argument(
        "--metadata-only", action="store_true", help="只保存完整正文/元数据，不下载媒体"
    )
    acknowledge = sub.add_parser(
        "acknowledge-warning", help="人工处理提醒后确认工具暂停；不访问平台"
    )
    acknowledge.add_argument(
        "--profile", default=str(Path.home() / ".redcrack" / "xhs_reader_profile")
    )
    acknowledge.add_argument("--reason", required=True, help="已人工检查/处理的情况")
    analyze = sub.add_parser("analyze", help="离线规则提取人的需求原句、去重与统计；不访问平台")
    analyze.add_argument(
        "--input", action="append", required=True, help="results.json、note.json或归档目录，可重复"
    )
    analyze.add_argument("--output", "-o", required=True, help="独立分析输出目录")
    collection = sub.add_parser(
        "collection-export", help="离线转换platform-collection-v1节点、边、能力与续跑材料"
    )
    collection.add_argument("--input", action="append", required=True)
    collection.add_argument("--output", "-o", required=True)
    collection.add_argument("--pause-record", help="已有访问暂停状态JSON；不清除暂停")
    collection.add_argument(
        "--tool-revision", default="unknown", help="导出工具提交版本；未知不推造"
    )
    bundle = sub.add_parser("family-bundle", help="离线导出自包含family-source-bundle-v1")
    bundle.add_argument("--input", action="append", required=True)
    bundle.add_argument("--bundle", required=True, help="新包目录；同内容可重放，不覆盖不同内容")
    bundle.add_argument("--source-id", required=True)
    bundle.add_argument(
        "--outcome",
        required=True,
        choices=["succeeded", "partial", "blocked_login", "blocked_verification", "failed"],
    )
    bundle.add_argument("--sample-mode", required=True, choices=["fixture", "historical", "live"])
    bundle.add_argument(
        "--publication-basis",
        choices=["unix_seconds", "unix_milliseconds", "iso8601", "year", "month", "date"],
    )
    bundle.add_argument("--offset-minutes", type=int)
    bundle.add_argument("--pause-record")
    bundle.add_argument("--report")
    actions = sub.add_parser("family-action", help="持久平台动作接口；当前执行器仅离线fixture")
    actions.add_argument("--state-dir", required=True)
    operations = actions.add_subparsers(dest="operation", required=True)
    enqueue = operations.add_parser("enqueue")
    enqueue.add_argument("--action", required=True)
    enqueue.add_argument("--fixture", required=True)
    enqueue.add_argument("--mode", choices=["head", "history"], required=True)
    for name in ("run", "refresh", "stop", "cancel", "status"):
        operation = operations.add_parser(name)
        operation.add_argument("--recipe-id", required=True)
        if name == "run":
            operation.add_argument("--max-pages", type=positive_int, default=1)
        if name in {"stop", "cancel"}:
            operation.add_argument("--reason", required=True)
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
    found = {}
    for value in values:
        url = normalize_note_url(value)
        key = note_id_from_url(url) or url
        if key not in found or "xsec_token=" in url:
            found[key] = url
    return list(found.values())


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
    notes = {}
    if (output / "results.json").exists():
        saved = json.loads((output / "results.json").read_text(encoding="utf-8"))
        notes = {row["note_id"]: NoteCache.restore(row) for row in saved.get("notes", [])}
    notes.update((note.note_id, note) for note in initial or [] if note.note_id not in notes)
    attempts: list[dict] = []
    handled: set[str] = set()
    keyword = getattr(args, "keyword", "")
    metadata_only = getattr(args, "metadata_only", False)
    if (output / "batch.json").exists():
        old = json.loads((output / "batch.json").read_text(encoding="utf-8"))
        attempts = old.get("attempts", [])
    completed = {
        row.get("note_id")
        for row in attempts
        if row.get("status") == "complete" or metadata_only and row.get("status") == "metadata_only"
    }

    async def before_media() -> None:
        await reader.runtime.before("media", reader.access_pause.check)
        if reader.page is not None:
            await reader._check_access()

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
        before_request=before_media,
        on_restriction=reader._stop_access,
    ) as downloader:
        for index, url in enumerate(urls, 1):
            public_url = url.split("?", 1)[0]
            note_id = note_id_from_url(url)
            if (
                metadata_only
                and note_id
                and note_id in completed
                and not args.refresh
                and (output / note_id / "note.json").is_file()
            ):
                progress(f"[{index}/{len(urls)}] 已完成，跳过重复任务")
                cached = reader.cache.get("detail", note_id)
                if cached:
                    notes[note_id] = cached[0]
                handled.add(note_id)
                save()
                continue
            progress(f"[{index}/{len(urls)}] 读取笔记…")
            try:
                note = await reader.get_note(url)
                if note.note_id in handled:
                    attempts.append(
                        {"url": public_url, "note_id": note.note_id, "status": "duplicate"}
                    )
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
                attempts.append(
                    {"url": public_url, "note_id": note.note_id, "status": result["status"]}
                )
                progress(f"  {note.title or note.note_id}: {result['status']}")
            except (LoginRequiredError, VerificationRequiredError, NetworkReadError) as exc:
                attempts.append(
                    {
                        "url": public_url,
                        "note_id": note_id or None,
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
                )
                raise
            except (ReaderError, ValueError, OSError) as exc:
                attempts.append(
                    {
                        "url": public_url,
                        "note_id": note_id or None,
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
                )
                progress(f"  失败：{exc}")
            finally:
                save()
    save(complete=True)
    latest = {attempt.get("note_id") or attempt["url"]: attempt for attempt in attempts}
    requested = {note_id_from_url(url) or url.split("?", 1)[0] for url in urls}
    failed = sum(
        attempt["status"] in ("failed", "partial")
        for key, attempt in latest.items()
        if key in requested
    )
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


async def _run(args: argparse.Namespace) -> int:
    if args.command == "family-bundle":
        from request.reader.family_bundle import bundle_cli

        print(json.dumps(bundle_cli(args), ensure_ascii=False))
        return 0
    if args.command == "family-action":
        from request.reader.family_actions import action_cli

        print(json.dumps(action_cli(args), ensure_ascii=False))
        return 0
    if args.command == "collection-export":
        from request.reader.collection import export_collection

        print(
            json.dumps(
                export_collection(
                    args.input,
                    args.output,
                    pause_record=args.pause_record,
                    tool_revision=args.tool_revision,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "analyze":
        from request.reader.analysis import analyze_archives

        result = analyze_archives(args.input, args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "acknowledge-warning":
        AccessPause(Path(args.profile).expanduser().resolve()).acknowledge(args.reason)
        progress("已记录人工确认；未访问平台，不保证平台限制已解除。")
        return 0
    # 在启动浏览器之前验证输入。
    urls = collect_urls(args) if args.command == "download" else []
    keywords = []
    if args.command == "search":
        keywords = [args.keyword.strip()] if args.keyword.strip() else []
        if args.queries_file:
            keywords += [
                line.strip()
                for line in Path(args.queries_file).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        keywords = list(dict.fromkeys(keywords))
        if not keywords:
            raise ValueError("请提供关键词或 --queries-file")
        if args.max_scrolls > 2:
            raise ValueError("每次搜索最多翻页2次")
    if getattr(args, "retries", 0) > 1:
        raise ValueError("网络重试上限为1次")
    async with XHSReader(
        profile_dir=args.profile,
        headless=args.headless,
        browser_channel=args.browser_channel,
        timeout_ms=args.timeout * 1000,
        interval=args.interval,
        refresh=args.refresh,
        evidence_dir=(Path(args.output) / "evidence" if hasattr(args, "output") else None),
        connect_existing=args.connect_existing,
        chrome_data_dir=args.chrome_data_dir,
    ) as reader:
        if args.command == "login":
            progress("请在打开的小红书窗口完成扫码登录…")
            await reader.login(timeout_seconds=args.wait)
            print(json.dumps({"status": "logged_in", "profile": args.profile}, ensure_ascii=False))
            return 0
        if args.command == "discover":
            output = Path(args.output).expanduser().resolve()

            def checkpoint(notes: list[Note]) -> None:
                export_notes(notes, output, complete=False)

            notes = await reader.discover(
                limit=args.limit,
                max_scrolls=args.max_scrolls,
                on_progress=checkpoint,
            )
            export_notes(notes, output)
            write_json(
                output / "discovery.json",
                {
                    "entry_type": "category_feed",
                    "entry_url": "https://www.xiaohongshu.com/explore",
                    "personalized": True,
                    "coverage": "bounded_profile_recommendations",
                    "requested_limit": args.limit,
                    "max_scrolls": args.max_scrolls,
                    "cached": all(note.cached for note in notes) if notes else None,
                    "not_a_global_hot_chart": True,
                    "end_reason": reader.list_end_reason,
                },
            )
            return 0
        if args.command == "search":
            output = Path(args.output).expanduser().resolve()
            combined: dict[str, Note] = {}
            for keyword in keywords:
                folder = (
                    output
                    if len(keywords) == 1
                    else output / "searches" / hashlib.sha256(keyword.encode()).hexdigest()[:16]
                )

                def checkpoint(notes: list[Note], folder=folder, keyword=keyword) -> None:
                    export_notes(notes, folder, keyword=keyword, complete=False)
                    combined.update((note.note_id, note) for note in notes)
                    if folder != output:
                        export_notes(
                            list(combined.values()),
                            output,
                            keyword=" | ".join(keywords),
                            complete=False,
                        )
                    progress(f"已保存搜索结果 {len(notes)} / {args.limit} 篇")

                export_notes([], folder, keyword=keyword, complete=False)
                try:
                    notes = await reader.search(
                        keyword,
                        limit=args.limit,
                        max_scrolls=args.max_scrolls,
                        on_progress=checkpoint,
                    )
                    export_notes(notes, folder, keyword=keyword)
                except ReaderError:
                    reader.runtime.event("search_failed")
                    raise
                combined.update((note.note_id, note) for note in notes)
                if folder != output:
                    export_notes(
                        list(combined.values()),
                        output,
                        keyword=" | ".join(keywords),
                        complete=False,
                    )
            notes = list(combined.values())
            export_notes(notes, output, keyword=" | ".join(keywords))
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


async def run(args: argparse.Namespace) -> int:
    if not hasattr(args, "output"):
        return await _run(args)
    output = Path(args.output).expanduser().resolve()
    task = {
        "schema_version": 1,
        "task_id": uuid.uuid4().hex,
        "command": args.command,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "finished_at": None,
        "exit_code": None,
        "error": None,
    }
    history_path = output / "tasks" / f"{task['task_id']}.json"

    def save() -> None:
        write_json(history_path, task)
        write_json(output / "task.json", task)

    save()
    try:
        code = await _run(args)
        task.update(status="complete" if code == 0 else "partial", exit_code=code)
        return code
    except (asyncio.CancelledError, KeyboardInterrupt):
        task.update(status="interrupted", exit_code=130)
        raise
    except Exception as exc:
        status, code = "failed", 2
        if isinstance(exc, LoginRequiredError):
            status, code = "login_required", 3
        elif isinstance(exc, VerificationRequiredError):
            status, code = "verification_required", 4
        elif isinstance(exc, NetworkReadError):
            status = "network_failed"
        task.update(status=status, exit_code=code, error=type(exc).__name__)
        raise
    finally:
        task["finished_at"] = datetime.now(UTC).isoformat()
        save()


def main(argv: list[str] | None = None) -> int:
    # Piped Windows shells may default to cp1252; CLI text/JSON use UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from request.creator import LoginRequiredError, PublishError, XHSCreatorPublisher


def _read_text(value: str | None, file_path: str | None, field_name: str) -> str:
    if file_path:
        return Path(file_path).expanduser().read_text(encoding="utf-8").strip()
    if value is not None:
        return value
    raise ValueError(f"必须提供 --{field_name} 或 --{field_name}-file")


def _add_common_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        default=str(Path.home() / ".redcrack" / "xhs_creator_profile"),
        help="浏览器持久化 profile 目录",
    )
    parser.add_argument("--headless", action="store_true", help="无窗口运行")
    parser.add_argument(
        "--browser-channel",
        default="auto",
        choices=["auto", "chrome", "msedge", "chromium"],
        help="浏览器通道，auto 优先使用本机 Chrome",
    )


def _add_note_args(parser: argparse.ArgumentParser) -> None:
    title_group = parser.add_mutually_exclusive_group(required=True)
    title_group.add_argument("--title", help="笔记标题")
    title_group.add_argument("--title-file", help="UTF-8 标题文件")

    content_group = parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="笔记正文")
    content_group.add_argument("--content-file", help="UTF-8 正文文件")

    parser.add_argument("--tags", nargs="*", default=[], help="话题标签，可传多个")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只上传并填写表单，不点击发布",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RedCrack 小红书自动发布")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="打开创作中心并扫码登录")
    _add_common_browser_args(login)
    login.add_argument("--wait", type=int, default=300, help="扫码等待秒数")

    image = subparsers.add_parser("image", help="发布图文笔记")
    _add_common_browser_args(image)
    _add_note_args(image)
    image.add_argument("--images", nargs="+", required=True, help="本地图片路径")

    video = subparsers.add_parser("video", help="发布视频笔记")
    _add_common_browser_args(video)
    _add_note_args(video)
    video.add_argument("--video", required=True, help="本地视频路径")
    return parser


async def _run(args: argparse.Namespace) -> int:
    publisher = XHSCreatorPublisher(
        profile_dir=args.profile,
        headless=args.headless,
        browser_channel=args.browser_channel,
    )
    try:
        async with publisher:
            if args.command == "login":
                await publisher.login(timeout_seconds=args.wait)
                print("LOGIN_OK")
                return 0

            title = _read_text(args.title, args.title_file, "title")
            content = _read_text(args.content, args.content_file, "content")
            if args.command == "image":
                result = await publisher.publish_image_note(
                    title=title,
                    content=content,
                    images=args.images,
                    tags=args.tags,
                    auto_publish=not args.dry_run,
                )
            else:
                result = await publisher.publish_video_note(
                    title=title,
                    content=content,
                    video=args.video,
                    tags=args.tags,
                    auto_publish=not args.dry_run,
                )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 0
    except LoginRequiredError as exc:
        print(f"NOT_LOGGED_IN: {exc}")
        return 3
    except (PublishError, ValueError, FileNotFoundError) as exc:
        print(f"PUBLISH_FAILED: {exc}")
        return 2


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

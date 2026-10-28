"""Chrome 144+ user-approved connection, without reading credential databases."""

import os
from pathlib import Path

from .errors import ReaderError


def existing_chrome_endpoint(data_dir: str | Path | None = None) -> str:
    if data_dir is None:
        if os.name != "nt":
            raise ReaderError("请用 --chrome-data-dir 指定正在运行的Chrome数据目录")
        data_dir = (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "Google/Chrome/User Data"
        )
    path = Path(data_dir).expanduser() / "DevToolsActivePort"
    if not path.is_file():
        raise ReaderError(
            "未发现Chrome授权连接入口。请在已登录Chrome打开 "
            "chrome://inspect/#remote-debugging，启用远程调试后重试；不会启动其他浏览器。"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535:
        raise ReaderError("Chrome连接信息无效，请检查Chrome远程调试页面")
    if not lines[1].startswith("/devtools/browser/"):
        raise ReaderError("Chrome连接路径无效，请检查Chrome远程调试页面")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"

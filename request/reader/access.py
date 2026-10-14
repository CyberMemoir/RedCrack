"""A persistent stop after an access warning; never schedules a retry."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .errors import VerificationRequiredError
from .export import write_json
from .runtime import ProfileRuntime


class AccessPause:
    def __init__(self, profile: Path) -> None:
        self.path = profile / "reader-access-pause.json"

    def check(self) -> None:
        if self.path.exists():
            raise VerificationRequiredError(
                "此浏览器资料目录因访问提醒暂停。请先在官方页面处理提醒；"
                "确认后执行 acknowledge-warning --reason 说明，不会自动恢复。"
            )

    def stop(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, {"stopped_at": datetime.now(UTC).isoformat(), "reason": reason})
        raise VerificationRequiredError(reason + "；已暂停此 profile 的读取，请人工处理提醒。")

    def acknowledge(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("请说明已经人工检查/处理的情况")
        if not self.path.exists():
            raise ValueError("此 profile 没有待确认的工具暂停记录")
        runtime = ProfileRuntime(self.path.parent)
        runtime.acquire()
        try:
            stopped = json.loads(self.path.read_text(encoding="utf-8"))
            stopped.update(acknowledged_at=datetime.now(UTC).isoformat(), note=reason.strip())
            with (self.path.parent / "reader-access-history.jsonl").open(
                "a", encoding="utf-8"
            ) as file:
                file.write(json.dumps(stopped, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
            self.path.unlink()
        finally:
            runtime.close()

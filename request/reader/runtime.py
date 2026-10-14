"""Profile budgets, exclusive ownership and credential-free action history."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .errors import ReaderError
from .export import write_json


class ProfileRuntime:
    def __init__(self, profile: Path, interval: float = 15) -> None:
        self.profile = profile
        self.interval = interval
        self.path = profile / "reader-budget.json"
        self.history = profile / "reader-actions.jsonl"
        self.handle = None

    def acquire(self) -> None:
        if self.handle is not None:
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        handle = (self.profile / "reader.lock").open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ReaderError("同一登录目录已有任务运行，请勿并行启动") from exc
        self.handle = handle

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def event(self, action: str, **fields) -> None:
        self.acquire()
        with self.history.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"at": time.time(), "action": action, **fields}) + "\n")

    async def before(self, kind: str, check_access) -> None:
        self.acquire()
        announced = False
        while True:
            check_access()
            current = time.time()
            try:
                state = (
                    json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                )
            except (ValueError, OSError) as exc:
                raise ReaderError("访问预算记录无法读取，停止而非重置预算") from exc
            started = state.get("started_at")
            cooldown = state.get("cooldown_until", 0)
            if not cooldown and started is not None:
                if current >= started + 600:
                    cooldown = started + 600 + 1200
                elif state.get("search", 0) >= 3 or state.get("detail", 0) >= 12:
                    cooldown = state.get("last_action_at", current) + 1200
                if cooldown:
                    state["cooldown_until"] = cooldown
                    write_json(self.path, state)
            wait = max(cooldown, state.get("last_action_at", 0) + self.interval) - current
            if wait > 0:
                if not announced:
                    print(
                        f"访问预算等待约 {int(wait) + 1} 秒，完成后自动继续…",
                        file=sys.stderr,
                        flush=True,
                    )
                    announced = True
                await asyncio.sleep(min(wait, 30))
                continue
            if cooldown or started is None:
                state = {"started_at": current, "search": 0, "detail": 0}
            state["last_action_at"] = current
            if kind in ("search", "detail"):
                state[kind] = state.get(kind, 0) + 1
            write_json(self.path, state)
            self.event(
                kind,
                segment_started_at=state["started_at"],
                searches=state["search"],
                details=state["detail"],
            )
            return

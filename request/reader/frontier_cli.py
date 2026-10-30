"""Finite/offline continuous frontier execution, independently inspectable and stoppable."""

import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from .export import write_json
from .family_actions import worker_lock
from .frontier import Frontier
from .frontier_driver import ReplayDriver


async def frontier_cli(args):
    if args.operation == "status":
        root = Path(args.state_dir).resolve()
        db = sqlite3.connect((root / "frontier.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            meta = {
                key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")
            }
            counts = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
        finally:
            db.close()
        return {
            "stopped": meta.get("stopped"),
            "platform_stop": meta.get("platform_stop"),
            "worker_pid": meta.get("worker_pid"),
            "new_watermark": meta.get("new_watermark", 0),
            "update_watermark": meta.get("update_watermark", 0),
            "frontier": counts,
            "snapshot": json.loads((root / "state.json").read_text(encoding="utf-8"))
            if (root / "state.json").exists()
            else None,
        }
    if args.operation != "run":
        engine = Frontier(args.state_dir)
        if args.operation == "stop":
            engine.stop(args.reason)
        elif args.operation == "resume":
            engine.resume(args.reason)
        elif args.operation == "resolve":
            engine.resolve(args.job_id, args.decision, args.reason)
        path = engine.root / "state.json"
        return {
            "stopped": engine.get("stopped"),
            "platform_stop": engine.get("platform_stop"),
            "snapshot": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None,
        }
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("driver") != "offline_replay":
        raise ValueError(
            "This execution is offline-only. Live mode cannot clear or skip AccessPause"
        )

    def path(key):
        value = Path(config[key])
        return value.resolve() if value.is_absolute() else (config_path.parent / value).resolve()

    if path("state_dir").is_relative_to(path("source_database").parent):
        raise ValueError("State directory must be outside source collector data")
    engine, driver = Frontier(path("state_dir")), ReplayDriver(path("replay_file"))
    rounds = args.rounds
    deadline = config.get("deadline_unix")
    if rounds is None and not deadline:
        raise ValueError("Continuous offline run requires an explicit finite deadline")
    for name, default, maximum in (
        ("batch", 5, 50),
        ("source_batch", 20, 200),
        ("update_batch", 5, 50),
        ("max_depth", 1, 2),
        ("idle_seconds", 2, 30),
    ):
        value = config.get(name, default)
        if (
            type(value) is not int
            or not 0 <= value <= maximum
            or name != "max_depth"
            and value == 0
        ):
            raise ValueError("Invalid frontier budget: " + name)
    if rounds is not None and not 1 <= rounds <= 10000:
        raise ValueError("rounds must be 1..10000")
    with worker_lock(engine.root):
        engine.recover()
        previous_sha = engine.get("replay_sha")
        if previous_sha not in (None, driver.sha):
            raise ValueError("Replay evidence changed; use a new frontier")
        with engine.db() as db:
            engine.put(db, "replay_sha", driver.sha)
            engine.put(db, "worker_pid", os.getpid())
        previous = {}
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(
                number, lambda signum, frame: engine.request_stop("operator_signal_stop")
            )
        count, receipt = 0, None
        try:
            while not engine.poll_stop() and not engine.get("platform_stop"):
                if deadline and time.time() >= deadline:
                    engine.stop("acceptance_deadline")
                    break
                receipt = await engine.round(
                    driver,
                    max_jobs=config.get("batch", 5),
                    source=path("source_database"),
                    source_batch=config.get("source_batch", 20),
                    update_batch=config.get("update_batch", 5),
                    max_depth=config.get("max_depth", 1),
                )
                count += 1
                if receipt.get("fault") or rounds is not None and count >= rounds:
                    break
                if not receipt["jobs"]:
                    until = time.monotonic() + config.get("idle_seconds", 2)
                    while time.monotonic() < until and not engine.poll_stop():
                        await asyncio.sleep(0.1)
        finally:
            engine.poll_stop()
            with engine.db() as db:
                engine.put(db, "worker_pid", None)
            for number, handler in previous.items():
                signal.signal(number, handler)
            current = (
                json.loads((engine.root / "state.json").read_text(encoding="utf-8"))
                if (engine.root / "state.json").exists()
                else {}
            )
            current.update(
                pid=None,
                last_worker_pid=os.getpid(),
                command=sys.argv,
                stopped=engine.get("stopped"),
                platform_stop=engine.get("platform_stop"),
                deadline_unix=deadline,
                mode=driver.mode,
            )
            write_json(engine.root / "state.json", current)
        return {
            "rounds": count,
            "mode": driver.mode,
            "platform_requests": 0,
            "stopped": engine.get("stopped"),
            "platform_stop": engine.get("platform_stop"),
            "state_path": str(engine.root / "state.json"),
            "database": str(engine.root / "frontier.sqlite3"),
            "last_receipt": receipt,
        }

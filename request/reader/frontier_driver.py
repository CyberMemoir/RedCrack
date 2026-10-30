"""Explicit offline replay plus a guarded adapter to the existing normal reader."""

import json
from pathlib import Path

from .access import AccessPause
from .browser import XHSReader
from .errors import LoginRequiredError, ReaderError, VerificationRequiredError
from .export import write_json
from .family_bundle import encoded, sha256
from .models import Note


class ReplayDriver:
    mode = "offline_replay"

    def __init__(self, path):
        if Path(path).stat().st_size > 8 * 1024**2:
            raise ValueError("Replay JSON exceeds the bounded offline fixture budget")
        body = Path(path).read_bytes()
        self.sha = sha256(body)
        self.data = json.loads(body)
        if (
            self.data.get("schema_name") != "xhs-frontier-replay-v1"
            or self.data.get("sample_mode") != "fixture"
        ):
            raise ValueError("Only explicitly labelled replay fixtures are accepted")
        self.calls = []

    async def read(self, job, accesses):
        self.calls.append((job["kind"], job["target"], json.loads(job["cursor"])))
        if job["kind"] == "detail":
            row = self.data["details"].get(job["target"])
            if row is None:
                return {"outcome": "replay_missing", "platform_requests": 0}
            return (
                {"note": row, "platform_requests": 0}
                if "note_id" in row
                else row | {"platform_requests": 0}
            )
        for page in self.data["searches"]:
            if page["query"] == job["target"] and encoded(page["cursor"]).decode() == job["cursor"]:
                return page | {"platform_requests": 0}
        return {"outcome": "replay_missing", "platform_requests": 0}


class NormalReaderDriver:
    """Not enabled by the offline CLI. A later explicit live authorization is required."""

    mode = "normal_reader_live_blocked"

    def __init__(self, profile, evidence_dir, *, permit_live=False, reader_factory=XHSReader):
        self.profile, self.evidence_dir = Path(profile), Path(evidence_dir)
        self.permit_live, self.factory = permit_live, reader_factory

    async def read(self, job, accesses):
        try:
            AccessPause(self.profile).check()
        except VerificationRequiredError:
            return {"outcome": "blocked_verification", "platform_requests": 0}
        if not self.permit_live:
            return {"outcome": "live_disabled", "platform_requests": 0}
        if job["kind"] == "search" and json.loads(job["cursor"]) is not None:
            return {"outcome": "unsupported_native_cursor", "platform_requests": 0}
        try:
            async with self.factory(
                profile_dir=self.profile,
                connect_existing=True,
                refresh=True,
                evidence_dir=self.evidence_dir / job["id"],
            ) as reader:
                if job["kind"] == "search":

                    def progress(notes):
                        write_json(
                            self.evidence_dir / job["id"] / "partial-cards.json",
                            [n.to_dict() for n in notes],
                        )

                    notes = await reader.search(
                        job["target"], limit=2, max_scrolls=0, on_progress=progress
                    )
                    exhausted = reader.list_end_reason == "entry_exhausted"
                    return {
                        "notes": [n.to_dict() for n in notes],
                        "exhausted": exhausted,
                        "next_cursor": None,
                        "continuation_unsupported": not exhausted,
                        "platform_requests": None,
                        "reader_calls": 1,
                    }
                # Keep full access URLs private, still bound to the exact stable note ID.
                selected = (
                    max(
                        accesses,
                        key=lambda row: (
                            "xsec_token=" in (row["access"].get("url") or ""),
                            row["access"].get("method") == "source_card",
                        ),
                    )
                    if accesses
                    else None
                )
                for entry in [selected] if selected else []:
                    if "xsec_token=" not in (entry["access"].get("url") or "") and reader.cache.get(
                        "access", entry["note_id"]
                    ):
                        continue
                    reader.cache.put(
                        "access",
                        entry["note_id"],
                        [
                            Note(
                                entry["note_id"],
                                url=entry["access"].get("url")
                                or Note(entry["note_id"]).canonical_url,
                                access=entry["access"],
                            )
                        ],
                    )
                note = await reader.get_note(Note(job["target"]).canonical_url)
                return {"note": note.to_dict(), "platform_requests": None, "reader_calls": 1}
        except VerificationRequiredError:
            return {"outcome": "blocked_verification", "platform_requests": None}
        except LoginRequiredError:
            return {"outcome": "blocked_login", "platform_requests": None}
        except ReaderError:
            return {"outcome": "unavailable", "platform_requests": None}

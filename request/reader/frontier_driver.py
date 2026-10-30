"""Explicit offline replay plus a guarded adapter to the existing normal reader."""

import json
from pathlib import Path

from .access import AccessPause
from .browser import XHSReader
from .errors import LoginRequiredError, ReaderError, VerificationRequiredError
from .evidence import EvidenceArchive
from .export import write_json
from .family_bundle import encoded, sha256
from .models import Note
from .native_search import SEARCH_PATH, search_page, search_request
from .runtime import ProfileRuntime


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
        self.related_enabled = bool(self.data.get("related"))

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
        pages = self.data.get("related", []) if job["kind"] == "related" else self.data["searches"]
        for page in pages:
            target = page.get("parent_note") if job["kind"] == "related" else page["query"]
            if target == job["target"] and encoded(page["cursor"]).decode() == job["cursor"]:
                if "native_request" in page:
                    cursor = json.loads(job["cursor"])
                    if cursor is not None and cursor.get("request_body") != page["native_request"]:
                        raise ValueError("Replay request must match persisted native continuation")
                    return search_page(target, page["native_request"], page["native_response"]) | {
                        "platform_requests": 0
                    }
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
        if job["kind"] == "related":
            return {"outcome": "related_contract_missing", "platform_requests": 0}
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


class NativeSearchDriver:
    """Explicit transport injection; never creates a session or enables the CLI.

    transport(path, body) returns (HTTP status, decoded JSON). Seeds must come
    from captured page requests. The adapter neither creates nor alters signing.
    """

    mode = "native_search_live_blocked"

    def __init__(
        self, profile, evidence_dir, *, transport, seeds, permit_live=False, detail_driver=None
    ):
        self.pause = AccessPause(Path(profile))
        self.evidence = EvidenceArchive(Path(evidence_dir))
        self.transport, self.seeds = transport, seeds
        self.permit_live, self.detail_driver = permit_live, detail_driver

    async def read(self, job, accesses):
        try:
            self.pause.check()
        except VerificationRequiredError:
            return {"outcome": "blocked_verification", "platform_requests": 0}
        if not self.permit_live:
            return {"outcome": "live_disabled", "platform_requests": 0}
        if job["kind"] != "search":
            if job["kind"] == "detail" and self.detail_driver:
                return await self.detail_driver.read(job, accesses)
            return {
                "outcome": "related_contract_missing"
                if job["kind"] == "related"
                else "detail_driver_missing",
                "platform_requests": 0,
            }
        cursor = json.loads(job["cursor"])
        if cursor is None:
            body = self.seeds.get(job["target"])
            if body is None:
                return {"outcome": "captured_search_request_missing", "platform_requests": 0}
        else:
            if not isinstance(cursor, dict) or cursor.get("contract") != "xhs-search-page-v1":
                raise ValueError("Cursor is not a native search request")
            body = cursor["request_body"]
        body = search_request(job["target"], body)
        request_ref = self.evidence.save(
            "native-search-request", {"path": SEARCH_PATH, "body": body}
        )
        runtime = ProfileRuntime(self.pause.path.parent)
        try:
            try:
                await runtime.before("search", self.pause.check)
            except VerificationRequiredError:
                return {"outcome": "blocked_verification", "platform_requests": 0}
            status, payload = await self.transport(SEARCH_PATH, body)
        finally:
            runtime.close()
        response_ref = self.evidence.save(
            "native-search-response",
            {"request_sha256": request_ref["sha256"], "status": status, "payload": payload},
        )
        from .browser import WARNING_MARKERS

        message = str(payload.get("msg") or payload.get("message") or "")
        if status in (403, 412, 429, 461, 471, 567) or any(m in message for m in WARNING_MARKERS):
            try:
                self.pause.stop(f"Native search access restriction HTTP {status}")
            except VerificationRequiredError:
                pass
            return {"outcome": "blocked_verification", "platform_requests": 1}
        if status == 401 or payload.get("code") in (-100, -100100, -101):
            return {"outcome": "blocked_login", "platform_requests": 1}
        if status >= 400 or payload.get("success") is False:
            raise ReaderError("Native search response failed; no automatic retry")
        result = search_page(job["target"], body, payload)
        for note in result["notes"]:
            note["observed_at"] = response_ref["observed_at"]
            note["evidence"] = [response_ref]
        return result | {"platform_requests": 1}

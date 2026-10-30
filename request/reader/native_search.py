"""Source-backed page/search_id contract; no transport, cookies or signer here.

Contract witness: request/web/apis/note.py::Note.search_notes. Real response
validation is still pending: saved local archives contain no search-response.
"""

from copy import deepcopy
from urllib.parse import quote

from .models import parse_note

SEARCH_PATH = "/api/sns/web/v1/search/notes"


def search_request(keyword, body):
    """Continue an observed request without dropping filters or session fields."""
    body = deepcopy(body)
    if body.get("keyword") != keyword:
        raise ValueError("Search request keyword mismatch")
    if type(body.get("page")) is not int or body["page"] < 1:
        raise ValueError("Search request needs a positive native page")
    if not isinstance(body.get("search_id"), str) or not body["search_id"]:
        raise ValueError("Observed search_id required; never regenerate it on continuation")
    if type(body.get("page_size")) is not int or not 1 <= body["page_size"] <= 20:
        raise ValueError("Native page_size must fit the existing 20-card page budget")
    return body


def search_page(keyword, request_body, payload):
    body = search_request(keyword, request_body)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Search response missing data.items")
    more = data.get("has_more", data.get("hasMore"))
    if type(more) is not bool:
        raise ValueError("Search continuation needs an explicit boolean has_more")
    notes = []
    for item in data["items"]:
        if not isinstance(item, dict) or item.get("model_type", "note") not in (
            "note",
            "note_card",
        ):
            continue
        note = parse_note(item)
        note.content_scope = "search_card"
        note.access = {
            "url": note.url,
            "method": "navigate" if "xsec_token=" in note.url else "source_card",
            "source_url": "https://www.xiaohongshu.com/search_result?keyword=" + quote(keyword),
        }
        notes.append(note.to_dict())
    following = deepcopy(body)
    following["page"] += 1
    return {
        "notes": notes,
        "exhausted": not more,
        "next_cursor": {"contract": "xhs-search-page-v1", "request_body": following}
        if more
        else None,
        "request_body": body,
        "contract_status": "source_backed_live_unverified",
    }

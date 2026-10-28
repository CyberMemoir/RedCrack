"""Local Bilibili-anchor input converter; never creates families or infers origins."""

import json
import re
import uuid
from pathlib import Path

from .export import write_json
from .family_actions import validate_action
from .family_bundle import encoded, sha256
from .models import Note, normalize_note_url, note_id_from_url


def anchor_actions(anchor):
    uuid.UUID(anchor["family_id"])
    explainer = anchor["explainer"]
    if not re.fullmatch(
        r"bilibili:BV[0-9A-Za-z]{10}", explainer.get("work_id", "")
    ) or not anchor.get("evidence_refs"):
        raise ValueError(
            "An existing family, Bilibili explainer work and evidence_refs are required"
        )
    terms = list(dict.fromkeys(anchor.get("aliases", []) + anchor.get("phrases", [])))
    if any(not isinstance(term, str) or not term.strip() for term in terms):
        raise ValueError("Anchor aliases/phrases must be explicit nonempty strings")
    roles = anchor.get("roles", ["derivative", "usage"])
    if not roles or set(roles) - {"source", "derivative", "usage"}:
        raise ValueError("Unknown anchor material role")
    actions, gaps = [], []
    context = {
        "explainer": explainer,
        "role_candidates": [r for r in roles if r != "source"],
        "relation_status": "candidate_not_confirmed",
        "incidental_names": "bilibili_review_leads_only",
    }

    def action(query, purpose, *, kind="search", local_context=None):
        return validate_action(
            {
                "action_type": kind,
                "target_platform": "xiaohongshu",
                "family_id": anchor["family_id"],
                "query": query,
                "purpose": purpose,
                "anchors": terms,
                "evidence_refs": anchor["evidence_refs"],
                "query_layer": "rule_seed",
                "is_actual_search_log": False,
                "xhs_anchor": local_context or context,
            }
        )

    if context["role_candidates"]:
        actions.extend(action(term, "find_independent_uses") for term in terms)
        if not terms:
            gaps.append("missing_explicit_query_phrase")
    if "source" in roles:
        hint = anchor.get("source_hint")
        if not hint:
            gaps.append("missing_source")
        elif (
            hint.get("kind") in {"named", "phrase"}
            and isinstance(hint.get("value"), str)
            and hint["value"].strip()
        ):
            actions.append(
                action(
                    hint["value"],
                    "find_original_reference",
                    local_context=context | {"role_candidates": ["source"], "source_hint": hint},
                )
            )
        elif hint.get("kind") == "url":
            try:
                note_id = note_id_from_url(normalize_note_url(hint["value"]))
            except ValueError:
                note_id = None
            if note_id:
                canonical = Note(note_id).canonical_url
                actions.insert(
                    0,
                    action(
                        canonical,
                        "find_original_reference",
                        kind="detail",
                        local_context=context
                        | {
                            "role_candidates": ["source"],
                            "source_hint": {"kind": "url", "value": canonical},
                        },
                    ),
                )
            else:
                gaps.append("explicit_source_url_for_other_route")
        else:
            raise ValueError("source_hint must be an explicit url, named source or phrase")
    return {
        "schema_name": "xhs-bilibili-anchor-plan-v1",
        "schema_version": 1,
        "family_id": anchor["family_id"],
        "actions": actions,
        "gaps": gaps,
        "bilibili_review_leads": anchor.get("new_name_leads", []),
        "creates_family": False,
        "source_hint": anchor.get("source_hint"),
        "contract_scope": "platform_local_input_conversion_not_central_schema",
    }


def anchor_cli(args):
    plan = anchor_actions(json.loads(Path(args.input).read_text(encoding="utf-8")))
    directory = Path(args.directory)
    if directory.exists():
        saved = directory / "plan.json"
        if saved.exists() and json.loads(saved.read_text(encoding="utf-8")) == plan:
            return plan
        raise ValueError(
            "Anchor action directory differs; choose a new directory to avoid stale actions"
        )
    for action in plan["actions"]:
        write_json(directory / ("action-" + sha256(encoded(action)) + ".json"), action)
    write_json(directory / "plan.json", plan)
    return plan

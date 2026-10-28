import json
import unittest
from pathlib import Path

from request.reader.family_anchor import anchor_actions

ACTION = json.loads(
    (Path(__file__).parent / "fixtures/family_action.json").read_text(encoding="utf-8")
)


class AnchorTests(unittest.TestCase):
    def anchor(self):
        return {
            "family_id": ACTION["family_id"],
            "explainer": {
                "work_id": "bilibili:BV1234567890",
                "segment": {"start_ms": 1000, "end_ms": 2000},
            },
            "evidence_refs": ACTION["evidence_refs"],
            "aliases": ["合成甲表达"],
            "phrases": ["合成甲表达"],
            "roles": ["source", "derivative", "usage"],
            "new_name_leads": ["偶遇名称"],
        }

    def test_missing_source_not_guessed_and_overlapping_roles_are_candidates(self):
        plan = anchor_actions(self.anchor())
        self.assertEqual(plan["gaps"], ["missing_source"])
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual(action["family_id"], ACTION["family_id"])
        self.assertEqual(action["evidence_refs"], ACTION["evidence_refs"])
        self.assertEqual(action["xhs_anchor"]["role_candidates"], ["derivative", "usage"])
        self.assertEqual(plan["bilibili_review_leads"], ["偶遇名称"])
        self.assertFalse(plan["creates_family"])

    def test_explicit_note_reference_is_a_direct_detail_before_usage_queries(self):
        anchor = self.anchor()
        anchor["source_hint"] = {
            "kind": "url",
            "value": "https://www.xiaohongshu.com/explore/000000000000000000000001?xsec_token=private",
        }
        plan = anchor_actions(anchor)
        direct = plan["actions"][0]
        self.assertEqual(direct["action_type"], "detail")
        self.assertEqual(direct["purpose"], "find_original_reference")
        self.assertNotIn("private", json.dumps(direct))
        self.assertEqual(len(plan["actions"]), 2)

    def test_named_source_uses_only_the_explicit_hint_not_a_guessed_origin(self):
        anchor = self.anchor()
        anchor["source_hint"] = {"kind": "named", "value": "讲解明确提及的作品"}
        result = anchor_actions(anchor)
        self.assertEqual(result["actions"][-1]["query"], anchor["source_hint"]["value"])
        self.assertEqual(
            result["actions"][-1]["xhs_anchor"]["relation_status"], "candidate_not_confirmed"
        )

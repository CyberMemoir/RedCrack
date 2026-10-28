import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xhs
from request.reader.analysis import analyze_archives, metric_value

ID = "68add7d50000000000000001"


def note(**values):
    return {
        "note_id": ID,
        "title": "求狼王撕衣的原图！！",
        "description": "分享表情包\n这个表情包叫什么？",
        "stats": {"likes": "10"},
        "observed_at": "2026-10-01T12:00:00Z",
        **values,
    }


def save(path, notes):
    path.write_text(
        json.dumps({"notes": notes, "complete": True}, ensure_ascii=False), encoding="utf-8"
    )


class AnalysisTests(unittest.TestCase):
    def test_exact_text_spans_hashes_and_query_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "results.json"
            original = note()
            save(source, [original])
            summary = analyze_archives([str(source)], root / "analysis")
            rows = [
                json.loads(s)
                for s in (root / "analysis/queries.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(summary["query_occurrences"], 2)
            for row in rows:
                text = original["title" if row["source_kind"] == "title" else "description"]
                self.assertEqual(
                    row["raw_text"], text[row["char_start"] : row["char_end_exclusive"]]
                )
                self.assertEqual(
                    row["evidence"][0]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
                )
                self.assertEqual(row["layer"], "real_expression")
                self.assertFalse(row["is_actual_search_log"])
                self.assertEqual(row["target_ids"], [])
            self.assertNotIn("分享表情包", [r["raw_text"] for r in rows])

    def test_revisits_do_not_double_count_and_keep_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root / "a.json", root / "b.json"
            save(a, [note(), note()])
            save(b, [note(stats={"likes": "7"}, observed_at="2026-10-02T12:00:00Z")])
            report = analyze_archives([str(a), str(b)], root / "analysis")
            self.assertEqual(
                (report["unique_notes"], report["unique_observations"], report["duplicate_rows"]),
                (1, 2, 1),
            )
            self.assertEqual(report["metrics"][0]["known_value_sum"], 7)  # Decline, not 10+7.
            self.assertEqual(report["query_occurrences"], 2)
            self.assertIsNone(report["metrics"][1]["known_value_sum"])

    def test_unknown_time_does_not_replace_known_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "results.json"
            save(source, [note(), note(observed_at=None, stats={"likes": "999"})])
            result = analyze_archives([str(source)], root / "analysis")
            self.assertEqual(result["metrics"][0]["known_value_sum"], 10)

    def test_incomplete_inputs_and_bad_identity_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "results.json"
            source.write_text('{"notes": [], "complete": false}', encoding="utf-8")
            report = analyze_archives([str(source)], root / "analysis")
            self.assertEqual(report["incomplete_input_files"], 1)
            self.assertEqual(report["query_occurrences"], 0)
            save(source, [note(url="https://www.xiaohongshu.com/explore/68add7d50000000000000002")])
            with self.assertRaisesRegex(ValueError, "URL与ID不一致"):
                analyze_archives([str(source)], root / "other")

    def test_cli_is_offline_and_records_terminal_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "results.json"
            save(source, [note()])
            with patch("xhs.XHSReader") as browser, contextlib.redirect_stdout(io.StringIO()):
                code = xhs.main(["analyze", "--input", str(source), "-o", str(root / "analysis")])
                browser.assert_not_called()
            self.assertEqual(code, 0)
            task = json.loads((root / "analysis/task.json").read_text(encoding="utf-8"))
            self.assertEqual(task["status"], "complete")
            report = json.loads((root / "analysis/summary.json").read_text(encoding="utf-8"))
            self.assertEqual((report["model_calls"], report["network_requests"]), (0, 0))

    def test_metric_display_precision_and_missing_are_preserved(self):
        self.assertEqual(metric_value("1.2万")["value"], 12000)
        self.assertTrue(metric_value("1.2万")["approximate"])
        self.assertEqual(metric_value("0")["value"], 0)
        self.assertEqual(metric_value("1,234")["value"], 1234)
        self.assertEqual(metric_value(None)["status"], "missing")
        self.assertEqual(metric_value("未知")["status"], "unparsed")

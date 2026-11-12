"""§20.7 本地纠错反馈：读取、校验与待复核清单。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.feedback import KINDS, load_feedback, write_review_list


def write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class FeedbackLoadTests(unittest.TestCase):
    def test_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp) / "fb.json",
                {
                    "schema": "vocab-reader-feedback/v1",
                    "items": [
                        {
                            "annotation_id": "a-1",
                            "surface": "swallowing",
                            "kind": "sense",
                            "note": "应为吞食",
                        }
                    ],
                },
            )
            feedback = load_feedback(path)
            self.assertEqual(len(feedback.items), 1)
            self.assertEqual(feedback.annotation_ids(), ["a-1"])
            self.assertEqual(feedback.items[0]["kind_label"], KINDS["sense"])
            self.assertEqual(feedback.summary()["kinds"], {"sense": 1})

    def test_directory_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write(folder / "a.json", {"items": [{"annotation_id": "a-1", "kind": "span"}]})
            write(folder / "b.json", {"items": [{"annotation_id": "a-1", "kind": "sense"}]})
            feedback = load_feedback(folder)
            self.assertEqual(len(feedback.items), 2)
            self.assertEqual(feedback.annotation_ids(), ["a-1"])

    def test_missing_id_and_unknown_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp) / "fb.json",
                {
                    "items": [
                        {"surface": "no id", "kind": "sense"},
                        {"annotation_id": "a-2", "kind": "bogus"},
                    ]
                },
            )
            feedback = load_feedback(path)
            self.assertEqual(feedback.annotation_ids(), ["a-2"])
            self.assertEqual(feedback.items[0]["kind"], "sense")
            self.assertTrue(feedback.issues)

    def test_missing_file(self) -> None:
        feedback = load_feedback(Path("no-such-feedback.json"))
        self.assertTrue(feedback.issues)
        self.assertEqual(feedback.items, [])

    def test_broken_json_reports_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            feedback = load_feedback(path)
            self.assertTrue(feedback.issues)
            self.assertEqual(feedback.items, [])


class ReviewListTests(unittest.TestCase):
    def test_writes_pending_review_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write(
                Path(tmp) / "fb.json",
                {"items": [{"annotation_id": "a-9", "surface": "x", "kind": "too_much"}]},
            )
            feedback = load_feedback(source)
            out = write_review_list(feedback, Path(tmp) / "review.json")
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "vocab-feedback-review/v1")
            self.assertEqual(len(payload["items"]), 1)
            self.assertIn("不得直接改写正文", payload["note"])


if __name__ == "__main__":
    unittest.main()

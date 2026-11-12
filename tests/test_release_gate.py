"""审计整改验收测试（任务书第 7 节）。

每项都断言：退出状态、报告字段、最终文件是否存在。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_units import DOC, EXTRA, LEXICON, build_book
from scripts.run_pipeline import (
    EXIT_BLOCKED,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_VALIDATION,
    main,
)


class ReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = build_book(self.tmp / "book.epub")
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.report = self.tmp / "report.json"
        self.requests = self.tmp / "req"
        code = main(
            [
                "--book", str(self.book), "--lexicon", str(LEXICON),
                "--engine", "units", "--emit-annotations", str(self.requests),
                *EXTRA, "--quiet",
            ]
        )  # fmt: skip
        self.assertEqual(code, EXIT_OK)
        self.req = json.loads(
            (self.requests / "windows-0001.json").read_text(encoding="utf-8")
        )

    # -- 构造 --------------------------------------------------------

    def _pid(self, tail: int) -> str:
        return f"{DOC}#{tail}"

    def _ids(self, tail: int, phrase: str, occurrence: int = 0) -> list[str]:
        payload = json.loads(
            (self.requests / "windows-0001.json").read_text(encoding="utf-8")
        )
        toks: list[dict] = []
        for window in payload["windows"]:
            for para in window["paragraphs"]:
                if para["pid"] == self._pid(tail):
                    toks = para["tokens"]
        words = [w.casefold() for w in phrase.split()]
        hits = [
            [t["id"] for t in toks[i : i + len(words)]]
            for i in range(len(toks) - len(words) + 1)
            if [t["t"].casefold() for t in toks[i : i + len(words)]] == words
        ]
        self.assertGreater(len(hits), occurrence, f"{phrase} @ {tail}")
        return hits[occurrence]

    def _ann(self, tail: int, phrase: str, gloss: str, **kwargs) -> dict:
        entry = kwargs.pop("entry", "")
        return {
            "annotation_id": kwargs.pop("annotation_id", f"a-{tail}-{phrase.replace(' ', '-')}"),
            "pid": self._pid(tail),
            "token_ids": self._ids(tail, phrase),
            "unit_type": kwargs.pop("unit", "word"),
            "source_type": kwargs.pop("source", "exact_entry" if entry else "out_of_lexicon"),
            "whole_entry_ids": [entry] if entry else [],
            "components": kwargs.pop("components", []),
            "short_gloss_zh": gloss,
            "reader_usage_note": kwargs.pop("reader_note", ""),
            "audit_note": kwargs.pop("audit_note", ""),
            "usage_category": kwargs.pop("category", ""),
            "usage_domain": "",
            "sense_key": gloss,
            "evidence": "测试",
            "action": kwargs.pop("action", "annotate"),
            "reason": kwargs.pop("reason", ""),
        }

    def _scope(self) -> dict:
        return json.loads((self.requests / "scope.json").read_text(encoding="utf-8"))

    def _window_for(self, pid: str) -> str:
        for window in self._scope()["windows"]:
            if pid in window["pids"]:
                return window["window_id"]
        raise AssertionError(f"pid 不在任务清单里：{pid}")

    def _binding(self) -> dict:
        scope = self._scope()
        return {
            "protocol": "v6",
            "tokenizer": "v1",
            "book_sha256": scope["book_sha256"],
            "lexicon_sha256": scope["lexicon_sha256"],
            "config_hash": scope["config_hash"],
            "identify_prompt": scope["identify_prompt"],
            "review_prompt": scope["review_prompt"],
            "context_pack_hash": scope["context_pack_hash"],
        }

    def _write(
        self,
        annotations: list[dict],
        *,
        verdicts=None,
        window_verdicts=None,
        write_review: bool = True,
        review_binding: dict | None = None,
        windows=None,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        scope = self._scope()
        for annotation in annotations:
            annotation.setdefault("window_id", self._window_for(annotation["pid"]))
        if windows is None:
            windows = [
                {
                    "window_id": window["window_id"],
                    "status": "identified",
                    "candidates": len(window["pids"]),
                    "annotations": [
                        a["annotation_id"] for a in annotations
                        if a["window_id"] == window["window_id"]
                    ],
                    "reason": "",
                }
                for window in scope["windows"]
            ]
        binding = self._binding()
        payload = {
            "schema": "vocab-annotation-decisions/v6",
            **binding,
            "scan": {"hash": binding["config_hash"]},
            "task_id": scope["task_id"],
            "windows": windows,
            "annotations": annotations,
            "suppressed": [],
        }
        (self.requests / "ann-0001.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        if not write_review:
            for stale in self.requests.glob("review-*.json"):
                stale.unlink()
            return
        review_target = self.tmp / "review_req"
        code = main(
            [
                "--book", str(self.book), "--lexicon", str(LEXICON),
                "--engine", "units", "--apply-annotations", str(self.requests),
                "--emit-review", str(review_target), *EXTRA, "--quiet", *extra_args,
            ]
        )  # fmt: skip
        self.assertEqual(code, EXIT_OK)
        hashes: dict[str, str] = {}
        window_hashes: dict[str, str] = {}
        for name in sorted(review_target.glob("review-request-*.json")):
            package = json.loads(name.read_text(encoding="utf-8"))
            for window in package["windows"]:
                window_hashes[window["window_id"]] = window["window_hash"]
                for item in window["proposed"]:
                    hashes[item["annotation_id"]] = item["revision_hash"]
        if verdicts is None:
            verdicts = [
                {
                    "annotation_id": a["annotation_id"],
                    "reviewed_revision_hash": hashes.get(a["annotation_id"], ""),
                    "review_status": "pass",
                    "issues": [],
                }
                for a in annotations
                if a["action"] == "annotate"
            ]
        if window_verdicts is None:
            window_verdicts = [
                {
                    "window_id": wid,
                    "reviewed_window_hash": value,
                    "coverage_status": "pass",
                    "issues": [],
                }
                for wid, value in window_hashes.items()
            ]
        review = {
            "schema": "vocab-annotation-review/v6",
            **binding,
            "scan": {"hash": binding["config_hash"]},
            "task_id": scope["task_id"],
            "review_round": 1,
            "reviewer": {"kind": "test", "name": "fixture"},
            "window_verdicts": window_verdicts,
            "verdicts": verdicts,
        }
        if review_binding:
            review.update(review_binding)
        (self.requests / "review-0001.json").write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8"
        )

    def _apply(self, *extra: str) -> tuple[int, dict]:
        code = main(
            [
                "--book", str(self.book), "--lexicon", str(LEXICON),
                "--engine", "units", "--apply-annotations", str(self.requests),
                "--out", str(self.out), "--report", str(self.report),
                "--no-epubcheck", *EXTRA, "--quiet", *extra,
            ]
        )  # fmt: skip
        report = (
            json.loads(self.report.read_text(encoding="utf-8"))
            if self.report.is_file()
            else {}
        )
        return code, report

    def _html(self, report: dict) -> str:
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            return zf.read(DOC).decode("utf-8")

    def _epubs(self) -> list[Path]:
        return sorted(self.out.glob("*.epub"))

    # -- 1. 改短义保留旧审查 -----------------------------------------

    def test_changed_gloss_invalidates_old_review(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation])
        ann = json.loads((self.requests / "ann-0001.json").read_text(encoding="utf-8"))
        ann["annotations"][0]["short_gloss_zh"] = "驾驶飞机"
        ann["annotations"][0]["sense_key"] = "驾驶飞机"
        (self.requests / "ann-0001.json").write_text(
            json.dumps(ann, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(report["release"]["delivery_status"], "blocked")
        self.assertTrue(report["release"]["pending_review"])
        self.assertEqual(self._epubs(), [])

    # -- 2. 单独改 review 的 book_sha256 ------------------------------

    def test_review_book_hash_mismatch_rejected(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation], review_binding={"book_sha256": "wrong-book"})
        code, report = self._apply()
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(self._epubs(), [])

    # -- 3. 改成员 / 用法 / 类别 -------------------------------------

    def test_member_change_invalidates_review(self) -> None:
        annotation = self._ann(
            2, "skin cancer", "皮肤癌", source="component_entries", unit="phrase",
            components=[
                {"token_ids": self._ids(2, "skin"), "entry_ids": ["skin|四级+雅思+牛津3000|n."]},
                {"token_ids": self._ids(2, "cancer"), "entry_ids": ["cancer|四级+雅思+牛津3000|n."]},
            ],
        )  # fmt: skip
        self._write([annotation])
        ann = json.loads((self.requests / "ann-0001.json").read_text(encoding="utf-8"))
        ann["annotations"][0]["components"][0]["entry_ids"] = ["cancer|四级+雅思+牛津3000|n."]
        (self.requests / "ann-0001.json").write_text(
            json.dumps(ann, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertTrue(report["release"]["pending_review"])

    # -- 4. 第二个分片绑定错配 ---------------------------------------

    def test_second_fragment_binding_mismatch_rejected(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation])
        first = json.loads((self.requests / "ann-0001.json").read_text(encoding="utf-8"))
        second = dict(first)
        second["book_sha256"] = "another-book"
        second["annotations"] = []
        second["windows"] = []
        (self.requests / "ann-0002.json").write_text(
            json.dumps(second, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(self._epubs(), [])

    # -- 5. modify 后必须有新版本 pass -------------------------------

    def test_modify_requires_review_of_new_revision(self) -> None:
        from scripts.units import (
            apply_review,
            freeze_revisions,
            load_annotations,
            load_review,
            load_scope,
        )

        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write(
            [annotation],
            verdicts=[
                {
                    "annotation_id": annotation["annotation_id"],
                    "reviewed_revision_hash": "__FILL__",
                    "review_status": "modify",
                    "issues": ["措辞需更自然"],
                    "revised": {"short_gloss_zh": "桅杆（船桅）"},
                }
            ],
        )
        # 补上旧版本的 hash
        review = json.loads((self.requests / "review-0001.json").read_text(encoding="utf-8"))
        package = json.loads(
            sorted((self.tmp / "review_req").glob("review-request-*.json"))[0].read_text(
                encoding="utf-8"
            )
        )
        old_hash = next(
            item["revision_hash"]
            for window in package["windows"]
            for item in window["proposed"]
        )
        review["verdicts"][0]["reviewed_revision_hash"] = old_hash
        (self.requests / "review-0001.json").write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(
            report["release"]["pending_review"][0]["status"], "modify_pending"
        )
        self.assertEqual(self._epubs(), [])

        # 计算修改后的新哈希，再明确对新版本 pass
        scope = load_scope(self.requests)
        decisions = load_annotations(self.requests, expected_binding=self._binding())
        freeze_revisions(decisions, scope)
        merged = apply_review(decisions, load_review(self.requests, self._binding()))
        new_hash = merged.annotations[0].revision_hash
        second = dict(review)
        second["review_round"] = 2
        second["verdicts"] = [
            {
                "annotation_id": annotation["annotation_id"],
                "reviewed_revision_hash": new_hash,
                "review_status": "pass",
                "issues": ["修订后通过"],
            }
        ]
        (self.requests / "review-0002.json").write_text(
            json.dumps(second, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        self.assertTrue(report["release"]["publishable"])

    # -- 6. 完全没有审查 ---------------------------------------------

    def test_no_review_files_blocked(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation], write_review=False)
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(report["release"]["delivery_status"], "blocked")
        self.assertEqual(self._epubs(), [])

    # -- 7. 单条缺审 / 审查 JSON 损坏 --------------------------------

    def test_one_missing_verdict_blocks(self) -> None:
        first = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        second = self._ann(0, "fished", "捕鱼", entry="fish|四级+雅思+牛津3000|n./v.")
        self._write([first, second])
        review = json.loads((self.requests / "review-0001.json").read_text(encoding="utf-8"))
        review["verdicts"] = review["verdicts"][:1]
        (self.requests / "review-0001.json").write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8"
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(report["release"]["pending_review"]), 1)

    def test_broken_review_json_blocks(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation])
        (self.requests / "review-0009.json").write_text("{broken", encoding="utf-8")
        code, _ = self._apply()
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(self._epubs(), [])

    # -- 8. 任务清单中的窗口结果丢失 ---------------------------------

    def test_missing_window_record_blocks(self) -> None:
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        scope = self._scope()
        self._write(
            [annotation],
            windows=[
                {
                    "window_id": scope["windows"][0]["window_id"],
                    "status": "identified",
                    "candidates": 1,
                    "annotations": [annotation["annotation_id"]],
                    "reason": "",
                }
            ],
        )
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        reasons = {item["reason"] for item in report["release"]["missing_scope"]}
        self.assertIn("missing_identify", reasons)

    # -- 9. allow-partial --------------------------------------------

    def test_allow_partial_marks_partial(self) -> None:
        good = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        bad = self._ann(0, "fished", "捕鱼", entry="sail|四级+雅思+牛津3000|v.")
        self._write([good, bad])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(report["release"]["delivery_status"], "partial")
        self.assertFalse(report["release"]["publishable"])
        self.assertTrue(report["release"]["omitted_items"])
        files = self._epubs()
        self.assertEqual(len(files), 1)
        self.assertIn("_partial", files[0].name)
        html = self._html(report)
        self.assertIn("(桅杆,", html)  # 11-11：正文括注不再带等级
        self.assertNotIn("[2]", html)
        self.assertTrue(report["validation"]["ok"])

    # -- 10. no_annotations 与缺审零标注 -----------------------------

    def test_no_annotations_distinct_from_missing_review(self) -> None:
        skipped = self._ann(3, "mast", "桅杆", action="skip", reason="no_learning_value")
        self._write([skipped])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["release"]["delivery_status"], "no_annotations")
        self.assertFalse(report["release"]["publishable"])
        self.assertEqual(self._epubs(), [])

        self._write([skipped], write_review=False)
        code, report = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(report["release"]["delivery_status"], "blocked")

    # -- 11. 同义表达不被判成词库缺义 --------------------------------

    def test_synonym_gloss_not_claimed_missing(self) -> None:
        annotation = self._ann(3, "mast", "荒漠", entry="mast|六级+托福|n.")
        self._write([annotation])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn("本句意思：荒漠", html)
        self.assertIn("词表完整释义", html)
        self.assertNotIn("词库未列该用法", html)
        self.assertNotIn("vocab-source", html)

    # -- 12. reader_note 与 audit_note 分字段 ------------------------

    def test_reader_note_and_audit_note_separated(self) -> None:
        annotation = self._ann(
            3,
            "mast",
            "桅杆",
            entry="mast|六级+托福|n.",
            reader_note="mast 的变形为 masts",
            audit_note="程序提示：词库匹配过程见调试日志",
        )
        self._write([annotation])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn("mast 的变形为 masts", html)
        self.assertNotIn("程序提示", html)
        notes = report["annotation"]["disambiguation"]["audit_notes"]
        self.assertTrue(any("程序提示" in item["note"] for item in notes))

    # -- 13. 已正确能力不回退 ----------------------------------------

    def test_existing_capabilities_not_regressed(self) -> None:
        annotation = self._ann(1, "taking", "捕到", entry="take|四级+牛津3000|vt.")
        self._write([annotation])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn(">taking</span>", html)
        self.assertIn(">take</span>", html)
        self.assertNotIn(">taki<", html)
        self.assertTrue(report["validation"]["checks"]["note_links"])
        self.assertTrue(report["validation"]["checks"]["text_integrity"])
        self.assertTrue(report["validation"]["checks"]["note_forms"])

    # -- 14. 初级（带中文译文）完整链路 -------------------------------

    def test_basic_reader_mode_renders_translations(self) -> None:
        """真实 EPUB：初级成品每个自然段后必须有「参考译文：」，免责只记在报告里。"""

        from scripts.inspect_book import sha256_file
        from scripts.reader_mode import build_translation_request

        # 档位进 config_hash，必须从 ② 起整条链路固定用同一档位。
        self.requests = self.tmp / "req_basic"
        code = main([
            "--book", str(self.book), "--lexicon", str(LEXICON),
            "--engine", "units", "--emit-annotations", str(self.requests),
            *EXTRA, "--quiet", "--reader-mode", "basic", "--require-reader-mode",
        ])
        self.assertEqual(code, EXIT_OK)

        request = build_translation_request(self.book, sha256_file(self.book))
        self.assertGreater(len(request["paragraphs"]), 0)
        payload = {
            "schema": "vocab-para-translations/v1",
            "book_sha256": sha256_file(self.book),
            "reader_mode": "basic",
            "paragraphs": [
                {
                    "pid": row["pid"],
                    "text_sha256": row["text_sha256"],
                    "translation": f"（试译）第 {i + 1} 段",
                }
                for i, row in enumerate(request["paragraphs"])
            ],
        }
        tpath = self.tmp / "translations.json"
        tpath.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        mode_args = (
            "--reader-mode", "basic", "--require-reader-mode",
            "--translations", str(tpath),
        )
        annotation = self._ann(3, "mast", "桅杆", entry="mast|六级+托福|n.")
        self._write([annotation], extra_args=mode_args)
        code, report = self._apply(*mode_args)
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        self.assertTrue(report["release"]["publishable"])

        mode = report["reader_mode"]
        self.assertTrue(mode["confirmed"])
        self.assertTrue(mode["has_chinese_translation"])
        self.assertGreaterEqual(mode["translation_coverage"]["ratio"], 0.95)

        html = self._html(report)
        self.assertIn('class="vocab-translation"', html)
        self.assertIn("参考译文：（试译）", html)
        # 免责声明不再逐段出现，改记在报告里
        self.assertNotIn("大模型翻译", html)
        self.assertIn("仅供参考", mode["translation_disclaimer"])
        # 英文原句仍在，译文是独立段落附注，不替换原文
        self.assertIn("near the", html)
        # 正文括注不带等级
        self.assertIn("(桅杆,", html)
        self.assertNotIn("(桅杆,6,", html)


if __name__ == "__main__":
    unittest.main()

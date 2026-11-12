"""阅读档位（初级 / 高级）与段落译文通道回归（11-11 拍板，P1-b）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from tests._bootstrap import FIXTURES, ROOT

from scripts.reader_mode import (
    ParagraphTranslation,
    READER_MODES,
    READER_MODE_UNSPECIFIED,
    ReaderModeError,
    TranslationSet,
    build_translation_request,
    load_translations,
    para_hash,
    paragraph_records,
    parse_reader_mode,
)
from scripts.run_pipeline import EXIT_BLOCKED, EXIT_OK, main

BOOK = FIXTURES / "epub2_basic.epub"


def _run(tmp: Path, *extra: str) -> int:
    return main([
        "--book", str(BOOK),
        "--presentation", "inline",
        "--out", str(tmp),
        "--report", str(tmp / "r.json"),
        "--no-epubcheck",
        "--quiet",
        *extra,
    ])


class ModeParseTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(parse_reader_mode("basic"), "basic")
        self.assertEqual(parse_reader_mode("初级"), "basic")
        self.assertEqual(parse_reader_mode("带译文"), "basic")
        self.assertEqual(parse_reader_mode("advanced"), "advanced")
        self.assertEqual(parse_reader_mode("高级"), "advanced")
        self.assertEqual(parse_reader_mode(""), READER_MODE_UNSPECIFIED)
        self.assertEqual(parse_reader_mode(None), READER_MODE_UNSPECIFIED)
        self.assertEqual(tuple(sorted(READER_MODES)), ("advanced", "basic"))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ReaderModeError):
            parse_reader_mode("中级")

    def test_hash_ignores_whitespace_only(self):
        self.assertEqual(para_hash("Hello  world."), para_hash(" Hello world. "))


class TranslationDataTests(unittest.TestCase):
    def test_request_lists_real_paragraphs(self):
        request = build_translation_request(BOOK, "0" * 64)
        self.assertEqual(request["schema"], "vocab-para-translation-request/v1")
        self.assertGreater(request["paragraph_count"], 0)
        self.assertEqual(request["paragraph_count"], len(paragraph_records(BOOK)))
        for row in request["paragraphs"]:
            self.assertIn("#", row["pid"])
            self.assertEqual(len(row["text_sha256"]), 64)
            self.assertEqual(row["text_sha256"], para_hash(row["text"]))

    def test_load_strict_and_reject_mismatched_book(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            good = tmp / "t.json"
            records = paragraph_records(BOOK)[:2]
            good.write_text(json.dumps({
                "schema": "vocab-para-translations/v1",
                "book_sha256": "a" * 64,
                "reader_mode": "basic",
                "paragraphs": [
                    {"pid": r["pid"], "text_sha256": r["text_sha256"],
                     "translation": "译文"}
                    for r in records
                ],
            }, ensure_ascii=False), encoding="utf-8")
            ts = load_translations(good, book_sha256="a" * 64)
            self.assertEqual(len(ts), 2)
            self.assertEqual(ts.issues, [])

            bad = tmp / "bad.json"
            bad.write_text(json.dumps({
                "schema": "vocab-para-translations/v1",
                "book_sha256": "b" * 64,
                "paragraphs": [],
            }), encoding="utf-8")
            with self.assertRaises(ReaderModeError):
                load_translations(bad, book_sha256="a" * 64)

    def test_wrong_schema_and_incomplete_rows_are_reported(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "t.json"
            path.write_text(json.dumps({
                "schema": "bogus/v1",
                "paragraphs": [{"pid": "x#0", "translation": "y"}],
            }), encoding="utf-8")
            with self.assertRaises(ReaderModeError):
                load_translations(path)

    def test_lookup_refuses_mismatched_text(self):
        pid = "doc#0"
        text = "He was an old man."
        ts = TranslationSet(items={
            pid: ParagraphTranslation(pid, para_hash(text), "他是个老人。")
        })
        self.assertEqual(ts.lookup(pid, text), "他是个老人。")
        self.assertEqual(ts.lookup(pid, "A completely different paragraph."), "")
        self.assertTrue(any("原文已变" in i for i in ts.issues))
        self.assertEqual(ts.lookup("doc#9", text), "")

    def test_coverage_counts_only_matching_records(self):
        records = [
            {"pid": "d#0", "text": "one", "text_sha256": para_hash("one")},
            {"pid": "d#1", "text": "two", "text_sha256": para_hash("two")},
        ]
        ts = TranslationSet(items={
            "d#0": ParagraphTranslation("d#0", para_hash("one"), "一"),
        })
        cov = ts.coverage(records)
        self.assertEqual((cov["total"], cov["covered"], cov["missing"]), (2, 1, 1))
        self.assertEqual(cov["ratio"], 0.5)


class TranslationRenderTests(unittest.TestCase):
    def test_translation_paragraph_is_inserted_after_source(self):
        from scripts.render_units import _append_translation

        body = etree.Element("body")
        source = etree.SubElement(body, "p")
        source.text = "He was an old man."
        para = SimpleNamespace(pid="doc#0", text="He was an old man.", element=source)
        ts = TranslationSet(items={
            "doc#0": ParagraphTranslation("doc#0", para_hash(para.text), "他是个老人。")
        })
        self.assertTrue(_append_translation(para, ts))
        xml = etree.tostring(body, encoding="unicode")
        self.assertIn('class="vocab-translation"', xml)
        self.assertIn("参考译文：他是个老人。", xml)
        # 免责声明不逐段重复，只在报告里留（11-11 拍板）
        self.assertNotIn("大模型翻译", xml)
        self.assertNotIn("中文译文：", xml)
        self.assertLess(xml.index("He was an old man."), xml.index("参考译文"))

    def test_title_paragraphs_are_never_translated(self):
        """书名 / 章节名 / 标题不译：请求里不导出，渲染也不接受（11-11 拍板）。"""

        from scripts.render_units import _append_translation
        from scripts.units import is_title_block

        cases = [
            ("h2", None, "第一章"),
            ("p", "chapter-title", "The Great Gatsby"),
            ("p", "bookTitle", "The Great Gatsby"),
        ]
        for tag, cls, text in cases:
            body = etree.Element("body")
            source = etree.SubElement(body, tag)
            if cls:
                source.set("class", cls)
            source.text = text
            self.assertTrue(is_title_block(source), f"{tag}/{cls} 应判为标题")
            para = SimpleNamespace(pid="doc#0", text=text, element=source)
            ts = TranslationSet(items={
                "doc#0": ParagraphTranslation("doc#0", para_hash(text), "第一章")
            })
            self.assertFalse(_append_translation(para, ts))
            self.assertNotIn("vocab-translation", etree.tostring(body, encoding="unicode"))

    def test_body_paragraph_not_mistaken_for_title(self):
        from scripts.units import is_title_block

        body = etree.Element("body")
        source = etree.SubElement(body, "p")
        source.set("class", "calibre1")
        source.text = "He was an old man who fished alone in a skiff."
        self.assertFalse(is_title_block(source, "The Old Man and the Sea", first_in_chapter=True))

    def test_missing_translation_is_omitted(self):
        from scripts.render_units import _append_translation

        body = etree.Element("body")
        source = etree.SubElement(body, "p")
        source.text = "He was an old man."
        para = SimpleNamespace(pid="doc#0", text="He was an old man.", element=source)
        self.assertFalse(_append_translation(para, TranslationSet()))
        self.assertNotIn("vocab-translation", etree.tostring(body, encoding="unicode"))


class TitlePageFilterTests(unittest.TestCase):
    """书名页整页不译：作者名、副题这类也会被一起排除（11-11 拍板）。"""

    @staticmethod
    def _p(text):
        return SimpleNamespace(text=text)

    def test_title_page_document_is_detected(self):
        from scripts.reader_mode import _looks_like_title_page

        page = [self._p("The Old Man"), self._p("and the Sea"), self._p("Ernest Hemingway")]
        self.assertTrue(_looks_like_title_page(page))

    def test_short_prose_is_not_a_title_page(self):
        from scripts.reader_mode import _looks_like_title_page

        self.assertFalse(_looks_like_title_page([self._p("He was an old man who fished alone.")]))
        # 段落多、或某段很长，都不算书名页
        many = [self._p("Line") for _ in range(6)]
        self.assertFalse(_looks_like_title_page(many))
        self.assertFalse(_looks_like_title_page([self._p("x" * 61)]))


class CliGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vocab-mode-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_host_link_blocks_unconfirmed_mode(self):
        self.assertEqual(_run(self.tmp, "--require-reader-mode"), EXIT_BLOCKED)
        self.assertFalse((self.tmp / "r.json").exists())

    def test_advanced_confirmed_is_recorded(self):
        self.assertEqual(
            _run(self.tmp, "--reader-mode", "advanced", "--require-reader-mode"),
            EXIT_OK,
        )
        report = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        mode = report["reader_mode"]
        self.assertTrue(mode["confirmed"])
        self.assertEqual(mode["mode"], "advanced")
        self.assertFalse(mode["has_chinese_translation"])
        self.assertTrue(mode["required_by_host"])

    def test_basic_without_translations_is_blocked(self):
        self.assertEqual(
            _run(self.tmp, "--reader-mode", "basic", "--require-reader-mode"),
            EXIT_BLOCKED,
        )

    def test_basic_direct_path_is_blocked(self):
        # 词表直注路径不交付初级成品，避免「名为初级、实则无译文」。
        self.assertEqual(_run(self.tmp, "--reader-mode", "basic"), EXIT_BLOCKED)

    def test_emit_translations_writes_request(self):
        target = self.tmp / "requests"
        self.assertEqual(_run(self.tmp, "--emit-translations", str(target)), EXIT_OK)
        payload = json.loads(
            (target / "translation-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["schema"], "vocab-para-translation-request/v1")
        self.assertGreater(payload["paragraph_count"], 0)


if __name__ == "__main__":
    unittest.main()

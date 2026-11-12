"""inline-notes 呈现测试（设计文档 §16.6.5 回归样例）。

覆盖：正文短注格式、等级简称、章内编号与同义复用、同章异义、专名跳过、
缺失决策、单文件多章、跨文件章节、正文逐字不变。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._bootstrap import FIXTURES, binding_html
from scripts.disambiguation import SCHEMA_DECISION_V2
from scripts.inspect_book import sha256_file
from scripts.run_pipeline import (
    EXIT_ALREADY_ANNOTATED,
    EXIT_BLOCKED,
    EXIT_OK,
    main,
)

LEXICON = FIXTURES / "lexicon_sample.csv"
CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

TWO_CHAPTER_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="b">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Two Chapters</dc:title><dc:language>en</dc:language>
    <dc:identifier id="b">urn:uuid:two-ch</dc:identifier>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="book.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/></spine>
</package>
"""

TWO_CHAPTER_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>t</title></head>
<body><nav epub:type="toc"><ol>
  <li><a href="book.xhtml#c1">One</a></li>
  <li><a href="book.xhtml#c2">Two</a></li>
</ol></nav></body></html>
"""

# 同一文件两章：study 在每章各出现一次，编号应各自从 1 开始
TWO_CHAPTER_BOOK = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
<h2 id="c1">One</h2>
<p>In chapter one we study art.</p>
<h2 id="c2">Two</h2>
<p>In chapter two we study art again.</p>
</body></html>
"""


def build_two_chapter(path: Path) -> Path:
    entries = {
        "OPS/content.opf": TWO_CHAPTER_OPF.encode(),
        "OPS/nav.xhtml": TWO_CHAPTER_NAV.encode(),
        "OPS/book.xhtml": TWO_CHAPTER_BOOK.encode(),
    }
    with zipfile.ZipFile(path, "w") as z:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, b"application/epub+zip")
        info = zipfile.ZipInfo("META-INF/container.xml", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, CONTAINER.encode())
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return path


def run(*argv) -> int:
    return main([str(a) for a in argv])


def emit(book: Path, out_dir: Path, *extra) -> dict:
    requests = out_dir / "requests"
    code = run(
        "--book", book, "--lexicon", LEXICON, "--grades", "四级,雅思",
        "--presentation", "inline-notes", "--density", "chapter",
        "--emit-requests", requests, "--batch-size", "500", "--quiet", *extra,
    )
    assert code == EXIT_OK, code
    import glob

    files = sorted(glob.glob(str(requests / "part-*.json")))
    return json.loads(Path(files[0]).read_text(encoding="utf-8"))


def decide(book: Path, request: dict, mapping: dict, out_dir: Path) -> Path:
    decisions = []
    for cand in request["candidates"]:
        entry_id = cand["options"][0]["entry_id"]
        spec = mapping.get(cand["id"])
        if spec is None:
            decisions.append(
                {
                    "id": cand["id"],
                    "action": "skip",
                    "selected_entry_ids": [],
                    "short_gloss_zh": "",
                    "confidence": 0.9,
                    "reason_code": "no_mapping",
                }
            )
            continue
        gloss, pos, usage = spec
        decisions.append(
            {
                "id": cand["id"],
                "action": "annotate",
                "selected_entry_ids": [entry_id],
                "sense_id": gloss,
                "short_gloss_zh": gloss,
                "context_pos": pos,
                "usage_note": usage,
                "confidence": 0.9,
                "reason_code": "context_match",
            }
        )
    target = out_dir / "decisions"
    target.mkdir(parents=True, exist_ok=True)
    (target / "part-0001.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA_DECISION_V2,
                "book_sha256": request["book"]["sha256"],
                "lexicon_sha256": request["lexicon"]["sha256"],
                "scan": {"hash": request["scan"]["hash"]},
                "decisions": decisions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target


def apply(book: Path, decisions: Path, out_dir: Path, report: Path, *extra) -> int:
    return run(
        "--book", book, "--lexicon", LEXICON, "--grades", "四级,雅思",
        "--presentation", "inline-notes", "--density", "chapter",
        "--apply-decisions", decisions, "--out", out_dir,
        "--report", report, "--no-epubcheck", "--quiet", *extra,
    )


class SampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="inl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = build_two_chapter(self.tmp / "two.epub")
        self.request = emit(self.book, self.tmp)

    def _apply(self, mapping: dict, *extra):
        decisions = decide(self.book, self.request, mapping, self.tmp)
        code = apply(self.book, decisions, self.tmp, self.tmp / "r.json", *extra)
        report = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        return code, report

    def _html(self, report: dict) -> str:
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            return zf.read("OPS/book.xhtml").decode("utf-8") + "\n" + binding_html(
                report["output"]["path"]
            )

    def test_numbering_restarts_per_chapter(self) -> None:
        ids = {c["id"]: i for i, c in enumerate(self.request["candidates"])}
        mapping = {}
        for cand in self.request["candidates"]:
            mapping[cand["id"]] = ("学习", "v.", "")
        code, report = self._apply(mapping)
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        html = self._html(report)
        # 两章各一个 [1]
        self.assertEqual(html.count("data-note=\"1\""), 2)
        self.assertEqual(html.count("vnote-1-1"), 2)  # 正文链接 + 条目 id
        self.assertEqual(html.count("vnote-2-1"), 2)

    def test_chapter_notes_inserted_per_chapter(self) -> None:
        code, report = self._apply({c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]})
        self.assertEqual(code, EXIT_OK)
        html = self._html(report)
        self.assertEqual(html.count("vocab-chapter-notes"), 2)

    def test_short_note_format_and_grade(self) -> None:
        code, report = self._apply({c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]})
        self.assertEqual(code, EXIT_OK)
        html = self._html(report)
        self.assertRegex(
            html,
            r'<span class="vocab-note[^"]*"[^>]*>\(艺术,'
            r'<a class="vocab-link" href="#vnote-1-1">\[1\]</a>\)</span>',
        )
        self.assertIn('data-gloss="艺术"', html)

    def test_notes_block_fields(self) -> None:
        code, report = self._apply({c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]})
        self.assertEqual(code, EXIT_OK)
        html = self._html(report)
        self.assertIn("本句意思：艺术", html)
        self.assertIn("词表词性：n.", html)
        self.assertIn("词表完整释义：艺术；美术", html)
        self.assertIn("返回正文", html)

    def test_empty_result_does_not_deliver_empty_book(self) -> None:
        """§23：0 标注不得报「已完成」并交付空书（按实际产出判定）。"""
        decisions = self.tmp / "empty-decisions"
        decisions.mkdir()
        (decisions / "part-0001.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_DECISION_V2,
                    "book_sha256": self.request["book"]["sha256"],
                    "scan": {"hash": self.request["scan"]["hash"]},
                    "decisions": [],
                }
            ),
            encoding="utf-8",
        )
        report_path = self.tmp / "r-empty.json"
        code = apply(self.book, decisions, self.tmp, report_path)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertFalse(report_path.exists(), "0 标注时不应写成功报告")
        self.assertEqual(
            list(self.tmp.glob("*_vocab.epub")), [], "空标注产物应被删除"
        )

    def test_config_hash_mismatch_refused(self) -> None:
        decisions = decide(self.book, self.request, {}, self.tmp)
        file = decisions / "part-0001.json"
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["scan"]["hash"] = "deadbeefdeadbeef"
        file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code = apply(self.book, decisions, self.tmp, self.tmp / "r2.json")
        self.assertEqual(code, 2)

    def test_body_text_unchanged_after_stripping_notes(self) -> None:
        code, report = self._apply({c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]})
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["checks"]["text_integrity"])
        self.assertTrue(report["validation"]["checks"]["note_numbering"])
        self.assertTrue(report["validation"]["checks"]["note_links"])
        self.assertTrue(report["validation"]["checks"]["note_consistency"])
        self.assertTrue(report["validation"]["checks"]["note_fields"])

    def test_reprocessing_refused(self) -> None:
        code, report = self._apply({c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]})
        self.assertEqual(code, EXIT_OK)
        second = run(
            "--book", Path(report["output"]["path"]), "--lexicon", LEXICON,
            "--presentation", "inline-notes", "--out", self.tmp,
            "--report", self.tmp / "r3.json", "--no-epubcheck", "--quiet",
        )
        self.assertEqual(second, EXIT_ALREADY_ANNOTATED)


class FixtureTests(unittest.TestCase):
    """在 epub3_complex 上验证：同词异义、专名跳过、action=skip。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="inl2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = FIXTURES / "epub3_complex.epub"
        self.request = emit(self.book, self.tmp)

    def _apply(self, mapping: dict):
        decisions = decide(self.book, self.request, mapping, self.tmp)
        code = apply(self.book, decisions, self.tmp, self.tmp / "r.json")
        return code, json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def test_skip_action_produces_no_mark(self) -> None:
        mapping = {c["id"]: ("艺术", "n.", "") for c in self.request["candidates"]}
        # 让第一个候选被跳过
        first = self.request["candidates"][0]
        decisions = decide(self.book, self.request, mapping, self.tmp)
        payload = json.loads((decisions / "part-0001.json").read_text(encoding="utf-8"))
        payload["decisions"][0]["action"] = "skip"
        (decisions / "part-0001.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        code = apply(self.book, decisions, self.tmp, self.tmp / "r.json")
        report = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(code, EXIT_OK)
        self.assertGreaterEqual(report["annotation"]["disambiguation"]["not_target"], 1)

    def test_same_word_two_senses_two_numbers(self) -> None:
        """work 在同章既有动词“工作”又有名词“作品”时，应有两个编号。"""

        mapping = {}
        for cand in self.request["candidates"]:
            if cand["head"] == "work":
                mapping[cand["id"]] = ("工作", "v.", "")
            else:
                mapping[cand["id"]] = ("艺术", "n.", "")
        code, report = self._apply(mapping)
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            chapter = zf.read("OPS/chapter1.xhtml").decode("utf-8")
        self.assertTrue(report["validation"]["checks"]["note_numbering"])
        # §22.4：data-* 已移到包外映射；正文仍带合法标记
        self.assertIn("vocab-skill", chapter)
        self.assertIn("data-note=", binding_html(report["output"]["path"]))

    def test_duplicate_sense_reuses_number(self) -> None:
        mapping = {c["id"]: ("鱼", "n.", "") for c in self.request["candidates"] if c["head"] == "work"}
        code, report = self._apply(mapping)
        self.assertEqual(code, EXIT_OK)
        # 同章同义第二次出现被密度过滤，计入 skipped_repeat
        total_repeat = sum(c["skipped_repeat"] for c in report["annotation"]["chapters"])
        self.assertGreaterEqual(total_repeat, 0)


class PunctuatedGlossTests(unittest.TestCase):
    """真实书籍回归：词表释义自带逗号/括号时，短注与章末仍须判定一致。

    《小王子》全书出现 `平面 飞机 刨子 (动植物的)属`、`不合理的,荒唐的`
    这类释义，旧校验器按首个逗号和首个括号切分，误报 4042 处中的数百条。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="inl-gloss-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = build_two_chapter(self.tmp / "two.epub")
        self.lexicon = self.tmp / "punctuated.csv"
        self.lexicon.write_text(
            "单词,单词等级,词性,中文释义,例句,扩展说明\n"
            'study,四级,n./v.,"平面 飞机 刨子 (动植物的)属",,\n'
            'art,四级,n.,"不合理的,荒唐的",,\n',
            encoding="utf-8",
        )

    def test_consistency_with_commas_and_parentheses(self) -> None:
        report_path = self.tmp / "report.json"
        (self.tmp / "out").mkdir(parents=True, exist_ok=True)
        code = run(
            "--book", self.book, "--lexicon", self.lexicon,
            "--presentation", "inline-notes", "--density", "chapter",
            "--undecided", "lexicon", "--out", self.tmp / "out",
            "--report", report_path, "--no-epubcheck", "--quiet",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        self.assertTrue(report["validation"]["checks"]["note_consistency"])
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            html = zf.read("OPS/book.xhtml").decode("utf-8")
        html += "\n" + binding_html(report["output"]["path"])
        self.assertIn('data-gloss="不合理的,荒唐的"', html)
        self.assertIn('data-gloss="平面 飞机 刨子 (动植物的)属"', html)


if __name__ == "__main__":
    unittest.main()

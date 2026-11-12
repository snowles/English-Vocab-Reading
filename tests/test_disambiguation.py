"""阶段 2：harness 语境消歧协议 + 标注密度测试。

这里不调用任何模型：测试扮演 harness 的角色，直接产出 decisions JSON。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._bootstrap import FIXTURES, binding_html, load_tiny_lexicon
from scripts.disambiguation import (  # noqa: E402
    SCHEMA_DECISION,
    SCHEMA_REQUEST,
    candidate_id,
    is_faithful_gloss,
    load_decisions,
    sense_variants,
)
from scripts.epub_common import local_name, open_epub, parse_xml
from scripts.inspect_book import sha256_file
from scripts.run_pipeline import EXIT_INPUT, EXIT_OK, main

LEXICON = FIXTURES / "lexicon_sample.csv"
BOOK = FIXTURES / "epub3_complex.epub"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="{opf}" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

REPEAT_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="b">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Repeat Fixture</dc:title><dc:language>en</dc:language>
    <dc:identifier id="b">urn:uuid:repeat</dc:identifier>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>
"""

REPEAT_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>t</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="c1.xhtml">1</a></li><li><a href="c2.xhtml">2</a></li>
</ol></nav></body></html>
"""

REPEAT_C1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>1</title></head><body>
<p>First we study hard.</p>
<p>Then we study more.</p>
<p>Finally we study again.</p>
</body></html>
"""

REPEAT_C2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>2</title></head><body>
<p>Another chapter, and we still study.</p>
</body></html>
"""


def build_repeat_epub(path: Path) -> Path:
    """一本含重复词的最小 EPUB（用于密度测试，不落进 fixtures 目录）。"""

    entries = {
        "OPS/content.opf": REPEAT_OPF.encode("utf-8"),
        "OPS/nav.xhtml": REPEAT_NAV.encode("utf-8"),
        "OPS/c1.xhtml": REPEAT_C1.encode("utf-8"),
        "OPS/c2.xhtml": REPEAT_C2.encode("utf-8"),
    }
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, b"application/epub+zip")
        info = zipfile.ZipInfo("META-INF/container.xml", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, CONTAINER.format(opf="OPS/content.opf").encode("utf-8"))
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return path


def run(*argv: str) -> int:
    args = [str(a) for a in argv]
    if "--presentation" not in args:  # 这些用例针对 legacy 引擎（footnote/inline）
        args += ["--presentation", "footnote"]
    return main(args)


class ProtocolTests(unittest.TestCase):
    def test_sense_variants(self) -> None:
        self.assertEqual(sense_variants("工作；起作用"), ["工作", "起作用"])
        self.assertEqual(sense_variants("艺术"), ["艺术"])
        self.assertEqual(sense_variants("a；；b"), ["a", "b"])

    def test_faithful_gloss_accepts_substring(self) -> None:
        self.assertTrue(is_faithful_gloss("起作用", ["工作；起作用"]))
        self.assertTrue(is_faithful_gloss("工作", ["工作；起作用"]))
        self.assertTrue(is_faithful_gloss("工作；起作用", ["工作；起作用"]))
        self.assertTrue(is_faithful_gloss(" 起 作用 ", ["工作；起作用"]))

    def test_faithful_gloss_rejects_invention(self) -> None:
        self.assertFalse(is_faithful_gloss("运转", ["工作；起作用"]))
        self.assertFalse(is_faithful_gloss("", ["工作"]))
        self.assertFalse(is_faithful_gloss("工作（引申义：运作）", ["工作"]))

    def test_candidate_id_is_deterministic(self) -> None:
        a = candidate_id("OPS/c1.xhtml", "study", 0)
        b = candidate_id("OPS/c1.xhtml", "study", 0)
        c = candidate_id("OPS/c1.xhtml", "study", 1)
        d = candidate_id("OPS/c2.xhtml", "study", 0)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertTrue(a.startswith("c-"))

    def test_load_single_file(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dec-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        payload = {
            "schema": SCHEMA_DECISION,
            "decisions": [
                {
                    "id": "c-abc",
                    "is_target_sense": True,
                    "selected_entry_ids": ["work|四级|n."],
                    "short_gloss_zh": "作品",
                    "confidence": 0.8,
                }
            ],
        }
        file = tmp / "part-0001.json"
        file.write_text(json.dumps(payload), encoding="utf-8")
        result = load_decisions(file)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.get("c-abc").short_gloss_zh, "作品")

    def test_load_bad_json_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dec-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "part-0001.json").write_text("{not json", encoding="utf-8")
        result = load_decisions(tmp)
        self.assertEqual(len(result), 0)
        self.assertTrue(result.issues)

    def test_load_duplicate_ids_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dec-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        item = {"id": "c-dup", "short_gloss_zh": "学习", "confidence": 0.9}
        (tmp / "a.json").write_text(json.dumps({"decisions": [item]}), encoding="utf-8")
        (tmp / "b.json").write_text(json.dumps({"decisions": [item]}), encoding="utf-8")
        result = load_decisions(tmp)
        self.assertEqual(len(result), 1)
        self.assertTrue(any("重复" in str(i["reason"]) for i in result.issues))

    def test_load_schema_mismatch_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dec-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a.json").write_text(
            json.dumps({"schema": "wrong/v9", "decisions": []}), encoding="utf-8"
        )
        result = load_decisions(tmp)
        self.assertTrue(any("协议" in str(i["reason"]) for i in result.issues))


class EmitRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="emit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = self.tmp / "requests"
        self.code = run(
            "--book", BOOK,
            "--lexicon", LEXICON,
            "--emit-requests", self.requests,
            "--report", self.tmp / "r.json",
            "--quiet",
        )  # fmt: skip

    def _payload(self) -> dict:
        return json.loads((self.requests / "part-0001.json").read_text(encoding="utf-8"))

    def test_emit_succeeds_without_writing_epub(self) -> None:
        self.assertEqual(self.code, EXIT_OK)
        self.assertTrue(self.requests.is_dir())
        self.assertEqual(list(self.tmp.glob("*.epub")), [])

    def test_request_schema_and_metadata(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["schema"], SCHEMA_REQUEST)
        self.assertIn("instruction", payload)
        self.assertIn("response_schema", payload)
        self.assertEqual(payload["book"]["sha256"], sha256_file(BOOK))
        self.assertEqual(payload["lexicon"]["sha256"], sha256_file(LEXICON))

    def test_only_ambiguous_candidates_emitted(self) -> None:
        payload = self._payload()
        self.assertGreaterEqual(len(payload["candidates"]), 1)
        for candidate in payload["candidates"]:
            self.assertGreaterEqual(len(candidate["options"]), 2)
            self.assertEqual(candidate["reason"], "multi_entry")
        heads = {c["head"] for c in payload["candidates"]}
        self.assertIn("work", heads)  # 词表里有两个 work 词条

    def test_context_is_single_sentence(self) -> None:
        for candidate in self._payload()["candidates"]:
            context = candidate["context"]
            self.assertLessEqual(len(context), 300)
            self.assertNotIn("Hidden art", context)
            self.assertEqual(context.count(". "), 0, context)

    def test_index_written(self) -> None:
        index = json.loads((self.requests / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["total_candidates"], len(self._payload()["candidates"]))
        self.assertIn("next_step", index)

    def test_ids_stable_across_runs(self) -> None:
        first = [c["id"] for c in self._payload()["candidates"]]
        other = self.tmp / "again"
        run(
            "--book", BOOK, "--lexicon", LEXICON,
            "--emit-requests", other, "--quiet",
        )  # fmt: skip
        second = [
            c["id"]
            for c in json.loads((other / "part-0001.json").read_text(encoding="utf-8"))[
                "candidates"
            ]
        ]
        self.assertEqual(first, second)


class ApplyDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="apply-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = self.tmp / "requests"
        run("--book", BOOK, "--lexicon", LEXICON, "--emit-requests", self.requests, "--quiet")
        self.payload = json.loads(
            (self.requests / "part-0001.json").read_text(encoding="utf-8")
        )
        self.candidate = self.payload["candidates"][0]

    def _decide(self, **overrides) -> Path:
        decision = {
            "id": self.candidate["id"],
            "is_target_sense": True,
            "selected_entry_ids": ["work|四级+雅思|v."],
            "short_gloss_zh": "起作用",
            "confidence": 0.92,
        }
        decision.update(overrides)
        decisions = self.tmp / "decisions"
        decisions.mkdir(exist_ok=True)
        (decisions / "part-0001.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_DECISION,
                    "book_sha256": self.payload["book"]["sha256"],
                    "lexicon_sha256": self.payload["lexicon"]["sha256"],
                    "decisions": [decision],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return decisions

    def _apply(self, decisions: Path, *extra: str) -> tuple[int, dict]:
        code = run(
            "--book", BOOK, "--lexicon", LEXICON,
            "--apply-decisions", decisions,
            "--out", self.tmp, "--report", self.tmp / "r.json",
            "--no-epubcheck", "--quiet", *extra,
        )  # fmt: skip
        report_file = self.tmp / "r.json"
        if not report_file.is_file():  # 提前失败（如 sha 不匹配）不会写报告
            return code, {}
        report = json.loads(report_file.read_text(encoding="utf-8"))
        return code, report

    def _chapter_html(self, report: dict) -> str:
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            return zf.read("OPS/chapter1.xhtml").decode("utf-8") + "\n" + binding_html(
                report["output"]["path"]
            )

    def test_applied_decision_changes_gloss(self) -> None:
        code, report = self._apply(self._decide())
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        info = report["annotation"]["disambiguation"]
        self.assertEqual(info["decisions_applied"], 1)
        html = self._chapter_html(report)
        self.assertIn("起作用", html)
        self.assertIn('data-entry-id="work|四级+雅思|v."', html)
        self.assertIn('data-confidence="0.92"', html)
        self.assertIn("按语境选择", html)

    def test_footnote_uses_only_chosen_sense(self) -> None:
        code, report = self._apply(self._decide())
        self.assertEqual(code, EXIT_OK)
        html = self._chapter_html(report)
        # 未选中的词条释义不应出现在该脚注里
        self.assertNotIn("作品；著作", html.split("vocab-conf")[0].split('id="vocab-fn-0-work_')[-1])

    def test_entry_id_follows_gloss_when_entry_not_specified(self) -> None:
        """只给释义不给 entry_ids 时，data-entry-id 必须指向释义真实来源。"""

        code, report = self._apply(
            self._decide(selected_entry_ids=[], short_gloss_zh="作品")
        )
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        html = self._chapter_html(report)
        self.assertIn('data-entry-id="work|四级|n."', html)
        self.assertIn("作品", html)

    def test_low_confidence_falls_back(self) -> None:
        code, report = self._apply(self._decide(confidence=0.2))
        self.assertEqual(code, EXIT_OK)
        info = report["annotation"]["disambiguation"]
        self.assertEqual(info["decisions_applied"], 0)
        self.assertEqual(info["low_confidence"], 1)
        chapter = report["annotation"]["chapters"][0]
        self.assertGreaterEqual(chapter["skipped_undecided"], 1)

    def test_unknown_entry_id_is_invalid(self) -> None:
        code, report = self._apply(
            self._decide(selected_entry_ids=["work|不存在的等级|n."])
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["annotation"]["disambiguation"]["invalid"], 1)
        self.assertEqual(report["annotation"]["disambiguation"]["decisions_applied"], 0)

    def test_unfaithful_gloss_is_invalid(self) -> None:
        code, report = self._apply(self._decide(short_gloss_zh="运转（引申）"))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["annotation"]["disambiguation"]["invalid"], 1)

    def test_not_target_sense_skips_annotation(self) -> None:
        code, report = self._apply(self._decide(is_target_sense=False))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["annotation"]["disambiguation"]["not_target"], 1)
        chapter = report["annotation"]["chapters"][0]
        self.assertGreaterEqual(chapter["skipped_undecided"], 1)

    def test_missing_decision_skip_policy(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "part-0001.json").write_text(
            json.dumps({"schema": SCHEMA_DECISION, "decisions": []}), encoding="utf-8"
        )
        code, report = self._apply(empty, "--undecided", "skip")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["annotation"]["disambiguation"]["missing_decision"], 1)
        html = self._chapter_html(report)
        self.assertNotIn('data-vocab-id="vocab-fn-0-work', html)

    def test_missing_decision_all_senses_policy(self) -> None:
        empty = self.tmp / "empty2"
        empty.mkdir()
        (empty / "part-0001.json").write_text(
            json.dumps({"schema": SCHEMA_DECISION, "decisions": []}), encoding="utf-8"
        )
        code, report = self._apply(empty, "--undecided", "all-senses")
        self.assertEqual(code, EXIT_OK)
        html = self._chapter_html(report)
        # 未消歧时列出词表全部释义
        self.assertIn("工作；起作用", html)
        self.assertIn("作品；著作", html)

    def test_book_sha_mismatch_refused(self) -> None:
        decisions = self._decide()
        file = decisions / "part-0001.json"
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["book_sha256"] = "0" * 64
        file.write_text(json.dumps(payload), encoding="utf-8")
        code, _ = self._apply(decisions)
        self.assertEqual(code, EXIT_INPUT)

    def test_lexicon_sha_mismatch_refused(self) -> None:
        decisions = self._decide()
        file = decisions / "part-0001.json"
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["lexicon_sha256"] = "1" * 64
        file.write_text(json.dumps(payload), encoding="utf-8")
        code, _ = self._apply(decisions)
        self.assertEqual(code, EXIT_INPUT)


class DensityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="density-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = build_repeat_epub(self.tmp / "repeat.epub")

    def _run(self, density: str) -> tuple[int, dict]:
        code = run(
            "--book", self.book, "--lexicon", LEXICON,
            "--density", density,
            "--out", self.tmp, "--report", self.tmp / f"r-{density}.json",
            "--no-epubcheck", "--quiet",
        )  # fmt: skip
        return code, json.loads(
            (self.tmp / f"r-{density}.json").read_text(encoding="utf-8")
        )

    def test_chapter_density_marks_first_per_chapter(self) -> None:
        code, report = self._run("chapter")
        self.assertEqual(code, EXIT_OK)
        chapters = report["annotation"]["chapters"]
        self.assertEqual([c["matches"] for c in chapters], [1, 1])
        self.assertEqual([c["skipped_repeat"] for c in chapters], [2, 0])
        self.assertEqual(report["annotation"]["total_matches"], 2)

    def test_book_density_marks_first_only(self) -> None:
        code, report = self._run("book")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([c["matches"] for c in report["annotation"]["chapters"]], [1, 0])
        self.assertEqual(report["annotation"]["total_matches"], 1)

    def test_all_density_marks_every_occurrence(self) -> None:
        code, report = self._run("all")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([c["matches"] for c in report["annotation"]["chapters"]], [3, 1])

    def test_default_density_is_chapter(self) -> None:
        code = run(
            "--book", self.book, "--lexicon", LEXICON,
            "--out", self.tmp, "--report", self.tmp / "r-default.json",
            "--no-epubcheck", "--quiet",
        )
        self.assertEqual(code, EXIT_OK)
        report = json.loads((self.tmp / "r-default.json").read_text(encoding="utf-8"))
        self.assertEqual(report["annotation"]["density"], "chapter")

    def test_repeat_count_shown_in_footnote(self) -> None:
        code, report = self._run("chapter")
        self.assertEqual(code, EXIT_OK)
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            html = zf.read("OPS/c1.xhtml").decode("utf-8")
        self.assertIn("本章出现 3 次", html)

    def test_first_occurrence_is_the_marked_one(self) -> None:
        code, report = self._run("chapter")
        self.assertEqual(code, EXIT_OK)
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            html = zf.read("OPS/c1.xhtml").decode("utf-8")
        first = html.split("</p>")[0]
        self.assertIn('class="vocab', first)
        # §22.4：class 变为 "vocab vocab-skill"（合法属性 + 已处理标记）
        import re as _re

        self.assertEqual(len(_re.findall(r'class="vocab[ "]', html)), 1)

    def test_body_text_still_intact(self) -> None:
        code, report = self._run("chapter")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["checks"]["text_integrity"])
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            html = zf.read("OPS/c1.xhtml").decode("utf-8")
        self.assertIn("Then we study more.", html)
        self.assertIn("Finally we study again.", html)


class InlinePresentationTests(unittest.TestCase):
    """正文括注模式：`fished（钓鱼）` 直接进正文，只标首次，无脚注。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="inline-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = build_repeat_epub(self.tmp / "repeat.epub")

    def _run(self, *extra: str) -> tuple[int, dict]:
        code = run(
            "--book", self.book, "--lexicon", LEXICON,
            "--presentation", "inline",
            "--out", self.tmp, "--report", self.tmp / "r.json",
            "--no-epubcheck", "--quiet", *extra,
        )  # fmt: skip
        return code, json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def _chapter(self, report: dict, name: str) -> str:
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            return zf.read(name).decode("utf-8") + "\n" + binding_html(
                report["output"]["path"]
            )

    def test_inline_marks_with_parenthesized_gloss(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
        c1 = self._chapter(report, "OPS/c1.xhtml")
        # §22.4：EPUB 2 产物只保留合法 class/id，data-* 在包外映射里
        self.assertIn('class="vocab', c1)
        self.assertIn('data-vocab-id="study"', c1)
        self.assertIn('<span class="vocab-note">（学习）</span>', c1)  # 只取第一义项

    def test_gloss_appears_right_after_word(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        c1 = self._chapter(report, "OPS/c1.xhtml")
        # 括注 span 紧跟在词 span 后
        import re as _re

        m = _re.search(
            r'<span class="vocab[^"]*"[^>]*>study</span>'
            r'<span class="vocab-note[^"]*"[^>]*>（[^）]+）</span>',
            c1,
        )
        self.assertIsNotNone(m, c1[:800])

    def test_only_first_occurrence_inlined(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        c1 = self._chapter(report, "OPS/c1.xhtml")
        self.assertEqual(c1.count('class="vocab-note"'), 1)
        # 后两句话里 study 不带括注
        tail = c1.split("</p>")[1]
        self.assertIn("Then we study more.", tail)

    def test_no_footnote_block_in_inline_mode(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        for name in ("OPS/c1.xhtml", "OPS/c2.xhtml"):
            html = self._chapter(report, name)
            self.assertNotIn("vocab-notes", html)
            self.assertNotIn("<aside", html)

    def test_footnotes_counted_zero(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["annotation"]["total_footnotes"], 0)
        self.assertEqual(report["annotation"]["presentation"], "inline")

    def test_text_integrity_passes_after_stripping_notes(self) -> None:
        """括注剥离后，正文必须与源文件逐字一致（防丢字/错位）。"""

        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["checks"]["text_integrity"])

    def test_source_text_integrity_real_compare(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        # 逐字比对只针对 EPUB 正文，不拼接包外映射文本
        with zipfile.ZipFile(report["output"]["path"]) as zf:
            c1 = zf.read("OPS/c1.xhtml").decode("utf-8")
        plain = _strip_inline(c1)
        source = _strip_inline(
            zipfile.ZipFile(self.book).read("OPS/c1.xhtml").decode("utf-8")
        )
        self.assertEqual(_norm_text(source), _norm_text(plain))

    def test_validation_ok_overall(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["ok"], report["validation"]["errors"])

    def test_reject_reprocessing_inline_output(self) -> None:
        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        second = run(
            "--book", Path(report["output"]["path"]), "--lexicon", LEXICON,
            "--presentation", "inline", "--out", self.tmp,
            "--report", self.tmp / "r2.json", "--no-epubcheck", "--quiet",
        )  # fmt: skip
        self.assertEqual(second, 3)  # EXIT_ALREADY_ANNOTATED

    def test_long_gloss_truncated(self) -> None:
        """超长释义截断加省略号（正文呈现要短）。"""

        lex = load_tiny_lexicon("art,四级,n.,艺术与审美创造活动及其作品")
        from scripts.annotate_epub import _inline_gloss, Placed
        from scripts.matching import Match

        match = Match(0, 3, "art", tuple(lex.lookup("art")), "word")
        placed = Placed(match=match, slot_index=0, group="art", entry_id="art|四级|n.")
        # 第一义项超长 -> 截断加省略号
        self.assertEqual(_inline_gloss(placed, "art", lex, 6), "艺术与审美创…")
        # max_chars=0 不截断
        self.assertEqual(_inline_gloss(placed, "art", lex, 0), "艺术与审美创造活动及其作品")
        # 正常长度不截断
        lex2 = load_tiny_lexicon("art,四级,n.,艺术；美术")
        match2 = Match(0, 3, "art", tuple(lex2.lookup("art")), "word")
        placed2 = Placed(match=match2, slot_index=0, group="art", entry_id="art|四级|n.")
        self.assertEqual(_inline_gloss(placed2, "art", lex2, 12), "艺术")

    def test_stoplist_skips_function_words(self) -> None:
        """虚词默认不标；--no-stoplist 可关。"""

        code, report = self._run()
        self.assertEqual(code, EXIT_OK)
        c1 = self._chapter(report, "OPS/c1.xhtml")
        self.assertRegex(
            c1, r'study</span><span class="vocab-note[^"]*"[^>]*>（学习）'
        )
        self.assertNotIn('data-vocab-id="that"', c1)
        self.assertGreaterEqual(report["annotation"]["disambiguation"]["stoplist_skipped"], 0)
        # 关掉 stoplist 后虚词也标
        code2, report2 = self._run("--no-stoplist")
        self.assertEqual(code2, EXIT_OK)
        c1b = self._chapter(report2, "OPS/c1.xhtml")
        self.assertIn('class="vocab-note"', c1b)

    def test_density_segment_with_inline(self) -> None:
        code, report = self._run("--density", "segment", "--segment-words", "5")
        self.assertEqual(code, EXIT_OK)
        # 段内只标一次，但全书可重复出现
        c1 = self._chapter(report, "OPS/c1.xhtml")
        self.assertGreaterEqual(c1.count('class="vocab-note"'), 1)


def _strip_inline(html_text: str) -> str:
    import re as _re

    return _re.sub(r'<span class="vocab-note">[^<]*</span>', "", html_text)


def _norm_text(html_text: str) -> str:
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", html_text)
    return " ".join(text.split())


if __name__ == "__main__":
    unittest.main()

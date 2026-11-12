"""§22 回归：EPUB 2 合规 DOM、标注映射、源书基线与兼容/严格发布判定。

覆盖 §22.7 验收表里能自动化的场景；需要 Java 的真实 EPUBCheck 用例
用 ``skipUnless`` 守卫，其余用官方 JSON 结构做确定性单测（离线、快速）。
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from tests._bootstrap import ROOT
from scripts.binding import (
    MARK_CLASS,
    VOCAB_ATTRS,
    apply_binding,
    audit_binding,
    binding_path_for,
    is_annotated,
    load_binding,
    strip_vocab_metadata,
    write_binding,
)
from scripts.compat_fixes import _encode_spaces, analyze, apply_fixes
from scripts.compatibility import (
    classify_issue,
    compare_reports,
    evaluate,
    summary_text,
)
from scripts.validate_epub import find_epubcheck

FIXTURES = ROOT / "tests" / "fixtures"
VERIFY = ROOT / "examples" / "preview_two_chapters" / "_verify"
STEP3 = VERIFY / "step3"

XHTML = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
<p>He <span class="vocab" id="vref-1-1-1" data-vocab-id="a1" data-note="1" data-chapter="1">fished</span>
<span class="vocab-note" data-gloss="\xe9\x92\x93\xe9\xb1\xbc">(\xe9\x92\x93\xe9\xb1\xbc,[1])</span> alone.</p>
<section class="vocab-chapter-notes" id="vnotes-1"><ol>
<li class="vocab-item" id="vnote-1-1" data-head="fish"><p class="vocab-sense" data-gloss="\xe9\x92\x93\xe9\xb1\xbc">\xe6\x9c\xac\xe5\x8f\xa5\xe6\x84\x8f\xe6\x80\x9d\xef\xbc\x9a\xe9\x92\x93\xe9\xb1\xbc</p></li>
</ol></section></body></html>"""


def _message(
    severity="ERROR",
    code="RSC-005",
    text='Error while parsing file: element "br" not allowed here',
    path="c.xhtml",
    line=1,
    column=1,
    merged=0,
):
    return {
        "severity": severity,
        "ID": code,
        "message": text,
        "locations": [{"path": path, "line": line, "column": column}],
        "additionalLocations": merged,
    }


def _report(*messages):
    return {"messages": list(messages), "checker": {"nError": len(messages), "nWarning": 0, "nFatal": 0}}


class BindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clean, self.records = strip_vocab_metadata(XHTML, "c.xhtml")

    def test_pending_and_final_share_one_basename(self) -> None:
        pending = binding_path_for(Path("x/book_vocab.epub.pending"), pending=True)
        final = binding_path_for(Path("x/book_vocab.epub"))
        self.assertEqual(pending.name, final.name + ".pending")

    def test_is_annotated_accepts_new_and_legacy_marks(self) -> None:
        self.assertTrue(is_annotated(self.clean))
        self.assertTrue(is_annotated(b'<span data-vocab-id="x"></span>'))
        self.assertFalse(is_annotated(b"<p>plain text</p>"))

    def test_strip_removes_only_whitelisted_attrs_and_section(self) -> None:
        self.assertNotIn(b"data-vocab-id", self.clean)
        self.assertNotIn(b"data-gloss", self.clean)
        self.assertNotIn(b"data-head", self.clean)
        self.assertNotIn(b"<section", self.clean)
        self.assertIn(b'<div class="vocab-chapter-notes"', self.clean)
        self.assertIn(MARK_CLASS.encode(), self.clean)
        self.assertIn(b"fished", self.clean)
        self.assertIn("vref-1-1-1", self.records)
        self.assertIn("vnote-1-1", self.records)
        self.assertEqual(len(self.records), 4)
        self.assertEqual(self.records["vref-1-1-1"]["data-vocab-id"], "a1")
        self.assertEqual(self.records["vref-1-1-1"]["_tag"], "span")

    def test_plain_xhtml_is_not_reserialized(self) -> None:
        plain = b"<html><body><p>plain</p></body></html>"
        clean, records = strip_vocab_metadata(plain, "a.xhtml")
        self.assertEqual(clean, plain)
        self.assertEqual(records, {})

    def test_apply_binding_restores_attrs_across_documents(self) -> None:
        root1 = etree.fromstring(self.clean).getroottree()
        root2 = etree.fromstring(b"<html><body><p>other</p></body></html>").getroottree()
        binding = {"attrs": self.records}
        stats = apply_binding([root1.getroot(), root2.getroot()], binding)
        self.assertEqual(stats["matched"], len(self.records))
        self.assertEqual(stats["missing"], 0)
        mark = next(el for el in root1.getroot().iter() if el.get("id") == "vref-1-1-1")
        self.assertEqual(mark.get("data-vocab-id"), "a1")

    def test_audit_detects_tampered_record(self) -> None:
        trees = {"c.xhtml": etree.fromstring(self.clean).getroottree()}
        binding = {"attrs": copy.deepcopy(self.records)}
        self.assertEqual(audit_binding(trees, binding), [])
        binding["attrs"]["vref-1-1-1"]["_text"] = "TAMPERED"
        errors = audit_binding(trees, binding)
        self.assertTrue(any("文本与产物不一致" in e for e in errors))

    def test_audit_detects_unregistered_mark(self) -> None:
        trees = {"c.xhtml": etree.fromstring(self.clean).getroottree()}
        binding = {"attrs": copy.deepcopy(self.records)}
        del binding["attrs"]["vref-1-1-1"]
        errors = audit_binding(trees, binding)
        self.assertTrue(any("未登记的标注节点" in e for e in errors))

    def test_write_binding_binds_epub_sha256(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-binding-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        epub = tmp / "book.epub"
        epub.write_bytes(b"fake")
        path = write_binding(tmp / "m.json", epub, self.records)
        payload = load_binding(path)
        self.assertEqual(payload["schema"], "vocab-reader-map/v1")
        trees = {"c.xhtml": etree.fromstring(self.clean).getroottree()}
        self.assertEqual(audit_binding(trees, payload, "WRONG"), ["标注映射与 EPUB 产物字节不一致（SHA-256 不匹配）"])

    def test_same_id_in_different_documents_is_allowed(self) -> None:
        """§P0-1：EPUB 锚点只在本文档内解析，跨章同名 ID 是合法输入。"""

        page = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="p1">x</p></body></html>'
        trees = {name: etree.fromstring(page) for name in ("ch1.xhtml", "ch2.xhtml")}
        self.assertEqual(audit_binding(trees, {"attrs": {}}), [])

    def test_duplicate_id_inside_one_document_is_rejected(self) -> None:
        """同文档内重复 ID 仍是产物损坏，不能因为放宽跨文档而整体放行。"""

        page = (
            b'<html xmlns="http://www.w3.org/1999/xhtml">'
            b'<body><p id="p1">a</p><p id="p1">b</p></body></html>'
        )
        errors = audit_binding({"ch1.xhtml": etree.fromstring(page)}, {"attrs": {}})
        self.assertTrue(any("ch1.xhtml" in e and "重复 id" in e for e in errors))

    def test_binding_binds_only_the_document_it_recorded(self) -> None:
        """§P0-1：映射按 _doc 定位，同名节点不会被错绑到另一章。"""

        page = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="p1">same</p></body></html>'
        trees = {name: etree.fromstring(page) for name in ("ch1.xhtml", "ch2.xhtml")}
        record = {
            "_doc": "ch2.xhtml",
            "_tag": "p",
            "_class": "",
            "_text": "same",
            "data-note": "7",
        }
        self.assertEqual(audit_binding(trees, {"attrs": {"p1": record}}), [])
        stats = apply_binding(trees, {"attrs": {"p1": record}})
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["missing"], 0)
        first = next(el for el in trees["ch1.xhtml"].iter() if el.get("id") == "p1")
        second = next(el for el in trees["ch2.xhtml"].iter() if el.get("id") == "p1")
        self.assertIsNone(first.get("data-note"))
        self.assertEqual(second.get("data-note"), "7")

    def test_mapping_without_doc_refuses_ambiguous_match(self) -> None:
        """缺 _doc 且同名跨章时宁可报歧义，也不猜一个节点注入。"""

        page = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="p1">same</p></body></html>'
        trees = {name: etree.fromstring(page) for name in ("ch1.xhtml", "ch2.xhtml")}
        record = {"_tag": "p", "_class": "", "_text": "same", "data-note": "7"}
        errors = audit_binding(trees, {"attrs": {"p1": record}})
        self.assertTrue(any("无法确定绑定" in e for e in errors))
        self.assertEqual(apply_binding(trees, {"attrs": {"p1": record}})["ambiguous"], 1)

    def test_audit_accepts_bare_elements(self) -> None:
        """§P0-1：审计器不能只接受 ElementTree，传根节点也应工作。"""

        page = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="p1">x</p></body></html>'
        self.assertEqual(audit_binding({"ch1.xhtml": etree.fromstring(page)}, {"attrs": {}}), [])

    def test_vocab_attrs_whitelist_is_not_empty(self) -> None:
        self.assertIn("data-gloss", VOCAB_ATTRS)
        self.assertIn("data-vocab-id", VOCAB_ATTRS)


class DifferenceTests(unittest.TestCase):
    def test_line_and_column_changes_are_not_new_issues(self) -> None:
        diff = compare_reports(_report(_message(line=10, column=5)),
                               _report(_message(line=88, column=42)))
        self.assertEqual(diff["new"], [])
        self.assertEqual(diff["fixed"], [])
        self.assertEqual(len(diff["inherited"]), 1)

    def test_merged_locations_are_flagged_not_silently_cleared(self) -> None:
        diff = compare_reports(_report(_message(merged=21)),
                               _report(_message(merged=21)))
        self.assertEqual(diff["new"], [])
        self.assertTrue(diff["inherited"][0]["locations_merged"])

    def test_same_error_code_new_illegal_node_is_blocking(self) -> None:
        source = _report(_message(text='Error while parsing file: element "br" not allowed here'))
        output = _report(
            _message(text='Error while parsing file: element "br" not allowed here'),
            _message(text='Error while parsing file: attribute "data-gloss" not allowed here'),
        )
        diff = compare_reports(source, output)
        self.assertEqual(len(diff["new"]), 1)
        self.assertEqual(classify_issue(diff["new"][0])[0], "blocking")

    def test_fixing_one_error_does_not_hide_a_new_one(self) -> None:
        source = _report(
            _message(code="OPF-054", text='Date value "1988-12-31 21:00:00-08:00" is not valid'),
            _message(text='Error while parsing file: attribute "clear" not allowed here'),
            _message(text='Error while parsing file: element "br" not allowed here'),
        )
        output = _report(
            _message(text='Error while parsing file: element "br" not allowed here'),
            _message(text='Error while parsing file: attribute "data-unit" not allowed here'),
        )
        diff = compare_reports(source, output)
        self.assertEqual(len(diff["fixed"]), 2)
        self.assertEqual(len(diff["new"]), 1)
        self.assertEqual(classify_issue(diff["new"][0])[0], "blocking")

    def test_content_blocking_issue_is_never_legacy_exception(self) -> None:
        item = {"severity": "ERROR", "code": "RSC-005",
                "message": "spine points to a missing manifest item"}
        self.assertEqual(classify_issue(item)[0], "blocking")

    def test_all_sample_source_issues_have_a_decision(self) -> None:
        for name in ("prince", "oldman"):
            path = VERIFY / f"epubcheck_{name}_source.json"
            if not path.is_file():
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            for message in report["messages"]:
                item = {"severity": message["severity"], "code": message["ID"],
                        "message": message["message"]}
                category, _ = classify_issue(item)
                with self.subTest(book=name, code=message["ID"], msg=message["message"][:50]):
                    self.assertIn(category, ("acceptable_legacy", "blocking"))
                    self.assertEqual(category, "acceptable_legacy")

    def test_summary_is_short_and_free_of_internal_terms(self) -> None:
        result = {
            "publishable": True,
            "external_validation_incomplete": False,
            "delivery_kind": "compatible_delivery",
            "issues": {"acceptable_legacy": [{}] * 3, "fixed": [], "new": [],
                       "blocking": [], "unresolved": []},
            "official": {"strict_error_count": 0},
        }
        text = summary_text(result)
        self.assertIn("原书有 3 类格式瑕疵", text)
        self.assertNotIn("data-", text)
        self.assertNotIn("RSC-005", text)
        self.assertLess(len(text), 100)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        import scripts.compatibility as module

        self.module = module
        self.original = module._run_epubcheck
        self.tmp = Path(tempfile.mkdtemp(prefix="vocab-policy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "source.epub").write_bytes(b"fake-source")
        (self.tmp / "s.epub").write_bytes(b"fake-source")

    def tearDown(self) -> None:
        self.module._run_epubcheck = self.original

    def _fake(self, report, source_report=None):
        def runner(path, *, report_path=None):
            payload = source_report if "source" in str(path) else report
            if report_path:
                Path(report_path).write_text(json.dumps(payload), encoding="utf-8")
            counts = (payload.get("checker") or {})
            return {"ran": True, "status": "passed" if counts.get("nError", 0) == 0 else "failed",
                    "ok": counts.get("nError", 0) == 0, "counts": counts,
                    "version": "EPUBCheck v5.4.0", "messages": []}
        self.module._run_epubcheck = runner

    def test_compatible_delivers_with_inherited_errors(self) -> None:
        legacy = _message(code="OPF-054",
                          text='Date value "1988-12-31 21:00:00-08:00" is not valid')
        src = _report(legacy)
        self._fake(_report(legacy), src)
        result = evaluate(source_path=self.tmp / "source.epub",
                          output_path=self.tmp / "out.epub",
                          artifact_dir=self.tmp, policy="compatible", internal_ok=True)
        self.assertTrue(result["publishable"])
        self.assertEqual(result["delivery_kind"], "compatible_delivery")
        self.assertEqual(len(result["issues"]["acceptable_legacy"]), 1)
        self.assertEqual(result["issues"]["new"], [])
        self.assertFalse(result["external_validation_incomplete"])

    def test_strict_rejects_nonzero_errors(self) -> None:
        legacy = _message(code="OPF-054",
                          text='Date value "1988-12-31 21:00:00-08:00" is not valid')
        self._fake(_report(legacy), _report(legacy))
        result = evaluate(source_path=self.tmp / "source.epub",
                          output_path=self.tmp / "out2.epub",
                          artifact_dir=self.tmp, policy="strict", internal_ok=True)
        self.assertFalse(result["publishable"])
        self.assertTrue(any(x.get("code") == "STRICT_POLICY"
                            for x in result["issues"]["blocking"]))

    def test_new_illegal_node_blocks_compatible(self) -> None:
        legacy = _message(code="OPF-054",
                          text='Date value "1988-12-31 21:00:00-08:00" is not valid')
        new = _message(text='Error while parsing file: attribute "data-gloss" not allowed here')
        self._fake(_report(legacy, new), _report(legacy))
        result = evaluate(source_path=self.tmp / "source.epub",
                          output_path=self.tmp / "out3.epub",
                          artifact_dir=self.tmp, policy="compatible", internal_ok=True)
        self.assertFalse(result["publishable"])
        self.assertTrue(result["issues"]["blocking"])

    def test_unclassified_inherited_issue_is_not_silently_accepted(self) -> None:
        weird = _message(code="OPF-999", text="unknown metadata problem")
        self._fake(_report(weird), _report(weird))
        result = evaluate(source_path=self.tmp / "source.epub",
                          output_path=self.tmp / "out4.epub",
                          artifact_dir=self.tmp, policy="compatible", internal_ok=True)
        self.assertFalse(result["publishable"])
        self.assertTrue(result["issues"]["unresolved"])

    def test_tool_error_blocks_and_not_run_is_disclosed(self) -> None:
        self.module._run_epubcheck = lambda *a, **k: {
            "ran": False, "status": "tool_error", "reason": "EPUBCheck 超时"}
        error = evaluate(source_path=self.tmp / "s.epub", output_path=self.tmp / "o.epub",
                         artifact_dir=self.tmp, internal_ok=True)
        self.assertFalse(error["publishable"])
        self.assertEqual(error["official"]["status"], "tool_error")
        self.module._run_epubcheck = lambda *a, **k: {
            "ran": False, "status": "not_run", "reason": "未找到 EPUBCheck"}
        missing = evaluate(source_path=self.tmp / "s.epub", output_path=self.tmp / "o.epub",
                           artifact_dir=self.tmp, internal_ok=True)
        self.assertTrue(missing["publishable"])
        self.assertTrue(missing["external_validation_incomplete"])
        self.assertEqual(missing["delivery_kind"], "compatible_unverified")

    def test_strict_without_tool_is_blocked(self) -> None:
        result = evaluate(source_path=self.tmp / "s.epub", output_path=self.tmp / "o.epub",
                          artifact_dir=self.tmp, policy="strict", run_epubcheck=False,
                          internal_ok=True)
        self.assertFalse(result["publishable"])
        self.assertEqual(result["official"]["status"], "not_run")


class CompatFixesTests(unittest.TestCase):
    def test_encode_spaces_does_not_double_encode_or_touch_fragment(self) -> None:
        self.assertEqual(_encode_spaces("a b.xhtml#p 1"), "a%20b.xhtml#p%201")
        self.assertEqual(_encode_spaces("a%20b.xhtml"), "a%20b.xhtml")
        self.assertEqual(_encode_spaces("https://x/a b"), "https://x/a b")
        self.assertEqual(_encode_spaces("#a b"), "#a b")

    def test_sample_books_have_no_blocking_legacy_issue(self) -> None:
        for path, expected in (
            (r"C:\Users\user\Downloads\The little prince.epub", "prince"),
            (r"C:\Users\user\Downloads\The Old Man and the Sea.epub", "oldman"),
        ):
            if not Path(path).is_file():
                continue
            with self.subTest(book=expected):
                info = analyze(path)
                self.assertEqual(info["blocking"], [])

    def test_work_copy_fixes_navigation_and_keeps_content(self) -> None:
        if not (STEP3 / "fixture.epub").is_file():
            self.skipTest("缺少 §22.5 fixture")
        tmp = Path(tempfile.mkdtemp(prefix="vocab-fixes-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        info = apply_fixes(STEP3 / "fixture.epub", tmp / "work.epub")
        verify = info["verification"]
        for key in ("visible_text_identical", "spine_order_identical",
                    "binary_resources_identical", "links_resolvable",
                    "input_sha256_unchanged"):
            self.assertTrue(verify[key], key)
        self.assertEqual(verify["ncx_navpoints"], 1)
        self.assertIn("missing_ncx", [x["rule"] for x in info["applied"]])
        with zipfile.ZipFile(tmp / "work.epub") as zf:
            names = zf.namelist()
            self.assertIn("OPS/chapter one.xhtml", names)  # 文件名未重命名
            self.assertNotIn(b"%2520", zf.read("OPS/content.opf"))

    def test_duplicate_id_refuses_work_copy(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-dup-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        book = tmp / "dup.epub"
        opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="b">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>D</dc:title>
<dc:language>en</dc:language><dc:identifier id="b">urn:uuid:dup</dc:identifier></metadata>
<manifest><item id="c1" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine toc="ncx"><itemref idref="c1"/></spine></package>"""
        xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>
<body><p id="same">one</p><p id="same">two</p></body></html>"""
        ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/>
<docTitle><text>D</text></docTitle><navMap><navPoint id="n1" playOrder="1">
<navLabel><text>D</text></navLabel><content src="c.xhtml"/></navPoint></navMap></ncx>"""
        container = ('<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:'
                     'xmlns:container"><rootfiles><rootfile full-path="content.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')
        with zipfile.ZipFile(book, "w") as zf:
            info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/epub+zip")
            for name, data in {"META-INF/container.xml": container, "content.opf": opf,
                               "c.xhtml": xhtml, "toc.ncx": ncx}.items():
                zf.writestr(name, data)
        self.assertEqual([x["rule"] for x in analyze(book)["blocking"]], ["duplicate_id"])
        with self.assertRaises(ValueError):
            apply_fixes(book, tmp / "work.epub")


@unittest.skipUnless(find_epubcheck().get("available"), "本机无 Java + EPUBCheck")
class OfficialCheckTests(unittest.TestCase):
    def test_clean_epub2_with_annotations_still_passes(self) -> None:
        """§22.7 场景 1：合法 EPUB 2 加词注后官方 ERROR 仍为零。"""

        from scripts.validate_epub import _run_epubcheck

        tmp = Path(tempfile.mkdtemp(prefix="vocab-official-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        import subprocess
        import sys

        code = subprocess.run(
            [sys.executable, "scripts/run_pipeline.py", "--book",
             str(FIXTURES / "epub2_basic.epub"), "--lexicon",
             str(FIXTURES / "lexicon_sample.csv"), "--out", str(tmp),
             "--report", str(tmp / "r.json"), "--presentation", "footnote",
             "--no-epubcheck", "--quiet"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        self.assertEqual(code.returncode, 0, code.stdout + code.stderr)
        outputs = list(tmp.glob("*.epub"))
        self.assertEqual(len(outputs), 1)
        result = _run_epubcheck(outputs[0])
        self.assertTrue(result["ran"])
        self.assertEqual((result.get("counts") or {}).get("nError"), 0)


if __name__ == "__main__":
    unittest.main()

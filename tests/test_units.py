"""§19 测试：token 定位、词形关联、成员边界、错误不复活、读者可见结果。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._bootstrap import FIXTURES, binding_html
from scripts.run_pipeline import EXIT_BLOCKED, EXIT_OK, EXIT_PARTIAL, main
from scripts.units import related_form

LEXICON = FIXTURES / "lexicon_v4.csv"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="b">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Units v5 Fixture</dc:title><dc:language>en</dc:language>
    <dc:identifier id="b">urn:uuid:units-v5</dc:identifier>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="book.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/></spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>t</title></head><body><nav epub:type="toc"><ol>
<li><a href="book.xhtml">One</a></li></ol></nav></body></html>
"""

P0 = "He was an old man who fished alone in a skiff."
P1 = "He was taking a fish from the sea."
P2 = "The benevolent skin cancer on his cheek was not dangerous."
P3 = "He gave up and caught three fish near the mast."
P4 = "He was tak<em>ing</em> the fish home."

BOOK = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
<p>{P0}</p>
<p>{P1}</p>
<p>{P2}</p>
<p>{P3}</p>
<p>{P4}</p>
</body></html>
"""

DOC = "OPS/book.xhtml"
EXTRA = ["--density", "all"]


def build_book(path: Path) -> Path:
    entries = {
        "OPS/content.opf": OPF.encode(),
        "OPS/nav.xhtml": NAV.encode(),
        "OPS/book.xhtml": BOOK.encode(),
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


class UnitsFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="units-v5-"))
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
        self.paras: dict[str, dict] = {}
        self.tokens: dict[str, list[dict]] = {}
        for name in sorted(self.requests.glob("windows-*.json")):
            payload = json.loads(name.read_text(encoding="utf-8"))
            for window in payload["windows"]:
                for para in window["paragraphs"]:
                    self.paras[para["pid"]] = para
                    self.tokens[para["pid"]] = para["tokens"]

    # -- 构造工具 ----------------------------------------------------

    def _pid(self, tail: int) -> str:
        return f"{DOC}#{tail}"

    def _ids(self, tail: int, phrase: str, occurrence: int = 0) -> list[str]:
        toks = self.tokens[self._pid(tail)]
        words = [w.casefold() for w in phrase.split()]
        hits = [
            [t["id"] for t in toks[i : i + len(words)]]
            for i in range(len(toks) - len(words) + 1)
            if [t["t"].casefold() for t in toks[i : i + len(words)]] == words
        ]
        self.assertGreater(len(hits), occurrence, f"{phrase} @ {tail}")
        return hits[occurrence]

    def _comp(self, tail: int, phrase: str, entry_id: str = "", occurrence: int = 0) -> dict:
        return {
            "token_ids": self._ids(tail, phrase, occurrence),
            "entry_ids": [entry_id] if entry_id else [],
        }

    def _ann(
        self,
        tail: int,
        phrase: str,
        gloss: str,
        *,
        source: str = "out_of_lexicon",
        entries: list[str] | None = None,
        components: list[dict] | None = None,
        category: str = "",
        unit: str = "word",
        surface: str = "",
        occurrence: int = 0,
        annotation_id: str = "",
    ) -> dict:
        token_ids = self._ids(tail, phrase, occurrence)
        return {
            "annotation_id": annotation_id or f"a-{tail}-{phrase.replace(' ', '-')}",
            "pid": self._pid(tail),
            "token_ids": token_ids,
            "surface": surface,
            "unit_type": unit,
            "source_type": source,
            "whole_entry_ids": entries or [],
            "components": components or [],
            "short_gloss_zh": gloss,
            "reader_usage_note": "",
            "usage_category": category,
            "usage_domain": "",
            "sense_key": gloss,
            "evidence": "测试",
            "action": "annotate",
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
        verdicts=None,
        *,
        schema_ok: bool = True,
        config: bool = True,
        tokenizer: str = "v1",
        binding: dict | None = None,
        windows: list[dict] | None = None,
        window_verdicts=None,
        write_review: bool = True,
    ):
        scope = self._scope()
        binding = dict(binding or self._binding())
        if not config:
            binding["config_hash"] = "deadbeefdeadbeef"
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
                        if a.get("window_id") == window["window_id"]
                    ],
                    "reason": "",
                }
                for window in scope["windows"]
            ]
        payload = {
            "schema": "vocab-annotation-decisions/v6" if schema_ok else "bogus/v1",
            "protocol": binding["protocol"] if schema_ok else "v4",
            **binding,
            "scan": {"hash": binding["config_hash"]},
            "task_id": scope["task_id"],
            "windows": windows,
            "annotations": annotations,
            "suppressed": [],
        }
        payload["tokenizer"] = tokenizer
        (self.requests / "ann-0001.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        if not schema_ok or not write_review or tokenizer != "v1" or not config:
            return
        # 必须先导出审查包，拿到每条的真实 revision_hash 与窗口哈希
        review_target = self.tmp / "review_req"
        code = main(
            [
                "--book", str(self.book), "--lexicon", str(LEXICON),
                "--engine", "units", "--apply-annotations", str(self.requests),
                "--emit-review", str(review_target), *EXTRA, "--quiet",
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
                if a.get("action") == "annotate"
            ]
        if window_verdicts is None:
            window_verdicts = [
                {
                    "window_id": wid,
                    "reviewed_window_hash": window_digest,
                    "coverage_status": "pass",
                    "issues": [],
                }
                for wid, window_digest in window_hashes.items()
            ]
        review = {
            "schema": "vocab-annotation-review/v6",
            "protocol": binding["protocol"],
            **{k: binding[k] for k in binding if k != "protocol"},
            "scan": {"hash": binding["config_hash"]},
            "task_id": scope["task_id"],
            "review_round": 1,
            "reviewer": {"kind": "test", "name": "fixture"},
            "window_verdicts": window_verdicts,
            "verdicts": verdicts,
        }
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
            return zf.read(DOC).decode("utf-8") + "\n" + binding_html(
                report["output"]["path"]
            )

    def _stats(self, report: dict) -> dict:
        return report["annotation"]["disambiguation"]

    def _codes(self, report: dict) -> dict:
        return (report.get("release", {}).get("validation") or {}).get(
            "invalid_codes", {}
        )

    # -- §19.2 / §19.3 位置与词形 ------------------------------------

    def test_taking_maps_to_take(self) -> None:
        ann = self._ann(
            1, "taking", "捕到", source="exact_entry", entries=["take|四级+牛津3000|vt."]
        )
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn("(捕到,", html)  # 11-11：正文括注不再带等级
        self.assertIn("taking", html)
        self.assertIn(" → ", html)
        self.assertIn(">take</span>", html)
        self.assertIn("词表完整释义：拿；取；花费；乘坐；认为", html)
        self.assertIn("扩展说明：音标：teɪk", html)
        self.assertNotIn(">taki<", html)  # 不允许残缺词成为标注/章末词形

    def test_substring_is_never_a_token(self) -> None:
        from scripts.load_lexicon import load_lexicon

        lex = load_lexicon(LEXICON)
        take = lex.lookup("take")[0]
        self.assertFalse(related_form("taki", take))
        self.assertTrue(related_form("taking", take))

    def test_legacy_span_decision_is_rejected(self) -> None:
        a = self._ann(
            1, "taking", "捕到", source="exact_entry", entries=["take|四级+牛津3000|vt."]
        )
        a.pop("token_ids")  # v4 记录：只有 span/surface
        self._write([a])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("missing_token_ids", 0), 1)
        self.assertEqual(report["output"]["path"], "")
        self.assertFalse((self.out / "book_vocab.epub").exists())

    def test_surface_echo_mismatch_rejected(self) -> None:
        ann = self._ann(
            1,
            "taking",
            "捕到",
            source="exact_entry",
            entries=["take|四级+牛津3000|vt."],
            surface="taki",
        )
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("surface_mismatch", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_wrong_entry_rejected(self) -> None:
        ann = self._ann(
            1,
            "taking",
            "捕到",
            source="exact_entry",
            entries=["sail|四级+雅思+牛津3000|v."],
        )
        self._write([ann])
        code, _ = self._apply()
        self.assertEqual(code, EXIT_BLOCKED)  # 默认严格：不生成成品
        self.assertFalse((self.out / "book_vocab.epub").exists())

        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("entry_form_mismatch", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_caught_maps_to_catch(self) -> None:
        ann = self._ann(
            3, "caught", "捉住", source="exact_entry",
            entries=["catch|四级+雅思+牛津3000|v."],
        )  # fmt: skip
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn(">caught</span>", html)
        self.assertIn(">catch</span>", html)
        self.assertIn("(捉住,", html)  # 11-11：正文括注不再带等级

    def test_phrase_whole_entry_with_inflection(self) -> None:
        ann = self._ann(
            3, "gave up", "放弃", source="exact_entry",
            entries=["give up|四级+雅思|phr."], unit="phrase",
        )  # fmt: skip
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn(">gave up</span>", html)
        self.assertIn(">give up</span>", html)
        self.assertIn("(放弃,", html)  # 11-11：正文括注不再带等级

    def test_repeated_word_positions_are_distinct(self) -> None:
        first = self._ann(1, "fish", "鱼")
        second = self._ann(3, "fish", "鱼")
        self._write([first, second])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertEqual(html.count(">fish</span>"), 2)
        self.assertIn("第 1 条", "第 1 条")  # 占位，避免误改
        # §22.4：标注节点靠合法 id 区分，data-note 已移到包外映射
        ids = [
            line.split('id="', 1)[1].split('"', 1)[0]
            for line in html.split('<span class="vocab')
            if 'id="vref-' in line
        ]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_unknown_token_id_rejected(self) -> None:
        ann = self._ann(1, "taking", "捕到")
        ann["token_ids"] = ["tk-0000000000"]
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("unknown_token", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_cross_paragraph_tokens_rejected(self) -> None:
        ann = self._ann(1, "taking", "捕到")
        ann["pid"] = self._pid(2)
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("cross_paragraph", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_member_outside_parent_rejected(self) -> None:
        ann = self._ann(2, "skin cancer", "皮肤癌", source="component_entries", unit="phrase")
        ann["components"] = [
            {"token_ids": self._ids(2, "benevolent"), "entry_ids": ["benevolent|托福|adj."]},
            {"token_ids": self._ids(2, "skin"), "entry_ids": ["skin|四级+雅思+牛津3000|n."]},
        ]
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("member_outside_parent", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_member_overlap_rejected(self) -> None:
        ann = self._ann(2, "skin cancer", "皮肤癌", source="component_entries", unit="phrase")
        ids = self._ids(2, "skin")
        ann["components"] = [
            {"token_ids": ids, "entry_ids": ["skin|四级+雅思+牛津3000|n."]},
            {"token_ids": ids, "entry_ids": ["skin|四级+雅思+牛津3000|n."]},
        ]
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("member_overlap", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_cross_inline_node_skipped(self) -> None:
        ann = self._ann(
            4, "taking", "拿到", source="exact_entry", entries=["take|四级+牛津3000|vt."]
        )
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["release"]["delivery_status"], "no_annotations")
        self.assertGreaterEqual(report["release"]["validation"]["skipped_unsupported"], 1)
        self.assertEqual(report["output"]["path"], "")

    # -- §19.5 错误不复活 --------------------------------------------

    def test_invalid_not_resurrected_on_rerun(self) -> None:
        ann = self._ann(
            1,
            "taking",
            "捕到",
            source="exact_entry",
            entries=["sail|四级+雅思+牛津3000|v."],
        )
        self._write([ann])
        code, first = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        bad_first = first["release"]["validation"]["invalid"]
        code, second = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(second["release"]["validation"]["invalid"], bad_first)
        self.assertEqual(second["output"]["path"], "")
        self.assertGreaterEqual(
            self._codes(second).get("entry_form_mismatch", 0), 1
        )

    def test_review_pass_cannot_override_hard_check(self) -> None:
        ann = self._ann(
            1,
            "taking",
            "捕到",
            source="exact_entry",
            entries=["sail|四级+雅思+牛津3000|v."],
        )
        self._write([ann])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertGreaterEqual(self._codes(report).get("entry_form_mismatch", 0), 1)
        self.assertEqual(report["output"]["path"], "")

    def test_duplicate_annotation_id_rejected(self) -> None:
        a = self._ann(1, "fish", "鱼", annotation_id="a-dup")
        b = self._ann(3, "fish", "鱼", annotation_id="a-dup")
        self._write([a, b])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(report["release"]["delivery_status"], "partial")
        self.assertFalse(report["release"]["publishable"])
        self.assertGreaterEqual(
            self._codes(report).get("duplicate_annotation_id", 0), 1
        )

    def test_allow_partial_drops_invalid_and_renumbers(self) -> None:
        good = self._ann(
            3, "mast", "桅杆", source="exact_entry", entries=["mast|六级+托福|n."]
        )
        bad = self._ann(
            1,
            "taking",
            "捕到",
            source="exact_entry",
            entries=["sail|四级+雅思+牛津3000|v."],
        )
        self._write([bad, good])
        code, report = self._apply("--allow-partial")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(report["release"]["delivery_status"], "partial")
        self.assertFalse(report["release"]["publishable"])
        self.assertTrue(report["release"]["omitted_items"])
        html = self._html(report)
        self.assertEqual(report["annotation"]["total_matches"], 1)
        self.assertNotIn("[2]", html)  # 丢弃无效项后重新编号
        self.assertEqual(report["release"]["validation"]["invalid"], 1)

    # -- §19.4 / §19.6 读者可见结果 ----------------------------------

    def test_merged_unit_member_lines(self) -> None:
        ann = self._ann(
            2,
            "benevolent skin cancer",
            "不凶险的皮肤癌",
            source="component_entries",
            unit="phrase",
            components=[
                self._comp(2, "benevolent", "benevolent|托福|adj."),
                self._comp(2, "skin", "skin|四级+雅思+牛津3000|n."),
                self._comp(2, "cancer", "cancer|四级+雅思+牛津3000|n."),
            ],
        )
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK, report.get("validation", {}).get("errors"))
        html = self._html(report)
        self.assertIn("(不凶险的皮肤癌,<a", html)
        self.assertIn("benevolent", html)
        self.assertIn("托福", html)
        self.assertIn("词表原义：慈善的；善心的", html)
        self.assertIn("皮肤", html)
        self.assertNotIn("整体未收录该组合", html)
        self.assertNotIn("正文不显示整体等级", html)
        self.assertTrue(report["validation"]["checks"]["note_no_impl_notes"])
        self.assertTrue(report["validation"]["checks"]["note_forms"])

    def test_whole_entry_gloss_not_truncated(self) -> None:
        ann = self._ann(
            3, "mast", "桅杆", source="exact_entry", entries=["mast|六级+托福|n."]
        )
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK)
        html = self._html(report)
        self.assertIn("词表完整释义：桅杆;杆", html)
        self.assertNotIn("…", html)  # 不做定长截断

    def test_text_integrity_and_all_checks(self) -> None:
        ann = self._ann(
            3, "mast", "桅杆", source="exact_entry", entries=["mast|六级+托福|n."]
        )
        self._write([ann])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["checks"]["text_integrity"])
        self.assertTrue(report["validation"]["ok"], report["validation"]["errors"])

    # -- 绑定 --------------------------------------------------------

    def test_config_hash_mismatch_rejected(self) -> None:
        ann = self._ann(
            3, "mast", "桅杆", source="exact_entry", entries=["mast|六级+托福|n."]
        )
        self._write([ann], config=False)
        code, _ = self._apply()
        self.assertEqual(code, 2)

    def test_tokenizer_mismatch_rejected(self) -> None:
        ann = self._ann(
            3, "mast", "桅杆", source="exact_entry", entries=["mast|六级+托福|n."]
        )
        self._write([ann], tokenizer="v0")
        code, _ = self._apply()
        self.assertEqual(code, 2)

    def test_protocol_mismatch_rejected(self) -> None:
        ann = self._ann(3, "mast", "桅杆")
        self._write([ann], schema_ok=False)
        code, _ = self._apply()
        self.assertEqual(code, 2)

    def test_para_notes_sit_after_their_paragraph(self) -> None:
        """11-11 读者规格：词条块就近放自然段后，章末只留隐藏数据层。"""

        from lxml import etree

        self._write([
            self._ann(0, "skiff", "小渔船"),
            self._ann(4, "fish", "鱼"),
        ])
        code, report = self._apply()
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["ok"])

        epub = next(self.out.glob("*.epub"))
        with zipfile.ZipFile(epub) as z:
            root = etree.fromstring(z.read(DOC))
        body = next(e for e in root.iter() if etree.QName(e).localname == "body")
        children = [e for e in body if isinstance(e.tag, str)]

        def cls(el):
            return (el.get("class") or "").split()

        blocks = [e for e in children if "vocab-para-notes" in cls(e)]
        archives = [e for e in children if "vocab-chapter-notes" in cls(e)]
        # 5 个短段：前 4 段合并成一组、第 5 段一组，两组都有词条。
        self.assertEqual(len(blocks), 2)
        self.assertTrue(archives)
        self.assertTrue(all("vocab-archive" in cls(e) for e in archives))
        for block in blocks:
            prev = block.getprevious()
            self.assertIsNotNone(prev)
            self.assertTrue(
                etree.QName(prev).localname == "p"
                or "vocab-translation" in cls(prev),
                "段后词条块必须紧跟自然段（或该段译文），不能退回章末",
            )
        last_block = max(
            i for i, e in enumerate(children) if "vocab-para-notes" in cls(e)
        )
        first_archive = min(
            i for i, e in enumerate(children) if "vocab-chapter-notes" in cls(e)
        )
        self.assertLess(last_block, first_archive)

        ids = [e.get("id") for e in root.iter() if e.get("id")]
        self.assertEqual(len(ids), len(set(ids)), "段后克隆不得产生重复 id")
        idset = set(ids)
        links = [e.get("href") for e in root.iter() if "vocab-link" in cls(e)]
        self.assertEqual(len(links), 2)
        self.assertTrue(all(h.startswith("#vpara-") and h[1:] in idset for h in links))
        items = [e for e in root.iter() if "vocab-para-item" in cls(e)]
        self.assertEqual(len(items), 2)
        for item in items:
            for sub in item.iter():
                if sub is item:
                    continue
                self.assertNotIn("id", sub.attrib)
                self.assertFalse(any(k.startswith("data-") for k in sub.attrib))


class ParagraphContainerTests(unittest.TestCase):
    """真实书籍回归：段落容器不能只认 <p>/<h*>。

    《小王子》（Calibre 转换）用空 <p class="calibre1"/> 做段间距，
    正文全在 <div class="bs"> 里；只认 <p> 会把整本 17,407 词的书
    判成「没有可用段落」。同时普通 EPUB 的 <div> 常包着多个 <p>，
    外层 div 必须让位，否则同一段文字会被切两次。
    """

    XHTML = '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>{body}</body></html>'

    def _paragraphs(self, body: str, id_map=None):
        from scripts.units import document_paragraphs

        raw = self.XHTML.format(body=body).encode("utf-8")
        _, paragraphs = document_paragraphs(raw, "book.xhtml", 1, id_map or {}, "sha-test")
        return paragraphs

    def test_calibre_div_paragraphs_not_empty_p(self) -> None:
        paragraphs = self._paragraphs(
            '<div class="bs"><p class="calibre1"/></div>'
            '<div class="bs"><span><span class="ts3">So I lived my life alone.</span></span></div>'
            '<div class="bs1"><span> </span></div>'
            '<div class="bs" id="c2"><span>Chapter II</span></div>'
        )
        self.assertEqual(
            [" ".join(p.text.split()) for p in paragraphs],
            ["So I lived my life alone.", "Chapter II"],
        )
        self.assertTrue(all(len(p.tokens) > 0 for p in paragraphs))

    def test_outer_div_does_not_duplicate_inner_p(self) -> None:
        paragraphs = self._paragraphs(
            '<div class="wrap"><p>One paragraph.</p><p>Another.</p></div>'
        )
        self.assertEqual(
            [" ".join(p.text.split()) for p in paragraphs],
            ["One paragraph.", "Another."],
        )

    def test_chapter_fragment_on_div_switches_chapter(self) -> None:
        paragraphs = self._paragraphs(
            '<div id="a"><span>Chapter One text.</span></div>'
            '<div id="c2"><span>Chapter Two text.</span></div>',
            id_map={"c2": 2},
        )
        self.assertEqual([p.chapter for p in paragraphs], [1, 2])


if __name__ == "__main__":
    unittest.main()

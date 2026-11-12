"""集成测试（设计文档 §11.2 EPUB 回归验收 + §15.3 测试门槛）。

覆盖：三本 fixture 全流程、结构保真、未改动资源字节一致、标注往返、
重复处理拒绝、源文件 SHA-256 不变、既有输出不覆盖、CLI 退出码。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._bootstrap import FIXTURES, ROOT, load_binding_for, load_sample_lexicon
from scripts.annotate_epub import AlreadyAnnotatedError, choose_output_path
from scripts.convert_source import NotSupportedInStage1, convert_source
from scripts.epub_common import EpubError, local_name, open_epub, parse_xml
from scripts.inspect_book import sha256_file
from scripts.run_pipeline import EXIT_ALREADY_ANNOTATED, EXIT_INPUT, EXIT_OK, main

FIXTURE_NAMES = ("epub2_basic", "epub2_ncx", "epub3_complex")
LEXICON = FIXTURES / "lexicon_sample.csv"


def run_cli(book: Path, out_dir: Path, *extra: str) -> int:
    argv = [
        "--book",
        str(book),
        "--lexicon",
        str(LEXICON),
        "--out",
        str(out_dir),
        "--report",
        str(out_dir / "report.json"),
        "--no-epubcheck",
        "--presentation",
        "footnote",
        "--quiet",
        *extra,
    ]
    return main(argv)


class FixturePresenceTests(unittest.TestCase):
    def test_all_fixtures_exist(self) -> None:
        for name in FIXTURE_NAMES:
            path = FIXTURES / f"{name}.epub"
            self.assertTrue(path.is_file(), f"缺少 fixture：{path}")
            self.assertGreater(path.stat().st_size, 500)

    def test_fixtures_are_valid_epub(self) -> None:
        for name in FIXTURE_NAMES:
            archive, package = open_epub(FIXTURES / f"{name}.epub")
            try:
                self.assertTrue(package.spine, name)
                self.assertTrue(package.items, name)
                self.assertEqual(archive.names[0], "mimetype", name)
            finally:
                archive.close()


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vocab-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, name: str, *extra: str) -> tuple[int, Path, dict]:
        book = FIXTURES / f"{name}.epub"
        code = run_cli(book, self.tmp, *extra)
        report = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        out = Path(report["output"]["path"])
        return code, out, report

    def test_all_fixtures_pass_validation(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                code, out, report = self._run(name)
                self.assertEqual(code, EXIT_OK, report["validation"]["errors"])
                self.assertTrue(report["validation"]["ok"])
                self.assertTrue(out.is_file())
                self.assertGreater(report["annotation"]["total_matches"], 0)
                for check, value in report["validation"]["checks"].items():
                    self.assertIsNot(value, False, f"{name} 校验项失败：{check}")

    def test_input_sha_unchanged(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                book = FIXTURES / f"{name}.epub"
                before = sha256_file(book)
                code, _, report = self._run(name)
                self.assertEqual(code, EXIT_OK)
                self.assertEqual(before, sha256_file(book))
                self.assertEqual(report["input_sha256_before"], report["input_sha256_after"])
                self.assertEqual(report["precheck"]["sha256"], before)

    def test_spine_and_navigation_preserved(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                code, out, _ = self._run(name)
                self.assertEqual(code, EXIT_OK)
                src, src_pkg = open_epub(FIXTURES / f"{name}.epub")
                dst, dst_pkg = open_epub(out)
                try:
                    src_paths = [s.item.path for s in src_pkg.spine]
                    dst_paths = [s.item.path for s in dst_pkg.spine][: len(src_paths)]
                    self.assertEqual(src_paths, dst_paths)
                    if src_pkg.ncx_item:
                        self.assertEqual(
                            src.read(src_pkg.ncx_item.path),
                            dst.read(dst_pkg.ncx_item.path),
                            "NCX 必须保持字节一致",
                        )
                    if src_pkg.nav_item:
                        self.assertEqual(
                            src.read(src_pkg.nav_item.path),
                            dst.read(dst_pkg.nav_item.path),
                            "nav 必须保持字节一致",
                        )
                finally:
                    src.close()
                    dst.close()

    def test_binary_resources_byte_identical(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                code, out, _ = self._run(name)
                self.assertEqual(code, EXIT_OK)
                with zipfile.ZipFile(FIXTURES / f"{name}.epub") as src, zipfile.ZipFile(out) as dst:
                    for entry in src.namelist():
                        if entry in {"mimetype"} or entry.endswith((".xhtml", ".html", ".opf")):
                            continue
                        self.assertEqual(
                            src.read(entry), dst.read(entry), f"{name}:{entry} 被改写"
                        )

    def test_mimetype_is_first_and_stored(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                code, out, _ = self._run(name)
                self.assertEqual(code, EXIT_OK)
                with zipfile.ZipFile(out) as zf:
                    info = zf.infolist()[0]
                    self.assertEqual(info.filename, "mimetype")
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(zf.read("mimetype"), b"application/epub+zip")

    def test_annotations_roundtrip(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                code, out, _ = self._run(name)
                self.assertEqual(code, EXIT_OK)
                archive, package = open_epub(out)
                try:
                    # §22.4：内部键已移到包外映射，按稳定 id 与产物节点核对
                    record = load_binding_for(out)["attrs"]
                    seen = 0
                    for spine_item in package.spine:
                        path = spine_item.item.path
                        tree = parse_xml(archive.read(path), path)
                        root = tree.getroot()
                        ids = {el.get("id") for el in root.iter() if el.get("id")}
                        for anchor in root.iter():
                            if local_name(anchor) != "a":
                                continue
                            if "vocab" not in (anchor.get("class") or "").split():
                                continue
                            seen += 1
                            target = record.get(anchor.get("id"), {}).get(
                                "data-vocab-id"
                            )
                            self.assertTrue(target, "映射缺少 data-vocab-id")
                            self.assertIn(target, ids)
                            self.assertEqual(anchor.get("href"), "#" + target)
                    self.assertGreater(seen, 0)
                finally:
                    archive.close()

    def test_text_integrity_check_is_reported(self) -> None:
        code, _, report = self._run("epub3_complex")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["validation"]["checks"]["text_integrity"])

    def test_body_text_unchanged(self) -> None:
        """去掉词注块后，输出正文与源正文逐字一致。"""

        code, out, _ = self._run("epub3_complex")
        self.assertEqual(code, EXIT_OK)

        def body_text(root) -> str:
            chunks: list[str] = []

            def walk(el) -> None:
                if set((el.get("class") or "").split()) & {"vocab-notes"}:
                    return
                if el.text:
                    chunks.append(el.text)
                for child in el:
                    if isinstance(child.tag, str):
                        walk(child)
                        if child.tail:
                            chunks.append(child.tail)

            for el in root.iter():
                if local_name(el) == "body":
                    walk(el)
                    break
            return " ".join("".join(chunks).split())

        with zipfile.ZipFile(FIXTURES / "epub3_complex.epub") as src, zipfile.ZipFile(out) as dst:
            for name in ("OPS/chapter1.xhtml", "OPS/chapter2.xhtml"):
                before = body_text(parse_xml(src.read(name), name).getroot())
                after = body_text(parse_xml(dst.read(name), name).getroot())
                self.assertEqual(before, after, f"{name} 正文被改动")

    def test_rerun_on_output_is_refused(self) -> None:
        code, out, _ = self._run("epub3_complex")
        self.assertEqual(code, EXIT_OK)
        with self.assertRaises(AlreadyAnnotatedError):
            from scripts.annotate_epub import AnnotationOptions, annotate_epub

            annotate_epub(out, load_sample_lexicon(), AnnotationOptions(), out_dir=self.tmp)

    def test_cli_returns_3_for_annotated_input(self) -> None:
        code, out, _ = self._run("epub3_complex")
        self.assertEqual(code, EXIT_OK)
        second = run_cli(out, self.tmp)
        self.assertEqual(second, EXIT_ALREADY_ANNOTATED)

    def test_existing_output_not_overwritten(self) -> None:
        book = FIXTURES / "epub3_complex.epub"
        first = choose_output_path(book, self.tmp)
        first.write_bytes(b"placeholder")
        second = choose_output_path(book, self.tmp)
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith("_2.epub"))

    def test_glossary_is_added_to_manifest_and_spine(self) -> None:
        code, out, report = self._run("epub3_complex", "--glossary")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(report["output"]["glossary"])
        archive, package = open_epub(out)
        try:
            paths = [s.item.path for s in package.spine]
            self.assertIn(report["output"]["glossary"], paths)
            self.assertEqual(paths[-1], report["output"]["glossary"])
            css = report["output"]["css"]
            self.assertIsNotNone(package.item_by_path(css))
        finally:
            archive.close()

    def test_repeat_runs_are_stable(self) -> None:
        book = FIXTURES / "epub3_complex.epub"
        code1 = run_cli(book, self.tmp)
        report1 = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        other = self.tmp / "second"
        other.mkdir()
        code2 = run_cli(book, other)
        report2 = json.loads((other / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(code1, EXIT_OK)
        self.assertEqual(code2, EXIT_OK)
        self.assertEqual(
            report1["annotation"]["total_matches"], report2["annotation"]["total_matches"]
        )
        self.assertEqual(
            report1["annotation"]["per_entry"].keys(), report2["annotation"]["per_entry"].keys()
        )
        with zipfile.ZipFile(Path(report1["output"]["path"])) as z1, zipfile.ZipFile(
            Path(report2["output"]["path"])
        ) as z2:
            self.assertEqual(
                z1.read("OPS/chapter1.xhtml"), z2.read("OPS/chapter1.xhtml")
            )


class SkipRuleTests(unittest.TestCase):
    """可见性规则的实际效果（不靠单元测试假设，直接看输出）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="vocab-skip-"))
        code = run_cli(FIXTURES / "epub3_complex.epub", cls.tmp)
        assert code == EXIT_OK, code
        cls.report = json.loads((cls.tmp / "report.json").read_text(encoding="utf-8"))
        cls.output = Path(cls.report["output"]["path"])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_hidden_text_not_annotated(self) -> None:
        with zipfile.ZipFile(self.output) as zf:
            data = zf.read("OPS/chapter1.xhtml").decode("utf-8")
        self.assertIn("Hidden art and study text must never be marked.", data)
        self.assertNotIn('>Hidden <a', data)

    def test_code_block_not_annotated(self) -> None:
        with zipfile.ZipFile(self.output) as zf:
            data = zf.read("OPS/chapter1.xhtml").decode("utf-8")
        self.assertIn("<code>int art = 1; // study</code>", data)

    def test_url_not_annotated(self) -> None:
        with zipfile.ZipFile(self.output) as zf:
            data = zf.read("OPS/chapter1.xhtml").decode("utf-8")
        self.assertIn("https://example.com/art-study", data)

    def test_existing_link_text_not_annotated(self) -> None:
        with zipfile.ZipFile(self.output) as zf:
            data = zf.read("OPS/chapter1.xhtml").decode("utf-8")
        self.assertIn('<a href="chapter2.xhtml#end">the end</a>', data)

    def test_cross_node_phrase_reported(self) -> None:
        chapter = self.report["annotation"]["chapters"][0]
        self.assertEqual(chapter["skipped_cross_node"], 1)
        self.assertNotIn("give up", chapter["per_entry"])

    def test_inside_link_skips_counted(self) -> None:
        chapter = self.report["annotation"]["chapters"][0]
        self.assertGreaterEqual(chapter["skipped_inside_link"], 1)

    def test_phrase_matched_when_contiguous(self) -> None:
        self.assertIn("look forward to", self.report["annotation"]["per_entry"])


class UnsupportedInputTests(unittest.TestCase):
    def test_markdown_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-md-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        book = tmp / "book.md"
        book.write_text("# hi\n", encoding="utf-8")
        with self.assertRaises(EpubError) as ctx:
            from scripts.inspect_book import inspect_book

            inspect_book(book)
        self.assertIn("阶段 3", str(ctx.exception))

    def test_convert_source_raises_for_all_stages(self) -> None:
        for suffix in (".md", ".txt", ".pdf"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(NotSupportedInStage1):
                    convert_source(f"dummy{suffix}")

    def test_cli_returns_2_for_unsupported(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-bad-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        book = tmp / "book.txt"
        book.write_text("hello\n", encoding="utf-8")
        self.assertEqual(run_cli(book, tmp), EXIT_INPUT)

    def test_missing_book_returns_2(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-missing-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(run_cli(tmp / "nope.epub", tmp), EXIT_INPUT)

    def test_corrupt_epub_returns_2(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-corrupt-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        book = tmp / "bad.epub"
        book.write_bytes(b"not a zip at all")
        self.assertEqual(run_cli(book, tmp), EXIT_INPUT)


class ReportSchemaTests(unittest.TestCase):
    def test_report_matches_schema_fields(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-report-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(run_cli(FIXTURES / "epub3_complex.epub", tmp, "--glossary"), EXIT_OK)
        report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["skill"], "vocab-reading-skill")
        self.assertEqual(report["stage"], 4)
        self.assertEqual(report["matching_mode"], "no-model+harness-semantic-units")
        for key in (
            "timestamp",
            "lexicon",
            "precheck",
            "annotation",
            "validation",
            "output",
        ):
            self.assertIn(key, report)
        for key in ("entries", "heads", "phrases", "issues", "encoding"):
            self.assertIn(key, report["lexicon"])
        self.assertIn("chapters", report["annotation"])
        self.assertIn("fidelity", report["validation"])
        self.assertEqual(
            report["annotation"]["total_matches"],
            sum(c["matches"] for c in report["annotation"]["chapters"]),
        )


class EpubCheckInvocationTests(unittest.TestCase):
    """EPUBCheck 5.x 的 CLI 参数与 JSON 解析（环境无 Java 时用 mock 覆盖）。"""

    def test_command_uses_json_not_format(self) -> None:
        import scripts.validate_epub as ve
        from unittest.mock import patch

        captured: dict = {}

        def fake_run(command, **_kwargs):
            captured["command"] = command
            if "--json" in command:
                target = Path(command[command.index("--json") + 1])
                target.write_text(
                    json.dumps({
                        "checker": {"nError": 2, "nWarning": 1, "nFatal": 0, "nUsage": 0},
                        "messages": [{
                            "ID": "RSC-005", "severity": "ERROR",
                            "message": "attribute not allowed",
                            "locations": [{"path": "chapter1.xhtml"}],
                        }],
                    }),
                    encoding="utf-8",
                )
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        found = {"available": True, "kind": "jar", "path": "epubcheck.jar", "java": "java"}
        with patch.object(ve, "find_epubcheck", return_value=found), \
             patch.object(ve.subprocess, "run", side_effect=fake_run):
            info = ve._run_epubcheck("book.epub")

        self.assertIn("--json", captured["command"])
        self.assertNotIn("--format", captured["command"])
        self.assertTrue(info["ran"])
        self.assertFalse(info["ok"])
        self.assertEqual(info["counts"]["nError"], 2)
        self.assertEqual(info["messages"][0]["id"], "RSC-005")
        self.assertEqual(info["messages"][0]["location"], "chapter1.xhtml")

    def test_find_java_falls_back_to_java_home(self) -> None:
        import os
        import scripts.validate_epub as ve
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp(prefix="vocab-java-home-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        exe = "java.exe" if os.name == "nt" else "java"
        (tmp / "bin").mkdir()
        (tmp / "bin" / exe).write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"JAVA_HOME": str(tmp)}, clear=False), \
             patch.object(ve.shutil, "which", return_value=None):
            self.assertEqual(ve._find_java(), str(tmp / "bin" / exe))

    def test_find_java_discovers_portable_tools_dir(self) -> None:
        import os
        import scripts.validate_epub as ve
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp(prefix="vocab-portable-java-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        exe = "java.exe" if os.name == "nt" else "java"
        fake = tmp / "tools" / "java" / "jre21" / "bin" / exe
        fake.parent.mkdir(parents=True)
        fake.write_text("", encoding="utf-8")
        clean_env = {
            key: value for key, value in os.environ.items()
            if key not in ("JAVA_HOME", "EPUBCHECK_JAVA")
        }
        with patch.dict(os.environ, clean_env, clear=True), \
             patch.object(ve.shutil, "which", return_value=None), \
             patch.object(ve.glob, "glob", return_value=[str(fake)]):
            self.assertEqual(ve._find_java(), str(fake))

    def test_epubcheck_label_shows_counts(self) -> None:
        from scripts.run_pipeline import _epubcheck_label

        self.assertEqual(_epubcheck_label(None), "未运行")
        self.assertEqual(_epubcheck_label({"ran": True, "ok": True}), "通过")
        self.assertIn("nError=3", _epubcheck_label(
            {"ran": True, "ok": False, "counts": {"nError": 3, "nWarning": 0}}))


class EmitDirSafetyTests(unittest.TestCase):
    """安全红线：导出口录只清自己的产物，绝不递归删用户目录。"""

    def test_clears_known_artifacts_only(self) -> None:
        from scripts.run_pipeline import _clear_emit_dir

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "units"
            (target / "review_requests").mkdir(parents=True)
            (target / "scope.json").write_text("{}", encoding="utf-8")
            (target / "windows-0001.json").write_text("{}", encoding="utf-8")
            (target / "ann-0001.json").write_text("{}", encoding="utf-8")
            (target / "review-0001.json").write_text("{}", encoding="utf-8")
            (target / "review_requests" / "review-request-0001.json").write_text(
                "{}", encoding="utf-8"
            )
            _clear_emit_dir(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_refuses_when_foreign_files_present(self) -> None:
        from scripts.run_pipeline import _clear_emit_dir

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "cases"
            target.mkdir()
            keep = target / "我的笔记.md"
            keep.write_text("别删我", encoding="utf-8")
            with self.assertRaises(EpubError):
                _clear_emit_dir(target)
            self.assertTrue(keep.exists(), "非产物文件被误删")

    def test_creates_missing_dir(self) -> None:
        from scripts.run_pipeline import _clear_emit_dir

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "fresh"
            _clear_emit_dir(target)
            self.assertTrue(target.is_dir())


class EmitEmptyScopeTests(unittest.TestCase):
    """导不出窗口不算成功：不能返回 0 让宿主拿空目录往下走。"""

    def test_no_windows_is_blocked(self) -> None:
        from scripts.run_pipeline import EXIT_BLOCKED, main

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "units"
            code = main([
                "--book", str(FIXTURES / "epub2_basic.epub"),
                "--lexicon", str(FIXTURES / "lexicon_sample.csv"),
                "--emit-annotations", str(out),
                "--paragraph-start", "99999",
                "--reader-mode", "advanced", "--require-reader-mode",
                "--quiet",
            ])
            self.assertEqual(code, EXIT_BLOCKED)
            self.assertFalse(out.exists(), "没有窗口时不应留下空目录")


if __name__ == "__main__":
    unittest.main()

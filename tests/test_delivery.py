"""§P1-2 回归：HTML 与 EPUB 同一条交付闭环。

要求：章节范围按原书目录的逻辑章节；不依赖示例配置与固定文件名；
两种格式分别报告成功状态；不覆盖既有阅读页。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
BOOK = FIXTURES / "epub2_basic.epub"

from scripts.build_reader_html import auto_sections, build_single  # noqa: E402


class AutoSectionTests(unittest.TestCase):
    def test_chapters_follow_the_book_toc(self) -> None:
        sections = auto_sections(BOOK)
        self.assertEqual([s["label"] for s in sections], ["One", "Two"])

    def test_range_selects_logical_chapters_not_paragraphs(self) -> None:
        sections = auto_sections(BOOK, "1")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["label"], "One")

    def test_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            auto_sections(BOOK, "9")


class BuildSingleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vocab-html-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_only_requested_chapter_is_rendered(self) -> None:
        out = self.tmp / "one.html"
        info = build_single(BOOK, out, chapters="1")
        self.assertTrue(info["ok"])
        self.assertEqual(info["sections"], 1)
        html = out.read_text(encoding="utf-8")
        self.assertEqual(html.count('class="chapter"'), 1)

    def test_existing_reader_is_not_overwritten(self) -> None:
        out = self.tmp / "taken.html"
        out.write_text("<html>old</html>", encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_single(BOOK, out, chapters="1")
        self.assertEqual(out.read_text(encoding="utf-8"), "<html>old</html>")

    def test_force_allows_overwrite(self) -> None:
        out = self.tmp / "taken.html"
        out.write_text("<html>old</html>", encoding="utf-8")
        build_single(BOOK, out, chapters="1", force=True)
        self.assertIn("class=\"chapter\"", out.read_text(encoding="utf-8"))


class PipelineDeliverableTests(unittest.TestCase):
    def _run(self, tmp: Path, *extra: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_pipeline",
                "--book",
                str(BOOK),
                "--presentation",
                "inline",
                "--out",
                str(tmp),
                "--report",
                str(tmp / "r.json"),
                "--no-epubcheck",
                *extra,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=180,
        )

    def test_html_is_recorded_next_to_epub(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-dual-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        result = self._run(tmp, "--html", str(tmp / "pages"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
        found = report["deliverables"]
        self.assertTrue(found["epub"]["ok"])
        self.assertTrue(found["html"]["ok"])
        self.assertTrue(Path(found["html"]["path"]).is_file())
        self.assertEqual(found["html"]["sections"], 2)

    def test_html_failure_is_reported_and_epub_still_delivered(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-dual-fail-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        taken = tmp / "taken.html"
        taken.write_text("<html>old</html>", encoding="utf-8")
        result = self._run(tmp, "--html", str(taken))
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("EPUB 已交付", result.stdout)
        report = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
        self.assertTrue(report["deliverables"]["epub"]["ok"])
        self.assertFalse(report["deliverables"]["html"]["ok"])
        self.assertEqual(taken.read_text(encoding="utf-8"), "<html>old</html>")


class ReleasePackTests(unittest.TestCase):
    """§P1-1：发布清单与私有路径扫描必须一直干净。"""

    def test_manifest_has_runtime_files_and_smoke_book(self) -> None:
        from scripts.pack_release import collect

        targets = {rel for _, rel in collect()}
        for required in (
            "SKILL.md",
            "INSTALL.md",
            "assets/lexicon.vault",
            "scripts/run_pipeline.py",
            "scripts/doctor.py",
            "smoke/book.epub",
        ):
            self.assertIn(required, targets)

    def test_no_private_paths_or_cache(self) -> None:
        from scripts.pack_release import check_cache, check_private_paths, collect

        entries = collect()
        self.assertEqual(check_private_paths(), [])
        self.assertEqual(check_cache(entries), [])

    def test_examples_and_tests_are_not_shipped(self) -> None:
        from scripts.pack_release import collect

        for _, rel in collect():
            self.assertFalse(rel.startswith("examples/"), rel)
            self.assertFalse(rel.startswith("tests/"), rel)


if __name__ == "__main__":
    unittest.main()

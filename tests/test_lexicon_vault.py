"""§23 回归：词表随包封装、零配置、不交付空书、终端人话化。

用户口径（2026-11-06）：只防君子不防小人；词表随包、零配置；
稍微设个门槛即可；本意是让大家更好地学习（产物完整词条必须保留）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
ASSET = ROOT / "assets" / "lexicon.vault"
BOOK = FIXTURES / "epub2_basic.epub"
SMALL_CSV = FIXTURES / "lexicon_sample.csv"

from scripts.console import configure_stdio  # noqa: E402
from scripts.lexicon_vault import (  # noqa: E402
    MAGIC,
    SCHEMA,
    VaultError,
    is_vault,
    pack_bytes,
    pack_file,
    unpack_bytes,
)
from scripts.load_lexicon import (  # noqa: E402
    LexiconError,
    lexicon_content_sha256,
    load_lexicon,
)
from scripts.run_pipeline import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    _print_summary,
)


def _tamper(text: str) -> str:
    """改动 base64 区一个字符（不动 magic 行），用于完整性校验负向用例。"""

    lines = text.splitlines()
    first = lines[1][0]
    lines[1] = ("B" if first != "B" else "A") + lines[1][1:]
    return "\n".join(lines) + "\n"


class VaultTests(unittest.TestCase):
    def test_roundtrip_bytes_identical(self) -> None:
        raw = "单词,中文释义\nstudy,学习\n".encode("utf-8-sig")
        self.assertEqual(unpack_bytes(pack_bytes(raw).encode("ascii")), raw)

    def test_container_is_plain_text_and_hides_headwords(self) -> None:
        raw = b"word,gloss\natmosphere,air\n"
        text = pack_bytes(raw)
        data = text.encode("ascii")
        self.assertTrue(text.startswith(MAGIC.decode("ascii")))
        self.assertTrue(is_vault(data), "容器头必须能被嗅探识别")
        self.assertTrue(SCHEMA.endswith("/v1"))
        self.assertNotIn(b"atmosphere", data, "容器不得出现明文词头")
        data.decode("ascii")  # 纯文本，不是二进制资源

    def test_tamper_is_detected(self) -> None:
        with self.assertRaises(VaultError):
            unpack_bytes(_tamper(pack_bytes(b"a,b\nstudy,learn\n")).encode("ascii"))

    def test_truncated_container_is_detected(self) -> None:
        # 必须足够大，容器才会分成多行；否则 lines[:2] 根本没截断。
        raw = b"a,b\n" + b"study,learn\n" * 200
        lines = pack_bytes(raw).splitlines()
        self.assertGreater(len(lines), 2, "用例前提：容器应有多行")
        with self.assertRaises(VaultError):
            unpack_bytes("\n".join(lines[:2]).encode("ascii"))

    def test_bad_magic_is_detected(self) -> None:
        with self.assertRaises(VaultError):
            unpack_bytes(b"NOTAVAULT\nAAAA\n")

    def test_pack_file_writes_plain_container(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source.csv"
            src.write_bytes(SMALL_CSV.read_bytes())
            target, size = pack_file(src, root / "assets" / "lex.vault")
            self.assertTrue(target.is_file())
            self.assertTrue(is_vault(target.read_bytes()))
            self.assertFalse(is_vault(SMALL_CSV.read_bytes()))
            self.assertEqual(unpack_bytes(target.read_bytes()), SMALL_CSV.read_bytes())
            self.assertEqual(size, SMALL_CSV.stat().st_size)

    def test_content_sha_is_stable_across_repacks(self) -> None:
        """容器随机 nonce 会改文件字节，v6 绑定必须按内容而非容器字节。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, _ = pack_file(SMALL_CSV, root / "a.vault")
            b, _ = pack_file(SMALL_CSV, root / "b.vault")
            self.assertNotEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(lexicon_content_sha256(a), lexicon_content_sha256(b))
            self.assertEqual(
                lexicon_content_sha256(a), lexicon_content_sha256(SMALL_CSV),
                "自备明文 CSV 与随包容器应视为同一词库",
            )


class BundledLexiconTests(unittest.TestCase):
    def test_bundled_vault_exists_and_loads(self) -> None:
        self.assertTrue(ASSET.is_file(), "随包词库缺失，零配置无法运行")
        lex = load_lexicon(ASSET)
        self.assertGreater(len(lex), 6000)

    def test_bundled_asset_is_ascii_text_without_plaintext(self) -> None:
        data = ASSET.read_bytes()
        self.assertEqual(data[: len(MAGIC)], MAGIC)
        data.decode("ascii")
        self.assertNotIn(b"atmosphere", data)

    def test_provenance_record_matches_bundled_content(self) -> None:
        """§P1-3：来源记录必须与随包内容一致，防止文档与词库悄悄漂移。"""

        record = json.loads(
            (ROOT / "assets" / "lexicon.source.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["schema"], "vocab-lexicon-source/v1")
        self.assertEqual(record["bundled"]["content_sha256"], lexicon_content_sha256(ASSET))
        lex = load_lexicon(ASSET)
        self.assertEqual(record["content"]["rows"], lex.rows_total)
        self.assertEqual(
            record["content"]["grade_labels"],
            sorted({grade for entry in lex.all_entries for grade in entry.grades}),
        )

    def test_bundled_labels_are_reported_honestly(self) -> None:
        """随包词库只有三级；文档不得宣称含托福/GRE/牛津3000/AWL。"""

        record = json.loads(
            (ROOT / "assets" / "lexicon.source.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("托福", record["content"]["grade_labels"])
        # 按字段名取，不按行号取：frontmatter 增删字段时这条断言不该跟着碎
        from scripts.pack_release import read_frontmatter

        description = read_frontmatter(ROOT / "SKILL.md")["description"]
        self.assertIn("6861", description)
        self.assertNotIn("用随包的四级/六级/雅思/托福", description)

    def test_user_plain_csv_still_supported(self) -> None:
        self.assertGreater(len(load_lexicon(SMALL_CSV)), 0)

    def test_corrupted_vault_raises_lexicon_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.vault"
            path.write_text(_tamper(pack_bytes(b"a,b\nx,y\n")), encoding="ascii")
            with self.assertRaises(LexiconError):
                load_lexicon(path)


class ZeroConfigTests(unittest.TestCase):
    def _run(self, tmp: Path, *extra: str) -> subprocess.CompletedProcess:
        # §P0-2：父子进程统一 UTF-8 契约。子进程自己也会把被重定向的输出切到
        # UTF-8（见 scripts/console.py），这里显式给出环境变量作为双保险；
        # 不设 errors="replace"，编码不一致时必须直接失败而不是被掩盖。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_pipeline",
                "--book",
                str(BOOK),
                "--out",
                str(tmp),
                "--report",
                str(tmp / "r.json"),
                "--no-epubcheck",
                "--quiet",
                *extra,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=180,
        )

    def test_default_without_lexicon_refuses_empty_book(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, EXIT_BLOCKED, proc.stdout + proc.stderr)
            self.assertFalse((tmp / "r.json").exists(), "0 标注不应写成功报告")
            self.assertEqual(list(tmp.glob("*_vocab.epub")), [], "空书应被删除")
            self.assertIn("0 处标注", proc.stderr)
            self.assertNotIn(
                "未找到词库", proc.stderr, "随包词库应已自动加载"
            )

    def test_inline_zero_config_produces_book_from_bundled_lexicon(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            proc = self._run(tmp, "--presentation", "inline")
            self.assertEqual(proc.returncode, EXIT_OK, proc.stdout + proc.stderr)
            report = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
            self.assertGreater(report["annotation"]["total_matches"], 0)
            self.assertEqual(
                Path(report["lexicon"]["path"]).resolve(), ASSET.resolve()
            )
            self.assertIs(
                report["release"]["publishable"], True,
                "直注档也要写 release.publishable，宿主编排契约才有统一入口",
            )

    def test_emit_annotations_zero_config_works(self) -> None:
        """宿主四步链路的起点：不给 --lexicon 也能导出窗口与任务清单。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.run_pipeline",
                    "--book",
                    str(BOOK),
                    "--emit-annotations",
                    str(tmp / "units"),
                    "--no-epubcheck",
                    "--quiet",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(proc.returncode, EXIT_OK, proc.stdout + proc.stderr)
            self.assertTrue((tmp / "units" / "scope.json").is_file())
            self.assertTrue(list((tmp / "units").glob("windows-*.json")))

    def test_user_csv_overrides_bundled_lexicon(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            proc = self._run(
                tmp, "--lexicon", str(SMALL_CSV), "--presentation", "inline"
            )
            self.assertEqual(proc.returncode, EXIT_OK, proc.stdout + proc.stderr)
            report = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
            self.assertEqual(
                Path(report["lexicon"]["path"]).resolve(), SMALL_CSV.resolve()
            )

    def test_full_glossary_fields_are_preserved_for_learning(self) -> None:
        """用户口径：章末完整释义 / 例句 / 音标不得被收紧。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            proc = self._run(tmp, "--undecided", "lexicon")
            self.assertEqual(proc.returncode, EXIT_OK, proc.stdout + proc.stderr)
            report = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
            with zipfile.ZipFile(report["output"]["path"]) as zf:
                text = b"".join(
                    zf.read(name)
                    for name in zf.namelist()
                    if name.lower().endswith((".xhtml", ".html", ".htm"))
                ).decode("utf-8", "replace")
            self.assertIn("词表完整释义", text)


class HumanReadableSummaryTests(unittest.TestCase):
    def test_epubcheck_line_is_reader_friendly_when_publishable(self) -> None:
        report = {
            "output": {"path": "x.epub"},
            "annotation": {
                "total_matches": 3,
                "per_entry": [],
                "density": "chapter",
                "disambiguation": {},
            },
            "validation": {
                "checks": {},
                "ok": True,
                "epubcheck": {
                    "ran": True,
                    "ok": False,
                    "counts": {"nError": 3},
                },
            },
            "compatibility": {
                "publishable": True,
                "issues": {"new": []},
                "delivery_kind": "compatible_delivery",
                "summary": "无新增问题",
            },
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _print_summary(report, quiet=False)
        text = buffer.getvalue()
        self.assertIn("原书遗留 3 处格式瑕疵", text)
        self.assertIn("可交付", text)
        self.assertNotIn("nError", text, "终端摘要不得暴露工程字段")
        self.assertNotIn("compatible_delivery", text, "终端摘要不得暴露内部术语")

    def test_summary_blocks_when_new_issues_exist(self) -> None:
        report = {
            "output": {"path": "x.epub"},
            "annotation": {
                "total_matches": 3,
                "per_entry": [],
                "density": "chapter",
                "disambiguation": {},
            },
            "validation": {
                "checks": {},
                "ok": False,
                "epubcheck": {"ran": True, "ok": False, "counts": {"nError": 4}},
            },
            "compatibility": {
                "publishable": False,
                "issues": {"new": [{"message": "新增非法属性"}]},
                "delivery_kind": "blocked",
                "summary": "有新增规范问题",
            },
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _print_summary(report, quiet=False)
        text = buffer.getvalue()
        self.assertIn("未交付", text)
        self.assertNotIn("可交付", text)
        self.assertIn("报告错误", text)  # 未交付时不淡化官方错误

    def test_no_epubcheck_does_not_claim_no_new_issues(self) -> None:
        report = {
            "output": {"path": "x.epub"},
            "annotation": {
                "total_matches": 3,
                "per_entry": [],
                "density": "chapter",
                "disambiguation": {},
            },
            "validation": {
                "checks": {},
                "ok": True,
                "epubcheck": {"ran": False, "reason": "--no-epubcheck"},
            },
            "compatibility": {
                "publishable": True,
                "external_validation_incomplete": True,
                "issues": {"new": []},
                "delivery_kind": "compatible_unverified",
                "summary": "外部校验未完成",
            },
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _print_summary(report, quiet=False)
        text = buffer.getvalue()
        self.assertIn("官方格式检查未运行", text)
        self.assertNotIn("未引入新的格式问题", text)


class ConsoleEncodingTests(unittest.TestCase):
    """§P0-2：父子进程统一 UTF-8 契约，中文提示不能被编码吃掉。"""

    def _run_raw(self, env: dict) -> subprocess.CompletedProcess:
        tmp = Path(tempfile.mkdtemp(prefix="vocab-enc-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_pipeline",
                "--book",
                str(BOOK),
                "--out",
                str(tmp),
                "--report",
                str(tmp / "r.json"),
                "--no-epubcheck",
            ],
            cwd=str(ROOT),
            capture_output=True,  # 管道：模拟宿主/CI 捕获
            env=env,
            timeout=180,
        )

    def test_captured_output_is_utf8_without_user_env(self) -> None:
        """不预设 PYTHONUTF8 / PYTHONIOENCODING，宿主按 UTF-8 读也应可读。"""

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONUTF8", "PYTHONIOENCODING")
        }
        result = self._run_raw(env)
        self.assertEqual(result.returncode, EXIT_BLOCKED)
        text = result.stdout.decode("utf-8", "strict") + result.stderr.decode(
            "utf-8", "strict"
        )
        self.assertIn("0 处标注", text)
        self.assertIn("未生成成品", text)

    def test_captured_output_is_utf8_even_if_env_forces_cp936(self) -> None:
        """最坏情况：宿主环境显式要求 cp936，也要输出可被 UTF-8 读取的中文。"""

        env = {**os.environ, "PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0"}
        result = self._run_raw(env)
        self.assertEqual(result.returncode, EXIT_BLOCKED)
        text = result.stdout.decode("utf-8", "strict") + result.stderr.decode(
            "utf-8", "strict"
        )
        self.assertIn("0 处标注", text)

    def test_interactive_terminal_encoding_is_left_alone(self) -> None:
        """直连终端时不改写编码，避免在 cp936 控制台写出乱码。"""

        class FakeTerminal(io.StringIO):
            encoding = "cp936"

            def isatty(self) -> bool:
                return True

            def reconfigure(self, **kwargs):  # pragma: no cover - 不应被调用
                raise AssertionError("终端流不应被 reconfigure")

        saved = sys.stdout
        sys.stdout = FakeTerminal()
        try:
            configure_stdio()
        finally:
            sys.stdout = saved


if __name__ == "__main__":
    unittest.main()

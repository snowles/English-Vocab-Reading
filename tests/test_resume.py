"""§20.6 断点续作的自动化回归（`--resume`）。

首版放行标准要求「处理中断可恢复或明确继续步骤」。此前 `--resume` 只有实现、
没有自动化测试（H1 矩阵标 🟡），这里把它补成可回归的三件事：

1. 补齐：缺窗口请求能补回来，且任务清单 `task_id` 不变（不把续作变成新任务）。
2. 超范围清理：书/范围/配置一变，旧窗口请求必须按新范围重建，不留越界分片。
3. 不毁宿主成果：续作只清理窗口请求，宿主的 `ann-*.json` 识别结果必须保留。

只加测试，不改流水线行为。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "tests" / "fixtures" / "epub2_basic.epub"


def _emit(units: Path, *extra: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "scripts.run_pipeline",
        "--book",
        str(BOOK),
        "--emit-annotations",
        str(units),
        "--window-paragraphs",
        "4",
        "--no-epubcheck",
        "--quiet",
        *extra,
    ]
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _scope(units: Path) -> dict:
    return json.loads((units / "scope.json").read_text(encoding="utf-8"))


def _window_names(units: Path) -> list[str]:
    return sorted(p.name for p in units.glob("windows-*.json"))


class ResumeTests(unittest.TestCase):
    def test_resume_rebuilds_missing_window_requests(self) -> None:
        """删掉一个窗口请求再续作：补回来，且 task_id 不变。"""
        with tempfile.TemporaryDirectory() as td:
            units = Path(td) / "units"
            first = _emit(units, "--max-paragraphs", "8")
            self.assertEqual(first.returncode, 0, first.stderr)
            before = _window_names(units)
            self.assertGreaterEqual(len(before), 2, "样例书应切出多个窗口")
            task_before = _scope(units)["task_id"]

            (units / before[1]).unlink()
            second = _emit(units, "--max-paragraphs", "8", "--resume")
            self.assertEqual(second.returncode, 0, second.stderr)

            self.assertEqual(_window_names(units), before, "续作应补齐缺失的窗口请求")
            self.assertEqual(
                _scope(units)["task_id"], task_before, "范围未变时 task_id 必须保持一致"
            )

    def test_resume_rebuilds_windows_when_scope_changes(self) -> None:
        """改变段落范围再续作：task_id 必须变，窗口分片按新范围重建。"""
        with tempfile.TemporaryDirectory() as td:
            units = Path(td) / "units"
            self.assertEqual(_emit(units, "--max-paragraphs", "8").returncode, 0)
            task_before = _scope(units)["task_id"]
            windows_before = len(_window_names(units))

            proc = _emit(units, "--max-paragraphs", "4", "--resume")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            scope = _scope(units)

            self.assertNotEqual(scope["task_id"], task_before, "范围变化必须产生新任务")
            self.assertEqual(scope["max_paragraphs"], 4)
            self.assertEqual(
                len(_window_names(units)),
                len(scope["windows"]),
                "窗口分片必须与新范围一致，不能残留越界分片",
            )
            self.assertLess(len(scope["windows"]), windows_before, "缩小范围应减少窗口")

    def test_resume_never_deletes_host_annotations(self) -> None:
        """续作只清窗口请求，宿主写好的 ann-*.json 不能被删。"""
        with tempfile.TemporaryDirectory() as td:
            units = Path(td) / "units"
            self.assertEqual(_emit(units, "--max-paragraphs", "8").returncode, 0)

            ann = units / "ann-0001.json"
            ann.write_text(
                json.dumps({"schema": "vocab-annotation-decisions/v6", "windows": {}}),
                encoding="utf-8",
            )

            proc = _emit(units, "--max-paragraphs", "8", "--resume")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(ann.exists(), "宿主的识别结果不能被续作清掉")


if __name__ == "__main__":
    unittest.main()

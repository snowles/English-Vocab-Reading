"""安装自检：确认运行环境、随包词库与可选工具，并可选跑一次端到端冒烟。

用法（在技能目录下，任何 shell 都行）::

    python -m scripts.doctor
    python -m scripts.doctor --smoke

退出码 0 = 就绪；1 = 有必须先修的项。EPUBCheck / Java 只作提示，不影响就绪判定。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/doctor.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .console import configure_stdio  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 10)
SMOKE_BOOKS = (ROOT / "smoke" / "book.epub", ROOT / "tests" / "fixtures" / "epub2_basic.epub")


def _line(label: str, value: str, good: bool | None = None) -> None:
    width = sum(2 if ord(ch) > 0x2E80 else 1 for ch in label)
    mark = "" if good is None else ("  OK" if good else "  ✗")
    print(f"  {label}{' ' * max(1, 14 - width)}{value}{mark}")


def check_environment() -> bool:
    ok = True
    print("词汇伴读 · 安装自检")
    _line("技能目录", str(ROOT))

    version = sys.version_info
    good = version[:2] >= MIN_PYTHON
    _line(
        "Python",
        f"{version.major}.{version.minor}.{version.micro}"
        + ("" if good else "（需要 3.10 或更高）"),
        good,
    )
    ok = ok and good

    try:
        from lxml import etree

        _line("lxml", ".".join(str(part) for part in etree.LXML_VERSION))
    except Exception as exc:  # noqa: BLE001
        _line("lxml", f"不可用（{exc}）→ python -m pip install -r requirements.txt", False)
        ok = False

    vault = ROOT / "assets" / "lexicon.vault"
    if not vault.is_file():
        _line("随包词库", f"缺失：{vault}", False)
        return False
    try:
        from .load_lexicon import load_lexicon

        lexicon = load_lexicon(vault)
        grades = sorted({g for entry in lexicon.all_entries for g in entry.grades})
        _line("随包词库", f"{lexicon.rows_total} 条｜标签 {'/'.join(grades)}", True)
    except Exception as exc:  # noqa: BLE001
        _line("随包词库", f"无法读取（{exc}）", False)
        ok = False

    try:
        from .validate_epub import find_epubcheck

        info = find_epubcheck()
        if info.get("available"):
            jar = Path(info.get("jar") or "").name
            _line("官方校验", f"已就绪（{jar or 'EPUBCheck'}，可选）")
        else:
            _line("官方校验", "未安装（可选，不影响出书）")
    except Exception as exc:  # noqa: BLE001
        _line("官方校验", f"探测失败（{exc}）（可选）")

    return ok


def run_smoke() -> bool:
    book = next((path for path in SMOKE_BOOKS if path.is_file()), None)
    if book is None:
        print("\n冒烟跳过：找不到自带的微型 EPUB（smoke/book.epub）")
        return False

    tmp = Path(tempfile.mkdtemp(prefix="vocab-doctor-"))
    try:
        print(f"\n端到端冒烟：{book.name}")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_pipeline",
                "--book",
                str(book),
                "--presentation",
                "inline",
                "--out",
                str(tmp),
                "--report",
                str(tmp / "report.json"),
                "--html",
                str(tmp / "pages"),
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
        if result.returncode != 0:
            print(f"  未通过（exit {result.returncode}）")
            print((result.stderr or result.stdout).strip()[:400])
            return False
        report = json.loads((tmp / "report.json").read_text(encoding="utf-8"))
        found = report.get("deliverables") or {}
        epub_ok = bool(found.get("epub", {}).get("ok"))
        html_ok = bool(found.get("html", {}).get("ok"))
        epub_path = found.get("epub", {}).get("path", "")
        html_path = found.get("html", {}).get("path", "")
        _line("EPUB", Path(epub_path).name or "未生成", epub_ok)
        _line("阅读页", Path(html_path).name or "未生成", html_ok)
        marks = (report.get("annotation") or {}).get("total_matches", 0)
        _line("标注", f"{marks} 处", marks > 0)
        return epub_ok and html_ok and marks > 0
    finally:
        shutil.rmtree(tmp, True)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="词汇伴读安装自检")
    parser.add_argument("--smoke", action="store_true", help="再跑一次端到端出书冒烟")
    args = parser.parse_args(argv)

    ok = check_environment()
    if args.smoke:
        ok = run_smoke() and ok
    print("\n结论：" + ("就绪" if ok else "未就绪，按上面的提示处理后重跑"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

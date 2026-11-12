"""关3 边界压测（上架三关之一）：畸形输入不崩、不产假货、退出码正确。

这是**验证工具**，不改产品行为。每个场景在独立临时目录里跑真实 CLI，检查四件事：

1. 退出码符合预期（2 输入错误 / 3 已标注 / 4 校验失败 / 6 已阻断）；
2. stderr 里没有 Python traceback（崩了就是交付事故）；
3. 失败场景不留下 `*_vocab.epub`（不产假货）；
4. 失败场景 stderr 非空（用户能看到中文指引，而不是静默失败）。

用法：
    python tests/stress_gate3.py
    python tests/stress_gate3.py --json 关3结果.json

退出码：0 全部符合预期 / 1 有偏差 / 2 脚本自身错误。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures.build_fixtures import CONTAINER, PNG_1PX, _write_epub  # noqa: E402

BOOK = ROOT / "tests" / "fixtures" / "epub2_basic.epub"
SAMPLE_CSV = ROOT / "tests" / "fixtures" / "lexicon_sample.csv"

MIN_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="img" href="cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx"><itemref idref="ch1"/></spine>
</package>
"""

MIN_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:{uid}"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
    <navPoint id="n1" playOrder="1">
      <navLabel><text>One</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>{body}</body>
</html>
"""

ENC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
  </enc:EncryptedData>
</encryption>
"""


def _tiny_epub(path: Path, title: str, body: str, uid: str) -> Path:
    _write_epub(
        path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": MIN_OPF.format(title=title, uid=uid).encode("utf-8"),
            "OEBPS/toc.ncx": MIN_NCX.format(title=title, uid=uid).encode("utf-8"),
            "OEBPS/chapter1.xhtml": XHTML.format(title=title, body=body).encode("utf-8"),
            "OEBPS/cover.png": PNG_1PX,
        },
    )
    return path


def _copy_with_encryption(src: Path, dst: Path) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("META-INF/encryption.xml", ENC_XML.encode("utf-8"))
    return dst


def _long_paragraph_epub(path: Path) -> Path:
    sentence = (
        "The old man fished alone in the sea and carried his net to the boat "
        "before the sun rose over the water. "  # 22 词
    )
    body = "<p>" + sentence * 140 + "</p>"  # ≈ 3080 词，单段
    return _tiny_epub(path, "Long Paragraph", body, "urn:uuid:stress-long-para")


@dataclass
class Case:
    name: str
    expect: int
    note: str
    build: Callable[[Path], list[str]]
    product_scope: str = "."


CASES: list[Case] = []


def case(name: str, expect: int, note: str, product_scope: str = "."):
    def register(fn: Callable[[Path], list[str]]) -> Callable[[Path], list[str]]:
        CASES.append(Case(name, expect, note, fn, product_scope))
        return fn

    return register


# ---- 输入形态 ---------------------------------------------------------------

@case("输入·路径不存在", 2, "应报输入错误，不崩")
def _(tmp: Path) -> list[str]:
    return ["--book", str(tmp / "nope.epub"), "--presentation", "inline",
            "--no-epubcheck", "--quiet"]


@case("输入·非 EPUB（txt 改名）", 2, "mimetype/结构缺失要报 2")
def _(tmp: Path) -> list[str]:
    fake = tmp / "fake.epub"
    fake.write_text("this is not an epub at all", encoding="utf-8")
    return ["--book", str(fake), "--presentation", "inline", "--no-epubcheck", "--quiet"]


@case("输入·截断 ZIP", 2, "半个文件不能让程序崩")
def _(tmp: Path) -> list[str]:
    broken = tmp / "truncated.epub"
    data = BOOK.read_bytes()
    broken.write_bytes(data[: int(len(data) * 0.6)])
    return ["--book", str(broken), "--presentation", "inline", "--no-epubcheck", "--quiet"]


@case("输入·空 ZIP", 2, "无 container/OPF 要报 2")
def _(tmp: Path) -> list[str]:
    empty = tmp / "empty.epub"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip")
    return ["--book", str(empty), "--presentation", "inline", "--no-epubcheck", "--quiet"]


@case("输入·带 DRM 加密", 2, "encryption.xml 必须拦住，不尝试解密")
def _(tmp: Path) -> list[str]:
    enc = _copy_with_encryption(BOOK, tmp / "encrypted.epub")
    return ["--book", str(enc), "--presentation", "inline", "--no-epubcheck", "--quiet"]


@case("输入·已标注过的书", 3, "重复处理要 exit 3，不叠加标注", product_scope="second")
def _(tmp: Path) -> list[str]:
    first_dir = tmp / "first"
    first = _run(
        ["--book", str(BOOK), "--presentation", "inline", "--no-epubcheck",
         "--quiet", "--out", str(first_dir)],
        check=False,
    )
    products = sorted(first_dir.glob("*_vocab.epub")) if first_dir.is_dir() else []
    if not products:
        raise RuntimeError("构造已标注样本失败：首次直注没有产物")
    return ["--book", str(products[0]), "--presentation", "inline",
            "--no-epubcheck", "--quiet", "--out", str(tmp / "second")]


@case("输入·只有图片页（无正文）", 6, "无正文段要 exit 6，不产空书")
def _(tmp: Path) -> list[str]:
    book = _tiny_epub(
        tmp / "image_only.epub", "Image Only",
        '<div><img src="cover.png" alt="cover"/></div>',
        "urn:uuid:stress-image-only",
    )
    return ["--book", str(book), "--presentation", "inline", "--no-epubcheck",
            "--quiet", "--out", str(tmp / "out")]


@case("输入·超长单段（约 3000 词）", 0, "长段不拆、不崩，能出书")
def _(tmp: Path) -> list[str]:
    book = _long_paragraph_epub(tmp / "long.epub")
    return ["--book", str(book), "--presentation", "inline", "--no-epubcheck",
            "--quiet", "--out", str(tmp / "out")]


# ---- 参数越界 ---------------------------------------------------------------

@case("参数·window-paragraphs 0", 2, "非法窗口大小要报 2，不得除零崩溃")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--emit-annotations", str(tmp / "u"),
            "--window-paragraphs", "0", "--no-epubcheck", "--quiet"]


@case("参数·max-paragraphs 负数", 2, "负数范围要报 2")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--emit-annotations", str(tmp / "u"),
            "--max-paragraphs", "-5", "--no-epubcheck", "--quiet"]


@case("参数·density 非法值", 2, "argparse 应拒绝")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--presentation", "inline",
            "--density", "whatever", "--no-epubcheck", "--quiet"]


@case("参数·presentation 非法值", 2, "argparse 应拒绝")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--presentation", "banana",
            "--no-epubcheck", "--quiet"]


@case("参数·空 CSV 词表", 2, "空词表要报词表不可用")
def _(tmp: Path) -> list[str]:
    csv = tmp / "empty.csv"
    csv.write_text("", encoding="utf-8")
    return ["--book", str(BOOK), "--presentation", "inline", "--lexicon", str(csv),
            "--no-epubcheck", "--quiet"]


@case("参数·CSV 缺必要列", 2, "缺 word/gloss 要报 LexiconError")
def _(tmp: Path) -> list[str]:
    csv = tmp / "bad.csv"
    csv.write_text("foo,bar\n1,2\n", encoding="utf-8")
    return ["--book", str(BOOK), "--presentation", "inline", "--lexicon", str(csv),
            "--no-epubcheck", "--quiet"]


# ---- 状态与产物 -------------------------------------------------------------

@case("状态·输出目录有外来文件", 2, "只清自己的产物，遇外来文件即拒绝")
def _(tmp: Path) -> list[str]:
    units = tmp / "units"
    units.mkdir(parents=True)
    (units / "用户的笔记.txt").write_text("不要删我", encoding="utf-8")
    return ["--book", str(BOOK), "--emit-annotations", str(units),
            "--max-paragraphs", "4", "--no-epubcheck", "--quiet"]


@case("状态·同目录重复出书", 0, "不覆盖，自动 _2 后缀，两次都成功")
def _(tmp: Path) -> list[str]:
    out = tmp / "out"
    _run(["--book", str(BOOK), "--presentation", "inline", "--no-epubcheck",
          "--quiet", "--out", str(out)], check=False)
    return ["--book", str(BOOK), "--presentation", "inline", "--no-epubcheck",
            "--quiet", "--out", str(out)]


# ---- 基线对照（证明不是"全都失败"） ------------------------------------------

@case("基线·正常书直注出书", 0, "零配置直注应成功")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--presentation", "inline", "--no-epubcheck",
            "--quiet", "--out", str(tmp / "out")]


@case("基线·正常书导出窗口", 0, "四步链路起点应成功")
def _(tmp: Path) -> list[str]:
    return ["--book", str(BOOK), "--emit-annotations", str(tmp / "u"),
            "--max-paragraphs", "4", "--no-epubcheck", "--quiet"]


def _run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.run_pipeline", *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"准备样本失败（exit {proc.returncode}）：{proc.stderr[-300:]}")
    return proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="关3 边界压测")
    parser.add_argument("--json", default="", help="把结果写到 JSON")
    args = parser.parse_args(argv)

    rows = []
    failed = 0
    for item in CASES:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            try:
                run_argv = item.build(tmp)
            except Exception as exc:  # 样本构造失败也是偏差
                rows.append({"场景": item.name, "期望": item.expect, "实际": None,
                             "结果": "样本构造失败", "说明": str(exc)[:200]})
                failed += 1
                continue

            proc = _run(run_argv)
            crashed = "Traceback (most recent call last)" in proc.stderr
            scope_dir = tmp / item.product_scope
            products = (
                list(scope_dir.rglob("*_vocab.epub")) if scope_dir.is_dir() else []
            )
            stderr_first = next(
                (ln.strip() for ln in proc.stderr.splitlines() if ln.strip()), ""
            )

            problems = []
            if proc.returncode != item.expect:
                problems.append(f"退出码 {proc.returncode} ≠ {item.expect}")
            if crashed:
                problems.append("出现 traceback")
            if item.expect != 0 and products:
                problems.append(f"失败场景仍留下产物 {len(products)} 个")
            if item.expect != 0 and not proc.stderr.strip():
                problems.append("stderr 为空（用户看不到原因）")

            ok = not problems
            failed += 0 if ok else 1
            rows.append({
                "场景": item.name,
                "期望": item.expect,
                "实际": proc.returncode,
                "结果": "OK" if ok else "；".join(problems),
                "提示": stderr_first[:120],
            })

    width = max(len(r["场景"]) for r in rows) + 2
    print("=" * 78)
    print(f"关3 边界压测 · {len(rows)} 个场景")
    print("=" * 78)
    for row in rows:
        mark = "OK  " if row["结果"] == "OK" else "FAIL"
        print(f"{mark} {row['场景']:<{width}} 期望 {row['期望']} 实际 {row['实际']}")
        if row["结果"] != "OK":
            print(f"       └─ {row['结果']}")
    print("-" * 78)
    print(f"合计 {len(rows)} · 偏差 {failed} · 判定 {'PASS' if failed == 0 else 'FAIL'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"结果已写入 {args.json}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

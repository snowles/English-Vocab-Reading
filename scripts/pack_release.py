"""打发布包：按显式清单收集文件，排除缓存、样例书与历史输出。

    python -m scripts.pack_release            # 生成 dist/vocab-reading-<版>.zip
    python -m scripts.pack_release --check    # 只校验清单与路径泄露

包内结构：``vocab-reading/...``，根目录附 ``release.json``（版本、文件清单、SHA-256）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .console import configure_stdio  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAME = "vocab-reading"
VERSION = "0.10.7"
PYTHON_MIN = "3.10"

#: 显式清单：只发运行必需与人读文档，不带样例产物与开发资料。
FILES = (
    "SKILL.md",
    "README.md",
    "INSTALL.md",
    "schema.md",
    "requirements.txt",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "assets/lexicon.vault",
    "assets/lexicon.forms.vault",
    "assets/lexicon.source.json",
    # H2 契约：用户自备词表的列名模板（表头与 COLUMN_ALIASES 逐字对齐，有单测守护）
    "assets/lexicon-template.csv",
    "references/ECDICT_LICENSE.txt",
    "references/h1-coverage.md",
    "references/maintenance.md",
    "references/testing.md",
)
GLOBS = ("scripts/*.py",)

#: 上架闸门（audit.py --profile upload）的必填字段；缺任一项平台直接判解析失败。
REQUIRED_FRONTMATTER = (
    "name",
    "display_name",
    "display_name_en",
    "description",
    "description_zh",
    "description_en",
    "version",
    "author",
    "category",
    "allowed-tools",
)
#: 冒烟素材：开发目录里的 fixture → 包内 smoke/，让新环境不依赖开发者 Downloads。
SMOKE = (
    ("tests/fixtures/epub2_basic.epub", "smoke/book.epub"),
    ("tests/fixtures/lexicon_sample.csv", "smoke/lexicon.csv"),
)

START_HERE = """词汇伴读 · 测试包使用说明
================================

这个包是给「另一台电脑」做真实安装与使用测试用的，不需要联网装别的东西
（除了最后一步装 lxml 依赖）。

第 1 步：双击「安装依赖并自检.bat」
--------------------------------
它会依次：检查 Python → 安装 lxml → 跑一次真实出书自检。
最后一行显示「结论：就绪」就说明这台电脑能用了。
如果提示找不到 python，请先装 Python 3.10 或更高版本，安装时勾选
「Add python.exe to PATH」，然后重开窗口再双击一次。

第 2 步：把 vocab-reading 文件夹放好
--------------------------------
整个 vocab-reading 文件夹复制到：
    %USERPROFILE%\\.workbuddy\\skills\\vocab-reading
（文件夹名保持 vocab-reading，不要改名）

第 3 步：在助手里启用
--------------------------------
打开 WorkBuddy → 技能管理 → 刷新 → 启用「英语词汇伴读」。
没看到就重启一次 WorkBuddy。

第 4 步：用一本陌生的书试一次
--------------------------------
把一本未加密的英文 EPUB 放到任意目录，对助手说：
    「把 D:\\Books\\书名.epub 做成伴读版」
助手会先问一句「初级（每段带中文译文）还是高级（不带）」，照你的习惯回答就行。
默认会在书旁边生成「书名_伴读」文件夹，里面有 EPUB 和阅读页 HTML。
请两个档位各试一次，并在「测试记录.md」里分别记下结果。

第 5 步：把结果记下来
--------------------------------
打开「测试记录.md」，按表填：电脑环境、用时、产物、遇到的问题。
特别要试一次「中途打断再继续」，看范围和剩余章节是否说得清楚。

出问题时要提供的信息：
- 测试记录.md 里填的内容
- 报错时的完整窗口文字（截图或复制文本）
- 用的是哪本书、助手是哪个（WorkBuddy / Codex / 其他）

注意：词库已经随包，不用准备词表；Java / EPUBCheck 不是必需。
"""

TEST_RECORD = """# 词汇伴读 · 另一台电脑测试记录

> 填完这份表就能判断「陌生人能不能装好、用起来」。没有实测的项请写「未测」，
> 不要凭印象填「正常」。

## 1. 环境

| 项 | 填写 |
|---|---|
| 测试日期 | |
| 电脑 / 系统版本 | |
| Python 版本（自检窗口第一屏） | |
| 助手名称与版本 | |
| 是否全新安装（之前没装过本技能） | 是 / 否 |

## 2. 安装

| 步骤 | 结果 | 备注 |
|---|---|---|
| 双击「安装依赖并自检.bat」 | 就绪 / 失败 | |
| 如果把 Python 换成了别的版本 | | |
| 复制到 skills 目录后宿主能发现 | 是 / 否 | |
| 没装 Java 时出书是否正常 | | |

## 3. 一句话流程（用一本**陌生**的书）

| 项 | 填写 |
|---|---|
| 书名与章节数 | |
| 对助手说的原话 | |
| 从说到出成品大约用时 | |
| 得到 EPUB | 是 / 否 |
| 得到阅读页 HTML | 是 / 否 |
| 助手有没有主动问「初级 / 高级」 | 有 / 没有 |
| 选初级：答复前有没有先出书 | 有 / 没有 |
| 选初级：每段后是否有「参考译文：」（书名/章节名不译） | 是 / 否 |
| 选初级：正文是否没有逐段重复「仅供参考」 | 是 / 否 |
| 选高级：是否完全不带中文译文 | 是 / 否 |
| 标注是否落在正文里、只标该标的位置 | |
| 正文括注是否不含等级数字（形如 `(捕鱼,[1])`） | 是 / 否 |
| 词条块是否紧跟自然段（短段合并）、而不是堆在章末 | |
| 段后词条是否完整（释义 / 例句 / 音标 / 词形信息） | |
| 音标是否按正文看到的词形给（如 fished 标 /fiʃt/） | 是 / 否 |
| 助手有没有把命令或参数甩给你 | 有 / 没有 |
| 有没有出现「0 处标注却说到完成」 | 有 / 没有 |

## 4. 中断续作

| 项 | 填写 |
|---|---|
| 在哪一步中断（导出 / 识别 / 审查 / 出书） | |
| 继续后是否重复处理已完成的章节 | |
| 剩余范围是否说得清楚 | |
| 改动后旧审查是否失效并重新审 | |

## 5. 问题清单

| # | 现象 | 复现步骤 | 严重度（阻断/影响/轻微） |
|---|---|---|---|
| 1 | | | |
| 2 | | | |

## 6. 结论

- 能不能装好并出书：
- 最影响体验的一点：
- 是否愿意给朋友用：
"""

SELF_CHECK_BAT = """@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  词汇伴读 - 安装依赖与自检
echo ============================================
echo.
echo [1/3] 检查 Python ...
python -V
if errorlevel 1 goto nopython
echo.
echo [2/3] 安装依赖 lxml ...
python -m pip install -r "vocab-reading\\requirements.txt"
if errorlevel 1 (
  echo.
  echo 直连安装失败，改用国内镜像重试 ...
  python -m pip install -r "vocab-reading\\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo.
echo [3/3] 安装自检（会真实出一本书，约 10 秒）...
echo.
python "vocab-reading\\scripts\\doctor.py" --smoke
echo.
echo ============================================
echo  最后一行显示「结论：就绪」即安装成功。
echo  其余情况请把整个窗口内容复制给助手。
echo ============================================
pause
exit /b 0

:nopython
echo.
echo 没有找到 python 命令。
echo 请先安装 Python 3.10 或更高版本，安装时勾选
echo   Add python.exe to PATH
echo 然后重新打开窗口，再双击本文件。
echo.
pause
exit /b 1
"""

PRIVATE_PATTERNS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    re.compile(r"/home/[A-Za-z0-9._-]+"),
    re.compile("Doubao" + "Work"),
)



def _text_files() -> list[Path]:
    names = [ROOT / rel for rel in FILES]
    for pattern in GLOBS:
        names.extend(sorted(ROOT.glob(pattern)))
    return [path for path in names if path.suffix in (".md", ".py", ".txt", ".json")]


def collect() -> list[tuple[Path, str]]:
    """返回 (源文件, 包内路径) 列表，缺文件直接报错。"""

    missing = [rel for rel in FILES if not (ROOT / rel).is_file()]
    if missing:
        raise SystemExit("发布清单里的文件不存在：" + "、".join(missing))

    entries: list[tuple[Path, str]] = [(ROOT / rel, rel) for rel in FILES]
    for pattern in GLOBS:
        found = sorted(ROOT.glob(pattern))
        if not found:
            raise SystemExit(f"没有匹配到脚本：{pattern}")
        entries.extend((path, path.relative_to(ROOT).as_posix()) for path in found)
    for source, target in SMOKE:
        path = ROOT / source
        if not path.is_file():
            raise SystemExit(f"冒烟素材缺失：{source}")
        entries.append((path, target))
    return entries


def check_private_paths() -> list[str]:
    leaks: list[str] = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in PRIVATE_PATTERNS:
            for hit in pattern.findall(text):
                leaks.append(f"{path.relative_to(ROOT).as_posix()}: {hit}")
    return leaks


def check_cache(entries: list[tuple[Path, str]]) -> list[str]:
    bad = []
    for _, target in entries:
        if "__pycache__" in target or target.endswith((".pyc", ".pending")):
            bad.append(target)
        if target.startswith(("examples/", "out/", "dist/")):
            bad.append(target)
    return bad


def read_frontmatter(path: Path) -> dict[str, str]:
    """极简 frontmatter 读取：不做 YAML 全解析，只为在打包时就拦住字段缺失。"""

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out: dict[str, str] = {}
    for line in parts[1].splitlines():
        if not line or line[:1] in (" ", "\t", "#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def check_frontmatter() -> list[str]:
    """上架闸门的前置条件：必填字段齐全、name/version 不与包本身分叉。"""

    fm = read_frontmatter(ROOT / "SKILL.md")
    if not fm:
        return ["SKILL.md 缺少 frontmatter"]
    problems = [f"SKILL.md 缺必填字段 {key}" for key in REQUIRED_FRONTMATTER if not fm.get(key)]
    if fm.get("name") and fm["name"] != NAME:
        problems.append(f"SKILL.md name={fm['name']} 与包名 {NAME} 不一致")
    if fm.get("version") and fm["version"] != VERSION:
        problems.append(
            f"版本分叉：SKILL.md version={fm['version']} ≠ pack_release.VERSION={VERSION}"
        )
    tools = fm.get("allowed-tools", "")
    if tools and "bash" not in tools.lower():
        problems.append("allowed-tools 未放行 Bash，运行期脚本无法被调用")
    return problems


def build(out_dir: Path) -> Path:
    entries = collect()
    problems = check_private_paths() + check_cache(entries) + check_frontmatter()
    if problems:
        raise SystemExit("发布包检查未通过：\n  " + "\n  ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{NAME}-{VERSION}.zip"
    records = []
    for source, rel in entries:
        data = source.read_bytes()
        records.append(
            {
                "path": rel,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": "vocab-release/v1",
        "name": NAME,
        "version": VERSION,
        "python_min": PYTHON_MIN,
        "built_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(records),
        "files": records,
    }

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for source, rel in entries:
            info = zipfile.ZipInfo(f"{NAME}/{rel}")
            info.date_time = datetime.now().timetuple()[:6]
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, source.read_bytes())
        zf.writestr(
            f"{NAME}/release.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        missing = [record["path"] for record in records if f"{NAME}/{record['path']}" not in names]
        if missing:
            raise SystemExit("打包后校验失败，缺文件：" + "、".join(missing))
    return target


def bundle(out_dir: Path) -> Path:
    """打「另一台电脑测试包」：技能本体 + 双击自检 + 使用说明 + 记录模板。"""

    entries = collect()
    problems = check_private_paths() + check_cache(entries) + check_frontmatter()
    if problems:
        raise SystemExit("发布包检查未通过：\n  " + "\n  ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{NAME}-{VERSION}-测试包.zip"
    root = f"{NAME}-{VERSION}-测试包"

    records = []
    for source, rel in entries:
        data = source.read_bytes()
        records.append(
            {
                "path": rel,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": "vocab-release/v1",
        "name": NAME,
        "version": VERSION,
        "python_min": PYTHON_MIN,
        "kind": "transfer-bundle",
        "built_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(records),
        "files": records,
    }

    extras = (
        ("开始这里.txt", START_HERE.encode("utf-8-sig")),
        ("测试记录.md", TEST_RECORD.encode("utf-8-sig")),
        # cmd 按本地 OEM 代码页读取批处理，中文用 GBK 写才不会变乱码
        ("安装依赖并自检.bat", SELF_CHECK_BAT.replace("\n", "\r\n").encode("gbk")),
    )

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        stamp = datetime.now().timetuple()[:6]
        for source, rel in entries:
            info = zipfile.ZipInfo(f"{root}/{NAME}/{rel}")
            info.date_time = stamp
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, source.read_bytes())
        info = zipfile.ZipInfo(f"{root}/{NAME}/release.json")
        info.date_time = stamp
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for name, data in extras:
            info = zipfile.ZipInfo(f"{root}/{name}")
            info.date_time = stamp
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        required = [f"{root}/{NAME}/{record['path']}" for record in records]
        required += [f"{root}/{name}" for name, _ in extras]
        missing = [item for item in required if item not in names]
        if missing:
            raise SystemExit("打包后校验失败，缺文件：" + "、".join(missing))
        # 回读校验：bat 必须还能按 GBK 解码出中文
        zf.read(f"{root}/安装依赖并自检.bat").decode("gbk")
    return target


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="打发布包（显式清单）")
    parser.add_argument("--out", default="dist", metavar="DIR", help="输出目录")
    parser.add_argument("--check", action="store_true", help="只校验清单，不打包")
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="改打「另一台电脑测试包」：附自检 bat、使用说明与测试记录模板",
    )
    args = parser.parse_args(argv)

    if args.check:
        entries = collect()
        problems = check_private_paths() + check_cache(entries) + check_frontmatter()
        print(f"清单 {len(entries)} 个文件")
        if problems:
            print("检查未通过：")
            for item in problems:
                print("  -", item)
            return 1
        print("检查通过：无私有绝对路径、无缓存与样例产物、frontmatter 字段齐全且版本一致")
        return 0

    if args.bundle:
        target = bundle(ROOT / args.out)
    else:
        target = build(ROOT / args.out)
    size = target.stat().st_size
    with zipfile.ZipFile(target) as zf:
        count = len(zf.namelist())
    print(f"发布包 -> {target}  ({size:,} bytes, {count} 项)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

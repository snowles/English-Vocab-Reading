"""EPUB 只读预检：结构、加密、章节、文本量与候选词预估（阶段 1）。

不修改任何文件，不联网，不调用模型。
"""

from __future__ import annotations

import hashlib
import posixpath
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/inspect_book.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"
from dataclasses import dataclass, field

from .binding import is_annotated
from .epub_common import (
    Archive,
    EpubError,
    Package,
    iter_text_slots,
    local_name,
    open_epub,
    parse_xml,
)
from .load_lexicon import Lexicon
from .matching import SEP, find_matches

__all__ = ["ChapterInfo", "Precheck", "inspect_book", "sha256_file", "SUPPORTED_SUFFIXES"]

SUPPORTED_SUFFIXES = {".epub"}
UNSUPPORTED_HINTS = {
    ".pdf": "可提取文本 PDF 属于阶段 4，阶段 1 未实现（且不承诺复刻固定版式）",
    ".md": "Markdown 导入属于阶段 3，阶段 1 未实现",
    ".markdown": "Markdown 导入属于阶段 3，阶段 1 未实现",
    ".txt": "TXT 导入属于阶段 3，阶段 1 未实现",
    ".docx": "DOCX 不属于阶段 1 支持范围",
    ".mobi": "MOBI/AZW3 不在支持范围，请先转换为 EPUB",
    ".azw3": "MOBI/AZW3 不在支持范围，请先转换为 EPUB",
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ChapterInfo:
    index: int
    path: str
    label: str
    skipped: bool = False
    reason: str = ""
    tokens: int = 0
    candidates_estimate: int = 0
    text_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "path": self.path,
            "label": self.label,
            "skipped": self.skipped,
            "reason": self.reason,
            "tokens": self.tokens,
            "candidates_estimate": self.candidates_estimate,
        }


@dataclass
class Precheck:
    path: str
    format: str = "epub"
    sha256: str = ""
    encrypted: bool = False
    version: str = ""
    title: str = ""
    opf: str = ""
    manifest_items: int = 0
    spine_items: int = 0
    chapters_total: int = 0
    chapters: list[ChapterInfo] = field(default_factory=list)
    text_bytes: int = 0
    tokens_total: int = 0
    candidates_estimate_total: int = 0
    already_annotated_files: int = 0
    unprocessable_chapters: list[dict] = field(default_factory=list)
    has_mimetype: bool = True
    has_nav: bool = False
    has_ncx: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "format": self.format,
            "sha256": self.sha256,
            "encrypted": self.encrypted,
            "version": self.version,
            "title": self.title,
            "opf": self.opf,
            "manifest_items": self.manifest_items,
            "spine_items": self.spine_items,
            "chapters_total": self.chapters_total,
            "chapters": [c.as_dict() for c in self.chapters],
            "text_bytes": self.text_bytes,
            "tokens_total": self.tokens_total,
            "candidates_estimate_total": self.candidates_estimate_total,
            "already_annotated_files": self.already_annotated_files,
            "unprocessable_chapters": list(self.unprocessable_chapters),
            "has_mimetype": self.has_mimetype,
            "has_nav": self.has_nav,
            "has_ncx": self.has_ncx,
            "warnings": list(self.warnings),
        }


def inspect_book(
    book_path: str | Path,
    lexicon: Lexicon | None = None,
    *,
    reduce_forms: bool = True,
) -> Precheck:
    """预检输入书。结构问题抛 `EpubError`，可容忍问题写入 warnings。"""

    path = Path(book_path)
    suffix = path.suffix.lower()
    if suffix and suffix not in SUPPORTED_SUFFIXES:
        hint = UNSUPPORTED_HINTS.get(suffix, f"不支持的输入格式：{suffix or '未知'}")
        raise EpubError(f"{hint}（文件：{path.name}）")
    if not path.is_file():
        raise EpubError(f"输入文件不存在：{path}")

    pre = Precheck(path=str(path), sha256=sha256_file(path))

    archive, package = open_epub(path)
    try:
        pre.version = package.version
        pre.title = package.title
        pre.opf = package.opf_path
        pre.manifest_items = len(package.items)
        pre.spine_items = len(package.spine)
        pre.has_mimetype = archive.exists("mimetype")
        pre.has_nav = package.nav_item is not None
        pre.has_ncx = package.ncx_item is not None
        if not pre.has_mimetype:
            pre.warnings.append("缺少 mimetype 条目（EPUB 规范要求存在且不压缩）")
        if not pre.has_nav and package.is_epub3:
            pre.warnings.append("EPUB 3 未声明 nav 文档，导航校验将降级")

        seen_paths: set[str] = set()
        for spine_item in package.spine:
            item = spine_item.item
            label = posixpath.basename(item.path)
            if item.path in seen_paths:
                continue  # 同一文档被 spine 引用多次，只统计一次
            seen_paths.add(item.path)

            if not item.is_xhtml:
                info = ChapterInfo(
                    index=spine_item.index,
                    path=item.path,
                    label=label,
                    skipped=True,
                    reason=f"媒体类型非 XHTML（{item.media_type or '未知'}）",
                )
                pre.chapters.append(info)
                pre.unprocessable_chapters.append({"path": item.path, "reason": info.reason})
                continue

            data = archive.read(item.path)
            info = ChapterInfo(index=spine_item.index, path=item.path, label=label)
            pre.chapters.append(info)
            pre.chapters_total += 1

            if is_annotated(data):
                pre.already_annotated_files += 1
                info.skipped = True
                info.reason = "已含本技能标注（vocab-skill / data-vocab-id）"
                continue

            try:
                tree = parse_xml(data, item.path)
            except EpubError as exc:
                info.skipped = True
                info.reason = "XHTML 无法解析"
                pre.unprocessable_chapters.append({"path": item.path, "reason": str(exc)})
                continue

            text = _visible_text(tree.getroot(), info)
            if lexicon is not None and text:
                result = find_matches(text, lexicon, reduce_forms=reduce_forms)
                info.candidates_estimate = len(result.matches)
                pre.candidates_estimate_total += info.candidates_estimate
            pre.tokens_total += info.tokens
            pre.text_bytes += info.text_bytes
    finally:
        archive.close()

    return pre


def _visible_text(root, info: ChapterInfo) -> str:
    """把可见文本槽拼成一段（用 SEP 保留跨节点边界），并统计 token。"""

    chunks: list[str] = []
    for slot in iter_text_slots(root):
        if not slot.writable:
            continue
        from .matching import TOKEN_RE  # 局部导入，避免循环依赖

        info.tokens += len(TOKEN_RE.findall(slot.text))
        info.text_bytes += len(slot.text)
        chunks.append(slot.text)
    return SEP.join(chunks)


def count_visible_tokens(root) -> int:
    """供其它模块复用的 token 计数。"""

    from .matching import TOKEN_RE

    return sum(
        len(TOKEN_RE.findall(slot.text))
        for slot in iter_text_slots(root)
        if slot.writable
    )


def describe_structure(package: Package) -> dict:
    """给报告用的结构摘要。"""

    return {
        "version": package.version,
        "opf": package.opf_path,
        "manifest_items": len(package.items),
        "spine_items": len(package.spine),
        "nav": package.nav_item.path if package.nav_item else None,
        "ncx": package.ncx_item.path if package.ncx_item else None,
        "xhtml_items": len(package.xhtml_items()),
    }


def _cli(argv: list[str] | None = None) -> int:
    """预检命令行：只读，不产生任何文件改动。"""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="inspect_book", description="EPUB 只读预检（不改任何文件）"
    )
    parser.add_argument("book", nargs="?", help="EPUB 路径")
    parser.add_argument("--book", dest="book_opt", help="EPUB 路径（与位置参数等价）")
    parser.add_argument("--lexicon", default="", help="可选词表 CSV，用于候选词预估")
    parser.add_argument("--grades", default="", help="词表等级筛选")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    book = args.book_opt or args.book
    if not book:
        parser.error("缺少 --book 或位置参数")

    lexicon = None
    if args.lexicon:
        from .load_lexicon import load_lexicon

        grades = [g.strip() for g in args.grades.replace("，", ",").split(",") if g.strip()]
        lexicon = load_lexicon(args.lexicon, grades or None)

    try:
        pre = inspect_book(book, lexicon)
    except EpubError as exc:
        print(f"预检失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(pre.as_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"文件      {pre.path}")
    print(f"结构      EPUB {pre.version or '未知'}｜OPF {pre.opf}")
    print(f"清单      manifest {pre.manifest_items} 项｜spine {pre.spine_items} 项")
    print(f"导航      nav={'有' if pre.has_nav else '无'}｜NCX={'有' if pre.has_ncx else '无'}")
    print(f"章节      {pre.chapters_total} 个可处理章节")
    print(f"文本      {pre.tokens_total} 词 / {pre.text_bytes} 字符")
    if lexicon is not None:
        print(f"候选      {pre.candidates_estimate_total} 处命中（预估）")
    if pre.already_annotated_files:
        print(f"注意      已有 {pre.already_annotated_files} 个章节带本技能标注，将被拒绝")
    for warning in pre.warnings:
        print(f"警告      {warning}")
    for item in pre.unprocessable_chapters:
        print(f"无法处理  {item['path']}：{item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

"""出书阅读档位与段落译文数据通道（11-11 读者拍板）。

    * 正式出书前宿主必须主动问：初级（带中文译文）/ 高级（不带）。
    * 技能本体不调用任何模型；译文由宿主按段落写回，程序只做校验与渲染。
    * 译文按 `pid + text_sha256` 双重定位，串段即跳过并报问题，绝不猜测。
    * EPUB 不支持点击：初级模式的译文直接显示在自然段后，不做折叠/开关。

协议：
    vocab-para-translation-request/v1   程序导出 → 宿主
    vocab-para-translations/v1          宿主写回 → 程序

宿主写回示例::

    {
      "schema": "vocab-para-translations/v1",
      "book_sha256": "<源书 SHA-256>",
      "reader_mode": "basic",
      "paragraphs": [
        {"pid": "OEBPS/ch1.xhtml#3", "text_sha256": "...", "translation": "……"}
      ]
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "READER_MODES",
    "READER_MODE_LABELS",
    "READER_MODE_UNSPECIFIED",
    "SCHEMA_REQUEST",
    "SCHEMA_TRANSLATIONS",
    "ReaderModeError",
    "ParagraphTranslation",
    "TranslationSet",
    "parse_reader_mode",
    "para_hash",
    "paragraph_records",
    "build_translation_request",
    "load_translations",
    "looks_like_title_page",
]

READER_MODES = ("basic", "advanced")
READER_MODE_UNSPECIFIED = "unspecified"
READER_MODE_LABELS = {
    "basic": "初级（带中文译文）",
    "advanced": "高级（不带中文译文）",
}
SCHEMA_REQUEST = "vocab-para-translation-request/v1"
SCHEMA_TRANSLATIONS = "vocab-para-translations/v1"

_WS_RE = re.compile(r"\s+")
_MODE_ALIASES = {
    "basic": "basic", "初级": "basic", "带译文": "basic", "双语": "basic",
    "advanced": "advanced", "高级": "advanced", "不带译文": "advanced",
    "原文": "advanced",
}


class ReaderModeError(ValueError):
    """阅读档位缺失、非法或与译文数据不匹配。"""


def parse_reader_mode(raw: str | None) -> str:
    """把用户/宿主给的档位归一成 basic / advanced / unspecified。"""

    if raw is None or not str(raw).strip():
        return READER_MODE_UNSPECIFIED
    value = str(raw).strip().lower()
    if value == READER_MODE_UNSPECIFIED:
        return READER_MODE_UNSPECIFIED
    if value in READER_MODES:
        return value
    if value in _MODE_ALIASES:
        return _MODE_ALIASES[value]
    raise ReaderModeError(
        f"未知的阅读档位 {raw!r}；只能是「初级（带中文译文）」或「高级（不带中文译文）」"
    )


def para_hash(text: str) -> str:
    """段落定位哈希：折叠空白后按原文字符算，避免排版差异造成串段。"""

    import unicodedata

    normalized = _WS_RE.sub(" ", unicodedata.normalize("NFC", text or "")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParagraphTranslation:
    pid: str
    text_sha256: str
    translation: str


@dataclass
class TranslationSet:
    items: dict[str, ParagraphTranslation] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    book_sha256: str = ""
    reader_mode: str = ""

    def __len__(self) -> int:
        return len(self.items)

    def lookup(self, pid: str, text: str) -> str:
        """按 pid + 原文哈希取译文；任一对不上都返回空串（整段省略）。"""

        item = self.items.get(pid)
        if item is None:
            return ""
        if item.text_sha256 != para_hash(text):
            self.issues.append(f"段落 {pid} 原文已变，译文作废（不串段）")
            return ""
        return item.translation

    def coverage(self, records: Iterable[dict]) -> dict:
        records = [r for r in records if r.get("text", "").strip()]
        total = len(records)
        covered = sum(
            1 for r in records
            if self.items.get(r["pid"], None)
            and self.items[r["pid"]].text_sha256 == r["text_sha256"]
        )
        missing = [
            r["pid"] for r in records
            if not (
                self.items.get(r["pid"], None)
                and self.items[r["pid"]].text_sha256 == r["text_sha256"]
            )
        ]
        return {
            "total": total,
            "covered": covered,
            "missing": len(missing),
            "missing_samples": missing[:5],
            "ratio": round(covered / total, 4) if total else 1.0,
        }


def looks_like_title_page(paragraphs: list) -> bool:
    """判断一个文档是不是「书名页」（书名 + 作者 + 副题这类，整页不译）。

    判据刻意保守：段落很少、每段都很短、整页词数少，且没有任何一段带句末标点。
    这样只有真正的扉页/书名页会命中，短篇正文、题记、独句成章都不会被误伤。
    """

    texts = [(getattr(p, "text", "") or "").strip() for p in paragraphs]
    texts = [t for t in texts if t]
    if not texts or len(texts) > 5:
        return False
    if any(len(t) > 60 for t in texts):
        return False
    if sum(len(t.split()) for t in texts) > 40:
        return False
    return not any(t.rstrip()[-1:] in ".!?…。！？" for t in texts)


#: 旧名保留（内部/测试曾用下划线版）。
_looks_like_title_page = looks_like_title_page


def paragraph_records(book_path: str | Path) -> list[dict]:
    """按源书 spine 顺序列出全部可读自然段（与 units 的段落口径一致）。

    书名 / 章节名 / 标题不导出（11-11 拍板：标题不翻译），
    所以宿主不会收到这类段落，也就不会译出「第一章」这种噪声。
    """

    from .chapters import detect_chapters, doc_chapter_map
    from .epub_common import open_epub
    from .units import document_paragraphs, is_title_block

    archive, package = open_epub(book_path)
    out: list[dict] = []
    try:
        chapters = detect_chapters(archive, package)
        labels = {c.index: c.label for c in chapters}
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        seen_chapters: set[int] = set()
        xhtml_index = -1
        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            xhtml_index += 1
            default, id_map = chapter_map.get(item.path, (0, {}))
            try:
                raw = archive.read(item.path)
                _, paragraphs = document_paragraphs(raw, item.path, default, id_map)
            except Exception:  # noqa: BLE001 - 单个坏文档不影响全书请求导出
                continue
            # 最前面一两个文档若整页就是书名 / 作者 / 副题，整页不译。
            if xhtml_index < 2 and looks_like_title_page(paragraphs):
                continue
            for p in paragraphs:
                if p.chapter <= 0:  # 前置页/目录不译，与渲染口径一致
                    continue
                first = p.chapter not in seen_chapters
                seen_chapters.add(p.chapter)
                if is_title_block(
                    p.element, labels.get(p.chapter, ""), first_in_chapter=first
                ):
                    continue
                text = (p.text or "").strip()
                if len(text) < 2:
                    continue
                out.append({
                    "pid": p.pid,
                    "text": text,
                    "text_sha256": para_hash(text),
                })
    finally:
        archive.close()
    return out


def build_translation_request(book_path: str | Path, book_sha256: str) -> dict:
    """导出给宿主的译文请求（宿主逐段翻译，不改原文、不编造）。"""

    records = paragraph_records(book_path)
    return {
        "schema": SCHEMA_REQUEST,
        "book_sha256": book_sha256,
        "reader_mode": "basic",
        "paragraph_count": len(records),
        "instructions": (
            "逐段给出中文译文，只写 translation 字段；保留原文语气，"
            "不增删情节、不解释单词、不输出原文。书名、章节名、标题不要出现在译文里"
            "（这类段落已排除，不会发给你）。缺把握的段落宁可留空，"
            "程序会按缺译文报告，不会替你编造。"
        ),
        "paragraphs": records,
    }


def _iter_json_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.json") if p.is_file())


def load_translations(path: str | Path, *, book_sha256: str = "") -> TranslationSet:
    """加载宿主写回的译文（单文件或目录），严格校验，坏数据不静默吞掉。"""

    p = Path(path)
    if not p.exists():
        raise ReaderModeError(f"译文文件不存在：{p}")
    files = _iter_json_files(p)
    if not files:
        raise ReaderModeError(f"译文目录里没有 JSON：{p}")
    result = TranslationSet()
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            result.issues.append(f"{file.name}: JSON 无法读取（{exc}）")
            continue
        if payload.get("schema") != SCHEMA_TRANSLATIONS:
            result.issues.append(f"{file.name}: schema 不是 {SCHEMA_TRANSLATIONS}")
            continue
        sha = str(payload.get("book_sha256") or "")
        if book_sha256 and sha and sha != book_sha256:
            result.issues.append(f"{file.name}: 译文不是为这本书写的（book_sha256 不一致）")
            continue
        if sha and not result.book_sha256:
            result.book_sha256 = sha
        result.reader_mode = str(payload.get("reader_mode") or result.reader_mode)
        result.files.append(str(file))
        for index, row in enumerate(payload.get("paragraphs") or [], start=1):
            pid = str(row.get("pid") or "").strip()
            translation = str(row.get("translation") or "").strip()
            digest = str(row.get("text_sha256") or "").strip()
            if not pid or not translation or len(digest) != 64:
                result.issues.append(f"{file.name}#{index}: pid/译文/哈希不完整，已跳过")
                continue
            item = ParagraphTranslation(pid, digest, translation)
            previous = result.items.get(pid)
            if previous is None:
                result.items[pid] = item
            elif previous.translation != translation:
                result.issues.append(f"{file.name}#{index}: 段落 {pid} 译文冲突，保留首次")
    if not result.items:
        raise ReaderModeError("译文文件里没有一条可用译文，不能当作初级成品出书")
    return result

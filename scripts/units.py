"""§19 语义单位流程：程序切词 → 模型选 token ID → 程序回填 → 硬校验 → 审查 → 写回。

与 v4 的差别（本轮重点，针对 `taki` 截断事故）：

    * **位置由程序生成**：段落先切 token（稳定 ID、原文、半开区间、DOM 槽位），
      模型只返回 token ID，不再自己数字符；`taki` 这种子串不可能成为 token。
    * **回填不可覆盖**：surface/span 一律由第一/最后一个 token 回填；
      模型若附带 surface 只做一致性检查，不一致即 `surface_mismatch`。
    * **完整词边界硬校验**：同段、连续、成员落在父区间内、成员之间不重叠。
    * **词形关联硬校验**：已有 entry ID 只证明条目存在，必须验证与原文词形的
      原形/变体关系（taking→take、caught→catch）；不相关即 `entry_form_mismatch`。
    * **错误不复活**：判定 invalid 后不再清空 entry_ids 重新关联；父组合一并不可发布。
    * 跨内联标签的词（`tak<em>ing</em>`）明确记 `skipped_unsupported`，不输出残缺词。

协议版本：v5。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .chapters import detect_chapters, doc_chapter_map
from .epub_common import EpubError, TextSlot, is_excluded, local_name, open_epub, parse_xml
from .load_lexicon import (
    DEFAULT_LABEL_ORDER,
    Entry,
    Lexicon,
    ordered_labels,
    pick_label,
)
from .matching import find_matches, lemma_candidates, normalize_span, strip_possessive
from . import revisions
from .revisions import (
    IDENTIFY_PROMPT_VERSION,
    PROTOCOL,
    REVIEW_PROMPT_VERSION,
    SCHEMA_ANN_REQUEST,
    SCHEMA_DECISIONS as SCHEMA_ANN_DECISION,
    SCHEMA_REVIEW as SCHEMA_ANN_REVIEW,
    SCHEMA_REVIEW_REQUEST,
    SCHEMA_SCOPE,
)
from .tokens import (
    TOKENIZER_VERSION,
    Sentence,
    Token,
    build_tokens,
    split_sentences,
    token_id,
    tokenize,
)

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "dd", "dt"}

#: 常见转换器（Calibre 等）用 <div> 承载正文，同时留空 <p> 做垂直间距。
#: 这类容器也要当段落候选，但必须让位于其内部的 <p>，否则会重复计数。
CONTAINER_TAGS = {"div", "section", "article", "main"}

USAGE_CATEGORIES = {"专": "专业术语", "书": "书面语", "俚": "俚语", "方": "方言", "名": "专有名称"}

#: 11-11 读者拍板：书名、章节名、标题一律不翻译（初级档只译正文自然段）。
TITLE_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_OPS_NS = "http://www.idpf.org/2007/ops"
_TITLE_TYPE_HINTS = ("titlepage", "title", "subtitle", "halftitle")
_LABEL_WS_RE = re.compile(r"\s+")


def _norm_title_text(value: str) -> str:
    return _LABEL_WS_RE.sub(" ", (value or "")).strip().casefold()


def is_title_block(
    element, chapter_label: str = "", *, first_in_chapter: bool = False
) -> bool:
    """判断段落块是不是书名 / 章节名 / 标题（这类内容不翻译、当标题排版）。

    判据按可信度排列：标题标签 → `epub:type` 标题语义 → class 含 title →
    祖先容器声明 titlepage → 章首段与章节名同文。任何一条命中即视为标题。
    """

    if element is None or not isinstance(element.tag, str):
        return False
    if local_name(element).lower() in TITLE_TAGS:
        return True

    def typed(node) -> bool:
        if node is None or not isinstance(node.tag, str):
            return False
        raw = (node.get(f"{{{_OPS_NS}}}type") or "").lower()
        if raw and any(hint in raw for hint in _TITLE_TYPE_HINTS):
            return True
        cls = (node.get("class") or "").lower()
        return "title" in cls or "titlepage" in cls

    if typed(element):
        return True
    parent = element.getparent()
    depth = 0
    while parent is not None and depth < 4:
        if typed(parent):
            return True
        parent = parent.getparent()
        depth += 1

    if first_in_chapter and chapter_label:
        text = _norm_title_text("".join(element.itertext()))
        label = _norm_title_text(chapter_label)
        if text and label and len(text) <= 80:
            if text == label or text in label or label in text:
                return True
    return False

#: §21.2 提示必要性。unknown 表示没有可靠判断，任何档位都保守保留。
READING_NEEDS = ("required", "useful", "optional", "unknown")

SOURCE_ALIASES = {
    "lexicon": "exact_entry",
    "supplement": "out_of_lexicon",
    "component": "component_entries",
    "components": "component_entries",
    "exact": "exact_entry",
}


# ---------------------------------------------------------------------------
# 段落与 token
# ---------------------------------------------------------------------------


@dataclass
class Paragraph:
    pid: str
    doc: str
    index: int
    element: object
    text: str = ""
    slots: list[TextSlot] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    chapter: int = 0
    book_sha256: str = ""
    tokens: list[Token] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "text": self.text,
            "tokens": [t.as_dict() for t in self.tokens],
            "sentences": [s.as_dict() for s in self.sentences],
        }


def _paragraph_elements(body) -> list:
    """按 DOM 顺序取「最内层且有可读文本的块容器」作为段落。

    两种常见排版都要支持：

    * 标准 EPUB：正文在 <p>/<h*> 里，外层常套 <div>。此时取内层 <p>，
      外层 <div> 必须丢弃，否则同一段文字会被切两次。
    * Calibre 等转换器：用空的 ``<p class="calibre1"/>`` 做段间距，
      正文全放在 ``<div class="bs">`` 里。只认 <p> 会把整本书判成
      「没有可用段落」（《小王子》实测）。此时 <div> 就是段落。

    判据是「有没有更内层的候选」，而不是标签名白名单；空容器按无名文本
    直接排除，不参与编号。
    """

    candidates = [
        el
        for el in body.iter()
        if isinstance(el.tag, str)
        and local_name(el) in BLOCK_TAGS | CONTAINER_TAGS
        and "".join(el.itertext()).strip()
        and not is_excluded(el)
    ]
    if not candidates:
        return [
            el for el in body if isinstance(el.tag, str) and "".join(el.itertext()).strip()
        ]
    candidate_ids = {id(el) for el in candidates}
    return [
        el
        for el in candidates
        if not any(id(child) in candidate_ids for child in el.iterdescendants())
    ]


def _collect_slots(paragraph) -> list[TextSlot]:
    slots: list[TextSlot] = []

    def walk(el, excluded: str) -> None:
        reason = excluded or is_excluded(el)
        if el.text:
            slots.append(TextSlot(el, None, el.text, reason))
        for child in el:
            if not isinstance(child.tag, str):
                continue
            child_reason = excluded or is_excluded(child)
            walk(child, child_reason)
            if child.tail:
                slots.append(TextSlot(el, child, child.tail, child_reason))

    walk(paragraph, "")
    return [s for s in slots if s.writable]


def build_paragraphs_single(
    element, doc: str, chapter: int, index: int, book_sha256: str = ""
) -> list[Paragraph]:
    slots = _collect_slots(element)
    if not slots:
        return []
    parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for slot in slots:
        offsets.append(cursor)
        parts.append(slot.text)
        cursor += len(slot.text)
    text = "".join(parts)
    if not text.strip():
        return []
    paragraph = Paragraph(
        pid=f"{doc}#{index}",
        doc=doc,
        index=index,
        element=element,
        text=text,
        slots=slots,
        offsets=offsets,
        chapter=chapter,
        book_sha256=book_sha256,
    )
    paragraph.tokens = build_tokens(paragraph)
    paragraph.sentences = split_sentences(
        text,
        paragraph.tokens,
        doc=doc,
        paragraph_index=index,
        book_sha256=book_sha256,
    )
    return [paragraph]


def document_paragraphs(
    raw: bytes,
    doc: str,
    default_chapter: int,
    id_map: dict[str, int],
    book_sha256: str = "",
):
    tree = parse_xml(raw, doc)
    root = tree.getroot()
    body = None
    for el in root.iter():
        if local_name(el) == "body":
            body = el
            break
    if body is None:
        return tree, []

    paragraphs: list[Paragraph] = []
    current = default_chapter
    for index, element in enumerate(_paragraph_elements(body)):
        ident = element.get("id")
        if ident and ident in id_map:
            current = id_map[ident]
        paragraphs.extend(
            build_paragraphs_single(element, doc, current, index, book_sha256)
        )
    return tree, paragraphs


def slot_bounds(paragraph: Paragraph, span: tuple[int, int]) -> tuple[int, int] | None:
    start, end = span
    for index, offset in enumerate(paragraph.offsets):
        slot = paragraph.slots[index]
        if offset <= start < offset + len(slot.text):
            if end <= offset + len(slot.text):
                return index, start - offset
            return None
    return None


# ---------------------------------------------------------------------------
# 请求（identify）
# ---------------------------------------------------------------------------


INSTRUCTION_V5 = """\
你是英语词汇伴读标注器。位置由程序提供：**你只能选 token ID，不能自己数偏移、不能截拼写。**

工作顺序：先理解语境 → 确定意义单位 → 查词库 → 写短义。

一、选单位（只用 token ID）
1. 单词：选一个完整 token；`taki` 不是 token，`article` 里的 `art` 也不可选。
2. 短语/专名：选同段内**连续**的 token 区间（可以跨空格、标点、连字符）。
   冠词、介词等原文照样保留在区间里；章末成员只选有学习价值的词。
3. 每个标注给 `token_ids`；如果顺手填了 `surface`，它必须与这些 token 的原文完全一致，
   否则程序判 `surface_mismatch` 并拒收。

二、查词库（顺序：原词 → 词形还原 → 完整短语 → 组成词）
1. 整条表达命中 → `source_type=exact_entry`，`whole_entry_ids` 放真实条目 ID。
2. 只查得到组成词 → `source_type=component_entries`，`components` 里每个成员给
   `token_ids` + `entry_ids`；成员必须落在整体区间内且互不重叠。
3. 原词与还原形都查不到 → `source_type=out_of_lexicon`。
4. 所选词条必须与该处原文**词形相关**：taking→take、caught→catch、orders→order。
   选一个"存在但无关"的词条会被 `entry_form_mismatch` 拒收。
   拿不准原形时留空 entry_ids，程序会按词形在词库复核；不要硬塞。
5. 查不到本句义项 ≠ 查不到这个词：benevolent 用语境义时仍保留托福来源。

三、写短义
1. `short_gloss_zh` 只给该语境一个首要意思，忠于原句；组合必须覆盖整个区间
   （skin cancer 写"皮肤癌"，不能只写"癌"）。
2. `usage_category` 只用于 out_of_lexicon 且确实需要分类时：专/书/俚/方/名；
   普通词留空。
3. 判断不了就 `action=skip` 并写 reason。

输出 JSON：{"annotations": [...], "suppressed": [...]}
"""

#: 设计文档第 20 节：提示量档位在识别阶段影响选词，在渲染阶段影响显示。
#: 档位只写进 instruction，不进入 config_hash / task_id，因此切换档位不会让
#: 已完成的审查失效；识别出的条目数仍是任何档位的上限。
READING_LEVEL_NOTES = {
    "light": (
        "\n\n【提示量：轻提示】只选影响本句理解的难点：习语与固定搭配、专有名词、"
        "词库未收录的难词、语境义与常见义不同的词。透明常见词（只有四级/牛津3000 "
        "标签且词库首义即可读懂）不要选。宁缺勿滥。"
    ),
    "standard": (
        "\n\n【提示量：标准】沿用现有伴读尺度：影响理解的难词、习语、专名、"
        "容易误解的用法。不要为凑数量标注透明常见词。"
    ),
    "detailed": (
        "\n\n【提示量：详细】在标准基础上增加有学习价值的词与搭配（六级/雅思/托福/"
        "GRE 标签、常见但易混的用法），并给必要的语境说明；仍不逐词标注，"
        "不输出整句翻译。"
    ),
}


RESPONSE_SCHEMA_V5 = {
    "annotations": [
        {
            "annotation_id": "a-1",
            "pid": "content/book.xhtml#12",
            "token_ids": ["tk-1a2b3c4d5e", "tk-6f7a8b9c0d"],
            "unit_type": "phrase",
            "source_type": "component_entries",
            "whole_entry_ids": [],
            "components": [
                {"token_ids": ["tk-1a2b3c4d5e"], "entry_ids": ["benevolent|托福|adj."]},
                {"token_ids": ["tk-6f7a8b9c0d"], "entry_ids": ["skin|四级+雅思+牛津3000|n."]},
            ],
            "short_gloss_zh": "不凶险的皮肤癌",
            "usage_category": "",
            "usage_domain": "",
            "sense_key": "不凶险的皮肤癌",
            "evidence": "benevolent 在此形容皮肤癌不致命",
            "action": "annotate",
        }
    ],
    "suppressed": [{"pid": "...", "token_ids": ["tk-..."], "reason": "inside_phrase"}],
}


def build_request(
    book_title: str,
    book_sha256: str,
    lexicon_sha256: str,
    config: dict,
    config_hash: str,
    windows: list[dict],
    scope: dict | None = None,
    reading_level: str = "standard",
) -> dict:
    payload = {
        "schema": SCHEMA_ANN_REQUEST,
        "protocol": PROTOCOL,
        "stage": "identify",
        "tokenizer": TOKENIZER_VERSION,
        "identify_prompt": IDENTIFY_PROMPT_VERSION,
        "review_prompt": REVIEW_PROMPT_VERSION,
        "instruction": INSTRUCTION_V5 + READING_LEVEL_NOTES.get(reading_level, ""),
        "reading_level": reading_level,
        "response_schema": RESPONSE_SCHEMA_V5,
        "book": {"title": book_title, "sha256": book_sha256},
        "lexicon": {"sha256": lexicon_sha256},
        "scan": {**config, "hash": config_hash},
        "windows": windows,
    }
    if scope:
        payload.update(
            {
                "task_id": scope.get("task_id", ""),
                "context_pack_hash": scope.get("context_pack_hash", ""),
                "scope": {
                    "paragraph_count": scope.get("paragraph_count", 0),
                    "window_count": len(scope.get("windows") or []),
                },
            }
        )
    return payload


@dataclass
class Window:
    id: str
    chapter: int
    chapter_label: str
    paragraphs: list[Paragraph]

    def as_dict(self, lexicon: Lexicon) -> dict:
        return {
            "window_id": self.id,
            "chapter": self.chapter,
            "chapter_label": self.chapter_label,
            "paragraphs": [p.as_dict() for p in self.paragraphs],
            "program_hints": _hints(self.paragraphs, lexicon),
        }


def _hints(paragraphs: list[Paragraph], lexicon: Lexicon) -> list[dict]:
    """词库预匹配提示：给词条参考，不构成标注边界；携带 token_ids 方便直接选。"""

    from .annotate_epub import STOPLIST

    hints: list[dict] = []
    for paragraph in paragraphs:
        result = find_matches(paragraph.text, lexicon)
        for match in result.matches:
            if match.entry_key in STOPLIST:
                continue
            ids = [
                token.id
                for token in paragraph.tokens
                if token.start >= match.start and token.end <= match.end
            ]
            if not ids:
                continue
            entry = match.entries[0]
            hints.append(
                {
                    "pid": paragraph.pid,
                    "surface": match.surface,
                    "token_ids": ids,
                    "span": [match.start, match.end],
                    "lemma": entry.head,
                    "entry_ids": [e.entry_id for e in match.entries],
                    "pos": entry.pos,
                    "grade": entry.grade_raw,
                    "labels": entry.labels,
                    "labels_full": entry.labels_full,
                    "label_short": entry.labels_short,
                    "gloss": entry.gloss,
                }
            )
    return hints


def collect_windows(
    book_path: str | Path,
    lexicon: Lexicon,
    *,
    window_paragraphs: int = 4,
    max_paragraphs: int | None = None,
    paragraph_start: int = 0,
) -> tuple[list[Window], dict]:
    from .inspect_book import sha256_file

    book_sha = sha256_file(book_path)
    archive, package = open_epub(book_path)
    try:
        chapters = detect_chapters(archive, package)
        if not chapters:
            raise EpubError("无法确定章节结构")
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        label_by_index = {c.index: c.label for c in chapters}

        all_paragraphs: list[Paragraph] = []
        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            default, id_map = chapter_map.get(item.path, (0, {}))
            _, paragraphs = document_paragraphs(
                archive.read(item.path), item.path, default, id_map, book_sha
            )
            all_paragraphs.extend([p for p in paragraphs if p.chapter > 0])

        if paragraph_start:
            all_paragraphs = all_paragraphs[paragraph_start:]
        if max_paragraphs:
            all_paragraphs = all_paragraphs[:max_paragraphs]

        windows: list[Window] = []
        current: list[Paragraph] = []
        for paragraph in all_paragraphs:
            if current and (
                paragraph.chapter != current[-1].chapter
                or len(current) >= window_paragraphs
            ):
                windows.append(
                    Window(
                        id=f"w-{len(windows) + 1:04d}",
                        chapter=current[0].chapter,
                        chapter_label=label_by_index.get(current[0].chapter, ""),
                        paragraphs=current,
                    )
                )
                current = []
            current.append(paragraph)
        if current:
            windows.append(
                Window(
                    id=f"w-{len(windows) + 1:04d}",
                    chapter=current[0].chapter,
                    chapter_label=label_by_index.get(current[0].chapter, ""),
                    paragraphs=current,
                )
            )

        meta = {
            "title": package.title,
            "chapters": [c.as_dict() for c in chapters],
            "paragraphs": len(all_paragraphs),
            "windows": len(windows),
            "tokens": sum(len(p.tokens) for p in all_paragraphs),
            "tokenizer": TOKENIZER_VERSION,
        }
        return windows, meta
    finally:
        archive.close()


# ---------------------------------------------------------------------------
# 决策与审查
# ---------------------------------------------------------------------------


@dataclass
class Component:
    token_ids: list[str] = field(default_factory=list)
    surface: str = ""
    span: tuple[int, int] = (0, 0)
    lemma: str = ""
    entry_ids: list[str] = field(default_factory=list)
    gloss: str = ""
    pos: str = ""
    labels: list[str] = field(default_factory=list)
    example: str = ""
    extra: str = ""
    status: str = ""
    resolved: bool = False

    @property
    def head(self) -> str:
        return self.lemma or self.surface

    @property
    def labels_full(self) -> str:
        return "、".join(self.labels)

    @property
    def label_short(self) -> str:
        return pick_label(self.labels) if self.labels else ""

    def as_dict(self) -> dict:
        return {
            "token_ids": list(self.token_ids),
            "surface": self.surface,
            "span": list(self.span),
            "lemma": self.lemma,
            "entry_ids": list(self.entry_ids),
            "gloss": self.gloss,
            "pos": self.pos,
            "labels": list(self.labels),
            "labels_full": self.labels_full,
            "status": self.status,
            "resolved": self.resolved,
        }


@dataclass
class Annotation:
    annotation_id: str
    pid: str
    token_ids: list[str] = field(default_factory=list)
    surface: str = ""
    span: tuple[int, int] = (0, 0)
    echo_surface: str = ""
    lemma: str = ""
    unit_type: str = "word"
    source_type: str = "out_of_lexicon"
    whole_entry_ids: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    gloss: str = ""
    #: 读者可见的学习说明（词性、变形、搭配、语境用法）；渲染器只认这个字段
    reader_usage_note: str = ""
    #: 审计备注（程序提示、词库匹配过程、跳过与修正原因），只进报告
    audit_note: str = ""
    #: §21.2 提示必要性：由语境识别给出、独立审查核对，决定轻提示能否省略。
    #: required / useful / optional / unknown；缺失按 unknown 保守保留。
    reading_need: str = ""
    #: §21.2 新义或特殊用法保护：真时任何档位与名单都不得省略
    sense_protected: bool = False
    usage_category: str = ""
    usage_domain: str = ""
    sense_key: str = ""
    evidence: str = ""
    action: str = "annotate"
    reason: str = ""
    window_id: str = ""
    revision_hash: str = ""
    reviewed_hash: str = ""
    review_history: list[dict] = field(default_factory=list)
    review_status: str = ""
    review_issues: list[str] = field(default_factory=list)
    invalid_codes: list[str] = field(default_factory=list)
    skip_reason: str = ""
    source_status: str = ""
    label_scope: str = ""
    display_label: str = ""
    labels_full: str = ""
    whole_entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def usage(self) -> str:
        """兼容旧字段名：读者说明就是 ``reader_usage_note``。"""

        return self.reader_usage_note

    @usage.setter
    def usage(self, value: str) -> None:
        self.reader_usage_note = value

    @property
    def body_tag(self) -> str:
        if self.display_label:
            return self.display_label
        if self.source_status == "out_of_lexicon" and self.usage_category:
            return self.usage_category
        return ""

    def as_dict(self) -> dict:
        return annotation_to_dict(self)


@dataclass
class AnnotationSet:
    annotations: list[Annotation] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    book_sha256: str = ""
    lexicon_sha256: str = ""
    config_hash: str = ""
    protocol: str = ""
    tokenizer: str = ""
    identify_prompt: str = ""
    review_prompt: str = ""
    context_pack_hash: str = ""
    task_id: str = ""
    #: 每个识别分片声明的窗口覆盖：{window_id, status, candidates, annotations, reason}
    windows: list[dict] = field(default_factory=list)

    def by_id(self) -> dict[str, Annotation]:
        return {a.annotation_id: a for a in self.annotations}

    def binding(self) -> dict:
        return {
            "protocol": self.protocol or PROTOCOL,
            "tokenizer": self.tokenizer or TOKENIZER_VERSION,
            "book_sha256": self.book_sha256,
            "lexicon_sha256": self.lexicon_sha256,
            "config_hash": self.config_hash,
            "identify_prompt": self.identify_prompt or IDENTIFY_PROMPT_VERSION,
            "review_prompt": self.review_prompt or REVIEW_PROMPT_VERSION,
            "context_pack_hash": self.context_pack_hash,
        }

    def summary(self) -> dict:
        return {
            "files": len(self.files),
            "annotations": len(self.annotations),
            "suppressed": len(self.suppressed),
            "issues": len(self.issues),
            "issues_sample": self.issues[:5],
            "windows": len(self.windows),
            "task_id": self.task_id,
        }


def _files(path: Path, prefix: str) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"目录不存在：{path}")
    return sorted(p for p in path.glob(f"{prefix}*.json"))


def annotation_fingerprint(annotation: Annotation) -> dict:
    """参与内容哈希的字段（读者输出 + 采用决定）。"""

    return {
        "annotation_id": annotation.annotation_id,
        "pid": annotation.pid,
        "token_ids": list(annotation.token_ids),
        "unit_type": annotation.unit_type,
        "source_type": annotation.source_type,
        "whole_entry_ids": list(annotation.whole_entry_ids),
        "components": [
            {"token_ids": list(c.token_ids), "entry_ids": list(c.entry_ids)}
            for c in annotation.components
        ],
        "short_gloss_zh": annotation.gloss,
        "reader_usage_note": annotation.reader_usage_note,
        "reading_need": annotation.reading_need,
        "sense_protected": bool(annotation.sense_protected),
        "usage_category": annotation.usage_category,
        "usage_domain": annotation.usage_domain,
        "sense_key": annotation.sense_key,
        "action": annotation.action,
        "reason": annotation.reason,
        "skip_reason": annotation.skip_reason,
        "evidence": annotation.evidence,
    }


def _binding_from_payload(payload: dict, file: str) -> dict:
    scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else {}
    binding = {
        "protocol": str(payload.get("protocol") or ""),
        "tokenizer": str(payload.get("tokenizer") or ""),
        "book_sha256": str(payload.get("book_sha256") or ""),
        "lexicon_sha256": str(payload.get("lexicon_sha256") or ""),
        "config_hash": str(payload.get("config_hash") or scan.get("hash") or ""),
        "identify_prompt": str(payload.get("identify_prompt") or ""),
        "review_prompt": str(payload.get("review_prompt") or ""),
        "context_pack_hash": str(payload.get("context_pack_hash") or ""),
    }
    missing = [key for key, value in binding.items() if not value]
    if missing:
        raise EpubError(
            f"分片缺少版本绑定（{file}：{', '.join(missing)}）。"
            "v5 及更早的识别/审查没有内容版本哈希，不能沿用旧审查；"
            "请用当前版本重新生成请求、识别与审查。"
        )
    if binding["protocol"] != PROTOCOL:
        raise EpubError(
            f"分片协议不匹配（{file}：{binding['protocol']}），当前需要 {PROTOCOL}；"
            "请重新生成请求、识别与审查。"
        )
    if binding["tokenizer"] != TOKENIZER_VERSION:
        raise EpubError(
            f"切词器版本不一致（{file}：{binding['tokenizer']}，"
            f"当前 {TOKENIZER_VERSION}）；请重新生成请求。"
        )
    return binding


def _merge_binding(target: dict, incoming: dict, file: str) -> None:
    if not target:
        target.update(incoming)
        return
    diffs = [key for key in incoming if target.get(key) != incoming[key]]
    if diffs:
        raise EpubError(
            f"分片版本绑定与首片不一致（{file}：{', '.join(diffs)}）。"
            "不同书籍/词表/配置/上下文的结论不能混用。"
        )


def load_annotations(
    path: str | Path, expected_binding: dict | None = None
) -> AnnotationSet:
    result = AnnotationSet()
    binding: dict = {}
    for file in _files(Path(path), "ann"):
        result.files.append(str(file))
        payload = _read_json(file, result.issues)
        if payload is None:
            raise EpubError(f"识别分片无法解析（{file}），严格模式拒绝继续")
        if payload.get("schema") != SCHEMA_ANN_DECISION:
            raise EpubError(
                f"识别结果协议不匹配（{payload.get('schema')}），"
                f"当前需要 {SCHEMA_ANN_DECISION}；请重新生成请求与识别"
            )
        incoming = _binding_from_payload(payload, str(file))
        if expected_binding:
            diffs = [k for k in incoming if incoming[k] != expected_binding.get(k)]
            if diffs:
                raise EpubError(
                    f"识别分片与当前任务不一致（{file}：{', '.join(diffs)}），拒绝应用"
                )
        _merge_binding(binding, incoming, str(file))
        for item in payload.get("annotations") or []:
            if not isinstance(item, dict):
                continue
            annotation = _parse_annotation(item, str(file), result.issues)
            if annotation is not None:
                result.annotations.append(annotation)
        for item in payload.get("suppressed") or []:
            if isinstance(item, dict) and item.get("pid"):
                result.suppressed.append(item)
        for item in payload.get("windows") or []:
            if not isinstance(item, dict) or not item.get("window_id"):
                raise EpubError(f"识别分片的窗口记录不完整（{file}）")
            result.windows.append(
                {
                    "window_id": str(item.get("window_id")),
                    "status": str(item.get("status") or "identified"),
                    "candidates": int(item.get("candidates") or 0),
                    "annotations": [str(x) for x in (item.get("annotations") or [])],
                    "reason": str(item.get("reason") or ""),
                    "file": str(file),
                }
            )

    result.protocol = binding.get("protocol", PROTOCOL)
    result.tokenizer = binding.get("tokenizer", TOKENIZER_VERSION)
    result.book_sha256 = binding.get("book_sha256", "")
    result.lexicon_sha256 = binding.get("lexicon_sha256", "")
    result.config_hash = binding.get("config_hash", "")
    result.identify_prompt = binding.get("identify_prompt", "")
    result.review_prompt = binding.get("review_prompt", "")
    result.context_pack_hash = binding.get("context_pack_hash", "")

    seen: set[str] = set()
    for annotation in result.annotations:
        if annotation.annotation_id in seen:
            annotation.invalid_codes.append("duplicate_annotation_id")
        seen.add(annotation.annotation_id)
    return result


@dataclass
class ReviewSet:
    verdicts: dict[str, list[dict]] = field(default_factory=dict)
    windows: dict[str, list[dict]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    binding: dict = field(default_factory=dict)
    task_id: str = ""
    reviewers: list[dict] = field(default_factory=list)
    rounds: list[int] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "files": len(self.files),
            "verdicts": sum(len(v) for v in self.verdicts.values()),
            "window_verdicts": len(self.windows),
            "issues": len(self.issues),
            "task_id": self.task_id,
            "rounds": sorted(set(self.rounds)),
            "reviewers": self.reviewers,
        }


def load_review(
    path: str | Path, expected_binding: dict | None = None
) -> ReviewSet:
    result = ReviewSet()
    for file in _files(Path(path), "review"):
        result.files.append(str(file))
        payload = _read_json(file, result.issues)
        if payload is None:
            raise EpubError(f"审查分片无法解析（{file}），严格模式拒绝继续")
        if payload.get("schema") != SCHEMA_ANN_REVIEW:
            raise EpubError(
                f"审查协议不匹配（{payload.get('schema')}），"
                f"当前需要 {SCHEMA_ANN_REVIEW}；请重新审查。"
            )
        incoming = _binding_from_payload(payload, str(file))
        if expected_binding:
            diffs = [k for k in incoming if incoming[k] != expected_binding.get(k)]
            if diffs:
                raise EpubError(
                    f"审查分片与当前任务不一致（{file}：{', '.join(diffs)}），拒绝应用"
                )
        _merge_binding(result.binding, incoming, str(file))
        task = str(payload.get("task_id") or "")
        if not task:
            raise EpubError(f"审查分片缺少 task_id（{file}）")
        if result.task_id and task != result.task_id:
            raise EpubError(
                f"审查分片对应的任务不一致（{file}：{task} != {result.task_id}）"
            )
        result.task_id = task
        if isinstance(payload.get("reviewer"), dict):
            result.reviewers.append(payload["reviewer"])
        try:
            result.rounds.append(int(payload.get("review_round") or 0))
        except (TypeError, ValueError):
            result.rounds.append(0)
        for item in payload.get("window_verdicts") or []:
            if not isinstance(item, dict):
                continue
            wid = str(item.get("window_id") or "").strip()
            if not wid:
                result.issues.append({"file": str(file), "reason": "窗口审查缺少 window_id"})
                continue
            result.windows.setdefault(wid, []).append(item)
        for item in payload.get("verdicts") or payload.get("annotations") or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("annotation_id") or item.get("id") or "").strip()
            if not ident:
                result.issues.append({"file": str(file), "reason": "审查条目缺少 annotation_id"})
                continue
            result.verdicts.setdefault(ident, []).append(item)
    return result


def _read_json(file: Path, issues: list[dict]) -> dict | None:
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        issues.append({"file": str(file), "reason": f"JSON 无法解析：{exc}"})
        return None
    if not isinstance(payload, dict):
        issues.append({"file": str(file), "reason": "顶层必须是对象"})
        return None
    return payload


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是")
    return bool(value)


def _parse_component(item: dict) -> Component | None:
    if not isinstance(item, dict):
        return None
    ids = [str(x) for x in (item.get("token_ids") or [])]
    if not ids:
        return None
    return Component(
        token_ids=ids,
        lemma=str(item.get("lemma") or ""),
        entry_ids=[str(x) for x in (item.get("entry_ids") or item.get("lexicon_entry_ids") or [])],
    )


def _parse_annotation(item: dict, file: str, issues: list[dict]) -> Annotation | None:
    ident = str(item.get("annotation_id") or item.get("id") or "").strip()
    pid = str(item.get("pid") or "").strip()
    token_ids = [str(x) for x in (item.get("token_ids") or [])]
    if not ident or not pid:
        issues.append({"file": file, "reason": f"字段不完整：{ident or '?'}"})
        return None
    if not token_ids:
        annotation = Annotation(annotation_id=ident, pid=pid)
        annotation.invalid_codes.append("missing_token_ids")
        return annotation
    source = str(item.get("source_type") or "out_of_lexicon").strip()
    source = SOURCE_ALIASES.get(source, source)
    if source not in ("exact_entry", "component_entries", "out_of_lexicon"):
        issues.append({"file": file, "reason": f"{ident}: 未知 source_type={source}"})
        source = "out_of_lexicon"

    category = str(item.get("usage_category") or "").strip()
    if category and category not in USAGE_CATEGORIES:
        issues.append({"file": file, "reason": f"{ident}: 未知用法类别 {category}（已忽略）"})
        category = ""

    need = str(item.get("reading_need") or "").strip()
    if need and need not in READING_NEEDS:
        issues.append({"file": file, "reason": f"{ident}: 未知 reading_need={need}（按 unknown 处理）"})
        need = "unknown"

    return Annotation(
        annotation_id=ident,
        pid=pid,
        token_ids=token_ids,
        echo_surface=str(item.get("surface") or ""),
        lemma=str(item.get("lemma") or ""),
        unit_type=str(item.get("unit_type") or "word"),
        source_type=source,
        whole_entry_ids=[
            str(x) for x in (item.get("whole_entry_ids") or item.get("lexicon_entry_ids") or [])
        ],
        components=[c for c in (_parse_component(x) for x in (item.get("components") or [])) if c],
        gloss=str(item.get("short_gloss_zh") or "").strip(),
        reader_usage_note=str(
            item.get("reader_usage_note") or item.get("usage_note") or ""
        ).strip(),
        audit_note=str(item.get("audit_note") or "").strip(),
        reading_need=need,
        sense_protected=_as_bool(item.get("sense_protected")),
        usage_category=category,
        usage_domain=str(item.get("usage_domain") or "").strip(),
        sense_key=str(item.get("sense_key") or "").strip(),
        evidence=str(item.get("evidence") or "").strip(),
        action=str(item.get("action") or "annotate"),
        reason=str(item.get("reason") or ""),
        window_id=str(item.get("window_id") or "").strip(),
        revision_hash=str(item.get("revision_hash") or "").strip(),
        skip_reason=str(item.get("skip_reason") or "").strip(),
    )


#: 审查可以修改的字段；annotation_id/pid/token_ids 属于定位，不允许被改
REVISABLE_FIELDS = {
    "short_gloss_zh",
    "reader_usage_note",
    "usage_note",
    "reading_need",
    "sense_protected",
    "usage_category",
    "usage_domain",
    "sense_key",
    "components",
    "whole_entry_ids",
    "unit_type",
    "source_type",
    "action",
    "reason",
    "skip_reason",
    "evidence",
    "audit_note",
}

MAX_REVIEW_ROUNDS = 4


def _clone_with_revision(
    annotation: Annotation, revised: dict, issues: list[dict]
) -> Annotation | None:
    data = annotation_to_dict(annotation)
    data.update({key: value for key, value in revised.items() if key in REVISABLE_FIELDS})
    data["annotation_id"] = annotation.annotation_id
    data["pid"] = annotation.pid
    data["token_ids"] = list(annotation.token_ids)
    patched = _parse_annotation(data, "review", issues)
    if patched is None:
        return None
    patched.invalid_codes = list(annotation.invalid_codes)
    patched.window_id = annotation.window_id
    patched.review_history = list(annotation.review_history)
    return patched


def apply_review(
    annotations: AnnotationSet, review: "ReviewSet | dict[str, list[dict]]"
) -> AnnotationSet:
    """按内容版本应用审查。

    只有 ``reviewed_revision_hash`` 与当前内容哈希完全相同的 verdict 才生效。
    修改建议会生成新版本（新哈希），旧 pass 不会自动延续；第二轮必须明确
    针对新哈希给出 pass。
    """

    if isinstance(review, dict):
        review = ReviewSet(verdicts=review)
    merged = AnnotationSet(
        annotations=[],
        suppressed=annotations.suppressed,
        files=annotations.files,
        issues=annotations.issues,
        book_sha256=annotations.book_sha256,
        lexicon_sha256=annotations.lexicon_sha256,
        config_hash=annotations.config_hash,
        protocol=annotations.protocol,
        tokenizer=annotations.tokenizer,
        identify_prompt=annotations.identify_prompt,
        review_prompt=annotations.review_prompt,
        context_pack_hash=annotations.context_pack_hash,
        task_id=annotations.task_id,
        windows=annotations.windows,
    )
    binding = annotations.binding()
    for annotation in annotations.annotations:
        current = annotation
        current.review_status = "missing"
        current.reviewed_hash = ""
        for _ in range(MAX_REVIEW_ROUNDS):
            rounds = review.verdicts.get(current.annotation_id) or []
            matched = [
                verdict
                for verdict in rounds
                if str(verdict.get("reviewed_revision_hash") or "")
                == current.revision_hash
            ]
            if not matched:
                break
            statuses = {str(v.get("review_status") or "") for v in matched}
            if len(statuses) > 1:
                current.review_status = "conflict"
                current.review_issues = [
                    "同一内容版本存在冲突判定：" + "、".join(sorted(statuses))
                ]
                break
            verdict = matched[-1]
            status = statuses.pop()
            current.reviewed_hash = current.revision_hash
            current.review_issues = [str(x) for x in (verdict.get("issues") or [])]
            if status == "pass":
                current.review_status = "pass"
                break
            if status == "reject":
                current.review_status = "reject"
                break
            if status == "modify":
                revised = verdict.get("revised")
                if not isinstance(revised, dict) or not revised:
                    current.review_status = "invalid_revision"
                    break
                patched = _clone_with_revision(current, revised, merged.issues)
                if patched is None:
                    current.review_status = "invalid_revision"
                    break
                patched.revision_hash = revisions.revision_hash(
                    annotation_fingerprint(patched), binding
                )
                patched.review_history = list(current.review_history) + [
                    {
                        "annotation_id": current.annotation_id,
                        "from": current.revision_hash,
                        "to": patched.revision_hash,
                        "issues": list(current.review_issues),
                        "revised": revised,
                    }
                ]
                patched.review_status = "modify_pending"
                current = patched
                continue
            current.review_status = (
                "pending" if status == "pending" else "invalid_status"
            )
            break
        merged.annotations.append(current)
    return merged


REVIEW_INSTRUCTION = """\
你是独立语义审查者。对每个待审标注和每个窗口，分别给出结论。

一、逐条审查（verdicts）
1. 只依据给出的原句、段落上下文、词库资料判断；不要臆测书外情节。
2. 检查：短义是否忠于本句语境；解释是否越出所声明区间（单词不得借用区间外的动作/修饰）；
   组合是否覆盖整个区间且没有过度合并；词条与原文词形是否相关；等级/词性/例句是否来自词库。
3. `reader_usage_note` 只能写有助学习的信息（词性、变形、搭配、指代、语境用法）；
   程序提示、词库匹配过程、跳过原因属于 audit，不得写进读者说明。
4. 必须回填 `reviewed_revision_hash`（照抄待审项的 `revision_hash`）。
5. `pass` 表示当前版本可发布；`modify` 必须在 `revised` 里给出修改后的字段；
   `reject` 表示不应采用；`pending` 表示信息不足、需补充上下文。
6. 修改后会产生新的 revision_hash，下一轮必须明确审查新哈希，旧 pass 不会延续。

二、窗口覆盖（window_verdicts）
1. 看完整窗口正文与未选中段落，判断是否漏掉影响理解场景/动作/修辞的重要词或短语。
2. `coverage_status=pass` 表示覆盖合理；`missing` 需在 `missed` 里列出建议补充的单位
   （给 surface 与理由）；`over_merged` 表示存在过度合并。
3. 必须回填 `reviewed_window_hash`。

输出 JSON：{"window_verdicts": [...], "verdicts": [...]}。
"""

RESPONSE_SCHEMA_REVIEW = {
    "window_verdicts": [
        {
            "window_id": "w-0001",
            "reviewed_window_hash": "wh-...",
            "coverage_status": "pass",
            "issues": [],
            "missed": [{"surface": "...", "reason": "..."}],
        }
    ],
    "verdicts": [
        {
            "annotation_id": "a-...",
            "reviewed_revision_hash": "rh-...",
            "review_status": "pass",
            "issues": [],
            "revised": {"short_gloss_zh": "..."},
        }
    ],
}


def build_scope(
    windows: list["Window"],
    lexicon: Lexicon,
    binding: dict,
    *,
    max_paragraphs: int = 0,
    window_paragraphs: int = 0,
    paragraph_start: int = 0,
) -> dict:
    """冻结任务范围：窗口、段落、分片与各自哈希。"""

    entries = []
    for window in windows:
        payload = window.as_dict(lexicon)
        entries.append(
            {
                "window_id": window.id,
                "chapter": window.chapter,
                "chapter_label": window.chapter_label,
                "pids": [p.pid for p in window.paragraphs],
                "hash": revisions.window_hash(
                    window.id,
                    [p.as_dict() for p in window.paragraphs],
                    payload["program_hints"],
                ),
            }
        )
    scope = {
        "schema": SCHEMA_SCOPE,
        "protocol": PROTOCOL,
        "tokenizer": TOKENIZER_VERSION,
        "book_sha256": str(binding.get("book_sha256") or ""),
        "lexicon_sha256": str(binding.get("lexicon_sha256") or ""),
        "config_hash": str(binding.get("config_hash") or ""),
        "identify_prompt": IDENTIFY_PROMPT_VERSION,
        "review_prompt": REVIEW_PROMPT_VERSION,
        "context_pack_hash": revisions.context_pack_hash([e["hash"] for e in entries]),
        "max_paragraphs": int(max_paragraphs or 0),
        "window_paragraphs": int(window_paragraphs or 0),
        "paragraph_start": int(paragraph_start or 0),
        "paragraph_count": sum(len(e["pids"]) for e in entries),
        "windows": entries,
        "pids": [pid for entry in entries for pid in entry["pids"]],
    }
    scope["task_id"] = revisions.task_id(scope)
    return scope


def load_scope(path: str | Path) -> dict:
    folder = Path(path)
    file = folder / "scope.json" if folder.is_dir() else folder
    if not file.exists():
        raise EpubError(
            "缺少任务清单 scope.json：无法证明任务范围完整。"
            "请用当前版本重新运行 --emit-annotations。"
        )
    try:
        scope = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EpubError(f"任务清单无法解析（{file}）：{exc}") from exc
    if scope.get("schema") != SCHEMA_SCOPE:
        raise EpubError(
            f"任务清单协议不匹配（{scope.get('schema')}），当前需要 {SCHEMA_SCOPE}"
        )
    if revisions.task_id(scope) != scope.get("task_id"):
        raise EpubError("任务清单哈希与内容不一致（scope.json 被修改），停止处理")
    return scope


def freeze_revisions(annotations: AnnotationSet, scope: dict) -> AnnotationSet:
    """按任务清单绑定上下文，并冻结每条内容哈希。"""

    windows = {w.get("window_id"): w for w in scope.get("windows") or []}
    pid_to_window: dict[str, str] = {}
    for wid, window in windows.items():
        for pid in window.get("pids") or []:
            pid_to_window[pid] = wid
    annotations.task_id = str(scope.get("task_id") or "")
    annotations.context_pack_hash = str(scope.get("context_pack_hash") or "")
    annotations.book_sha256 = str(scope.get("book_sha256") or annotations.book_sha256)
    annotations.lexicon_sha256 = str(
        scope.get("lexicon_sha256") or annotations.lexicon_sha256
    )
    annotations.config_hash = str(scope.get("config_hash") or annotations.config_hash)
    annotations.identify_prompt = str(
        scope.get("identify_prompt") or IDENTIFY_PROMPT_VERSION
    )
    annotations.review_prompt = str(scope.get("review_prompt") or REVIEW_PROMPT_VERSION)
    binding = annotations.binding()
    for annotation in annotations.annotations:
        declared = annotation.revision_hash
        wid = annotation.window_id or pid_to_window.get(annotation.pid, "")
        if not wid or wid not in windows:
            annotation.invalid_codes.append("unbound_window")
            continue
        expected = pid_to_window.get(annotation.pid)
        if expected is not None and expected != wid:
            annotation.invalid_codes.append("window_mismatch")
            continue
        annotation.window_id = wid
        computed = revisions.revision_hash(annotation_fingerprint(annotation), binding)
        if declared and declared != computed:
            annotation.invalid_codes.append("revision_hash_mismatch")
        annotation.revision_hash = computed
    return annotations


def _sentence_for(paragraph: "Paragraph", span: tuple[int, int]) -> str:
    start, end = span
    for sentence in paragraph.sentences:
        if sentence.start <= start and end <= sentence.end:
            return paragraph.text[sentence.start : sentence.end].strip()
    return paragraph.text


def _unselected_fragments(paragraph: "Paragraph", spans: list[tuple[int, int]]) -> list[str]:
    fragments: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start > cursor:
            piece = paragraph.text[cursor:start].strip()
            if piece:
                fragments.append(piece)
        cursor = max(cursor, end)
    tail = paragraph.text[cursor:].strip()
    if tail:
        fragments.append(tail)
    return fragments


def _note_preview(annotation: Annotation, entry_map: dict[str, Entry]) -> dict:
    whole = [entry_map[eid] for eid in annotation.whole_entry_ids if eid in entry_map]
    components = []
    for component in annotation.components:
        entry = next(
            (entry_map[eid] for eid in component.entry_ids if eid in entry_map), None
        )
        components.append(
            {
                "surface": component.surface or "",
                "lemma": entry.head if entry else component.lemma,
                "entry_ids": list(component.entry_ids),
                "labels_full": entry.labels_full if entry else "",
                "pos": entry.pos if entry else "",
                "gloss": entry.gloss if entry else "",
                "example": entry.example if entry else "",
                "extra": entry.extra if entry else "",
            }
        )
    primary = whole[0] if whole else None
    return {
        "short_gloss_zh": annotation.gloss,
        "reader_usage_note": annotation.reader_usage_note,
        "reading_need": annotation.reading_need or "unknown",
        "sense_protected": bool(annotation.sense_protected),
        "source_type": annotation.source_type,
        "unit_type": annotation.unit_type,
        "head": primary.head if primary else "",
        "labels_full": primary.labels_full if primary else "",
        "pos": primary.pos if primary else "",
        "full_gloss": primary.gloss if primary else "",
        "example": primary.example if primary else "",
        "extra": primary.extra if primary else "",
        "components": components,
    }


def build_review_package(
    windows: list["Window"],
    annotations: AnnotationSet,
    scope: dict,
    lexicon: Lexicon,
    *,
    review_round: int = 1,
    max_windows: int = 2,
) -> list[dict]:
    """导出独立审查包：完整原句、未选中正文、拟输出内容与版本哈希。"""

    paragraphs = {p.pid: p for window in windows for p in window.paragraphs}
    index = token_index(paragraphs)
    entry_map = {entry.entry_id: entry for entry in lexicon.all_entries}
    by_pid: dict[str, list[Annotation]] = {}
    for annotation in annotations.annotations:
        if annotation.action != "annotate":
            continue
        if annotation.invalid_codes:
            continue
        if annotation.pid not in paragraphs:
            continue
        backfill(annotation, paragraphs, index)
        # 审查者需要看到成员的真实词形与区间；成员回填失败不在这里判死，
        # 由后续硬校验给出 member_* 错误码。
        if annotation.components:
            backfill_components(annotation, paragraphs[annotation.pid], index)
        by_pid.setdefault(annotation.pid, []).append(annotation)

    suppressed_by_pid: dict[str, list[dict]] = {}
    for item in annotations.suppressed:
        suppressed_by_pid.setdefault(str(item.get("pid") or ""), []).append(item)

    packages: list[dict] = []
    chunk: list[dict] = []
    for window in windows:
        wid = window.id
        window_meta = next(
            (w for w in scope.get("windows") or [] if w.get("window_id") == wid), None
        )
        if window_meta is None:
            continue
        proposed = []
        for paragraph in window.paragraphs:
            spans = [a.span for a in by_pid.get(paragraph.pid, [])]
            for annotation in sorted(
                by_pid.get(paragraph.pid, []), key=lambda a: a.span[0]
            ):
                proposed.append(
                    {
                        "annotation_id": annotation.annotation_id,
                        "revision_hash": annotation.revision_hash,
                        "pid": annotation.pid,
                        "surface": annotation.surface,
                        "span": list(annotation.span),
                        "unit_type": annotation.unit_type,
                        "source_type": annotation.source_type,
                        "evidence": annotation.evidence,
                        "audit_note": annotation.audit_note,
                        "proposed_output": _note_preview(annotation, entry_map),
                        "context": {
                            "sentence": _sentence_for(paragraph, annotation.span),
                            "paragraph": paragraph.text,
                            "unselected_text": _unselected_fragments(
                                paragraph, spans
                            ),
                        },
                    }
                )
        unselected_paragraphs = [
            {"pid": paragraph.pid, "text": paragraph.text}
            for paragraph in window.paragraphs
            if not by_pid.get(paragraph.pid)
        ]
        chunk.append(
            {
                "window_id": wid,
                "window_hash": window_meta.get("hash", ""),
                "chapter": window.chapter,
                "chapter_label": window.chapter_label,
                "paragraphs": [
                    {"pid": p.pid, "text": p.text} for p in window.paragraphs
                ],
                "proposed": proposed,
                "unselected_paragraphs": unselected_paragraphs,
                "suppressed": [
                    item
                    for paragraph in window.paragraphs
                    for item in suppressed_by_pid.get(paragraph.pid, [])
                ],
            }
        )
        if len(chunk) >= max_windows:
            packages.append(chunk)
            chunk = []
    if chunk:
        packages.append(chunk)

    binding = annotations.binding()
    payloads = []
    for number, windows_chunk in enumerate(packages, start=1):
        payloads.append(
            {
                "schema": SCHEMA_REVIEW_REQUEST,
                "protocol": PROTOCOL,
                "stage": "review",
                "task_id": scope.get("task_id", ""),
                "review_round": review_round,
                "binding": binding,
                "instruction": REVIEW_INSTRUCTION,
                "response_schema": RESPONSE_SCHEMA_REVIEW,
                "windows": windows_chunk,
            }
        )
    return payloads


def precheck_units(
    book_path: str | Path, lexicon: Lexicon, annotations: AnnotationSet
) -> tuple[list[Annotation], dict, dict[str, "Paragraph"]]:
    """渲染前的结构预校验：让发布门槛能看到 invalid / 未审查，而不是等渲染才失败。"""

    from .inspect_book import sha256_file

    book_sha = sha256_file(book_path)
    archive, package = open_epub(book_path)
    try:
        chapters = detect_chapters(archive, package)
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        paragraphs: dict[str, Paragraph] = {}
        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            default, id_map = chapter_map.get(item.path, (0, {}))
            _, plist = document_paragraphs(
                archive.read(item.path), item.path, default, id_map, book_sha
            )
            for paragraph in plist:
                if paragraph.chapter > 0:
                    paragraphs[paragraph.pid] = paragraph
        usable, stats = validate_annotations(annotations, paragraphs, lexicon)
        return usable, stats, paragraphs
    finally:
        archive.close()


def evaluate_gate(
    annotations: AnnotationSet,
    scope: dict,
    review: ReviewSet,
    *,
    allow_partial: bool = False,
    usable_count: int | None = None,
) -> dict:
    """任务范围 + 覆盖审查 + 逐条审查的发布门槛。

    与 ``validate_annotations`` 的分工：这里判断「是否被完整审查、范围是否
    覆盖」，结构/词形/来源由 validate 判断。两者都通过才可发布。
    """

    scope_windows = {str(w.get("window_id")): w for w in scope.get("windows") or []}
    identified: dict[str, list[dict]] = {}
    for record in annotations.windows:
        identified.setdefault(str(record.get("window_id")), []).append(record)
    reviewed: dict[str, list[dict]] = {}
    for wid, items in review.windows.items():
        reviewed.setdefault(wid, []).extend(items)

    missing_scope: list[dict] = []
    for wid, window in scope_windows.items():
        if wid not in identified:
            missing_scope.append({"window_id": wid, "reason": "missing_identify"})
            continue
        if wid not in reviewed:
            missing_scope.append({"window_id": wid, "reason": "missing_coverage_review"})
            continue
        if not any(
            str(item.get("reviewed_window_hash") or "") == str(window.get("hash") or "")
            for item in reviewed[wid]
        ):
            missing_scope.append({"window_id": wid, "reason": "stale_coverage_review"})
    scope_complete = not missing_scope

    pending_review: list[dict] = []
    rejected_items: list[dict] = []
    annotate_count = sum(
        1 for annotation in annotations.annotations if annotation.action == "annotate"
    )
    for annotation in annotations.annotations:
        if annotation.action != "annotate":
            continue
        if annotation.review_status == "reject":
            # 审查明确否决 = 已确认的合理 skip，不算「待审」
            rejected_items.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "pid": annotation.pid,
                    "surface": annotation.surface,
                    "issues": list(annotation.review_issues),
                }
            )
            continue
        if annotation.review_status != "pass":
            pending_review.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "pid": annotation.pid,
                    "surface": annotation.surface,
                    "status": annotation.review_status,
                    "issues": list(annotation.review_issues),
                }
            )
        elif annotation.reviewed_hash and annotation.reviewed_hash != annotation.revision_hash:
            pending_review.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "pid": annotation.pid,
                    "surface": annotation.surface,
                    "status": "stale_review",
                    "issues": ["审查针对的是旧内容版本"],
                }
            )
    review_complete = not pending_review

    invalid_items = [
        {
            "annotation_id": a.annotation_id,
            "pid": a.pid,
            "surface": a.surface or a.echo_surface,
            "codes": list(a.invalid_codes),
        }
        for a in annotations.annotations
        if a.invalid_codes
    ]

    # 有拟标注、但全部被结构校验跳过（跨内联标签等），或本身就没有拟标注：
    # 完整审查后确实没有可发布内容，归入 no_annotations，而不是假装 success。
    no_annotate = annotate_count == 0 or (
        usable_count is not None and usable_count == 0 and not invalid_items
    )

    omitted_items: list[dict] = []
    status = "blocked"
    if not invalid_items and scope_complete and review_complete:
        status = "no_annotations" if no_annotate else "complete"
    elif allow_partial and no_annotate is False:
        status = "partial"
    elif allow_partial and no_annotate:
        status = "partial" if (invalid_items or not scope_complete) else "no_annotations"

    if status == "partial":
        omitted_items = [
            *[
                {"kind": "invalid", **item}
                for item in invalid_items
            ],
            *[
                {"kind": "review", **item}
                for item in pending_review
            ],
            *[
                {"kind": "scope", **item}
                for item in missing_scope
            ],
        ]

    return {
        "scope_complete": scope_complete,
        "review_complete": review_complete,
        "delivery_status": status,
        "publishable": status == "complete",
        "missing_scope": missing_scope,
        "pending_review": pending_review,
        "rejected_items": rejected_items,
        "invalid_items": invalid_items,
        "omitted_items": omitted_items,
        "counts": {
            "scope_windows": len(scope_windows),
            "identified_windows": len(identified),
            "reviewed_windows": len(reviewed),
            "annotate_items": sum(
                1 for a in annotations.annotations if a.action == "annotate"
            ),
        },
    }


def filter_publishable(
    annotations: AnnotationSet,
    gate: dict,
    usable: list[Annotation] | None = None,
) -> AnnotationSet:
    """allow-partial：只保留审查通过且结构未标错的条目，并记录遗漏。"""

    if usable is not None:
        keep = list(usable)
    else:
        keep = [
            annotation
            for annotation in annotations.annotations
            if annotation.action == "annotate"
            and annotation.review_status == "pass"
            and not annotation.invalid_codes
            and (
                not annotation.reviewed_hash
                or annotation.reviewed_hash == annotation.revision_hash
            )
        ]
    filtered = AnnotationSet(
        annotations=keep,
        suppressed=annotations.suppressed,
        files=annotations.files,
        issues=annotations.issues,
        book_sha256=annotations.book_sha256,
        lexicon_sha256=annotations.lexicon_sha256,
        config_hash=annotations.config_hash,
        protocol=annotations.protocol,
        tokenizer=annotations.tokenizer,
        identify_prompt=annotations.identify_prompt,
        review_prompt=annotations.review_prompt,
        context_pack_hash=annotations.context_pack_hash,
        task_id=annotations.task_id,
        windows=annotations.windows,
    )
    return filtered


def annotation_to_dict(annotation: Annotation) -> dict:
    return {
        "annotation_id": annotation.annotation_id,
        "pid": annotation.pid,
        "token_ids": list(annotation.token_ids),
        "surface": annotation.surface,
        "span": list(annotation.span),
        "lemma": annotation.lemma,
        "unit_type": annotation.unit_type,
        "source_type": annotation.source_type,
        "whole_entry_ids": list(annotation.whole_entry_ids),
        "components": [c.as_dict() for c in annotation.components],
        "short_gloss_zh": annotation.gloss,
        "reader_usage_note": annotation.reader_usage_note,
        "audit_note": annotation.audit_note,
        "reading_need": annotation.reading_need,
        "sense_protected": bool(annotation.sense_protected),
        "window_id": annotation.window_id,
        "usage_note": annotation.reader_usage_note,
        "usage_category": annotation.usage_category,
        "usage_domain": annotation.usage_domain,
        "sense_key": annotation.sense_key,
        "evidence": annotation.evidence,
        "action": annotation.action,
        "reason": annotation.reason,
    }


# ---------------------------------------------------------------------------
# 位置回填与词形关联（§19.2 / §19.3）
# ---------------------------------------------------------------------------


def token_index(paragraphs: dict[str, Paragraph]) -> dict[str, tuple[Paragraph, Token]]:
    return {t.id: (p, t) for p in paragraphs.values() for t in p.tokens}


def _collect_tokens(
    ids: list[str],
    paragraph: Paragraph,
    index: dict[str, tuple[Paragraph, Token]],
) -> tuple[list[Token], list[str]]:
    tokens: list[Token] = []
    codes: list[str] = []
    for ident in ids:
        found = index.get(ident)
        if found is None:
            codes.append("unknown_token")
            continue
        owner, token = found
        if owner.pid != paragraph.pid:
            codes.append("cross_paragraph")
            continue
        tokens.append(token)
    tokens.sort(key=lambda t: t.index)
    if tokens and [t.index for t in tokens] != list(
        range(tokens[0].index, tokens[0].index + len(tokens))
    ):
        codes.append("non_contiguous")
    return tokens, list(dict.fromkeys(codes))


def backfill(
    annotation: Annotation,
    paragraphs: dict[str, Paragraph],
    index: dict[str, tuple[Paragraph, Token]],
) -> list[str]:
    """用 token 回填 surface/span；模型文本只作一致性检查。"""

    paragraph = paragraphs.get(annotation.pid)
    if paragraph is None:
        return ["unknown_pid"]
    tokens, codes = _collect_tokens(annotation.token_ids, paragraph, index)
    if not tokens:
        return list(dict.fromkeys(codes + ["no_tokens"]))
    annotation.token_ids = [t.id for t in tokens]
    annotation.span = (tokens[0].start, tokens[-1].end)
    annotation.surface = paragraph.text[annotation.span[0] : annotation.span[1]]
    if any(t.cross_slot for t in tokens):
        annotation.skip_reason = "cross_inline_node"
    if annotation.echo_surface and annotation.echo_surface != annotation.surface:
        codes.append("surface_mismatch")
    return list(dict.fromkeys(codes))


def backfill_components(
    annotation: Annotation,
    paragraph: Paragraph,
    index: dict[str, tuple[Paragraph, Token]],
) -> list[str]:
    codes: list[str] = []
    parent = set(annotation.token_ids)
    used: list[tuple[int, int]] = []
    for component in annotation.components:
        tokens, member_codes = _collect_tokens(component.token_ids, paragraph, index)
        if member_codes:
            codes.extend(member_codes)
            component.status = "invalid"
            continue
        ids = [t.id for t in tokens]
        if not set(ids).issubset(parent):
            codes.append("member_outside_parent")
            component.status = "invalid"
            continue
        span = (tokens[0].start, tokens[-1].end)
        if any(not (span[1] <= s or span[0] >= e) for s, e in used):
            codes.append("member_overlap")
            component.status = "invalid"
            continue
        if any(t.cross_slot for t in tokens):
            codes.append("member_cross_inline_node")
            component.status = "invalid"
            continue
        used.append(span)
        component.token_ids = ids
        component.span = span
        component.surface = paragraph.text[span[0] : span[1]]
    annotation.components.sort(key=lambda c: c.span[0])
    return list(dict.fromkeys(codes))


def _variants(entry: Entry) -> list[str]:
    return [normalize_span(v) for v in entry.variants] or [normalize_span(entry.head)]


def _word_related(surface: str, head: str) -> bool:
    """单个词形与词头的关系：相同、所有格、规则/不规则还原。"""

    key = normalize_span(surface)
    target = normalize_span(head)
    if not key or not target:
        return False
    if key == target:
        return True
    stripped = strip_possessive(key)
    if stripped and normalize_span(stripped) == target:
        return True
    return any(normalize_span(c) == target for c in lemma_candidates(key))


def related_form(surface: str, entry: Entry) -> bool:
    """原文词形与词条是否有可靠的原形/变体关系（§19.3）。

    单词：相同 / 所有格 / 规则或不规则还原命中词头或并列写法。
    短语：词数一致时逐词校验（gave up → give up 也能通过）。
    """

    key = normalize_span(surface)
    head = normalize_span(entry.head)
    variants = set(_variants(entry))
    if key == head or key in variants:
        return True
    stripped = strip_possessive(key)
    if stripped and (normalize_span(stripped) == head or normalize_span(stripped) in variants):
        return True
    surface_words = key.split()
    head_words = head.split()
    if len(surface_words) > 1 and len(surface_words) == len(head_words):
        return all(_word_related(a, b) for a, b in zip(surface_words, head_words))
    if len(head_words) > 1:
        return False
    return _word_related(key, head)


def _filtered_ids(lexicon: Lexicon) -> set[str]:
    return {entry.entry_id for entry in lexicon.entries}


def _lookup_forms(text: str, lexicon: Lexicon, *, filtered: bool = True):
    getter = lexicon.lookup if filtered else lexicon.lookup_any
    key = normalize_span(text)
    entries = getter(key)
    if entries:
        return entries
    stripped = strip_possessive(key)
    if stripped:
        entries = getter(normalize_span(stripped))
        if entries:
            return entries
    for candidate in lemma_candidates(key, lexicon.form_map):
        entries = getter(normalize_span(candidate))
        if entries:
            return entries
    return []


def resolve_component(component: Component, lexicon: Lexicon, order: tuple[str, ...]) -> None:
    """成员关联：模型给的条目要过词形校验；缺失时程序只在"词形相关"时补。"""

    entries: list[Entry] = []
    if component.entry_ids:
        entries = [e for e in (lexicon.entry_by_id(i) for i in component.entry_ids) if e]
        kept = [e for e in entries if e.entry_id in _filtered_ids(lexicon)]
        if entries and not kept:
            component.status = "filtered"
            component.labels = ordered_labels({x for e in entries for x in e.grades}, order)
            component.gloss = "；".join(dict.fromkeys(e.gloss for e in entries))
            component.lemma = entries[0].head
            return
        if kept and not all(related_form(component.surface, e) for e in kept):
            component.status = "form_mismatch"
            component.gloss = ""
            component.labels = []
            return
        entries = kept
    if not entries:
        candidates = _lookup_forms(component.surface, lexicon)
        entries = [e for e in candidates if related_form(component.surface, e)]
        if entries:
            component.resolved = True
            component.entry_ids = [e.entry_id for e in entries]
            component.status = "exact"
        else:
            full = _lookup_forms(component.surface, lexicon, filtered=False)
            if full:
                component.status = "filtered"
                component.labels = ordered_labels(
                    {x for e in full for x in e.grades}, order
                )
                component.gloss = "；".join(dict.fromkeys(e.gloss for e in full))
                component.lemma = full[0].head
            elif lemma_candidates(normalize_span(component.surface)):
                component.status = "form_match_failed"
            else:
                component.status = "absent"
            return
    else:
        component.status = "exact"
    component.lemma = entries[0].head
    labels: list[str] = []
    for entry in entries:
        for label in ordered_labels(entry.grades, order):
            if label not in labels:
                labels.append(label)
    component.labels = labels
    component.gloss = component.gloss or "；".join(dict.fromkeys(e.gloss for e in entries))
    component.pos = "/".join(dict.fromkeys(e.pos for e in entries if e.pos))
    component.example = component.example or next((e.example for e in entries if e.example), "")
    component.extra = component.extra or next((e.extra for e in entries if e.extra), "")


def resolve_annotation(
    annotation: Annotation,
    lexicon: Lexicon,
    *,
    label_order: tuple[str, ...] | None = None,
) -> list[str]:
    """来源判定 + 词形校验；返回硬错误码（空列表表示通过）。"""

    order = label_order or lexicon.label_order or DEFAULT_LABEL_ORDER
    allowed = set(lexicon.filter_grades) or None
    filtered_ids = _filtered_ids(lexicon)
    codes: list[str] = []
    source = SOURCE_ALIASES.get(annotation.source_type, annotation.source_type)

    if source == "exact_entry":
        entries = [
            e for e in (lexicon.entry_by_id(i) for i in annotation.whole_entry_ids) if e
        ]
        if not entries:
            entries = _lookup_forms(annotation.surface, lexicon, filtered=False)
            if entries:
                annotation.warnings.append("模型未给出有效整体词条，已按词库自动关联")
        if entries:
            if not any(e.entry_id in filtered_ids for e in entries):
                annotation.source_status = "filtered_out"
                annotation.label_scope = "whole"
                annotation.labels_full = "、".join(
                    ordered_labels({x for e in entries for x in e.grades}, order)
                )
                annotation.whole_entries = entries
                return ["filtered_out"]
            kept = [e for e in entries if e.entry_id in filtered_ids]
            if not all(related_form(annotation.surface, e) for e in kept):
                annotation.source_status = "exact_entry"
                return ["entry_form_mismatch"]
            labels = {label for e in kept for label in e.grades}
            annotation.source_status = "exact_entry"
            annotation.label_scope = "whole"
            annotation.display_label = pick_label(labels, allowed=allowed, order=order)
            annotation.labels_full = "、".join(ordered_labels(labels, order))
            annotation.whole_entries = kept
            annotation.whole_entry_ids = [e.entry_id for e in kept]
            return []
        annotation.source_status = (
            "form_match_failed"
            if lemma_candidates(normalize_span(annotation.surface))
            else "out_of_lexicon"
        )
        annotation.label_scope = "none"
        return []

    if source == "component_entries":
        for component in annotation.components:
            if component.status in ("invalid", "range_mismatch"):
                continue
            resolve_component(component, lexicon, order)
        annotation.source_status = "component_entries"
        annotation.label_scope = "components"
        annotation.display_label = ""
        for component in annotation.components:
            if component.status == "form_mismatch":
                codes.append("entry_form_mismatch")
            elif component.status == "filtered" and component.entry_ids:
                codes.append("component_filtered")
        return list(dict.fromkeys(codes))

    found = _lookup_forms(annotation.surface, lexicon, filtered=False)
    if found:
        in_filter = [e for e in found if e.entry_id in filtered_ids]
        if not in_filter:
            annotation.source_status = "filtered_out"
            annotation.label_scope = "whole"
            annotation.labels_full = "、".join(
                ordered_labels({x for e in found for x in e.grades}, order)
            )
            return ["filtered_out"]
        if all(related_form(annotation.surface, e) for e in in_filter):
            labels = {label for e in in_filter for label in e.grades}
            annotation.source_type = "exact_entry"
            annotation.source_status = "exact_entry"
            annotation.label_scope = "whole"
            annotation.display_label = pick_label(labels, allowed=allowed, order=order)
            annotation.labels_full = "、".join(ordered_labels(labels, order))
            annotation.whole_entries = in_filter
            annotation.whole_entry_ids = [e.entry_id for e in in_filter]
            annotation.warnings.append("模型标为未收录，但完整词库中存在该词，已按收录处理")
            return []

    for component in annotation.components:
        if component.status not in ("invalid", "range_mismatch"):
            resolve_component(component, lexicon, order)
    annotation.source_status = (
        "form_match_failed"
        if lemma_candidates(normalize_span(annotation.surface))
        else "out_of_lexicon"
    )
    annotation.label_scope = "none"
    return []


def _gloss_may_not_cover(annotation: Annotation) -> bool:
    if annotation.unit_type == "word" or len(annotation.components) < 2:
        return False
    gloss = re.sub(r"\s+", "", annotation.gloss)
    return len(gloss) <= 2


# ---------------------------------------------------------------------------
# 程序侧校验
# ---------------------------------------------------------------------------


def validate_annotations(
    annotations: AnnotationSet,
    paragraphs: dict[str, Paragraph],
    lexicon: Lexicon,
    *,
    label_order: tuple[str, ...] | None = None,
) -> tuple[list[Annotation], dict]:
    """结构 + 词形 + 来源 + 审查四层校验；invalid 记录不会在后续被重新关联。"""

    usable: list[Annotation] = []
    index = token_index(paragraphs)
    stats = {
        "total": len(annotations.annotations),
        "not_annotate": 0,
        "unreviewed": 0,
        "pending_review": 0,
        "rejected_review": 0,
        "stale_review": 0,
        "invalid": 0,
        "invalid_codes": {},
        "skipped_unsupported": 0,
        "accepted": 0,
        "exact_entry": 0,
        "component_entries": 0,
        "out_of_lexicon": 0,
        "form_match_failed": 0,
        "filtered_out": 0,
        "sensed_as_context": 0,
        "merged_units": 0,
        "component_resolved": 0,
        "component_filtered": 0,
        "component_unlinked": 0,
        "gloss_coverage_warnings": 0,
        "category_used": {},
        "range_mismatch": 0,
        "unknown_pid": 0,
        "overlap": 0,
        "bad_entry": 0,
        "empty_gloss": 0,
    }
    spans_by_pid: dict[str, list[tuple[int, int]]] = {}

    def fail(annotation: Annotation, codes: list[str]) -> None:
        annotation.invalid_codes = list(dict.fromkeys(annotation.invalid_codes + codes))
        stats["invalid"] += 1
        for code in annotation.invalid_codes:
            bucket = stats["invalid_codes"]
            bucket[code] = bucket.get(code, 0) + 1
        annotations.issues.append(
            {
                "id": annotation.annotation_id,
                "codes": annotation.invalid_codes,
                "surface": annotation.surface or annotation.echo_surface,
                "pid": annotation.pid,
            }
        )

    for annotation in annotations.annotations:
        if annotation.action != "annotate":
            stats["not_annotate"] += 1
            continue
        if annotation.invalid_codes:
            # §19.5：已判无效的记录不再清字段重新关联
            fail(annotation, [])
            continue
        paragraph = paragraphs.get(annotation.pid)
        if paragraph is None:
            stats["unknown_pid"] += 1
            fail(annotation, ["unknown_pid"])
            continue

        codes = backfill(annotation, paragraphs, index)
        if annotation.skip_reason == "cross_inline_node":
            stats["skipped_unsupported"] += 1
            continue
        if codes:
            fail(annotation, codes)
            continue
        if annotation.review_status != "pass":
            if annotation.review_status == "modify_pending":
                stats["pending_review"] += 1
            elif annotation.review_status in (
                "reject",
                "conflict",
                "invalid_revision",
                "invalid_status",
            ):
                stats["rejected_review"] += 1
            else:
                stats["unreviewed"] += 1
            continue
        if annotation.reviewed_hash and annotation.reviewed_hash != annotation.revision_hash:
            stats["stale_review"] += 1
            fail(annotation, ["stale_review"])
            continue
        if not annotation.gloss:
            fail(annotation, ["missing_gloss"])
            continue

        if annotation.components:
            member_codes = backfill_components(annotation, paragraph, index)
            if member_codes:
                fail(annotation, member_codes)
                continue

        codes = resolve_annotation(annotation, lexicon, label_order=label_order)
        if codes:
            fail(annotation, codes)
            continue

        start, end = annotation.span
        overlaps = spans_by_pid.setdefault(annotation.pid, [])
        if any(not (end <= s or start >= e) for s, e in overlaps):
            stats["overlap"] += 1
            fail(annotation, ["overlap"])
            continue
        overlaps.append((start, end))

        stats["accepted"] += 1
        if annotation.source_status == "exact_entry":
            stats["exact_entry"] += 1
            if not annotation.display_label:
                stats["sensed_as_context"] += 1
        elif annotation.source_status == "component_entries":
            stats["component_entries"] += 1
            stats["merged_units"] += 1
            if any(c.resolved for c in annotation.components):
                stats["component_resolved"] += 1
            if any(c.status == "filtered" for c in annotation.components):
                stats["component_filtered"] += 1
            if any(
                c.status in ("absent", "form_match_failed", "invalid")
                for c in annotation.components
            ):
                stats["component_unlinked"] += 1
            if _gloss_may_not_cover(annotation):
                stats["gloss_coverage_warnings"] += 1
                annotation.warnings.append(
                    "组合短义可能未覆盖整个区间，请审查确认（如 skin cancer 应写「皮肤癌」）"
                )
        else:
            stats["out_of_lexicon"] += 1
            if annotation.source_status == "form_match_failed":
                stats["form_match_failed"] += 1
            if annotation.usage_category:
                bucket = stats["category_used"]
                bucket[annotation.usage_category] = bucket.get(annotation.usage_category, 0) + 1
            if annotation.components:
                stats["merged_units"] += 1
                if any(
                    c.status in ("absent", "form_match_failed", "invalid")
                    for c in annotation.components
                ):
                    stats["component_unlinked"] += 1
                if _gloss_may_not_cover(annotation):
                    stats["gloss_coverage_warnings"] += 1

        usable.append(annotation)

    return usable, stats


def config_fingerprint(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def annotation_id_for(pid: str, token_ids: list[str]) -> str:
    raw = f"v5|{pid}|" + ",".join(token_ids)
    return "a-" + hashlib.blake2s(raw.encode("utf-8"), digest_size=5).hexdigest()

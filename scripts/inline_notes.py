"""`inline-notes` 呈现（设计文档 §16）：正文短注 + 章末完整词条表。

正文效果（保留原文词形，短注只给**当前语境**的一个意思）：

    fished(钓鱼,4,[1])  alone(独自一人,4,[2])

章末按编号列出完整学习内容（词形→词头、全部等级、本句意思、词性、完整释义、
例句、扩展说明、返回正文）。编号在**逻辑章节**内从 1 开始，按被采用标注的首次
出现顺序；同一「词条+义项」复用编号，异义新增编号。

硬约束：
    * 每个准备标注的命中都必须有宿主（harness）给出的决策；
      缺失/低置信/非法/action=skip 一律不标并计数，绝不静默回退成"第一义项"。
    * 英文正文除新增短注外逐字不变；短注与词条块都可被剥离后比对。
    * 词注块插在**逻辑章节真正结束处**，不新增章节、不改原目录与文件顺序。
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import os
import posixpath
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .binding import is_annotated
from .chapters import Chapter, detect_chapters, doc_chapter_map
from .disambiguation import (
    DEFAULT_MIN_CONFIDENCE,
    Candidate,
    DecisionSet,
    SenseDecision,
    candidate_id_v2,
    sense_variants,
)
from .epub_common import (
    Archive,
    EpubError,
    Package,
    TextSlot,
    is_excluded,
    local_name,
    open_epub,
    parse_xml,
    qualified,
    relative_href,
    serialize_xml,
)
from .load_lexicon import Entry, Lexicon
from .matching import SEP, TOKEN_RE, Match, find_matches

__all__ = [
    "NoteItem",
    "collect_candidates",
    "render_book",
    "CHAPTER_NOTES_CLASS",
    "GRADE_SHORT",
]

CHAPTER_NOTES_CLASS = "vocab-chapter-notes"
NOTE_CLASS = "vocab-note"

#: 等级简称（§16.2）：四级→4、六级→6、雅思→ielts
GRADE_SHORT = {"四级": "4", "六级": "6", "雅思": "ielts"}
GRADE_PRIORITY = ("四级", "六级", "雅思")


def short_grade(grades: frozenset[str] | set[str] | list[str]) -> str:
    """正文只展示一个简称：四级 > 六级 > 雅思。完整等级留在章末。"""

    values = set(grades)
    for grade in GRADE_PRIORITY:
        if grade in values:
            return GRADE_SHORT[grade]
    return ""


def full_grades(entries: list[Entry]) -> str:
    seen: list[str] = []
    for entry in entries:
        for grade in str(entry.grade_raw).replace("＋", "+").split("+"):
            grade = grade.strip()
            if grade and grade not in seen:
                seen.append(grade)
    ordered = [g for g in GRADE_PRIORITY if g in seen] + [g for g in seen if g not in GRADE_PRIORITY]
    return "、".join(ordered)


@dataclass
class NoteItem:
    """章末一条词条（编号在章节内唯一）。"""

    number: int
    chapter: int
    surface: str
    head: str
    entry_id: str
    grades_full: str
    grade_short: str
    gloss: str
    usage: str = ""
    pos: str = ""
    full_gloss: str = ""
    example: str = ""
    extra: str = ""
    phonetic: str = ""          # 被标注词形自己的 L1 音标；无则整行省略
    form_info: str = ""         # 中文可读词形信息；无则整行省略
    sense_id: str = ""
    confidence: float = 0.0
    gloss_source: str = "lexicon"  # lexicon | cleaned | undecided
    refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "chapter": self.chapter,
            "surface": self.surface,
            "head": self.head,
            "entry_id": self.entry_id,
            "grades": self.grades_full,
            "grade_short": self.grade_short,
            "gloss": self.gloss,
            "usage": self.usage,
            "pos": self.pos,
            "full_gloss": self.full_gloss,
            "example": self.example,
            "extra": self.extra,
            "confidence": round(self.confidence, 3),
            "gloss_source": self.gloss_source,
            "refs": list(self.refs),
        }


@dataclass
class ChapterState:
    chapter: Chapter
    lexicon: Lexicon | None = None
    items: list[NoteItem] = field(default_factory=list)
    by_sense: dict[tuple[str, str], NoteItem] = field(default_factory=dict)
    density_seen: set[tuple[str, str]] = field(default_factory=set)
    notes_written: bool = False
    tokens: int = 0
    matches: int = 0
    skipped_repeat: int = 0
    skipped_undecided: int = 0
    skipped_stoplist: int = 0
    skipped_cross_node: int = 0
    skipped_inside_link: int = 0
    candidates: int = 0

    def next_number(self) -> int:
        return len(self.items) + 1


@dataclass
class Placed:
    match: Match
    slot_index: int
    item: NoteItem


@dataclass
class DocAnalysis:
    path: str
    tree: object
    body: object
    slots: dict[int, list[TextSlot]] = field(default_factory=dict)
    offsets: dict[int, list[int]] = field(default_factory=dict)
    matches: dict[int, list[Match]] = field(default_factory=dict)
    excluded_link_hits: dict[int, int] = field(default_factory=dict)
    cross_node: dict[int, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 扫描：一文件 -> 按逻辑章节分组的文本槽与命中
# ---------------------------------------------------------------------------


def _walk_slots(body, default_chapter: int, id_map: dict[str, int]):
    """按文档顺序收集文本槽，并标注每个槽所属章节（0 = 前置页）。"""

    slots: dict[int, list[TextSlot]] = {}

    def walk(element, chapter: int) -> int:
        current = id_map.get(element.get("id") or "", chapter)
        reason = is_excluded(element)
        if current == 0 and not reason:
            reason = "front-matter"
        if element.text:
            slots.setdefault(current, []).append(
                TextSlot(element, None, element.text, reason)
            )
        for child in element:
            if not isinstance(child.tag, str):
                continue
            current = walk(child, current)
            if child.tail:
                child_reason = is_excluded(child)
                if current == 0 and not child_reason:
                    child_reason = "front-matter"
                slots.setdefault(current, []).append(
                    TextSlot(element, child, child.tail, child_reason)
                )
        return current

    walk(body, default_chapter)
    return slots


def analyze_doc(
    raw: bytes,
    path: str,
    default_chapter: int,
    id_map: dict[str, int],
    lexicon: Lexicon,
    reduce_forms: bool,
) -> DocAnalysis:
    tree = parse_xml(raw, path)
    root = tree.getroot()
    body = None
    for el in root.iter():
        if local_name(el) == "body":
            body = el
            break
    if body is None:
        return DocAnalysis(path=path, tree=tree, body=None)

    slots = _walk_slots(body, default_chapter, id_map)
    analysis = DocAnalysis(path=path, tree=tree, body=body, slots=slots)

    for chapter, chapter_slots in slots.items():
        writable = [s for s in chapter_slots if s.writable]
        if not writable or chapter == 0:
            continue
        offsets: list[int] = []
        cursor = 0
        for slot in writable:
            offsets.append(cursor)
            cursor += len(slot.text) + len(SEP)
        combined = SEP.join(s.text for s in writable)
        result = find_matches(combined, lexicon, reduce_forms=reduce_forms)
        analysis.offsets[chapter] = offsets
        analysis.matches[chapter] = result.matches
        analysis.cross_node[chapter] = result.cross_node_phrases

        link_hits = 0
        for slot in chapter_slots:
            if slot.writable or slot.excluded != "inside-link":
                continue
            link_hits += len(
                find_matches(slot.text, lexicon, reduce_forms=reduce_forms).matches
            )
        analysis.excluded_link_hits[chapter] = link_hits

    return analysis


# ---------------------------------------------------------------------------
# 候选导出（v2 协议：全量送审）
# ---------------------------------------------------------------------------


def collect_candidates(
    book_path: str | Path,
    lexicon: Lexicon,
    *,
    reduce_forms: bool = True,
    use_stoplist: bool = True,
    max_context_chars: int = 300,
) -> tuple[list[Candidate], dict]:
    archive, package = open_epub(book_path)
    try:
        chapters = detect_chapters(archive, package)
        if not chapters:
            raise EpubError("无法确定章节结构，已停止")
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        stoplist = _stoplist()

        candidates: list[Candidate] = []
        chapter_titles = {c.index: c.label for c in chapters}
        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            default, id_map = chapter_map.get(item.path, (0, {}))
            analysis = analyze_doc(
                archive.read(item.path), item.path, default, id_map, lexicon, reduce_forms
            )
            for chapter, matches in analysis.matches.items():
                writable = [s for s in analysis.slots.get(chapter, []) if s.writable]
                combined = SEP.join(s.text for s in writable)
                occurrences: Counter = Counter()
                for match in matches:
                    key = match.entry_key
                    index = occurrences[key]
                    occurrences[key] += 1
                    if use_stoplist and key in stoplist:
                        continue
                    candidates.append(
                        Candidate(
                            id=candidate_id_v2(item.path, chapter, key, index),
                            chapter=str(chapter),
                            chapter_index=chapter,
                            surface=match.surface,
                            head=match.entries[0].head,
                            entry_key=key,
                            occurrence=index,
                            context=_sentence(combined, match.start, match.end, max_context_chars),
                            options=_option_rows(match.entries),
                            reason=_reason(match.entries),
                        )
                    )
        meta = {
            "title": package.title,
            "opf": package.opf_path,
            "version": package.version,
            "chapters": [c.as_dict() for c in chapters],
            "chapter_titles": chapter_titles,
        }
        return candidates, meta
    finally:
        archive.close()


def _option_rows(entries: list[Entry]) -> list[dict]:
    return [
        {
            "entry_id": entry.entry_id,
            "pos": entry.pos,
            "grade": entry.grade_raw,
            "gloss": entry.gloss,
            "senses": sense_variants(entry.gloss),
        }
        for entry in entries
    ]


def _reason(entries: list[Entry]) -> str:
    if len(entries) > 1:
        return "multi_entry"
    if len(sense_variants(entries[0].gloss)) > 1:
        return "multi_sense"
    return "context_check"


def _stoplist() -> frozenset[str]:
    from .annotate_epub import STOPLIST

    return STOPLIST


def _sentence(text: str, start: int, end: int, max_chars: int) -> str:
    plain = text.replace(SEP, " ")
    separators = ".!?…\n"
    left = max(plain.rfind(ch, 0, start) for ch in separators)
    rights = [plain.find(ch, end) for ch in separators]
    rights = [r for r in rights if r >= 0]
    low, high = left + 1, (min(rights) + 1 if rights else len(plain))
    if high - low < 40:
        previous = max(plain.rfind(ch, 0, max(0, low - 1)) for ch in separators)
        low = max(0, previous + 1, low - 120)
        later = [plain.find(ch, high) for ch in separators]
        later = [n for n in later if n >= 0]
        high = min(len(plain), (min(later) + 1) if later else len(plain), high + 120)
    if high - low > max_chars:
        center = (start + end) // 2
        half = max_chars // 2
        low = max(low, center - half)
        high = min(high, center + half)
    return plain[low:high].strip()


INSTRUCTION_V2 = (
    "逐条判断下列英文句子里，目标词**在当前语境**中的意思，然后按 response_schema 输出 JSON。\n"
    "规则：\n"
    "1) short_gloss_zh 只给该语境的一个首要中文意思，短（≤8 字为宜），不要堆词典义项；\n"
    "2) selected_entry_ids 从 options 的 entry_id 里选，必须选一个；\n"
    "3) sense_id 用稳定的义项标识（建议直接用 short_gloss_zh），同一章里同词同义会复用编号；\n"
    "4) context_pos 写该词在本句的词性；usage_note 写必要的用法说明（如“fished 为过去式”），没有就留空；\n"
    "5) 专有名词组合（如 Gulf Stream）不适合逐词学习：对涉及的每个词返回 action=skip，"
    "reason_code=proper_noun；\n"
    "6) 判断不了返回 action=uncertain，不要猜；单词不属于该词表义项时也可以 action=skip；\n"
    "7) confidence 0~1，低于 min_confidence 会被丢弃。"
)

RESPONSE_SCHEMA_V2 = {
    "decisions": [
        {
            "id": "c-2f06e72bfa",
            "action": "annotate",
            "selected_entry_ids": ["fish|四级+雅思|n./v.|h9c1a2b"],
            "sense_id": "钓鱼",
            "short_gloss_zh": "钓鱼",
            "context_pos": "v.",
            "usage_note": "fished 为过去式",
            "confidence": 0.92,
            "reason_code": "context_match",
        }
    ]
}


def request_payload_v2(
    candidates: list[Candidate],
    *,
    book_title: str,
    book_sha256: str,
    lexicon_sha256: str,
    config: dict,
    config_hash: str,
    part: int,
    parts: int,
    min_confidence: float,
    max_context_chars: int,
) -> dict:
    from .disambiguation import SCHEMA_REQUEST_V2

    return {
        "schema": SCHEMA_REQUEST_V2,
        "protocol": "v2",
        "part": part,
        "parts": parts,
        "instruction": INSTRUCTION_V2,
        "response_schema": RESPONSE_SCHEMA_V2,
        "book": {"title": book_title, "sha256": book_sha256},
        "lexicon": {"sha256": lexicon_sha256},
        "scan": {**config, "hash": config_hash},
        "min_confidence": min_confidence,
        "max_context_chars": max_context_chars,
        "candidates": [
            {
                **c.as_dict(),
                "chapter_index": c.chapter_index,
            }
            for c in candidates
        ],
    }


# ---------------------------------------------------------------------------
# 决策校验（v2）
# ---------------------------------------------------------------------------


def judge(
    decision: SenseDecision | None,
    entries: list[Entry],
    *,
    min_confidence: float,
    allow_cleaned_gloss: bool = True,
) -> tuple[SenseDecision | None, str, str]:
    """返回 (可用决策, 原因, 释义来源)。"""

    if decision is None:
        return None, "missing", ""
    if decision.action == "skip":
        return None, "skipped_by_model", ""
    if decision.action == "uncertain":
        return None, "uncertain", ""
    if decision.confidence < min_confidence:
        return None, "low_confidence", ""
    gloss = decision.short_gloss_zh.strip()
    if not gloss:
        return None, "empty_gloss", ""

    options = {entry.entry_id: entry for entry in entries}
    selected = [i for i in decision.selected_entry_ids if i in options]
    if decision.selected_entry_ids and len(selected) != len(decision.selected_entry_ids):
        return None, "unknown_entry_id", ""

    candidates = [options[i].gloss for i in selected] if selected else [e.gloss for e in entries]
    squeezed = re.sub(r"\s+", "", gloss)
    for source in candidates:
        if squeezed and squeezed in re.sub(r"\s+", "", source):
            return decision, "", "lexicon"
        for variant in sense_variants(source):
            if squeezed in re.sub(r"\s+", "", variant):
                return decision, "", "lexicon"
    if allow_cleaned_gloss and (selected or decision.selected_entry_ids):
        return decision, "", "cleaned"
    if allow_cleaned_gloss:
        return decision, "", "cleaned"
    return None, "unfaithful_gloss", ""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_book(
    book_path: str | Path,
    lexicon: Lexicon,
    options,
    *,
    out_dir: str | Path | None = None,
    output_path: str | Path | None = None,
):
    from .annotate_epub import (
        AlreadyAnnotatedError,
        AnnotationOutcome,
        ChapterResult,
        _build_glossary,
        _unique_id,
        _unique_resource_path,
        _write_epub,
        choose_output_path,
    )

    opts = options
    archive, package = open_epub(book_path)
    try:
        for spine_item in package.spine:
            if spine_item.item.is_xhtml and is_annotated(
                archive.read(spine_item.item.path)
            ):
                raise AlreadyAnnotatedError(
                    "输入 EPUB 已包含本技能的词汇标注（vocab-skill / data-vocab-id），"
                    "已拒绝重复处理；请使用原始文件重新生成。"
                )

        out_path = (
            Path(output_path) if output_path else choose_output_path(book_path, out_dir)
        )
        if out_path.exists():
            raise EpubError(f"输出文件已存在，拒绝覆盖：{out_path}")

        chapters = detect_chapters(archive, package)
        if not chapters:
            raise EpubError("无法确定章节结构：nav/NCX 与 spine 均不可用")
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        states = {
            c.index: ChapterState(chapter=c, lexicon=lexicon) for c in chapters
        }
        chapter_by_index = {c.index: c for c in chapters}

        css_path = _unique_resource_path(package, archive, "vocab.css", "vocab-skill.css")
        css_href = relative_href(css_path, package.opf_dir)

        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}
        modified: list[str] = []
        stats: Counter = Counter()
        used_entries: dict[str, dict] = {}

        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            default, id_map = chapter_map.get(item.path, (0, {}))
            analysis = analyze_doc(
                archive.read(item.path),
                item.path,
                default,
                id_map,
                lexicon,
                opts.reduce_forms,
            )
            changed = False
            for chapter_index in sorted(analysis.matches):
                state = states.get(chapter_index)
                if state is None:
                    continue
                changed |= _render_chapter_doc(analysis, chapter_index, state, lexicon, opts, stats)

            # 在本文件结束的章节 -> 追加词注块
            for chapter in chapters:
                if chapter.end_path != item.path or chapter.index not in states:
                    continue
                state = states[chapter.index]
                if state.notes_written or not state.items:
                    continue
                _insert_chapter_notes(analysis, chapter, state)
                state.notes_written = True
                changed = True

            if changed:
                _ensure_css_link(analysis.tree, relative_href(css_path, posixpath.dirname(item.path)))
                replacements[item.path] = serialize_xml(analysis.tree)
                modified.append(item.path)

        # 汇总统计
        for index, state in states.items():
            if state.items:
                stats["chapters_with_notes"] += 1
            for item_ in state.items:
                slot = used_entries.setdefault(
                    item_.entry_id,
                    {"display": item_.head, "gloss": item_.full_gloss, "count": 0},
                )
                slot["count"] += 1

        total_marks = sum(state.matches for state in states.values())
        total_items = sum(len(state.items) for state in states.values())
        outcome = AnnotationOutcome(
            source=str(book_path),
            density=opts.density,
            presentation="inline-notes",
        )
        outcome.output = str(out_path)
        outcome.version = package.version
        outcome.css_path = css_path
        outcome.modified_paths = modified
        outcome.spine_paths = modified
        outcome.total_matches = total_marks
        outcome.total_footnotes = total_items
        outcome.per_entry = used_entries
        outcome.chapters = [
            ChapterResult(
                index=index,
                path=chapter_by_index[index].start_path,
                label=chapter_by_index[index].label,
                tokens=state.tokens,
                matches=state.matches,
                footnotes=len(state.items),
                skipped_repeat=state.skipped_repeat,
                skipped_undecided=state.skipped_undecided,
                skipped_stoplist=state.skipped_stoplist,
                skipped_cross_node=state.skipped_cross_node,
                skipped_inside_link=state.skipped_inside_link,
                candidates=state.candidates,
                modified=bool(state.items),
            )
            for index, state in sorted(states.items())
        ]
        outcome.disambiguation = {
            "mode": "harness-v2",
            "protocol": "v2",
            "density": opts.density,
            "presentation": "inline-notes",
            "segment_words": opts.segment_words if opts.density == "segment" else None,
            "min_confidence": opts.min_confidence,
            "undecided_policy": opts.undecided,
            "candidates": stats["candidates"],
            "decisions_loaded": len(opts.decisions) if opts.decisions else 0,
            "decisions_applied": stats["applied"],
            "missing_decision": stats["missing"],
            "low_confidence": stats["low_confidence"],
            "invalid": stats["invalid"],
            "not_target": stats["skipped_by_model"] + stats["uncertain"],
            "stoplist_skipped": sum(s.skipped_stoplist for s in states.values()),
            "cleaned_gloss": stats["cleaned_gloss"],
            "undecided_version": opts.decisions is None,
            "chapters": len(chapters),
            "front_matter_skipped": stats["front_slots"],
        }
        if opts.decisions is None:
            outcome.warnings.append(
                "未提供宿主消歧决策：本轮输出为**未消歧版本**，短注取自词表首个义项，"
                "不代表语境判断结果（见 SKILL.md 阶段 2）"
            )

        if total_marks == 0:
            outcome.warnings.append("没有产生任何标注，输出为原书副本")
            _write_epub(archive, out_path, {}, {})
            return outcome

        manifest_el = package.manifest_element()
        if css_path not in archive.names:
            additions[css_path] = _CSS.encode("utf-8")
        if package.item_by_path(css_path) is None:
            item_el = etree.SubElement(manifest_el, qualified(manifest_el, "item"))
            item_el.set("id", _unique_id(package, "vocab-css"))
            item_el.set("href", css_href)
            item_el.set("media-type", "text/css")

        if opts.glossary and used_entries:
            glossary_path = _unique_resource_path(
                package, archive, "vocab-glossary.xhtml", "vocab-skill-glossary.xhtml"
            )
            additions[glossary_path] = _build_glossary(
                used_entries,
                chapter_count=len(chapters),
                css_href=relative_href(css_path, posixpath.dirname(glossary_path)),
                title=f"{package.title or '本书'} 词汇表",
                epub3=package.is_epub3,
                density=opts.density,
            )
            glossary_id = _unique_id(package, "vocab-glossary")
            el = etree.SubElement(manifest_el, qualified(manifest_el, "item"))
            el.set("id", glossary_id)
            el.set("href", relative_href(glossary_path, package.opf_dir))
            el.set("media-type", "application/xhtml+xml")
            spine_el = package.spine_element()
            itemref = etree.SubElement(spine_el, qualified(spine_el, "itemref"))
            itemref.set("idref", glossary_id)
            outcome.glossary_path = glossary_path
            modified.append(glossary_path)

        replacements[package.opf_path] = serialize_xml(package.tree)
        modified.append(package.opf_path)
        outcome.modified_paths = modified

        _write_epub(archive, out_path, replacements, additions)
        return outcome
    finally:
        archive.close()


def _render_chapter_doc(analysis: DocAnalysis, chapter_index: int, state: ChapterState,
                        lexicon: Lexicon, opts, stats: Counter) -> bool:
    writable = [s for s in analysis.slots.get(chapter_index, []) if s.writable]
    matches = analysis.matches.get(chapter_index, [])
    state.tokens += sum(len(TOKEN_RE.findall(s.text)) for s in writable)
    state.skipped_cross_node += analysis.cross_node.get(chapter_index, 0)
    state.skipped_inside_link += analysis.excluded_link_hits.get(chapter_index, 0)
    if not writable:
        return False

    offsets = analysis.offsets[chapter_index]
    combined = SEP.join(s.text for s in writable)
    stoplist = _stoplist()

    occurrences: Counter = Counter()
    placed: list[Placed] = []
    seg_words = max(1, opts.segment_words) if opts.density == "segment" else 0
    segment_seen: set[tuple[str, str]] = set()
    words = 0
    last_pos = 0

    for match in matches:
        key = match.entry_key
        index = occurrences[key]
        occurrences[key] += 1
        if opts.use_stoplist and key in stoplist:
            state.skipped_stoplist += 1
            continue

        cid = candidate_id_v2(analysis.path, chapter_index, key, index)
        state.candidates += 1
        stats["candidates"] += 1
        decision = opts.decisions.get(cid) if opts.decisions else None

        chosen_entry = None
        if decision is None and opts.undecided == "lexicon":
            usable, reason, source = (None, "missing", "undecided")
            chosen_entry = match.entries[0]
            gloss = sense_variants(chosen_entry.gloss)[0]
            usage = ""
            confidence = 0.0
            sense_id = gloss
        else:
            usable, reason, source = judge(
                decision,
                list(match.entries),
                min_confidence=opts.min_confidence,
            )
            if usable is None:
                stats[reason] += 1
                state.skipped_undecided += 1
                continue
            gloss = usable.short_gloss_zh.strip()
            usage = usable.usage_note
            confidence = usable.confidence
            sense_id = usable.sense_id or gloss
            chosen_entry = _pick_entry(match.entries, usable.selected_entry_ids, gloss)

        if seg_words:
            words += len(combined[last_pos:match.start].replace(SEP, " ").split())
            last_pos = match.start
            if words >= seg_words:
                seg_words = max(1, opts.segment_words)
                segment_seen.clear()
                words = 0
        sense_key = (key, sense_id)
        if opts.density in ("chapter", "book", "segment"):
            bucket = state.density_seen if opts.density != "segment" else segment_seen
            if sense_key in bucket:
                state.skipped_repeat += 1
                continue
            bucket.add(sense_key)

        stats["applied"] += 1
        item = state.by_sense.get(sense_key)
        if item is None:
            item = _make_item(
                state, match, gloss, usage, confidence, source, sense_id,
                chosen_entry or match.entries[0],
            )
            state.by_sense[sense_key] = item
            state.items.append(item)
            if source == "cleaned":
                stats["cleaned_gloss"] += 1

        slot_index = bisect.bisect_right(offsets, match.start) - 1
        placed.append(Placed(match=match, slot_index=slot_index, item=item))
        state.matches += 1

    if not placed:
        return False

    by_slot: dict[int, list[Placed]] = {}
    for entry in placed:
        by_slot.setdefault(entry.slot_index, []).append(entry)

    for slot_index in sorted(by_slot):
        entries = sorted(by_slot[slot_index], key=lambda p: p.match.start)
        slot = writable[slot_index]
        base = offsets[slot_index]
        segments: list[tuple[str, object]] = []
        cursor = 0
        for entry in entries:
            start = entry.match.start - base
            end = entry.match.end - base
            if start > cursor:
                segments.append(("text", slot.text[cursor:start]))
            number = entry.item.number
            ref_id = f"vref-{chapter_index}-{number}-{len(entry.item.refs) + 1}"
            entry.item.refs.append(ref_id)

            mark = etree.Element(qualified(slot.parent, "span"))
            mark.set("class", "vocab")
            mark.set("id", ref_id)
            mark.set("data-vocab-id", entry.item.entry_id)
            mark.set("data-entry-id", entry.item.entry_id)
            mark.set("data-note", str(number))
            mark.set("data-chapter", str(chapter_index))
            mark.text = entry.match.surface
            segments.append(("link", mark))

            note = etree.Element(qualified(slot.parent, "span"))
            note.set("class", NOTE_CLASS)
            # 短义可能自带逗号/括号，校验器不能靠标点猜；用属性传原值。
            note.set("data-gloss", entry.item.gloss)
            # 11-11 拍板：正文括注不显示等级，只留 `(短义,[n])`。
            note.text = f"({entry.item.gloss},"
            link = etree.SubElement(note, qualified(slot.parent, "a"))
            link.set("class", "vocab-link")
            link.set("href", f"#vnote-{chapter_index}-{number}")
            link.text = f"[{number}]"
            link.tail = ")"
            segments.append(("link", note))
            cursor = end
        if cursor < len(slot.text):
            segments.append(("text", slot.text[cursor:]))
        _splice(slot.parent, slot.owner, segments)
    return True


def _pick_entry(entries: list[Entry], selected: list[str], gloss: str) -> Entry:
    """选出与本句释义对应的那一条词条（决定词性、等级、例句、完整释义）。"""

    by_id = {entry.entry_id: entry for entry in entries}
    for ident in selected:
        if ident in by_id:
            return by_id[ident]
    squeezed = re.sub(r"\s+", "", gloss)
    for entry in entries:
        if squeezed and squeezed in re.sub(r"\s+", "", entry.gloss):
            return entry
        for variant in sense_variants(entry.gloss):
            if squeezed in re.sub(r"\s+", "", variant):
                return entry
    return entries[0]


def _make_item(state: ChapterState, match: Match, gloss: str, usage: str,
               confidence: float, source: str, sense_id: str, entry: Entry) -> NoteItem:
    lex = state.lexicon
    if lex is not None:
        phonetic = lex.phonetic_for(match.surface, entry)
        form_info = lex.form_info_for(match.surface, entry)
    else:
        phonetic = entry.phonetic
        form_info = entry.form_info
    return NoteItem(
        number=state.next_number(),
        chapter=state.chapter.index,
        surface=match.surface,
        head=entry.head,
        entry_id=entry.entry_id,
        grades_full=full_grades([entry]),
        grade_short=short_grade(_grade_set([entry])),
        gloss=gloss,
        usage=usage,
        pos=entry.pos,
        full_gloss=entry.gloss,
        example=entry.example,
        extra=entry.extra,
        phonetic=phonetic,
        form_info=form_info,
        sense_id=sense_id,
        confidence=confidence,
        gloss_source=source,
    )


def _grade_set(entries: list[Entry]) -> set[str]:
    out: set[str] = set()
    for entry in entries:
        out.update(entry.grades)
    return out


def _insert_chapter_notes(analysis: DocAnalysis, chapter: Chapter, state: ChapterState) -> None:
    body = analysis.body
    ns = _namespace(body)
    section = etree.Element(f"{{{ns}}}section" if ns else "section")
    section.set("class", CHAPTER_NOTES_CLASS)
    section.set("id", f"vnotes-{chapter.index}")

    heading = etree.SubElement(section, f"{{{ns}}}h2" if ns else "h2")
    heading.set("class", "vocab-notes-title")
    heading.text = f"本章词汇（{chapter.label}）"

    listing = etree.SubElement(section, f"{{{ns}}}ol" if ns else "ol")
    listing.set("class", "vocab-list")
    for item in state.items:
        li = etree.SubElement(listing, f"{{{ns}}}li" if ns else "li")
        li.set("class", "vocab-item")
        li.set("id", f"vnote-{chapter.index}-{item.number}")

        head = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
        head.set("class", "vocab-head")
        num = etree.SubElement(head, f"{{{ns}}}span" if ns else "span")
        num.set("class", "vocab-num")
        num.text = f"[{item.number}]"
        word = etree.SubElement(head, f"{{{ns}}}span" if ns else "span")
        word.set("class", "vocab-word")
        word.text = " " + item.surface
        arrow = etree.SubElement(head, f"{{{ns}}}span" if ns else "span")
        arrow.set("class", "vocab-arrow")
        arrow.text = " → "
        lemma = etree.SubElement(head, f"{{{ns}}}span" if ns else "span")
        lemma.set("class", "vocab-lemma")
        lemma.text = item.head
        if item.grades_full:
            grades = etree.SubElement(head, f"{{{ns}}}span" if ns else "span")
            grades.set("class", "vocab-grade")
            grades.text = f" | {item.grades_full}"

        # 层 A/B 字段：有就显示，没有整行省略（不回退词头、不编造）。
        for css_class, label, value in (
            ("vocab-phonetic", "音标：", item.phonetic),
            ("vocab-form", "词形信息：", item.form_info),
        ):
            if value:
                line = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
                line.set("class", css_class)
                line.text = label + value

        sense = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
        sense.set("class", "vocab-sense")
        sense.set("data-gloss", item.gloss)
        sense.text = f"本句意思：{item.gloss}" + (f"（{item.usage}）" if item.usage else "")

        if item.pos:
            pos = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
            pos.set("class", "vocab-pos")
            pos.text = f"词表词性：{item.pos}"
        if item.full_gloss:
            gloss = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
            gloss.set("class", "vocab-gloss")
            gloss.text = f"词表完整释义：{item.full_gloss}"
        if item.example:
            example = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
            example.set("class", "vocab-example")
            example.text = f"词表例句：{item.example}"
        if item.extra:
            extra = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
            extra.set("class", "vocab-extra")
            extra.text = f"扩展说明：{item.extra}"

        back = etree.SubElement(li, f"{{{ns}}}p" if ns else "p")
        back.set("class", "vocab-back")
        for label, ref in enumerate(item.refs, start=1):
            link = etree.SubElement(back, f"{{{ns}}}a" if ns else "a")
            link.set("href", f"#{ref}")
            link.text = "返回正文" if len(item.refs) == 1 else f"返回正文({label})"
            if label != len(item.refs):
                link.tail = " "

    target = None
    if chapter.end_before_id:
        for el in body.iter():
            if el.get("id") == chapter.end_before_id:
                target = el
                break
    if target is not None:
        target.addprevious(section)
    else:
        body.append(section)


def _namespace(element) -> str:
    tag = element.tag if element is not None else ""
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _splice(parent, owner, segments: list[tuple[str, object]]) -> None:
    if owner is None:
        parent.text = ""
        position = None
    else:
        owner.tail = ""
        position = parent.index(owner) + 1
    for kind, value in segments:
        if kind == "text":
            if position is None:
                parent.text = (parent.text or "") + value
            else:
                previous = parent[position - 1]
                previous.tail = (previous.tail or "") + value
        else:
            if position is None:
                parent.insert(0, value)
                position = 1
            else:
                parent.insert(position, value)
                position += 1


def _ensure_css_link(tree, href: str) -> None:
    root = tree.getroot()
    head = None
    for el in root.iter():
        if local_name(el) == "head":
            head = el
            break
    if head is None:
        ns = _namespace(root)
        head = etree.Element(f"{{{ns}}}head" if ns else "head")
        root.insert(0, head)
    for child in head:
        if local_name(child) == "link" and (child.get("href") or "") == href:
            return
    ns = _namespace(root)
    link = etree.Element(f"{{{ns}}}link" if ns else "link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", href)
    head.append(link)


_CSS = """.vocab {
  border-bottom: 0.08em dotted #9a9a9a;
  text-decoration: none;
  color: inherit;
}
.vocab-note, .vocab-brief { color: #666; font-size: 0.82em; }
.vocab-link { color: #8a6a45; font-size: 0.82em; text-decoration: none; }
.vocab-chapter-notes {
  margin-top: 3em;
  border-top: 0.06em solid #ccc;
  padding-top: 1em;
  font-size: 0.95em;
}
.vocab-notes-title { font-size: 1.05em; margin: 0 0 0.8em; }
.vocab-list { padding-left: 0; list-style: none; margin: 0; }
.vocab-item { margin: 0 0 1.1em; padding-bottom: 0.6em; border-bottom: 1px dashed #e0dcd2; }
.vocab-head { margin: 0 0 0.3em; font-weight: bold; }
.vocab-num { color: #8a6a45; margin-right: 0.2em; }
.vocab-arrow { color: #999; }
.vocab-grade { color: #666; font-size: 0.85em; font-weight: normal; }
.vocab-phonetic { color: #555; font-size: 0.9em; margin: 0.12em 0 0.3em; }
.vocab-form { color: #666; font-size: 0.86em; margin: 0.12em 0 0.3em; }
.vocab-translation {
  color: #7a5a3a; font-size: 0.88em; margin: 0.2em 0 0.9em;
  padding-left: 0.7em; border-left: 2px solid #d9c7a8;
}
.vocab-translation-note { color: #999; font-size: 0.78em; font-weight: normal; }
.vocab-category { color: #b06a2c; font-size: 0.85em; font-weight: normal; }
.vocab-item p { margin: 0.15em 0; }
.vocab-sense { color: #222; }
.vocab-pos, .vocab-gloss, .vocab-example, .vocab-extra { color: #555; font-size: 0.92em; }
.vocab-member { color: #444; font-size: 0.92em; }
.vocab-member-word { font-weight: bold; }
.vocab-member-head { font-weight: bold; }
.vocab-member-label { color: #8a6a45; font-size: 0.88em; }
.vocab-member-gloss { color: #666; }
.vocab-source { color: #b06a2c; font-size: 0.88em; }
.vocab-back { margin-top: 0.4em; }
.vocab-back a { color: #8a6a45; text-decoration: none; font-size: 0.85em; }
/* 段后就近词条：章末块降级为隐藏数据层，读者只看段后这一份。 */
.vocab-chapter-notes.vocab-archive { display: none; }
.vocab-para-notes {
  margin: 0.55em 0 1.05em; padding: 0.4em 0.7em 0.5em;
  border-left: 3px solid #d9c2a0; background: #faf7f1;
  font-size: 0.9em; color: #4a443b;
}
.vocab-para-title { margin: 0 0 0.3em; font-size: 0.88em; font-weight: 600; color: #8a6d3b; }
.vocab-para-item { margin: 0 0 0.4em; padding: 0 0 0.35em; border-bottom: 1px dotted #e2d7c3; }
.vocab-para-item:last-child { border-bottom: 0; margin-bottom: 0; }
.vocab-para-notes p { margin: 0.1em 0; line-height: 1.45; }
.vocab-para-notes .vocab-back { display: none; }
.vocab-repeat-tag { color: #9c8a6f; font-weight: 400; font-size: 0.9em; }
"""

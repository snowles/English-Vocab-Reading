"""EPUB DOM 标注与最小包写回（阶段 1 无模型 + 阶段 2 harness 消歧）。

关键约束（见设计文档 §7、§9）：
    * 用 XML DOM 改文本节点，绝不对 XHTML 原始字符串做正则替换；
    * 只处理 spine 中的可见正文，跳过链接/隐藏/代码/脚注编号；
    * 未命中的章节**不重写**，保证未改动资源与源文件字节一致；
    * mimetype 保持首个且不压缩；输出写临时文件，校验通过后原子改名；
    * 已含本技能标注的输入直接拒绝，防止重复加标；
    * 语境消歧的**判断由宿主智能体（harness）做**，本模块只消费结构化 decisions，
      不含任何模型 SDK、网络请求。

呈现方式（`AnnotationOptions.presentation`）：
    footnote  章末脚注（默认）：正文只加虚线，释义在章末词注块里
    inline    **正文括注**：命中词后直接跟中文释义，如 `fished（钓鱼）`；
              无脚注、无词注块；`--glossary` 书末词表仍可用

标注密度（`AnnotationOptions.density`）：
    all      每处出现都标注
    chapter  每个词条在**每章只标首次出现**（默认）
    book     每个词条**全书只标首次出现**
    segment  每 `segment_words` 词算一段，每个词条**每段只标首次出现**
             （单文件长书没有章节边界时用它，避免标记全挤在开头）
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

from .binding import binding_path_for, is_annotated, is_xhtml, strip_vocab_metadata, write_binding
from .disambiguation import (
    DEFAULT_MIN_CONFIDENCE,
    Candidate,
    DecisionSet,
    SenseDecision,
    candidate_id,
    is_faithful_gloss,
    sense_variants,
)
from .epub_common import (
    Archive,
    EpubError,
    ManifestItem,
    Package,
    iter_text_slots,
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
    "AnnotationOptions",
    "ChapterResult",
    "AnnotationOutcome",
    "AlreadyAnnotatedError",
    "annotate_epub",
    "collect_candidates",
    "choose_output_path",
    "DENSITIES",
    "VOCAB_CSS",
]

OPS_NS = "http://www.idpf.org/2007/ops"
XHTML_NS = "http://www.w3.org/1999/xhtml"

DENSITIES = ("all", "chapter", "book", "segment")
PRESENTATIONS = ("footnote", "inline", "inline-notes")

#: 虚词/代词/助动词等"无释义价值"的高频功能词。
#: 只影响**是否标注**，不影响词表与匹配本身；报告计数 `skipped_stoplist`。
STOPLIST = frozenset("""
a an the and or but nor so yet if then than that this these those
there here when where why how what which who whom whose because
although though while until unless since after before as of to in
on at by for with about into over under up down out off from
is am are was were be been being has have had do does did
will would shall should can could may might must not no
it its he him his she her they them their we us our you your i me my
all any some such own same too very just also both each few more most
other another several only again further once
""".split())

VOCAB_CSS = """.vocab {
  border-bottom: 0.1em dotted #8a8a8a;
  text-decoration: none;
  color: inherit;
  cursor: help;
}
.vocab-notes {
  margin-top: 2.5em;
  border-top: 0.05em solid #c8c8c8;
  padding-top: 0.8em;
  font-size: 0.9em;
}
.vocab-notes ol { padding-left: 1.4em; }
.vocab-notes li { margin: 0.35em 0; line-height: 1.4; }
.vocab-notes-title { font-weight: bold; margin: 0 0 0.4em 0; }
.vocab-word { font-weight: bold; }
.vocab-grade { color: #666; font-size: 0.85em; }
.vocab-pos { font-style: italic; color: #555; }
.vocab-count { color: #666; font-size: 0.85em; }
.vocab-conf { color: #888; font-size: 0.8em; }
.vocab-sense { display: block; }
.vocab-back { margin-left: 0.4em; text-decoration: none; }
.vocab-note { color: #666; font-size: 0.82em; }
.vocab-glossary { margin: 1em; }
.vocab-glossary li { line-height: 1.5; margin: 0.3em 0; }
"""


class AlreadyAnnotatedError(EpubError):
    """输入已包含本技能标注，拒绝重复处理。"""


@dataclass
class AnnotationOptions:
    glossary: bool = False
    reduce_forms: bool = True
    grades: list[str] = field(default_factory=list)
    density: str = "chapter"
    presentation: str = "footnote"  # footnote | inline
    use_stoplist: bool = True
    max_note_chars: int = 12  # inline 括注限长；0 = 不截断
    segment_words: int = 1200
    decisions: DecisionSet | None = None
    undecided: str = "all-senses"  # all-senses | skip
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    include_multi_sense: bool = False
    max_context_chars: int = 300

    def __post_init__(self) -> None:
        if self.density not in DENSITIES:
            raise ValueError(
                f"未知的标注密度：{self.density}（可选 {', '.join(DENSITIES)}）"
            )
        if self.undecided not in ("all-senses", "skip", "lexicon"):
            raise ValueError("undecided 只能是 all-senses / skip / lexicon")
        if self.undecided == "lexicon" and self.presentation != "inline-notes":
            raise ValueError("undecided=lexicon 只用于 inline-notes 呈现")


@dataclass
class ChapterResult:
    index: int
    path: str
    label: str
    tokens: int = 0
    matches: int = 0
    words: int = 0
    phrases: int = 0
    footnotes: int = 0
    skipped_inside_link: int = 0
    skipped_cross_node: int = 0
    skipped_hidden: int = 0
    skipped_repeat: int = 0
    skipped_undecided: int = 0
    skipped_stoplist: int = 0
    candidates: int = 0
    per_entry: dict[str, int] = field(default_factory=dict)
    modified: bool = False

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "path": self.path,
            "label": self.label,
            "tokens": self.tokens,
            "matches": self.matches,
            "words": self.words,
            "phrases": self.phrases,
            "footnotes": self.footnotes,
            "skipped_inside_link": self.skipped_inside_link,
            "skipped_cross_node": self.skipped_cross_node,
            "skipped_hidden": self.skipped_hidden,
            "skipped_repeat": self.skipped_repeat,
            "skipped_undecided": self.skipped_undecided,
            "skipped_stoplist": self.skipped_stoplist,
            "candidates": self.candidates,
            "per_entry": dict(self.per_entry),
            "modified": self.modified,
        }


@dataclass
class AnnotationOutcome:
    source: str = ""
    output: str = ""
    version: str = ""
    css_path: str = ""
    glossary_path: str = ""
    density: str = "chapter"
    presentation: str = "footnote"
    spine_paths: list[str] = field(default_factory=list)
    modified_paths: list[str] = field(default_factory=list)
    chapters: list[ChapterResult] = field(default_factory=list)
    per_entry: dict[str, dict] = field(default_factory=dict)
    total_matches: int = 0
    total_footnotes: int = 0
    disambiguation: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "density": self.density,
            "presentation": self.presentation,
            "chapters": [c.as_dict() for c in self.chapters],
            "per_entry": {k: dict(v) for k, v in self.per_entry.items()},
            "modified_entries": list(self.modified_paths),
            "total_matches": self.total_matches,
            "total_footnotes": self.total_footnotes,
            "disambiguation": dict(self.disambiguation),
            "warnings": list(self.warnings),
        }


@dataclass
class Placed:
    """一条即将写入正文的标注。"""

    match: Match
    slot_index: int
    group: str
    entry_id: str
    gloss: str | None = None
    confidence: float | None = None


@dataclass
class FootnoteGroup:
    group: str
    display: str
    grade: str
    senses: list[tuple[str, str]] = field(default_factory=list)
    marks: int = 0
    occurrences: int = 0
    confidence: float | None = None


@dataclass
class DensityState:
    """跨章节共享的密度状态（"全书首次" 与 "分段首次" 需要连续计数）。"""

    book_seen: set[str] = field(default_factory=set)
    segment: int = 0
    segment_seen: set[str] = field(default_factory=set)
    words: int = 0


@dataclass
class ChapterScan:
    result: ChapterResult
    tree: object | None = None
    body: object | None = None
    slots: list = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    placed: list[Placed] = field(default_factory=list)
    groups: dict[str, FootnoteGroup] = field(default_factory=dict)
    footnote_ids: dict[str, str] = field(default_factory=dict)
    anchor_ids: dict[str, str] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# 输出路径
# ---------------------------------------------------------------------------


def choose_output_path(book_path: str | Path, out_dir: str | Path | None = None) -> Path:
    """`原文件名_vocab.epub`；已存在时加序号，绝不覆盖。"""

    source = Path(book_path)
    directory = Path(out_dir) if out_dir else source.parent
    if not directory.is_dir():
        raise EpubError(f"输出目录不存在：{directory}")
    base = source.stem + "_vocab"
    candidate = directory / f"{base}.epub"
    index = 2
    while candidate.exists():
        candidate = directory / f"{base}_{index}.epub"
        index += 1
    return candidate


# ---------------------------------------------------------------------------
# 单章扫描（纯计算，不改 DOM）
# ---------------------------------------------------------------------------


MIN_CONTEXT_CHARS = 60
NEIGHBOUR_BUDGET = 120


def _sentence_context(text: str, start: int, end: int, max_chars: int) -> str:
    """取命中词所在句子；句子过短时补邻近句子，但绝不导出整章。

    这是隐私边界：发给 harness 的上下文只到"句"级别，且总长受 max_chars 约束。
    """

    plain = text.replace(SEP, " ")
    separators = ".!?…\n"
    left = max(plain.rfind(ch, 0, start) for ch in separators)
    rights = [plain.find(ch, end) for ch in separators]
    rights = [r for r in rights if r >= 0]
    low, high = left + 1, (min(rights) + 1 if rights else len(plain))

    if high - low < MIN_CONTEXT_CHARS:
        previous = max(
            plain.rfind(ch, 0, max(0, low - 1)) for ch in separators
        )
        low = max(0, previous + 1, low - NEIGHBOUR_BUDGET)
        later = [plain.find(ch, high) for ch in separators]
        later = [n for n in later if n >= 0]
        limit = min(later) + 1 if later else len(plain)
        high = min(len(plain), limit, high + NEIGHBOUR_BUDGET)

    if high - low > max_chars:
        center = (start + end) // 2
        half = max_chars // 2
        low = max(low, center - half)
        high = min(high, center + half)

    return plain[low:high].strip()


def _needs_disambiguation(entries: list[Entry], include_multi_sense: bool) -> str:
    if len(entries) > 1:
        return "multi_entry"
    if include_multi_sense and len(sense_variants(entries[0].gloss)) > 1:
        return "multi_sense"
    return ""


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


def _judge(
    decision: SenseDecision | None,
    entries: list[Entry],
    opts: AnnotationOptions,
) -> tuple[SenseDecision | None, str]:
    """校验 harness 的决策。返回 (可用决策或 None, 原因码)。"""

    if decision is None:
        return None, "missing"
    if decision.confidence < opts.min_confidence:
        return None, "low_confidence"
    if not decision.is_target_sense:
        return None, "not_target"
    if not decision.short_gloss_zh:
        return None, "empty_gloss"

    options = {entry.entry_id: entry for entry in entries}
    selected = [i for i in decision.selected_entry_ids if i in options]
    if decision.selected_entry_ids and len(selected) != len(decision.selected_entry_ids):
        return None, "unknown_entry_id"
    glosses = [options[i].gloss for i in selected] if selected else [e.gloss for e in entries]
    if not is_faithful_gloss(decision.short_gloss_zh, glosses):
        return None, "unfaithful_gloss"
    return decision, ""


def scan_chapter(
    raw: bytes,
    item: ManifestItem,
    chapter_index: int,
    lexicon: Lexicon,
    opts: AnnotationOptions,
    state: DensityState,
) -> ChapterScan:
    """匹配 + 密度过滤 + 消歧判定，产出"要写什么"，不修改 DOM。"""

    result = ChapterResult(
        index=chapter_index, path=item.path, label=posixpath.basename(item.path)
    )
    scan = ChapterScan(result=result)
    tree = parse_xml(raw, item.path)
    root = tree.getroot()
    body = _find_body(root)
    if body is None:
        return scan
    scan.tree = tree
    scan.body = body

    slots = list(iter_text_slots(body))
    writable = [s for s in slots if s.writable]
    scan.slots = writable
    result.tokens = sum(len(TOKEN_RE.findall(s.text)) for s in writable)
    result.skipped_hidden = sum(
        1
        for s in slots
        if not s.writable
        and s.excluded in ("hidden", "aria-hidden", "css-hidden", "class-hidden")
    )
    for slot in slots:
        if slot.writable or slot.excluded != "inside-link":
            continue
        result.skipped_inside_link += len(
            find_matches(slot.text, lexicon, reduce_forms=opts.reduce_forms).matches
        )
    if not writable:
        return scan

    offsets: list[int] = []
    cursor = 0
    for slot in writable:
        offsets.append(cursor)
        cursor += len(slot.text) + len(SEP)
    scan.offsets = offsets
    combined = SEP.join(s.text for s in writable)

    matched = find_matches(combined, lexicon, reduce_forms=opts.reduce_forms)
    result.skipped_cross_node = matched.cross_node_phrases
    if not matched.matches:
        return scan

    occurrences: Counter = Counter()
    kept: list[tuple[int, Match]] = []
    seg_words = max(1, opts.segment_words)
    plain = combined.replace(SEP, " ") if opts.density == "segment" else ""
    cursor = 0
    for match in matched.matches:
        key = match.entry_key
        if opts.use_stoplist and key in STOPLIST:
            result.skipped_stoplist += 1
            continue
        index = occurrences[key]
        occurrences[key] += 1
        result.per_entry[key] = occurrences[key]

        keep = True
        if opts.density == "chapter":
            keep = index == 0
        elif opts.density == "book":
            keep = key not in state.book_seen
            if keep:
                state.book_seen.add(key)
        elif opts.density == "segment":
            state.words += len(plain[cursor : match.start].split())
            cursor = match.start
            while state.words >= (state.segment + 1) * seg_words:
                state.segment += 1
                state.segment_seen.clear()
            keep = key not in state.segment_seen
            if keep:
                state.segment_seen.add(key)
        if not keep:
            result.skipped_repeat += 1
            continue
        kept.append((index, match))

    for index, match in kept:
        entries = list(match.entries)
        key = match.entry_key
        slot_index = bisect.bisect_right(offsets, match.start) - 1

        entry_id = entries[0].entry_id
        gloss_override: str | None = None
        confidence: float | None = None
        group = key
        reason = _needs_disambiguation(entries, opts.include_multi_sense)

        if reason:
            cid = candidate_id(item.path, key, index)
            decision = opts.decisions.get(cid) if opts.decisions else None
            usable, verdict = _judge(decision, entries, opts)
            if usable is None and verdict == "not_target":
                scan.stats["not_target"] += 1
                result.skipped_undecided += 1
                continue
            if usable is None:
                if opts.decisions is not None:
                    scan.stats[verdict] += 1
                    if opts.undecided == "skip":
                        result.skipped_undecided += 1
                        continue
                else:
                    scan.stats["no_decisions_available"] += 1
            else:
                scan.stats["applied"] += 1
                gloss_override = usable.short_gloss_zh
                confidence = usable.confidence
                chosen = [e for e in entries if e.entry_id in usable.selected_entry_ids]
                if chosen:
                    entry_id = chosen[0].entry_id
                else:
                    # 只给了释义、没指定词条时，追溯锚点跟随"释义实际来源"，不盲取第一条
                    entry_id = (
                        _entry_for_gloss(usable.short_gloss_zh, entries)
                        or entries[0].entry_id
                    )
                group = f"{key}||{gloss_override}"

            scan.candidates.append(
                Candidate(
                    id=cid,
                    chapter=item.path,
                    chapter_index=chapter_index,
                    surface=match.surface,
                    head=entries[0].head,
                    entry_key=key,
                    occurrence=index,
                    context=_sentence_context(
                        combined, match.start, match.end, opts.max_context_chars
                    ),
                    options=_option_rows(entries),
                    reason=reason,
                )
            )

        scan.placed.append(
            Placed(
                match=match,
                slot_index=slot_index,
                group=group,
                entry_id=entry_id,
                gloss=gloss_override,
                confidence=confidence,
            )
        )

    result.matches = len(scan.placed)
    result.candidates = len(scan.candidates)
    for placed in scan.placed:
        if placed.match.kind == "phrase":
            result.phrases += 1
        else:
            result.words += 1

    _build_groups(scan, lexicon)

    existing_ids = {el.get("id") for el in root.iter() if el.get("id")}
    for group in scan.groups:
        slug = _group_slug(group)
        scan.footnote_ids[group] = _reserve_id(
            f"vocab-fn-{chapter_index}-{slug}", existing_ids
        )
        scan.anchor_ids[group] = _reserve_id(
            f"vocab-ref-{chapter_index}-{slug}", existing_ids
        )
    result.footnotes = len(scan.groups)
    return scan


def _build_groups(scan: ChapterScan, lexicon: Lexicon) -> None:
    for placed in scan.placed:
        group = placed.group
        info = scan.groups.get(group)
        if info is None:
            entries = lexicon.lookup(group.split("||")[0])
            if placed.gloss:
                senses = [(_pos_in(placed.entry_id, entries), placed.gloss)]
            else:
                senses = [(e.pos, e.gloss) for e in entries]
            info = FootnoteGroup(
                group=group,
                display=entries[0].head if entries else group.split("||")[0],
                grade="、".join(sorted({e.grade_raw for e in entries if e.grade_raw})),
                senses=[s for s in senses if s[1]],
                confidence=placed.confidence,
            )
            scan.groups[group] = info
        info.marks += 1
        key = group.split("||")[0]
        info.occurrences = max(info.occurrences, scan.result.per_entry.get(key, info.marks))


def _entry_for_gloss(short: str, entries: list[Entry]) -> str:
    """返回释义包含 `short` 的第一个词条 ID（用于追溯锚点）。"""

    squeezed = re.sub(r"\s+", "", short)
    if not squeezed:
        return ""
    for entry in entries:
        for variant in sense_variants(entry.gloss):
            if squeezed in re.sub(r"\s+", "", variant):
                return entry.entry_id
        if squeezed in re.sub(r"\s+", "", entry.gloss):
            return entry.entry_id
    return ""


def _pos_in(entry_id: str, entries: list[Entry]) -> str:
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry.pos
    return entries[0].pos if entries else ""


def _group_slug(group: str) -> str:
    key, _, gloss = group.partition("||")
    slug = _slug(key)
    if gloss:
        tag = hashlib.blake2s(gloss.encode("utf-8"), digest_size=3).hexdigest()
        slug = f"{slug}_{tag}"
    return slug


# ---------------------------------------------------------------------------
# 单章写回
# ---------------------------------------------------------------------------


def render_chapter(
    scan: ChapterScan,
    *,
    css_href: str,
    epub3: bool,
    presentation: str = "footnote",
    lexicon: Lexicon | None = None,
    max_note_chars: int = 12,
) -> bytes:
    by_slot: dict[int, list[Placed]] = {}
    for placed in scan.placed:
        by_slot.setdefault(placed.slot_index, []).append(placed)

    first_seen: set[str] = set()
    for slot_index in sorted(by_slot):
        placed_list = sorted(by_slot[slot_index], key=lambda p: p.match.start)
        slot = scan.slots[slot_index]
        base = scan.offsets[slot_index]
        segments: list[tuple[str, object]] = []
        cursor = 0
        for placed in placed_list:
            start = placed.match.start - base
            end = placed.match.end - base
            if start > cursor:
                segments.append(("text", slot.text[cursor:start]))
            group = placed.group

            if presentation == "inline":
                mark = etree.Element(qualified(slot.parent, "span"))
                mark.set("class", "vocab")
                mark.set("data-vocab-id", group.split("||")[0])
                mark.set("data-entry-id", placed.entry_id)
                if placed.confidence is not None:
                    mark.set("data-confidence", f"{placed.confidence:.2f}")
                mark.text = placed.match.surface
                segments.append(("link", mark))

                note = etree.Element(qualified(slot.parent, "span"))
                note.set("class", "vocab-note")
                note.text = f"（{_inline_gloss(placed, group, lexicon, max_note_chars)}）"
                segments.append(("link", note))
            else:
                anchor = etree.Element(qualified(slot.parent, "a"))
                anchor.set("class", "vocab")
                anchor.set("data-vocab-id", scan.footnote_ids[group])
                anchor.set("data-entry-id", placed.entry_id)
                anchor.set("href", "#" + scan.footnote_ids[group])
                if placed.confidence is not None:
                    anchor.set("data-confidence", f"{placed.confidence:.2f}")
                if group not in first_seen:
                    anchor.set("id", scan.anchor_ids[group])
                    first_seen.add(group)
                anchor.text = placed.match.surface
                segments.append(("link", anchor))
            cursor = end
        if cursor < len(slot.text):
            segments.append(("text", slot.text[cursor:]))
        _splice(slot.parent, slot.owner, segments)

    if presentation != "inline":
        _append_footnotes(scan.body, scan.result.index, scan, epub3=epub3)
    _ensure_css_link(scan.tree, css_href)
    return serialize_xml(scan.tree)


def _inline_gloss(
    placed: Placed, group: str, lexicon: Lexicon | None, max_chars: int = 12
) -> str:
    """inline 括注文本：短释义优先。

    消歧释义 > 第一个词条的第一个义项；`max_chars > 0` 时超长截断加省略号。
    正文呈现要短——完整释义用 footnote 模式或 `--glossary`。
    """

    if placed.gloss:
        text = placed.gloss
    elif lexicon is not None:
        entries = lexicon.lookup(group.split("||")[0])
        text = sense_variants(entries[0].gloss)[0] if entries else group.split("||")[0]
    else:
        text = group.split("||")[0]

    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip("；;、 ") + "…"
    return text


def _append_footnotes(body, chapter_index: int, scan: ChapterScan, *, epub3: bool) -> None:
    nsmap = {"epub": OPS_NS} if epub3 else None
    # EPUB 2 的 XHTML 1.1 没有 <aside>，降级为 <div>；EPUB 3 用 aside + epub:type
    container_tag = "aside" if epub3 else "div"
    aside = etree.SubElement(body, qualified(body, container_tag), nsmap=nsmap)
    aside.set("class", "vocab-notes")
    aside.set("id", f"vocab-notes-{chapter_index}")
    if epub3:
        aside.set(f"{{{OPS_NS}}}type", "footnote")

    heading = etree.SubElement(aside, qualified(body, "p"))
    heading.set("class", "vocab-notes-title")
    heading.text = "本章词汇"

    listing = etree.SubElement(aside, qualified(body, "ol"))
    for group in sorted(scan.groups, key=lambda g: g.split("||")[0]):
        info = scan.groups[group]
        li = etree.SubElement(listing, qualified(body, "li"))
        li.set("id", scan.footnote_ids[group])

        word = etree.SubElement(li, qualified(body, "span"))
        word.set("class", "vocab-word")
        word.text = info.display

        if info.grade:
            grade = etree.SubElement(li, qualified(body, "span"))
            grade.set("class", "vocab-grade")
            grade.text = " [" + info.grade + "]"

        if info.occurrences > info.marks:
            counter = etree.SubElement(li, qualified(body, "span"))
            counter.set("class", "vocab-count")
            counter.text = f" 本章出现 {info.occurrences} 次"
        elif info.marks > 1:
            counter = etree.SubElement(li, qualified(body, "span"))
            counter.set("class", "vocab-count")
            counter.text = f" ×{info.marks}"

        back = etree.SubElement(li, qualified(body, "a"))
        back.set("class", "vocab-back")
        back.set("href", "#" + scan.anchor_ids[group])
        back.set("title", "返回正文")
        back.text = "↩"

        for pos, gloss in info.senses:
            sense = etree.SubElement(li, qualified(body, "span"))
            sense.set("class", "vocab-sense")
            if pos:
                pos_el = etree.SubElement(sense, qualified(body, "span"))
                pos_el.set("class", "vocab-pos")
                pos_el.text = pos + " "
            gloss_el = etree.SubElement(sense, qualified(body, "span"))
            gloss_el.set("class", "vocab-gloss")
            gloss_el.text = gloss

        if info.confidence is not None:
            conf = etree.SubElement(li, qualified(body, "span"))
            conf.set("class", "vocab-conf")
            conf.text = f" 按语境选择 · 置信 {info.confidence:.2f}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _assert_not_annotated(archive: Archive, package: Package) -> None:
    for spine_item in package.spine:
        item = spine_item.item
        if not item.is_xhtml:
            continue
        if is_annotated(archive.read(item.path)):
            raise AlreadyAnnotatedError(
                "输入 EPUB 已包含本技能的词汇标注（vocab-skill / data-vocab-id），"
                "已拒绝重复处理；请使用原始文件重新生成。"
            )


def _spine_documents(archive: Archive, package: Package):
    seen: set[str] = set()
    for spine_item in package.spine:
        item = spine_item.item
        if item.path in seen or not item.is_xhtml:
            continue
        seen.add(item.path)
        yield spine_item, archive.read(item.path)


def collect_candidates(
    book_path: str | Path,
    lexicon: Lexicon,
    options: AnnotationOptions | None = None,
) -> tuple[list[Candidate], dict]:
    """只扫描、不写出：产出给 harness 的消歧候选。"""

    opts = options or AnnotationOptions()
    if opts.presentation == "inline-notes":
        from .inline_notes import collect_candidates as collect_v2

        return collect_v2(
            book_path,
            lexicon,
            reduce_forms=opts.reduce_forms,
            use_stoplist=opts.use_stoplist,
            max_context_chars=opts.max_context_chars,
        )
    archive, package = open_epub(book_path)
    try:
        state = DensityState()
        candidates: list[Candidate] = []
        chapters = 0
        for spine_item, raw in _spine_documents(archive, package):
            scan = scan_chapter(
                raw, spine_item.item, spine_item.index, lexicon, opts, state
            )
            chapters += 1
            candidates.extend(scan.candidates)
        return candidates, {
            "title": package.title,
            "opf": package.opf_path,
            "version": package.version,
            "chapters_scanned": chapters,
        }
    finally:
        archive.close()


def annotate_epub(
    book_path: str | Path,
    lexicon: Lexicon,
    options: AnnotationOptions | None = None,
    *,
    out_dir: str | Path | None = None,
    output_path: str | Path | None = None,
) -> AnnotationOutcome:
    """标注一本书并写出新 EPUB。调用方必须随后运行校验器再宣告成功。"""

    opts = options or AnnotationOptions()
    if opts.presentation == "inline-notes":
        from .inline_notes import render_book

        return render_book(
            book_path, lexicon, opts, out_dir=out_dir, output_path=output_path
        )
    outcome = AnnotationOutcome(
        source=str(book_path), density=opts.density, presentation=opts.presentation
    )
    archive, package = open_epub(book_path)
    try:
        _assert_not_annotated(archive, package)
        out_path = (
            Path(output_path) if output_path else choose_output_path(book_path, out_dir)
        )
        if out_path.exists():
            raise EpubError(f"输出文件已存在，拒绝覆盖：{out_path}")

        css_path = _unique_resource_path(package, archive, "vocab.css", "vocab-skill.css")
        css_href = relative_href(css_path, package.opf_dir)

        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}
        entry_stats: dict[str, dict] = {}
        stats: Counter = Counter()
        state = DensityState()
        pending: list[Candidate] = []

        for spine_item, raw in _spine_documents(archive, package):
            item = spine_item.item
            scan = scan_chapter(raw, item, spine_item.index, lexicon, opts, state)
            outcome.chapters.append(scan.result)
            pending.extend(scan.candidates)
            stats.update(scan.stats)
            for key, count in scan.result.per_entry.items():
                slot = entry_stats.setdefault(
                    key,
                    {
                        "display": lexicon.lookup(key)[0].head
                        if lexicon.lookup(key)
                        else key,
                        "gloss": _gloss_summary(lexicon, key),
                        "count": 0,
                    },
                )
                slot["count"] = max(slot["count"], count)
            if scan.placed:
                replacements[item.path] = render_chapter(
                    scan,
                    css_href=relative_href(css_path, posixpath.dirname(item.path)),
                    epub3=package.is_epub3,
                    presentation=opts.presentation,
                    lexicon=lexicon,
                    max_note_chars=opts.max_note_chars,
                )
                scan.result.modified = True
                outcome.modified_paths.append(item.path)
                outcome.spine_paths.append(item.path)
                if opts.presentation == "inline":
                    scan.result.footnotes = 0

        outcome.per_entry = entry_stats
        outcome.total_matches = sum(c.matches for c in outcome.chapters)
        outcome.total_footnotes = sum(c.footnotes for c in outcome.chapters)
        outcome.disambiguation = {
            "mode": "harness" if opts.decisions is not None else "off",
            "density": opts.density,
            "presentation": opts.presentation,
            "segment_words": opts.segment_words if opts.density == "segment" else None,
            "include_multi_sense": opts.include_multi_sense,
            "min_confidence": opts.min_confidence,
            "undecided_policy": opts.undecided,
            "candidates": len(pending),
            "decisions_loaded": len(opts.decisions) if opts.decisions else 0,
            "decisions_applied": stats.get("applied", 0),
            "missing_decision": stats.get("missing", 0),
            "low_confidence": stats.get("low_confidence", 0),
            "no_decisions_available": stats.get("no_decisions_available", 0),
            "stoplist_skipped": sum(c.skipped_stoplist for c in outcome.chapters),
            "max_note_chars": opts.max_note_chars if opts.presentation == "inline" else None,
            "invalid": (
                stats.get("unknown_entry_id", 0)
                + stats.get("unfaithful_gloss", 0)
                + stats.get("empty_gloss", 0)
            ),
            "not_target": stats.get("not_target", 0),
        }
        if opts.decisions is None and pending:
            outcome.warnings.append(
                f"{len(pending)} 处命中存在多义/多词条，本次未做语境消歧"
                "（如需消歧，见 SKILL.md「阶段 2：由 harness 消歧」）"
            )
        if opts.decisions is not None and stats.get("missing", 0):
            outcome.warnings.append(
                f"{stats['missing']} 处候选没有对应决策，按 {opts.undecided} 策略处理"
            )

        if outcome.total_matches == 0:
            outcome.warnings.append("没有产生任何标注，输出为原书副本")
            _write_epub(archive, out_path, {}, {})
            outcome.output = str(out_path)
            outcome.version = package.version
            return outcome

        manifest_el = package.manifest_element()
        spine_el = package.spine_element()

        if css_path not in archive.names:
            additions[css_path] = VOCAB_CSS.encode("utf-8")
        if package.item_by_path(css_path) is None:
            _add_manifest_item(
                manifest_el,
                item_id=_unique_id(package, "vocab-css"),
                href=css_href,
                media_type="text/css",
            )
        outcome.css_path = css_path

        if opts.glossary and entry_stats:
            glossary_path = _unique_resource_path(
                package, archive, "vocab-glossary.xhtml", "vocab-skill-glossary.xhtml"
            )
            additions[glossary_path] = _build_glossary(
                entry_stats,
                chapter_count=len(outcome.chapters),
                css_href=relative_href(css_path, posixpath.dirname(glossary_path)),
                title=f"{package.title or '本书'} 词汇表",
                epub3=package.is_epub3,
                density=opts.density,
            )
            glossary_id = _unique_id(package, "vocab-glossary")
            _add_manifest_item(
                manifest_el,
                item_id=glossary_id,
                href=relative_href(glossary_path, package.opf_dir),
                media_type="application/xhtml+xml",
            )
            itemref = etree.SubElement(spine_el, qualified(spine_el, "itemref"))
            itemref.set("idref", glossary_id)
            outcome.glossary_path = glossary_path
            outcome.modified_paths.append(glossary_path)

        # §22.4：_write_epub 统一净化，EPUB 2 产物只保留 XHTML 1.1 合法的
        # id/class/href；内部标注信息在包外 *.vocab-map.json。无需再提示 DTD 风险。

        replacements[package.opf_path] = serialize_xml(package.tree)
        outcome.modified_paths.append(package.opf_path)

        _write_epub(archive, out_path, replacements, additions)
        outcome.output = str(out_path)
        outcome.version = package.version
        return outcome
    finally:
        archive.close()


# ---------------------------------------------------------------------------
# DOM 工具
# ---------------------------------------------------------------------------


def _splice(parent, owner, segments: list[tuple[str, object]]) -> None:
    """把 (文本|元素) 片段按序写回 parent.text / owner.tail 位置。

    必须**先清空**原槽位文本，否则原 tail 会与片段内容重复。
    """

    if owner is None:
        parent.text = ""
        position = None
    else:
        owner.tail = ""
        position = parent.index(owner) + 1

    for kind, value in segments:
        if kind == "text":
            if position is None:
                parent.text = (parent.text or "") + value  # type: ignore[operator]
            else:
                previous = parent[position - 1]
                previous.tail = (previous.tail or "") + value  # type: ignore[operator]
        else:
            if position is None:
                parent.insert(0, value)  # type: ignore[arg-type]
                position = 1
            else:
                parent.insert(position, value)  # type: ignore[arg-type]
                position += 1


def _find_body(root):
    for el in root.iter():
        if local_name(el) == "body":
            return el
    return None


def _find_head(root):
    for el in root.iter():
        if local_name(el) == "head":
            return el
    return None


def _ensure_css_link(tree, href: str) -> None:
    root = tree.getroot()
    head = _find_head(root)
    if head is None:
        head = etree.Element(qualified(root, "head"))
        root.insert(0, head)
    for child in head:
        if local_name(child) == "link" and (child.get("href") or "") == href:
            return
    link = etree.Element(qualified(root, "link"))
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", href)
    head.append(link)


def _slug(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return slug or "word"


def _reserve_id(base: str, existing: set[str]) -> str:
    if base not in existing:
        existing.add(base)
        return base
    index = 1
    while f"{base}-{index}" in existing:
        index += 1
    new = f"{base}-{index}"
    existing.add(new)
    return new


def _gloss_summary(lexicon: Lexicon, key: str) -> str:
    return "；".join(e.gloss for e in lexicon.lookup(key))


def _unique_resource_path(
    package: Package, archive: Archive, preferred: str, fallback: str
) -> str:
    base = package.opf_dir
    candidate = posixpath.join(base, preferred) if base else preferred
    if not archive.exists(candidate) and package.item_by_path(candidate) is None:
        return candidate
    return posixpath.join(base, fallback) if base else fallback


def _unique_id(package: Package, base: str) -> str:
    if base not in package.items:
        return base
    index = 2
    while f"{base}-{index}" in package.items:
        index += 1
    return f"{base}-{index}"


def _add_manifest_item(manifest_el, *, item_id: str, href: str, media_type: str) -> None:
    item = etree.SubElement(manifest_el, qualified(manifest_el, "item"))
    item.set("id", item_id)
    item.set("href", href)
    item.set("media-type", media_type)


# ---------------------------------------------------------------------------
# 词表页 / 包写回
# ---------------------------------------------------------------------------


def _build_glossary(
    entry_stats: dict[str, dict],
    *,
    chapter_count: int,
    css_href: str,
    title: str,
    epub3: bool = True,
    density: str = "chapter",
) -> bytes:
    ns = XHTML_NS
    root = etree.Element(f"{{{ns}}}html", nsmap={None: ns})
    head = etree.SubElement(root, f"{{{ns}}}head")
    meta = etree.SubElement(head, f"{{{ns}}}meta")
    if epub3:
        meta.set("charset", "utf-8")
    else:
        meta.set("http-equiv", "Content-Type")
        meta.set("content", "application/xhtml+xml; charset=utf-8")
    title_el = etree.SubElement(head, f"{{{ns}}}title")
    title_el.text = title
    link = etree.SubElement(head, f"{{{ns}}}link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", css_href)

    body = etree.SubElement(root, f"{{{ns}}}body")
    body.set("class", "vocab-glossary")
    heading = etree.SubElement(body, f"{{{ns}}}h1")
    heading.text = title
    intro = etree.SubElement(body, f"{{{ns}}}p")
    intro.set("class", "vocab-intro")
    density_hint = {
        "chapter": "每个词条每章标注首次出现",
        "book": "每个词条全书标注首次出现",
        "segment": "每个词条每段标注首次出现",
        "all": "所有出现均已标注",
    }.get(density, "")
    intro.text = (
        f"本书共标注 {len(entry_stats)} 个词条，覆盖 {chapter_count} 个章节"
        f"（{density_hint}）。"
    )

    listing = etree.SubElement(body, f"{{{ns}}}ul")
    for key in sorted(entry_stats, key=lambda k: k.casefold()):
        info = entry_stats[key]
        li = etree.SubElement(listing, f"{{{ns}}}li")
        word = etree.SubElement(li, f"{{{ns}}}span")
        word.set("class", "vocab-word")
        word.text = info["display"]
        counter = etree.SubElement(li, f"{{{ns}}}span")
        counter.set("class", "vocab-count")
        counter.text = f" ×{info['count']}"
        sense = etree.SubElement(li, f"{{{ns}}}span")
        sense.set("class", "vocab-sense")
        sense.text = " " + info["gloss"]

    return serialize_xml(root.getroottree())


def _write_epub(
    archive: Archive,
    out_path: Path,
    replacements: dict[str, bytes],
    additions: dict[str, bytes],
) -> None:
    """写临时文件 -> 原子改名；失败清理，绝不留下半成品。

    §22.4：写盘前统一净化 XHTML——把本工具写入的 ``data-*`` 剥到包外映射
    文件、章末 ``<section>`` 换成 ``<div>``、给标注节点加合法 class 标记。
    三条渲染引擎（units / inline-notes / footnote）共用这一层，EPUB 2
    产物因此只含 XHTML 1.1 合法元素与属性。EPUB 自身的正文、词注、回链
    全部保留，阅读不依赖映射文件。
    """

    clean = dict(replacements)
    records: dict[str, dict] = {}
    for name, data in replacements.items():
        if not is_xhtml(name):
            continue
        stripped, found = strip_vocab_metadata(data, name)
        if found or stripped != data:
            clean[name] = stripped
            records.update(found)

    temp = out_path.with_name(out_path.name + f".part{os.getpid()}")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zout:
            _write_zip_entries(archive, zout, clean, additions)
        os.replace(temp, out_path)
        if records:
            write_binding(binding_path_for(out_path, pending=True), out_path, records)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _write_zip_entries(
    archive: Archive,
    zout: zipfile.ZipFile,
    replacements: dict[str, bytes],
    additions: dict[str, bytes],
) -> None:
    if archive.exists("mimetype"):
        info = copy.copy(archive.info("mimetype"))
    else:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    zout.writestr(info, b"application/epub+zip")

    for name in archive.names:
        if name == "mimetype":
            continue
        data = replacements.get(name)
        if data is None:
            with archive.zipfile.open(name) as handle:
                data = handle.read()
        zout.writestr(copy.copy(archive.info(name)), data)

    for name, data in additions.items():
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(info, data)

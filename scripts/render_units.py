"""§18 语义单位渲染：正文短注 + 段后词条 + 对照表。

规则要点：

    * 词条块就近放在自然段后（短段合并成一组，长段单独成组），章末块只保留
      为隐藏数据层，供校验、包外映射与阅读页名单使用（11-11 读者规格）；
    * 正文标签只来自「整体真实命中的词库条目」；只命中成员时**不显示等级**，
      段后词条按成员分别列出各自的标签与词表原义（§18.3）；
    * 未收录词不写"补充"：有确切用法类别才显示「方/名/专/书/俚」，普通词只有短义；
    * 合并项内部不再单独标注；一个区间只产生一个短注与一个编号；
    * 词表原始资料（释义/词性/例句/扩展说明）与语境短义分开保存。
"""

from __future__ import annotations

import bisect
import copy
import html
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .binding import is_annotated
from .chapters import Chapter, detect_chapters, doc_chapter_map
from .epub_common import (
    EpubError,
    local_name,
    open_epub,
    qualified,
    relative_href,
    serialize_xml,
)
from .inline_notes import _CSS
from .load_lexicon import Lexicon
from .matching import normalize_span
from .reader_mode import looks_like_title_page
from .units import (
    USAGE_CATEGORIES,
    Annotation,
    AnnotationSet,
    Component,
    Paragraph,
    document_paragraphs,
    is_title_block,
    validate_annotations,
)

__all__ = ["render_units", "build_comparison_table"]

# 段后词条分组口径，与外机试跑 v1 的版式重排一致：
# 长段自己一组；短段最多连续 4 段、或累计 110 词就出一组，避免小块刷屏。
PARA_SHORT_WORDS = 60
PARA_MAX_SHORT = 4
PARA_GROUP_WORDS = 110


@dataclass
class NoteItem:
    number: int
    chapter: int
    surface: str
    lemma: str = ""
    gloss: str = ""
    usage: str = ""
    tag: str = ""                 # 正文本体标签（等级简称或用法类别，可空）
    labels_full: str = ""
    label_scope: str = "none"
    source_status: str = "out_of_lexicon"
    unit_type: str = "word"
    pos: str = ""
    full_gloss: str = ""
    example: str = ""
    extra: str = ""
    phonetic: str = ""          # 被标注词形自己的 L1 音标；无则整行省略
    form_info: str = ""         # 中文可读词形信息；无则整行省略
    context_only: bool = False
    components: list[Component] = field(default_factory=list)
    evidence: str = ""
    review_status: str = ""
    refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "surface": self.surface,
            "lemma": self.lemma,
            "gloss": self.gloss,
            "usage": self.usage,
            "tag": self.tag,
            "labels_full": self.labels_full,
            "label_scope": self.label_scope,
            "source_status": self.source_status,
            "unit_type": self.unit_type,
            "pos": self.pos,
            "full_gloss": self.full_gloss,
            "components": [c.as_dict() for c in self.components],
            "evidence": self.evidence,
            "review_status": self.review_status,
        }


@dataclass
class ChapterState:
    chapter: Chapter
    density: str = "chapter"
    lexicon: Lexicon | None = None
    items: list[NoteItem] = field(default_factory=list)
    by_key: dict[tuple[str, str], NoteItem] = field(default_factory=dict)
    seen: set[tuple[str, str]] = field(default_factory=set)
    marks: int = 0
    skipped_density: int = 0
    skipped_render: int = 0
    notes_written: bool = False
    # 段后词条：按段落记录本段新出现的词条与正文 [n] 链接，供分组时定位。
    para_items: dict[str, list[NoteItem]] = field(default_factory=dict)
    para_links: dict[str, list[tuple[object, int]]] = field(default_factory=dict)

    def next_number(self) -> int:
        return len(self.items) + 1


def _dedup_key(annotation: Annotation) -> tuple[str, str]:
    """编号复用单位：单词按「词头+义项」，组合按「整段表面+义项」。"""

    if annotation.unit_type == "word":
        base = normalize_span(annotation.lemma or annotation.surface)
    else:
        base = normalize_span(annotation.surface)
    return (base, normalize_span(annotation.gloss))


# 审计整改 P2：不再用中文字符串比较推断「词库未列该用法」。
# ``荒漠/沙漠``、``昏倒/晕倒`` 只是同义改写，不是词库缺义；章末统一并列
# 「本句意思」与「词表原义」。确需断言缺义时，只能由语义审查明确给出结论，
# 并纳入被审内容哈希。


def render_units(
    book_path: str | Path,
    lexicon: Lexicon,
    annotations: AnnotationSet,
    *,
    density: str = "chapter",
    out_dir: str | Path | None = None,
    output_path: str | Path | None = None,
    glossary: bool = False,
    comparison_path: str | Path | None = None,
    allow_partial: bool = False,
    reading_level: str = "standard",
    word_list=None,
    translations=None,
    reader_mode: str = "advanced",
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

    from .inspect_book import sha256_file

    book_sha = sha256_file(book_path)
    archive, package = open_epub(book_path)
    try:
        for spine_item in package.spine:
            if spine_item.item.is_xhtml and is_annotated(
                archive.read(spine_item.item.path)
            ):
                raise AlreadyAnnotatedError(
                    "输入 EPUB 已包含标注，拒绝重复处理；请使用原始文件。"
                )

        out_path = (
            Path(output_path) if output_path else choose_output_path(book_path, out_dir)
        )
        if out_path.exists():
            raise EpubError(f"输出文件已存在，拒绝覆盖：{out_path}")

        chapters = detect_chapters(archive, package)
        spine_paths = [s.item.path for s in package.spine]
        chapter_map = doc_chapter_map(chapters, spine_paths)
        states = {
            c.index: ChapterState(chapter=c, density=density, lexicon=lexicon)
            for c in chapters
        }

        documents: dict[str, tuple[object, list[Paragraph]]] = {}
        paragraphs: dict[str, Paragraph] = {}
        for spine_item in package.spine:
            item = spine_item.item
            if not item.is_xhtml:
                continue
            default, id_map = chapter_map.get(item.path, (0, {}))
            tree, plist = document_paragraphs(
                archive.read(item.path), item.path, default, id_map, book_sha
            )
            documents[item.path] = (tree, plist)
            for paragraph in plist:
                if paragraph.chapter > 0:
                    paragraphs[paragraph.pid] = paragraph

        usable, stats = validate_annotations(annotations, paragraphs, lexicon)
        if stats["invalid"] and not allow_partial:
            codes = "、".join(f"{k}×{v}" for k, v in stats["invalid_codes"].items())
            raise EpubError(
                f"有 {stats['invalid']} 条标注未通过硬校验（{codes}），按默认策略不生成成品；"
                "请修正决策文件，或显式加 --allow-partial 丢弃无效项后重新编号"
            )
        # 第 20 节 A 批：提示量档位与本地熟词名单只决定「显示哪些」，
        # 不影响已完成的审查结论与硬校验；被筛除的原因单独记录。
        from .reading_prefs import decide

        reading = decide(usable, level=reading_level, word_list=word_list)
        by_pid: dict[str, list[Annotation]] = {}
        for annotation in reading.kept:
            by_pid.setdefault(annotation.pid, []).append(annotation)

        css_path = _unique_resource_path(package, archive, "vocab.css", "vocab-skill.css")
        css_href = relative_href(css_path, package.opf_dir)
        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}
        modified: list[str] = []

        # 书名页（书名 / 作者 / 副题）整页不译，即便宿主多给了译文也不印进书。
        title_docs = {
            name
            for name, (_tree, plist) in documents.items()
            if looks_like_title_page(plist)
        }

        for doc, (tree, plist) in documents.items():
            changed = False
            for paragraph in plist:
                if paragraph.chapter <= 0:
                    continue
                items = by_pid.get(paragraph.pid) or []
                if items:
                    state = states.get(paragraph.chapter)
                    if state is not None:
                        changed |= _render_paragraph(paragraph, items, state)
                # 初级模式：译文直接显示在自然段后（EPUB 不支持点击，不做折叠）。
                if (
                    reader_mode == "basic"
                    and translations is not None
                    and doc not in title_docs
                ):
                    changed |= _append_translation(paragraph, translations)

            # 词条块就近落到自然段后（短段合并）；章末块降级为隐藏数据层。
            if _insert_para_notes(plist, states, doc):
                changed = True

            for chapter in chapters:
                if chapter.end_path != doc:
                    continue
                state = states.get(chapter.index)
                if state is None or not state.items or state.notes_written:
                    continue
                _insert_notes(tree, chapter, state)
                state.notes_written = True
                changed = True

            if changed:
                _ensure_css_link(tree, relative_href(css_path, posixpath.dirname(doc)))
                replacements[doc] = serialize_xml(tree)
                modified.append(doc)

        outcome = AnnotationOutcome(
            source=str(book_path), density=density, presentation="inline-notes"
        )
        outcome.output = str(out_path)
        outcome.version = package.version
        outcome.css_path = css_path
        outcome.modified_paths = modified
        outcome.spine_paths = modified
        outcome.total_matches = sum(state.marks for state in states.values())
        outcome.total_footnotes = sum(len(state.items) for state in states.values())
        outcome.chapters = [
            ChapterResult(
                index=index,
                path=state.chapter.start_path,
                label=state.chapter.label,
                matches=state.marks,
                footnotes=len(state.items),
                skipped_repeat=state.skipped_density,
                modified=bool(state.items),
            )
            for index, state in sorted(states.items())
        ]

        entry_stats: dict[str, dict] = {}
        for state in states.values():
            for item in state.items:
                key = item.lemma or item.surface
                slot = entry_stats.setdefault(
                    key,
                    {
                        "display": item.lemma or item.surface,
                        "gloss": item.gloss or item.full_gloss,
                        "grade": item.tag,
                        "count": 0,
                    },
                )
                slot["count"] += 1
        outcome.per_entry = entry_stats

        categories = dict(stats.get("category_used") or {})
        outcome.disambiguation = {
            "mode": "harness-v5",
            "protocol": "v5",
            "density": density,
            "presentation": "inline-notes",
            "annotations_received": stats["total"],
            "accepted": stats["accepted"],
            "exact_entry": stats["exact_entry"],
            "component_entries": stats["component_entries"],
            "out_of_lexicon": stats["out_of_lexicon"],
            "form_match_failed": stats["form_match_failed"],
            "filtered_out": stats["filtered_out"],
            "merged_units": stats["merged_units"],
            "component_resolved": stats["component_resolved"],
            "component_filtered": stats["component_filtered"],
            "component_unlinked": stats["component_unlinked"],
            "context_sense": stats["sensed_as_context"],
            "usage_categories": categories,
            "gloss_coverage_warnings": stats["gloss_coverage_warnings"],
            # 审计备注只进报告，不渲染到 EPUB（读者说明见 reader_usage_note）
            "audit_notes": [
                {"annotation_id": a.annotation_id, "note": a.audit_note}
                for a in usable
                if a.audit_note
            ],
            "invalid": stats["invalid"],
            "invalid_codes": dict(stats["invalid_codes"]),
            "skipped_unsupported": stats["skipped_unsupported"],
            # 是否可发布由发布门槛（scope/review/structural）决定，
            # 渲染层不自行宣称可发布；报告写入前会被 release.publishable 覆盖。
            "publishable": False,
            "allow_partial": bool(allow_partial),
            "rejected_range": stats["range_mismatch"],
            "rejected_overlap": stats["overlap"],
            "rejected_unreviewed": stats["unreviewed"],
            "rejected_pending": stats["pending_review"],
            "rejected_by_review": stats["rejected_review"],
            "rejected_stale_review": stats["stale_review"],
            "rejected_unknown_entry": stats["bad_entry"],
            "rejected_other": stats["unknown_pid"] + stats["empty_gloss"],
            "skipped_density": sum(s.skipped_density for s in states.values()),
            "skipped_render": sum(s.skipped_render for s in states.values()),
            # 第 20 节 A 批：档位、熟词名单与主动筛除明细（与写回数量对得上）
            "reading": reading.report(word_list, density),
            "rendered_items": outcome.total_footnotes,
            "rendered_marks": outcome.total_matches,
        }

        if annotations.issues:
            outcome.warnings.append(
                f"程序校验发现 {len(annotations.issues)} 条问题（见报告 issues）"
            )
        warnings = [w for a in usable for w in a.warnings]
        if warnings:
            outcome.warnings.extend(dict.fromkeys(warnings))

        if comparison_path:
            _write_comparison(
                Path(comparison_path),
                states,
                usable,
                paragraphs,
                book_title=package.title,
                stats=stats,
            )

        manifest_el = package.manifest_element()
        if outcome.total_footnotes == 0:
            # 审计整改：禁止「复制一本原书冒充成果」。
            # 完整审查后确实无内容可标时，由发布门槛报 no_annotations，不进入渲染。
            raise EpubError(
                "没有可写回的标注：拒绝复制原书冒充成果。"
                "若确认无内容值得标注，应通过审查门槛报告 no_annotations。"
            )

        if css_path not in archive.names:
            additions[css_path] = _CSS.encode("utf-8")
        if package.item_by_path(css_path) is None:
            el = etree.SubElement(manifest_el, qualified(manifest_el, "item"))
            el.set("id", _unique_id(package, "vocab-css"))
            el.set("href", css_href)
            el.set("media-type", "text/css")

        if glossary:
            if entry_stats:
                glossary_path = _unique_resource_path(
                    package, archive, "vocab-glossary.xhtml", "vocab-skill-glossary.xhtml"
                )
                additions[glossary_path] = _build_glossary(
                    entry_stats,
                    chapter_count=len(chapters),
                    css_href=relative_href(css_path, posixpath.dirname(glossary_path)),
                    title=f"{package.title or '本书'} 词汇表",
                    epub3=package.is_epub3,
                    density=density,
                )
                gid = _unique_id(package, "vocab-glossary")
                el = etree.SubElement(manifest_el, qualified(manifest_el, "item"))
                el.set("id", gid)
                el.set("href", relative_href(glossary_path, package.opf_dir))
                el.set("media-type", "application/xhtml+xml")
                spine_el = package.spine_element()
                itemref = etree.SubElement(spine_el, qualified(spine_el, "itemref"))
                itemref.set("idref", gid)
                outcome.glossary_path = glossary_path

        replacements[package.opf_path] = serialize_xml(package.tree)
        modified.append(package.opf_path)
        outcome.modified_paths = modified
        _write_epub(archive, out_path, replacements, additions)
        return outcome
    finally:
        archive.close()


def _render_paragraph(
    paragraph: Paragraph, items: list[Annotation], state: ChapterState
) -> bool:
    placed: list[tuple[Annotation, int, int]] = []
    for annotation in sorted(items, key=lambda a: a.span[0]):
        key = _dedup_key(annotation)
        if state.density != "all" and state.chapter.index and key in state.seen:
            state.skipped_density += 1
            continue

        index = bisect.bisect_right(paragraph.offsets, annotation.span[0]) - 1
        if index < 0:
            state.skipped_render += 1
            continue
        slot = paragraph.slots[index]
        local = annotation.span[0] - paragraph.offsets[index]
        length = annotation.span[1] - annotation.span[0]
        if local + length > len(slot.text):
            # 跨内联节点：保留语义结果，报告渲染跳过（不破坏标签）
            state.skipped_render += 1
            continue
        state.seen.add(key)
        placed.append((annotation, index, local))

    if not placed:
        return False

    para_items = state.para_items.setdefault(paragraph.pid, [])
    para_links = state.para_links.setdefault(paragraph.pid, [])
    para_keys = {(i.chapter, i.number) for i in para_items}

    by_slot: dict[int, list[tuple[Annotation, int]]] = {}
    for annotation, index, local in placed:
        by_slot.setdefault(index, []).append((annotation, local))

    for index, group in by_slot.items():
        slot = paragraph.slots[index]
        group.sort(key=lambda item: item[1])
        segments: list[tuple[str, object]] = []
        cursor = 0
        for annotation, local in group:
            length = annotation.span[1] - annotation.span[0]
            if local > cursor:
                segments.append(("text", slot.text[cursor:local]))

            key = _dedup_key(annotation)
            item = state.by_key.get(key)
            if item is None:
                item = _make_item(state, annotation)
                state.by_key[key] = item
                state.items.append(item)
            if (item.chapter, item.number) not in para_keys:
                para_keys.add((item.chapter, item.number))
                para_items.append(item)

            ref_id = f"vref-{state.chapter.index}-{item.number}-{len(item.refs) + 1}"
            item.refs.append(ref_id)

            mark = etree.Element(qualified(slot.parent, "span"))
            mark.set("class", "vocab")
            mark.set("id", ref_id)
            mark.set("data-vocab-id", annotation.annotation_id)
            mark.set(
                "data-entry-id",
                annotation.whole_entry_ids[0] if annotation.whole_entry_ids else "",
            )
            mark.set("data-note", str(item.number))
            mark.set("data-chapter", str(state.chapter.index))
            mark.set("data-source", annotation.source_status)
            mark.set("data-label-scope", annotation.label_scope)
            mark.set("data-unit", annotation.unit_type)
            mark.set("data-importance", getattr(annotation, "reading_rank", ""))
            mark.text = slot.text[local : local + length]
            segments.append(("link", mark))

            note = etree.Element(qualified(slot.parent, "span"))
            note.set("class", "vocab-note")
            note.set("data-gloss", item.gloss)
            # 11-11 拍板：正文括注不显示单词等级，只留 `(短义,[n])`；
            # 等级/标签放在章末词条块一次展示，避免正文被数字打断。
            note.text = f"({item.gloss},"
            link = etree.SubElement(note, qualified(slot.parent, "a"))
            link.set("class", "vocab-link")
            link.set("href", f"#vnote-{state.chapter.index}-{item.number}")
            link.text = f"[{item.number}]"
            link.tail = ")"
            segments.append(("link", note))
            # 分组完成后会把它改成就近词条块的锚点（vpara-*）。
            para_links.append((link, item.number))

            state.marks += 1
            cursor = local + length
        if cursor < len(slot.text):
            segments.append(("text", slot.text[cursor:]))
        _splice(slot.parent, slot.owner, segments)
    return True


def _make_item(state: ChapterState, annotation: Annotation) -> NoteItem:
    entry = annotation.whole_entries[0] if annotation.whole_entries else None
    lex = state.lexicon
    if lex is not None:
        phonetic = lex.phonetic_for(annotation.surface, entry)
        form_info = lex.form_info_for(annotation.surface, entry)
    else:
        phonetic = entry.phonetic if entry else ""
        form_info = entry.form_info if entry else ""
    return NoteItem(
        number=state.next_number(),
        chapter=state.chapter.index,
        surface=annotation.surface,
        lemma=annotation.lemma or (entry.head if entry else ""),
        gloss=annotation.gloss,
        usage=annotation.usage,
        tag=annotation.body_tag,
        labels_full=annotation.labels_full,
        label_scope=annotation.label_scope,
        source_status=annotation.source_status,
        unit_type=annotation.unit_type,
        pos=annotation.whole_entries and "/".join(
            dict.fromkeys(e.pos for e in annotation.whole_entries if e.pos)
        ) or "",
        full_gloss="；".join(
            dict.fromkeys(e.gloss for e in annotation.whole_entries)
        ) if annotation.whole_entries else "",
        example=next((e.example for e in annotation.whole_entries if e.example), ""),
        extra=next((e.extra for e in annotation.whole_entries if e.extra), ""),
        phonetic=phonetic,
        form_info=form_info,
        components=[_clone_component(c) for c in annotation.components],
        evidence=annotation.evidence,
        review_status=annotation.review_status,
    )


def _clone_component(c: Component) -> Component:
    return Component(
        token_ids=list(c.token_ids),
        surface=c.surface,
        span=c.span,
        lemma=c.lemma,
        entry_ids=list(c.entry_ids),
        gloss=c.gloss,
        pos=c.pos,
        labels=list(c.labels),
        example=c.example,
        extra=c.extra,
        status=c.status,
        resolved=c.resolved,
    )


def _insert_notes(tree, chapter: Chapter, state: ChapterState) -> None:
    root = tree.getroot()
    body = next((el for el in root.iter() if local_name(el) == "body"), None)
    if body is None:
        return
    ns = _ns(body)
    section = etree.Element(f"{{{ns}}}section" if ns else "section")
    # 11-11 读者规格：词条已就近放在自然段后，章末这份只作隐藏数据层，
    # 供结构校验、包外映射与离线阅读页的词表名单使用，读者不看到重复内容。
    section.set("class", "vocab-chapter-notes vocab-archive")
    section.set("id", f"vnotes-{chapter.index}")

    heading = etree.SubElement(section, f"{{{ns}}}h2" if ns else "h2")
    heading.set("class", "vocab-notes-title")
    heading.text = f"本章词汇（{chapter.label}）"

    listing = etree.SubElement(section, f"{{{ns}}}ol" if ns else "ol")
    listing.set("class", "vocab-list")
    for item in state.items:
        _append_item(listing, ns, chapter.index, item)

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


def _para_word_count(paragraph: Paragraph) -> int:
    """段落长短按英文词数估；中文括注不计入（原文段落里本就没有括注）。"""

    return sum(
        1 for word in (paragraph.text or "").split() if re.search(r"[A-Za-z]", word)
    )


def _group_paragraphs(plist: list[Paragraph]) -> list[list[Paragraph]]:
    """长段单独成一组，短段最多连续 4 段 / 110 词出一组；章界不跨。"""

    groups: list[list[Paragraph]] = []
    cur: list[Paragraph] = []
    cur_words = 0
    cur_chapter: int | None = None
    for paragraph in plist:
        if paragraph.chapter <= 0:
            continue
        if cur_chapter is not None and paragraph.chapter != cur_chapter and cur:
            groups.append(cur)
            cur, cur_words = [], 0
        cur_chapter = paragraph.chapter
        words = _para_word_count(paragraph)
        if words >= PARA_SHORT_WORDS:
            if cur:
                groups.append(cur)
                cur, cur_words = [], 0
            groups.append([paragraph])
            continue
        cur.append(paragraph)
        cur_words += words
        if len(cur) >= PARA_MAX_SHORT or cur_words >= PARA_GROUP_WORDS:
            groups.append(cur)
            cur, cur_words = [], 0
    if cur:
        groups.append(cur)
    return groups


def _insert_para_notes(plist: list[Paragraph], states: dict, doc: str) -> int:
    """把词条块插到自然段后；正文 [n] 链接改指向就近词条，而非隐藏的章末块。"""

    groups = _group_paragraphs(plist)
    if not groups:
        return 0
    doc_token = re.sub(r"[^A-Za-z0-9]", "", doc)[:24] or "d"
    seen: set[tuple[int, int]] = set()
    blocks = 0
    for gi, group in enumerate(groups, start=1):
        entries: list[NoteItem] = []
        known: set[tuple[int, int]] = set()
        for paragraph in group:
            state = states.get(paragraph.chapter)
            if state is None:
                continue
            for item in state.para_items.get(paragraph.pid, []):
                key = (item.chapter, item.number)
                if key not in known:
                    known.add(key)
                    entries.append(item)
        if not entries:
            continue

        # 锚点：组最后一段；初级档若该段有译文，落在译文之后。
        # 最终 DOM 顺序固定为 正文 → 中文译文 → 段后词条。
        anchor = group[-1].element
        nxt = anchor.getnext()
        while nxt is not None and "vocab-translation" in (
            (nxt.get("class") or "").split()
        ):
            anchor = nxt
            nxt = nxt.getnext()
        ns = _ns(anchor)
        box = etree.Element(f"{{{ns}}}div" if ns else "div")
        box.set("class", "vocab-para-notes vocab-notes")
        title = _p(box, ns, "vocab-para-title")
        title.text = f"词汇注释 · {len(entries)} 条"

        id_map: dict[tuple[int, int], str] = {}
        for item in entries:
            key = (item.chapter, item.number)
            ident = f"vpara-{item.chapter}-{item.number}-{doc_token}-g{gi}"
            _append_para_item(box, ns, item, ident, repeat=key in seen)
            id_map[key] = ident
            seen.add(key)

        for paragraph in group:
            state = states.get(paragraph.chapter)
            if state is None:
                continue
            for link, number in state.para_links.get(paragraph.pid, []):
                target = id_map.get((paragraph.chapter, number))
                if target:
                    link.set("href", f"#{target}")
        anchor.addnext(box)
        blocks += 1
    return blocks


def _append_para_item(box, ns: str, item: NoteItem, ident: str, *,
                      repeat: bool = False) -> None:
    """复用章末词条字段排版；复现时只留词形与本句意思，且不复制 id / data-*。"""

    node = etree.SubElement(box, f"{{{ns}}}div" if ns else "div")
    node.set("class", "vocab-para-item")
    node.set("id", ident)

    template = etree.Element(f"{{{ns}}}ol" if ns else "ol")
    _append_item(template, ns, item.chapter, item)
    for child in template[0]:
        if not isinstance(child.tag, str):
            continue
        cls = set((child.get("class") or "").split())
        if "vocab-back" in cls:
            continue
        if repeat and not ({"vocab-head", "vocab-sense"} & cls):
            continue
        node.append(copy.deepcopy(child))

    # 同一文档里出现重复 id / data-* 会让 EPUBCheck 与包外映射失败。
    for sub in node.iter():
        if sub is node:
            continue
        for attr in list(sub.attrib):
            if attr == "id" or attr.startswith("data-"):
                del sub.attrib[attr]
    if repeat:
        for sub in node.iter():
            if "vocab-head" in (sub.get("class") or "").split():
                tag = etree.SubElement(sub, f"{{{ns}}}span" if ns else "span")
                tag.set("class", "vocab-repeat-tag")
                tag.text = " · 再遇"
                break


def _p(parent, ns: str, cls: str):
    el = etree.SubElement(parent, f"{{{ns}}}p" if ns else "p")
    el.set("class", cls)
    return el


def _span(parent, ns: str, cls: str, text: str = ""):
    el = etree.SubElement(parent, f"{{{ns}}}span" if ns else "span")
    el.set("class", cls)
    if text:
        el.text = text
    return el


def _append_translation(paragraph: Paragraph, translations) -> bool:
    """初级模式：在自然段后直接插入参考译文（不折叠、不依赖点击）。

    译文按 `pid + 原文哈希` 双重定位；缺译文或原文变了就整段省略，
    绝不拿相邻段落的译文顶替。书名 / 章节名 / 标题不译（11-11 拍板），
    每段只加「参考译文：」前缀，不逐段重复免责声明（免责说明写在报告里）。
    """

    text = (paragraph.text or "").strip()
    if len(text) < 2:
        return False
    if is_title_block(paragraph.element):
        return False
    translation = translations.lookup(paragraph.pid, text)
    if not translation:
        return False
    element = paragraph.element
    ns = _ns(element)
    tag = f"{{{ns}}}p" if ns else "p"
    line = etree.Element(tag)
    line.set("class", "vocab-translation")
    line.text = f"参考译文：{translation}"
    element.addnext(line)
    return True


def _append_item(listing, ns: str, chapter_index: int, item: NoteItem) -> None:
    li = etree.SubElement(listing, f"{{{ns}}}li" if ns else "li")
    li.set("class", "vocab-item")
    li.set("id", f"vnote-{chapter_index}-{item.number}")
    li.set("data-source", item.source_status)
    li.set("data-label-scope", item.label_scope)
    if item.label_scope == "whole" and item.lemma:
        li.set("data-head", item.lemma)

    head = _p(li, ns, "vocab-head")
    _span(head, ns, "vocab-num", f"[{item.number}]")
    _span(head, ns, "vocab-word", " " + item.surface)

    # 词形与词头不同才显示箭头（taking → take）
    if item.lemma and item.lemma.casefold() != item.surface.casefold():
        if item.label_scope == "whole":
            _span(head, ns, "vocab-arrow", " → ")
            _span(head, ns, "vocab-lemma", item.lemma)
    if item.labels_full:
        _span(head, ns, "vocab-grade", f" | {item.labels_full}")
    elif item.tag and item.source_status == "out_of_lexicon":
        _span(head, ns, "vocab-category", f" | {item.tag}")

    # 层 A/B 字段：有就显示，没有整行省略（不回退词头、不编造）。
    if item.phonetic:
        _p(li, ns, "vocab-phonetic").text = f"音标：{item.phonetic}"
    if item.form_info:
        _p(li, ns, "vocab-form").text = f"词形信息：{item.form_info}"

    if item.components and item.label_scope != "whole":
        sense = _p(li, ns, "vocab-sense")
        sense.set("data-gloss", item.gloss)
        sense.text = f"整体理解：{item.gloss}" + (f"（{item.usage}）" if item.usage else "")
        for component in item.components:
            member = _p(li, ns, "vocab-member")
            member.set("data-head", component.head or component.surface)
            _span(member, ns, "vocab-member-word", component.surface)
            if component.head and component.head.casefold() != component.surface.casefold():
                _span(member, ns, "vocab-arrow", " → ")
                _span(member, ns, "vocab-member-head", component.head)
            if component.labels_full:
                _span(member, ns, "vocab-member-label", f"｜{component.labels_full}")
            if component.gloss:
                gloss_line = _p(li, ns, "vocab-member-gloss")
                gloss_line.text = f"词表原义：{component.gloss}"
            if component.example:
                example = _p(li, ns, "vocab-example")
                example.text = f"词表例句（{component.head}）：{component.example}"
            if component.extra:
                extra = _p(li, ns, "vocab-extra")
                extra.text = f"扩展说明（{component.head}）：{component.extra}"
    else:
        sense = _p(li, ns, "vocab-sense")
        sense.set("data-gloss", item.gloss)
        sense.text = f"本句意思：{item.gloss}" + (f"（{item.usage}）" if item.usage else "")
        if item.pos:
            pos = _p(li, ns, "vocab-pos")
            pos.text = f"词表词性：{item.pos}"
        if item.full_gloss:
            gloss = _p(li, ns, "vocab-gloss")
            gloss.text = f"词表完整释义：{item.full_gloss}"
        if item.example:
            example = _p(li, ns, "vocab-example")
            example.text = f"词表例句：{item.example}"
        if item.extra:
            extra = _p(li, ns, "vocab-extra")
            extra.text = f"扩展说明：{item.extra}"

    back = _p(li, ns, "vocab-back")
    for index, ref in enumerate(item.refs, start=1):
        link = etree.SubElement(back, f"{{{ns}}}a" if ns else "a")
        link.set("href", f"#{ref}")
        link.text = "返回正文" if len(item.refs) == 1 else f"返回正文({index})"
        if index != len(item.refs):
            link.tail = " "


# ---------------------------------------------------------------------------
# 对照表
# ---------------------------------------------------------------------------


def _relation_cell(item: NoteItem) -> str:
    if item.label_scope == "whole":
        return (
            f'整体命中 · {html.escape(item.labels_full) or "—"}'
            if item.source_status == "exact_entry"
            else "整体命中但被等级筛选排除"
        )
    if item.label_scope == "components":
        rows = []
        for c in item.components:
            label = c.labels_full or (
                {"filtered": "被筛选", "absent": "未收录", "form_match_failed": "词形失败"}.get(
                    c.status, "—"
                )
            )
            name = c.surface + (f"→{c.head}" if c.head and c.head.casefold() != c.surface.casefold() else "")
            rows.append(
                f'{html.escape(name)}<span class="muted">｜{html.escape(label)}</span>'
            )
        return "仅成员命中<br>" + "<br>".join(rows)
    category = f"类别 {html.escape(item.tag)}" if item.tag else "普通词（无类别）"
    return "未收录<br>" + f'<span class="warn">{category}</span>'


def build_comparison_table(
    states: dict[int, ChapterState],
    usable: list[Annotation],
    paragraphs: dict[str, Paragraph],
) -> list[dict]:
    index: dict[tuple[str, str], NoteItem] = {}
    for state in states.values():
        for item in state.items:
            index[(normalize_span(item.surface), normalize_span(item.gloss))] = item
    rows: list[dict] = []
    for annotation in usable:
        paragraph = paragraphs.get(annotation.pid)
        item = index.get(
            (normalize_span(annotation.surface), normalize_span(annotation.gloss))
        )
        rows.append(
            {
                "pid": annotation.pid,
                "chapter": paragraph.chapter if paragraph else 0,
                "number": item.number if item else "",
                "surface": annotation.surface,
                "span": list(annotation.span),
                "snippet": _snippet(paragraph.text, annotation.span) if paragraph else "",
                "gloss": annotation.gloss,
                "tag": item.tag if item else "",
                "labels_full": item.labels_full if item else "",
                "label_scope": annotation.label_scope,
                "source_status": annotation.source_status,
                "unit_type": annotation.unit_type,
                "components": [c.as_dict() for c in annotation.components],
                "evidence": annotation.evidence,
                "review_status": annotation.review_status,
                "item": item,
            }
        )
    return rows


def _snippet(text: str, span: tuple[int, int], width: int = 34) -> str:
    start, end = span
    left = max(0, start - width)
    right = min(len(text), end + width)
    return (
        ("…" if left else "")
        + text[left:right].replace("\n", " ")
        + ("…" if right < len(text) else "")
    )


def _stat_block(stats: dict) -> str:
    categories = stats.get("category_used") or {}
    category_text = "、".join(f"{k}×{v}" for k, v in categories.items()) or "无"
    cells = [
        ("整体命中", stats["exact_entry"]),
        ("仅成员命中", stats["component_entries"]),
        ("真正未收录", stats["out_of_lexicon"]),
        ("词形还原失败", stats["form_match_failed"]),
        ("被等级筛选排除", stats["filtered_out"]),
        ("组合项", stats["merged_units"]),
        ("语境义（词库无此义项）", stats["sensed_as_context"]),
        ("成员由程序补全关联", stats["component_resolved"]),
        ("组合短义覆盖告警", stats["gloss_coverage_warnings"]),
        ("拒收：区间不符", stats["range_mismatch"]),
        ("拒收：区间重叠", stats["overlap"]),
        ("拒收：未审查", stats["unreviewed"]),
    ]
    chips = "".join(
        f'<span class="chip">{name}<b>{value}</b></span>' for name, value in cells
    )
    return (
        f'<div class="stats">{chips}'
        f'<span class="chip">用法类别<b>{html.escape(category_text)}</b></span></div>'
    )


def _write_comparison(
    path: Path,
    states: dict[int, ChapterState],
    usable: list[Annotation],
    paragraphs: dict[str, Paragraph],
    *,
    book_title: str,
    stats: dict | None = None,
) -> None:
    rows = build_comparison_table(states, usable, paragraphs)
    path.parent.mkdir(parents=True, exist_ok=True)

    trs = []
    for row in rows:
        item = row["item"]
        if item is None:
            body_cell, relation = "—", "—"
        else:
            body_cell = html.escape(item.gloss)
            relation = _relation_cell(item)
        if item is not None and item.components and item.label_scope != "whole":
            member_detail = "<br>".join(
                f'{html.escape(c.surface)}｜{html.escape(c.gloss or "—")}'
                f'<span class="muted">（{html.escape(c.status)}）</span>'
                for c in item.components
            )
        elif item is not None and item.full_gloss:
            member_detail = f'完整释义 {html.escape(item.full_gloss)}'
        else:
            member_detail = "—"
        tag_text = (item.tag if item else "") or "—"
        trs.append(
            "<tr>"
            f'<td class="num">[{row["number"]}]</td>'
            f'<td class="snip">{html.escape(row["snippet"])}</td>'
            f'<td class="surf">{html.escape(row["surface"])}'
            f'<span class="span">{row["unit_type"]} · {row["span"][0]}–{row["span"][1]}</span></td>'
            f'<td class="gloss">{body_cell}</td>'
            f'<td class="tag">{html.escape(tag_text)}</td>'
            f'<td class="rel">{relation}</td>'
            f'<td class="detail">{member_detail}</td>'
            f'<td class="ev">{html.escape(row["evidence"] or "")}</td>'
            f'<td class="rev">{html.escape(row["review_status"] or "")}</td>'
            "</tr>"
        )

    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>{html.escape(book_title)} · 标注对照表</title>
<style>
body{{margin:0;padding:30px 20px 60px;background:#fbf9f4;color:#1b1b1b;
font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;line-height:1.7}}
h1{{font-size:1.14rem;margin:0 0 .3rem}}
.meta{{color:#6b6b6b;font-size:.8rem;margin-bottom:1rem;padding-bottom:.8rem;border-bottom:1px solid #ded8cd}}
.stats{{display:flex;flex-wrap:wrap;gap:.35rem .5rem;margin:0 0 1.1rem}}
.chip{{background:#f2ece0;border:1px solid #e2d9c8;border-radius:999px;padding:.12rem .55rem;
font-size:.76rem;color:#5c5142}}
.chip b{{margin-left:.3rem;color:#8a6a45}}
table{{border-collapse:collapse;width:100%;font-size:.8rem}}
th,td{{border-bottom:1px solid #e8e2d6;padding:.48rem .5rem;vertical-align:top;text-align:left}}
th{{color:#6b6b6b;font-weight:600;background:#f5f1e8;position:sticky;top:0}}
.num{{color:#8a6a45;font-weight:700;white-space:nowrap}}
.snip{{font-family:"Iowan Old Style",Georgia,serif;color:#333;min-width:14rem}}
.surf{{font-weight:600;min-width:8rem}}
.span{{display:block;color:#aaa;font-weight:400;font-size:.7rem}}
.gloss{{font-weight:600;white-space:nowrap}}
.tag{{color:#8a6a45;font-weight:600;white-space:nowrap}}
.rel{{color:#555;min-width:11rem;font-size:.78rem}}
.muted{{color:#999;font-weight:400}}
.warn{{color:#b06a2c;font-weight:600}}
.detail{{color:#666;min-width:10rem}}
.ev{{color:#777;font-size:.76rem;min-width:12rem}}
.rev{{color:#3a7a4a;font-weight:600;white-space:nowrap}}
</style></head><body>
<h1>{html.escape(book_title)} · 标注对照表（§18 词库优先 + 组合解释）</h1>
<div class="meta">共 {len(rows)} 条通过审查的标注｜正文标签由程序按词库生成，未采信模型填写｜
「仅成员命中」的组合正文不显示等级，成员标签见下表</div>
{_stat_block(stats) if stats else ""}
<table>
<thead><tr><th>编号</th><th>原文片段</th><th>标注单位 / 区间</th><th>正文短义</th>
<th>正文标签</th><th>词库关联</th><th>章末详情</th><th>依据</th><th>审查</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
</body></html>"""
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _ns(element) -> str:
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
    head = next((el for el in root.iter() if local_name(el) == "head"), None)
    if head is None:
        ns = _ns(root)
        head = etree.Element(f"{{{ns}}}head" if ns else "head")
        root.insert(0, head)
    for child in head:
        if local_name(child) == "link" and (child.get("href") or "") == href:
            return
    ns = _ns(root)
    link = etree.Element(f"{{{ns}}}link" if ns else "link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", href)
    head.append(link)

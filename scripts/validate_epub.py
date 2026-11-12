"""输出 EPUB 的独立校验与保真比对（阶段 1）。

校验器**独立于标注器**：重新打开输出文件，从结构出发做检查，不复用标注过程中的中间状态。
EPUBCheck 未安装时明确标注为未运行，绝不把自研检查说成 EPUBCheck 通过。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .binding import (
    VOCAB_ATTRS,
    apply_binding,
    audit_binding,
    binding_path_for,
    load_binding,
)
from .epub_common import (
    EpubError,
    MIMETYPE,
    local_name,
    open_epub,
    parse_xml,
    resolve_href,
)

__all__ = ["ValidationReport", "validate_output", "find_epubcheck"]

EXTERNAL_PREFIXES = ("http:", "https:", "mailto:", "tel:", "javascript:", "data:", "ftp:")
MEDIA_ATTRS = ("src", "href", "poster")


@dataclass
class ValidationReport:
    ok: bool = False
    checks: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fidelity: dict = field(default_factory=dict)
    epubcheck: dict | None = None
    binding: dict | None = None
    compatibility: dict | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "fidelity": dict(self.fidelity),
            "epubcheck": self.epubcheck,
            "binding": self.binding,
            "compatibility": self.compatibility,
        }


def validate_output(
    source_path: str | Path,
    output_path: str | Path,
    *,
    modified_paths: set[str] | None = None,
    run_epubcheck: bool = True,
    lexicon=None,
    presentation: str = "",
    binding_path: str | Path | None = None,
    policy: str = "compatible",
    artifact_dir: str | Path | None = None,
    compatibility_mapping: dict | None = None,
    artifact_name: str | None = None,
) -> ValidationReport:
    report = ValidationReport()
    modified = set(modified_paths or ())
    checks: dict[str, object] = {}

    with zipfile.ZipFile(output_path) as zf:
        names = zf.namelist()
        checks["mimetype"] = _check_mimetype(zf, names, report)
        checks["container"] = _safe(checks, report, _check_container, zf, report)

    try:
        source_archive, source_pkg = open_epub(source_path)
        source_archive.close()
    except EpubError as exc:
        report.errors.append(f"源文件无法重新打开：{exc}")
        source_pkg = None

    try:
        out_archive, out_pkg = open_epub(output_path)
    except EpubError as exc:
        report.errors.append(f"输出文件无法打开：{exc}")
        report.checks = checks
        report.ok = False
        return report

    try:
        checks["manifest"] = _check_manifest(out_archive, out_pkg, report)
        checks["spine"] = _check_spine(out_pkg, source_pkg, report)
        trees, ids = _parse_xhtml(out_archive, out_pkg, report)
        checks["xhtml"] = not any(e.startswith("XHTML") for e in report.errors)

        # §22.4：映射必须在注入前按「最终产物实际节点」核对，且 EPUB 2
        # 合规检查也必须在注入前做；注入只影响内存 DOM，不回写 EPUB。
        _apply_binding_for_validation(
            output_path, out_pkg, trees, checks, report, binding_path
        )

        checks["links"] = _check_links(out_archive, out_pkg, trees, ids, report)
        checks["vocab_refs"] = _check_vocab_refs(trees, ids, report)
        checks["sense_trace"] = _check_sense_trace(trees, report)
        notes = _collect_notes(trees)
        checks["note_numbering"] = _check_note_numbering(notes, report)
        checks["note_links"] = _check_note_links(trees, notes, report)
        checks["note_consistency"] = _check_note_consistency(notes, report)
        checks["note_fields"] = _check_note_fields(notes, lexicon, report)
        checks["note_forms"] = _check_note_forms(trees, lexicon, report)
        checks["note_no_impl_notes"] = _check_no_impl_notes(trees, report)
        checks["nav"] = _check_navigation(source_path, out_pkg, out_archive, trees, report)
        checks["text_integrity"] = _check_text_integrity(
            source_path, out_pkg, trees, report
        )
        checks["fidelity"] = _check_fidelity(
            source_path, output_path, modified, checks, report
        )
    finally:
        out_archive.close()

    # §22.6：内部硬校验与官方规范校验是两个独立结论。官方失败不再自动
    # 写进 errors 阻断发布，而是交给兼容层按「新增 / 遗留 / 修复」判定；
    # checks.epubcheck 仍如实保留真实 bool，不强行写成通过。
    internal_ok = not report.errors and all(
        value is not False
        for key, value in checks.items()
        if key != "epubcheck"
    )

    from .compatibility import evaluate  # 局部导入，避免与 validate_epub 形成环

    report.compatibility = evaluate(
        source_path=source_path,
        output_path=output_path,
        artifact_dir=artifact_dir or Path(output_path).parent,
        policy=policy,
        run_epubcheck=run_epubcheck,
        internal_ok=internal_ok,
        mapping=compatibility_mapping,
        report_name=artifact_name,
    )
    official = report.compatibility["official"]
    status = official["status"]
    checks["epubcheck"] = True if status == "passed" else (
        None if status == "not_run" else False
    )
    report.epubcheck = official.get("run") or {
        "ran": False,
        "status": status,
        "reason": "官方检查未运行",
    }
    if status == "not_run" and run_epubcheck:
        report.warnings.append("环境中无 EPUBCheck（Java 工具），外部校验未完成")

    report.checks = checks
    report.ok = internal_ok
    return report


def _resolve_binding(output_path: Path, explicit: str | Path | None):
    """映射文件自动发现：显式路径 > pending 版 > 正式版。"""

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        candidates.extend(
            (
                binding_path_for(output_path, pending=True),
                binding_path_for(output_path),
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return load_binding(candidate), candidate
    return None, None


def _check_epub2_dom(out_pkg, trees, report: ValidationReport) -> bool:
    """EPUB 2 产物不得残留本工具写入的 data-* 或章末 <section>。

    只检查白名单属性名，不动源书其它属性；EPUB 3 允许 HTML5 写法，跳过。
    """

    if str(out_pkg.version).startswith("3"):
        return True
    bad: list[str] = []
    for path, tree in trees.items():
        for el in tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            keys = [key for key in el.attrib if key in VOCAB_ATTRS]
            if keys:
                bad.append(f"{path}: <{local_name(el)}> 残留 {','.join(sorted(keys))}")
            if local_name(el) == "section" and "vocab-chapter-notes" in (
                (el.get("class") or "").split()
            ):
                bad.append(f"{path}: 章末词条仍是 <section>")
    for item in bad[:8]:
        report.errors.append(f"EPUB 2 DOM 不合规：{item}")
    if len(bad) > 8:
        report.errors.append(f"EPUB 2 DOM 不合规：另有 {len(bad) - 8} 处")
    return not bad


def _apply_binding_for_validation(
    output_path, out_pkg, trees, checks, report: ValidationReport, explicit
) -> None:
    """加载映射 → 按实际节点审计 → 合规检查 → 注入内存 DOM 供后续检查复用。

    没有映射时（旧版产物 / 纯复制）保持旧行为，只记 warning，不伪称合规。
    """

    try:
        binding, path = _resolve_binding(Path(output_path), explicit)
    except (OSError, ValueError) as exc:
        report.errors.append(f"标注映射无法读取：{exc}")
        checks["binding"] = False
        checks["epub2_dom"] = False
        report.binding = {"present": True, "error": str(exc)}
        return

    if not binding:
        checks["binding"] = None
        checks["epub2_dom"] = None
        report.binding = {"present": False}
        report.warnings.append(
            "未找到 vocab-reader-map 映射（旧版产物或无标注），"
            "跳过绑定核对与 EPUB 2 DOM 合规检查"
        )
        return

    epub_sha = hashlib.sha256(Path(output_path).read_bytes()).hexdigest()
    audit_errors = audit_binding(trees, binding, epub_sha)
    for error in audit_errors:
        report.errors.append(f"标注映射核对失败：{error}")
    checks["binding"] = not audit_errors
    checks["epub2_dom"] = _check_epub2_dom(out_pkg, trees, report)

    stats = apply_binding(trees, binding)
    matched = stats["matched"]
    missing = stats["missing"] + stats.get("ambiguous", 0)
    attrs = stats["attrs"]
    report.binding = {
        "present": True,
        "schema": binding.get("schema"),
        "path": str(path),
        "records": len(binding.get("attrs") or {}),
        "matched": matched,
        "missing": missing,
        "injected_attrs": attrs,
        "audit_errors": len(audit_errors),
    }
    if missing:
        report.errors.append(f"标注映射有 {missing} 个稳定 ID 在产物中找不到节点")


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def _safe(checks, report, func, *args) -> bool:
    try:
        return bool(func(*args))
    except Exception as exc:  # noqa: BLE001 - 校验不允许抛出去打断报告
        report.errors.append(f"校验异常（{func.__name__}）：{exc}")
        return False


def _check_mimetype(zf: zipfile.ZipFile, names: list[str], report: ValidationReport) -> bool:
    if "mimetype" not in names:
        report.errors.append("输出缺少 mimetype 条目")
        return False
    if names[0] != "mimetype":
        report.errors.append("mimetype 不是 ZIP 的第一个条目")
        return False
    info = zf.getinfo("mimetype")
    if info.compress_type != zipfile.ZIP_STORED:
        report.errors.append("mimetype 被压缩（必须 ZIP_STORED）")
        return False
    data = zf.read("mimetype").strip()
    if data != MIMETYPE:
        report.errors.append(f"mimetype 内容非法：{data[:40]!r}")
        return False
    return True


def _check_container(zf: zipfile.ZipFile, report: ValidationReport) -> bool:
    if "META-INF/container.xml" not in zf.namelist():
        report.errors.append("输出缺少 META-INF/container.xml")
        return False
    tree = parse_xml(zf.read("META-INF/container.xml"), "container.xml")
    rootfiles = [
        el for el in tree.getroot().iter() if local_name(el) == "rootfile"
    ]
    if not rootfiles:
        report.errors.append("container.xml 未声明 rootfile")
        return False
    for el in rootfiles:
        path = resolve_href("", el.get("full-path") or "")
        if path not in zf.namelist():
            report.errors.append(f"rootfile 指向不存在的文件：{path}")
            return False
    return True


def _check_manifest(archive, package, report: ValidationReport) -> bool:
    ok = True
    hrefs: set[str] = set()
    for item in package.items.values():
        if not item.media_type:
            report.errors.append(f"manifest 项缺少 media-type：{item.id}")
            ok = False
        if item.path in hrefs:
            report.errors.append(f"manifest 中 href 重复：{item.href}")
            ok = False
        hrefs.add(item.path)
        if not archive.exists(item.path):
            report.errors.append(f"manifest 引用了不存在的资源：{item.path}")
            ok = False
    return ok


def _check_spine(out_pkg, source_pkg, report: ValidationReport) -> bool:
    ok = True
    for spine_item in out_pkg.spine:
        if spine_item.idref not in out_pkg.items:
            report.errors.append(f"spine 引用未声明的 id：{spine_item.idref}")
            ok = False
    if source_pkg is not None:
        source_paths = [s.item.path for s in source_pkg.spine]
        output_paths = [s.item.path for s in out_pkg.spine]
        if output_paths[: len(source_paths)] != source_paths:
            report.errors.append(
                "spine 阅读顺序与源书不一致："
                f"{output_paths[: len(source_paths)]} != {source_paths}"
            )
            ok = False
        if len(output_paths) < len(source_paths):
            report.errors.append("输出 spine 比源书短，章节可能丢失")
            ok = False
    return ok


def _parse_xhtml(archive, package, report: ValidationReport):
    trees: dict[str, object] = {}
    ids: dict[str, set[str]] = {}
    for item in package.xhtml_items():
        if not archive.exists(item.path):
            report.errors.append(f"缺少 XHTML 资源：{item.path}")
            continue
        try:
            tree = parse_xml(archive.read(item.path), item.path)
        except EpubError as exc:
            report.errors.append(f"XHTML 无法解析（{item.path}）：{exc}")
            continue
        trees[item.path] = tree
        ids[item.path] = {
            el.get("id") for el in tree.getroot().iter() if el.get("id")
        }
    return trees, ids


def _resolve_link(current: str, href: str) -> tuple[str, str]:
    path, _, fragment = href.partition("#")
    if path:
        target = resolve_href(posixpath.dirname(current), path)
    else:
        target = current
    return target, fragment


def _check_links(archive, package, trees, ids, report: ValidationReport) -> bool:
    ok = True
    all_names = set(archive.names)
    for path, tree in trees.items():
        for el in tree.getroot().iter():
            for attr in MEDIA_ATTRS:
                value = el.get(attr)
                if not value or value.startswith(EXTERNAL_PREFIXES):
                    continue
                target, fragment = _resolve_link(path, value)
                if target not in all_names:
                    report.errors.append(f"{path}: 链接目标不存在 {value}")
                    ok = False
                    continue
                if fragment and target in ids:
                    if fragment not in ids[target]:
                        report.errors.append(f"{path}: 锚点不存在 #{fragment}")
                        ok = False
            for attr in ("srcset",):
                if el.get(attr):
                    for candidate in str(el.get(attr)).split(","):
                        url = candidate.strip().split(" ")[0]
                        if url and not url.startswith(EXTERNAL_PREFIXES):
                            target, _ = _resolve_link(path, url)
                            if target not in all_names:
                                report.errors.append(f"{path}: srcset 目标不存在 {url}")
                                ok = False
    return ok


def _check_vocab_refs(trees, ids, report: ValidationReport) -> bool:
    """标注完整性：footnote 模式要求链接↔脚注往返；inline 模式只要求可追溯属性。"""

    ok = True
    annotated_files = 0
    for path, tree in trees.items():
        marks = [
            el
            for el in tree.getroot().iter()
            if isinstance(el.tag, str)
            and "vocab" in (el.get("class") or "").split()
        ]
        marks = [el for el in marks if local_name(el) in ("a", "span")]
        if not marks:
            continue
        annotated_files += 1
        footnotes = {
            el.get("id")
            for el in tree.getroot().iter()
            if el.get("id") and str(el.get("id")).startswith("vocab-fn-")
        }
        inline_marks = 0
        for mark in marks:
            target_id = mark.get("data-vocab-id")
            if not target_id:
                report.errors.append(f"{path}: 标注缺少 data-vocab-id")
                ok = False
                continue
            if local_name(mark) == "span":
                # inline 模式：无脚注可跳，data-vocab-id 记词条键即可
                inline_marks += 1
                continue
            if target_id not in footnotes:
                report.errors.append(f"{path}: 标注指向不存在的脚注 {target_id}")
                ok = False
            href = mark.get("href") or ""
            if href != "#" + target_id:
                report.errors.append(
                    f"{path}: 标注 href 与 data-vocab-id 不一致（{href}）"
                )
                ok = False
        for el in tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            if local_name(el) == "a" and "vocab-back" in (el.get("class") or "").split():
                href = el.get("href") or ""
                if href.startswith("#") and href[1:] not in ids[path]:
                    report.errors.append(f"{path}: 脚注回链目标不存在 {href}")
                    ok = False
    if annotated_files == 0:
        report.warnings.append("输出中没有任何词汇标注")
    return ok


def _check_sense_trace(trees, report: ValidationReport) -> bool:
    """每个标注都必须能追溯回词表词条（data-entry-id）。"""

    ok = True
    total = 0
    for path, tree in trees.items():
        for el in tree.getroot().iter():
            if not isinstance(el.tag, str) or local_name(el) not in ("a", "span"):
                continue
            if "vocab" not in (el.get("class") or "").split():
                continue
            total += 1
            entry_id = (el.get("data-entry-id") or "").strip()
            if not entry_id:
                # §17：补充词语没有词表条目，但必须有可追溯的 annotation id
                if (el.get("data-source") or "") in (
                    "out_of_lexicon",
                    "component_entries",
                    "form_match_failed",
                    "supplement",
                ) and (
                    el.get("data-vocab-id") or ""
                ).strip():
                    continue
                report.errors.append(f"{path}: 标注缺少 data-entry-id，无法追溯到词表")
                ok = False
            confidence = el.get("data-confidence")
            if confidence is not None:
                try:
                    value = float(confidence)
                except ValueError:
                    report.errors.append(f"{path}: data-confidence 非法：{confidence}")
                    ok = False
                    continue
                if not 0.0 <= value <= 1.0:
                    report.errors.append(f"{path}: data-confidence 越界：{confidence}")
                    ok = False
    if total == 0:
        report.warnings.append("输出中没有任何可追溯的词汇标注")
    return ok


@dataclass
class _NoteMark:
    chapter: str
    number: int
    entry_id: str
    brief: str
    brief_grade: str
    path: str
    gloss_attr: str = ""


#: 正文括注里可能出现的等级/类别位，用于给没有 data-gloss 的旧产物做回退解析
_NOTE_TAGS = {
    "4", "6", "ielts", "toefl", "GRE", "牛津", "AWL",
    "专", "书", "俚", "方", "名",
}


@dataclass
class _NoteItem:
    chapter: str
    number: int
    path: str
    fields: dict
    refs: list[str]


def _collect_notes(trees) -> tuple[dict, dict]:
    marks: dict[tuple[str, int], list[_NoteMark]] = {}
    items: dict[tuple[str, int], _NoteItem] = {}
    for path, tree in trees.items():
        root = tree.getroot()
        # 章末条目
        for el in root.iter():
            if not isinstance(el.tag, str) or local_name(el) != "li":
                continue
            ident = el.get("id") or ""
            if not ident.startswith("vnote-"):
                continue
            parts = ident.split("-")
            if len(parts) < 3:
                continue
            chapter, number = parts[1], parts[2]
            fields = {}
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                classes = (child.get("class") or "").split()
                text = " ".join("".join(child.itertext()).split())
                for key in (
                    "vocab-sense",
                    "vocab-pos",
                    "vocab-gloss",
                    "vocab-example",
                    "vocab-extra",
                ):
                    if key in classes:
                        fields[key] = text
                        if key == "vocab-sense":
                            fields["vocab-sense-gloss"] = child.get("data-gloss") or ""
                if "vocab-back" in classes:
                    fields["back"] = [
                        a.get("href") for a in child if local_name(a) == "a"
                    ]
                if "vocab-head" in classes:
                    for span in child:
                        classes2 = (span.get("class") or "").split()
                        if "vocab-grade" in classes2:
                            fields["grade"] = "".join(span.itertext()).strip()
                        if "vocab-lemma" in classes2:
                            fields["lemma"] = "".join(span.itertext()).strip()
                        if "vocab-word" in classes2:
                            fields["surface"] = "".join(span.itertext()).strip()
            items[(chapter, int(number))] = _NoteItem(
                chapter=chapter,
                number=int(number),
                path=path,
                fields=fields,
                refs=[href for href in fields.get("back", []) if href],
            )

        # 正文短注
        for el in root.iter():
            if not isinstance(el.tag, str) or local_name(el) != "span":
                continue
            if "vocab" not in (el.get("class") or "").split():
                continue
            raw = el.get("data-note")
            if not raw:
                continue
            chapter = el.get("data-chapter") or "?"
            number = int(raw)
            brief = grade = gloss_attr = ""
            nxt = el.getnext()
            if nxt is not None and "vocab-note" in ((nxt.get("class") or "").split()):
                gloss_attr = nxt.get("data-gloss") or ""
                text = "".join(nxt.itertext()).strip()
                if text.startswith("(") and text.endswith(")"):
                    inner = re.sub(r",\s*\[\d+\]$", "", text[1:-1])
                    head, sep, tail = inner.rpartition(",")
                    if sep and tail.strip() in _NOTE_TAGS:
                        brief, grade = head, tail.strip()
                    else:
                        brief = inner
            marks.setdefault((chapter, number), []).append(
                _NoteMark(
                    chapter=chapter,
                    number=number,
                    entry_id=el.get("data-entry-id") or "",
                    brief=brief,
                    brief_grade=grade,
                    path=path,
                    gloss_attr=gloss_attr,
                )
            )
    return marks, items


def _check_note_numbering(notes, report: ValidationReport) -> bool:
    """编号按首次出现顺序从 1 连续递增（每章独立）。"""

    marks, items = notes
    if not marks and not items:
        return True
    ok = True
    order: dict[str, list[int]] = {}
    for path_tree in (marks,):
        for (chapter, number) in path_tree:
            order.setdefault(chapter, []).append(number)
    for chapter, numbers in order.items():
        seen: list[int] = []
        for _ in range(len(numbers)):
            pass
        # 去重后应为 1..N
        unique = sorted(set(numbers))
        if unique != list(range(1, len(unique) + 1)):
            report.errors.append(
                f"第 {chapter} 章词注编号不连续：{unique[:12]}"
            )
            ok = False
    for (chapter, number), item in items.items():
        if number < 1:
            report.errors.append(f"第 {chapter} 章出现非法编号 {number}")
            ok = False
    return ok


def _check_note_links(trees, notes, report: ValidationReport) -> bool:
    marks, items = notes
    if not marks and not items:
        return True
    ok = True
    for (chapter, number), item in items.items():
        for href in item.refs:
            if not href.startswith("#"):
                continue
            target = href[1:]
            found = False
            for path, tree in trees.items():
                for el in tree.getroot().iter():
                    if el.get("id") == target:
                        found = True
                        break
                if found:
                    break
            if not found:
                report.errors.append(
                    f"{item.path}: 词条 [{number}] 的返回链接指向不存在的锚点 {href}"
                )
                ok = False
    for (chapter, number), group in marks.items():
        if (chapter, number) not in items:
            report.errors.append(
                f"{group[0].path}: 正文短注 [{number}] 找不到对应的章末条目"
            )
            ok = False
    return ok


def _check_note_consistency(notes, report: ValidationReport) -> bool:
    marks, items = notes
    if not marks:
        return True
    ok = True
    for key, group in marks.items():
        item = items.get(key)
        if item is None:
            continue
        # 新产物带 data-gloss：短义原值直接比对，不受释义内的逗号/括号影响。
        # 旧产物没有属性时按文本回退，只剥离结尾的「（用法说明）」，
        # 不能用首个括号切分——释义本身常含 (动植物的) 这类括号。
        gloss_attr = item.fields.get("vocab-sense-gloss", "")
        sense_text = item.fields.get("vocab-sense", "")
        body_gloss = sense_text.split("：", 1)[-1] if "：" in sense_text else sense_text
        body_gloss = re.sub(r"[（(][^（）()]*[）)]$", "", body_gloss).strip()
        if gloss_attr:
            body_gloss = gloss_attr
        for mark in group:
            mark_gloss = mark.gloss_attr or mark.brief
            if mark_gloss and body_gloss and mark_gloss != body_gloss:
                report.errors.append(
                    f"{mark.path}: 正文短注「{mark_gloss}」与章末本句意思"
                    f"「{body_gloss}」不一致（编号 {mark.number}）"
                )
                ok = False
    return ok


def _check_note_fields(notes, lexicon, report: ValidationReport) -> bool:
    """章末字段必须与词表一致（这里是"数据一致"，不是"选义正确"）。"""

    if lexicon is None:
        return True
    marks, items = notes
    if not items:
        return True
    ok = True
    for (chapter, number), item in items.items():
        entry_id = ""
        for mark in marks.get((chapter, number), []):
            entry_id = mark.entry_id
            break
        if not entry_id:
            continue
        entries = [e for e in lexicon.entries if e.entry_id == entry_id]
        if not entries:
            report.errors.append(
                f"{item.path}: 词条 [{number}] 的 entry_id 在词表中不存在：{entry_id}"
            )
            ok = False
            continue
        expected_gloss = "；".join(dict.fromkeys(e.gloss for e in entries))
        got = item.fields.get("vocab-gloss", "")
        if got.startswith("词表完整释义："):
            got = got[len("词表完整释义：") :]
        if got and got != expected_gloss:
            report.errors.append(
                f"{item.path}: 词条 [{number}] 的释义与词表不一致：{got!r} != {expected_gloss!r}"
            )
            ok = False
    return ok


def _check_note_forms(trees, lexicon, report: ValidationReport) -> bool:
    """章末词形必须与标注词头有可靠关系（taking→take；taki 永远不合格）。"""

    if lexicon is None:
        return True
    from .units import related_form

    ok = True
    for path, tree in trees.items():
        for el in tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            classes = (el.get("class") or "").split()
            if local_name(el) == "li" and "vocab-item" in classes:
                head = (el.get("data-head") or "").strip()
                if not head:
                    continue
                word = ""
                for span in el.iter():
                    if "vocab-word" in (span.get("class") or "").split():
                        word = "".join(span.itertext()).strip()
                        break
                if word and not _form_ok(word, head, lexicon):
                    report.errors.append(
                        f"{path}: 章末词形「{word}」与词头「{head}」无可靠关联（疑似截断/错配）"
                    )
                    ok = False
            if local_name(el) == "p" and "vocab-member" in classes:
                head = (el.get("data-head") or "").strip()
                word_el = next(
                    (
                        x
                        for x in el
                        if "vocab-member-word" in (x.get("class") or "").split()
                    ),
                    None,
                )
                word = "".join(word_el.itertext()).strip() if word_el is not None else ""
                if head and word and not _form_ok(word, head, lexicon):
                    report.errors.append(
                        f"{path}: 章末成员「{word}」与词头「{head}」无可靠关联（疑似截断/错配）"
                    )
                    ok = False
    return ok


def _form_ok(word: str, head: str, lexicon) -> bool:
    from .units import related_form

    entries = lexicon.lookup_any(head.casefold().strip())
    if not entries:
        return True  # 词头本身不在词库时无法判定，交给字段校验
    return any(related_form(word, entry) for entry in entries)


_IMPL_PHRASES = (
    "整体未收录该组合",
    "正文不显示整体等级",
    "原词与词形还原均无对应条目",
    "词形还原失败",
    "实现信息",
)


def _check_no_impl_notes(trees, report: ValidationReport) -> bool:
    """读者词注里不能出现面向实现者的规则说明。"""

    ok = True
    for path, tree in trees.items():
        for el in tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            if "vocab-chapter-notes" not in (el.get("class") or "").split():
                continue
            text = " ".join("".join(el.itertext()).split())
            for phrase in _IMPL_PHRASES:
                if phrase in text:
                    report.errors.append(f"{path}: 章末出现实现说明「{phrase}」")
                    ok = False
    return ok


def _check_navigation(source_path, out_pkg, archive, trees, report: ValidationReport) -> bool:
    ok = True
    if out_pkg.nav_item is not None:
        tree = trees.get(out_pkg.nav_item.path)
        if tree is None:
            report.errors.append("nav 文档无法解析")
            return False
        out_count = sum(1 for el in tree.getroot().iter() if local_name(el) == "a")
        src_count = _source_nav_count(source_path, out_pkg.nav_item.path)
        if src_count is not None and src_count and out_count != src_count:
            report.warnings.append(
                f"nav 链接数变化（源 {src_count} -> 输出 {out_count}）"
            )
    elif out_pkg.ncx_item is not None:
        if not archive.exists(out_pkg.ncx_item.path):
            report.errors.append("NCX 文件缺失")
            return False
        try:
            tree = parse_xml(archive.read(out_pkg.ncx_item.path), out_pkg.ncx_item.path)
        except EpubError as exc:
            report.errors.append(f"NCX 无法解析：{exc}")
            return False
        if not any(local_name(el) == "navPoint" for el in tree.getroot().iter()):
            report.warnings.append("NCX 中没有任何 navPoint")
    return ok


def _source_nav_count(source_path, nav_path: str) -> int | None:
    try:
        with zipfile.ZipFile(source_path) as zf:
            if nav_path not in zf.namelist():
                return None
            tree = parse_xml(zf.read(nav_path), nav_path)
    except (zipfile.BadZipFile, EpubError, OSError):
        return None
    return sum(1 for el in tree.getroot().iter() if local_name(el) == "a")


def _body_text(element) -> str:
    """正文可见文本；跳过本技能新增的词注块、词表页与 **inline 括注**。

    inline 模式正文里多了「（释义）」文本，剥离后才能与源文件逐字比对。
    """

    chunks: list[str] = []

    def walk(el) -> None:
        classes = set((el.get("class") or "").split())
        if classes & {
            "vocab-notes",
            "vocab-glossary",
            "vocab-note",
            "vocab-chapter-notes",
            "vocab-para-notes",
            "vocab-list",
            # 初级模式的段落中文译文是本技能新增的附注，不参与「正文逐字一致」比对
            "vocab-translation",
        }:
            return
        if el.text:
            chunks.append(el.text)
        for child in el:
            if not isinstance(child.tag, str):
                continue
            walk(child)
            if child.tail:
                chunks.append(child.tail)

    walk(element)
    return " ".join("".join(chunks).split())


def _check_text_integrity(source_path, out_pkg, trees, report: ValidationReport) -> bool:
    """改写前后正文必须逐字一致（防止丢字、重复、错位）。"""

    ok = True
    try:
        with zipfile.ZipFile(source_path) as source_zip:
            names = set(source_zip.namelist())
            for spine_item in out_pkg.spine:
                path = spine_item.item.path
                if path not in trees or path not in names:
                    continue
                source_tree = parse_xml(source_zip.read(path), path)
                source_body = _first_body(source_tree.getroot())
                output_body = _first_body(trees[path].getroot())
                if source_body is None or output_body is None:
                    continue
                before = _body_text(source_body)
                after = _body_text(output_body)
                if before == after:
                    continue
                ok = False
                position = next(
                    (i for i, (a, b) in enumerate(zip(before, after)) if a != b),
                    min(len(before), len(after)),
                )
                report.errors.append(
                    f"{path}: 正文文本与源文件不一致（位置 {position}）："
                    f"源…{before[max(0, position - 30):position + 40]!r} "
                    f"输出…{after[max(0, position - 30):position + 40]!r}"
                )
    except (zipfile.BadZipFile, EpubError, OSError) as exc:
        report.errors.append(f"正文一致性检查失败：{exc}")
        return False
    return ok


def _first_body(root):
    for el in root.iter():
        if local_name(el) == "body":
            return el
    return None


def _check_fidelity(
    source_path, output_path, modified: set[str], checks, report: ValidationReport
) -> bool:
    identical = 0
    diffs: list[dict] = []
    with zipfile.ZipFile(source_path) as src, zipfile.ZipFile(output_path) as out:
        out_names = set(out.namelist())
        for name in src.namelist():
            if name == "mimetype" or name in modified:
                continue
            if name not in out_names:
                diffs.append({"path": name, "reason": "输出中丢失"})
                continue
            if src.read(name) != out.read(name):
                diffs.append({"path": name, "reason": "内容被改写"})
                continue
            identical += 1
    checked = identical + len(diffs)
    report.fidelity = {
        "checked": checked,
        "identical": identical,
        "changed_unexpected": len(diffs),
        "diffs": diffs,
    }
    if diffs:
        report.errors.append(f"{len(diffs)} 个未声明改动的资源发生了变化")
        return False
    return True


# ---------------------------------------------------------------------------
# EPUBCheck（可选）
# ---------------------------------------------------------------------------


def _find_java() -> str | None:
    """先查 PATH；便携 JRE 常不在 PATH 上，再查 JAVA_HOME / EPUBCHECK_JAVA。"""

    java = shutil.which("java")
    if java:
        return java
    exe = "java.exe" if os.name == "nt" else "java"
    for var in ("EPUBCHECK_JAVA", "JAVA_HOME"):
        base = os.environ.get(var)
        if not base:
            continue
        root = Path(base)
        for candidate in (root, root / "bin" / exe):
            if candidate.is_file():
                return str(candidate)
    # 便携 JRE 的约定目录，与 EPUBCheck 的 ~/tools 探测对称；
    # 这样即使宿主进程没有继承新设的用户环境变量也能自动发现。
    portable = os.path.join(os.path.expanduser("~"), "tools", "java", "**", "bin", exe)
    for hit in sorted(glob.glob(portable, recursive=True)):
        if Path(hit).is_file():
            return hit
    return None


def find_epubcheck() -> dict:
    """探测 EPUBCheck：环境变量、PATH、常见安装目录。"""

    java = _find_java()
    candidates: list[str] = []
    env_jar = os.environ.get("EPUBCHECK_JAR")
    if env_jar:
        candidates.append(env_jar)
    exe = shutil.which("epubcheck")
    if exe:
        candidates.append(exe)
    patterns = [
        "epubcheck*.jar",
        os.path.join(os.path.expanduser("~"), "epubcheck*", "epubcheck.jar"),
        os.path.join(os.path.expanduser("~"), "tools", "epubcheck*", "epubcheck.jar"),
        os.path.join("C:\\", "epubcheck*", "epubcheck.jar") if os.name == "nt" else "/opt/epubcheck*/epubcheck.jar",
    ]
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    for candidate in candidates:
        if candidate.lower().endswith(".jar") and Path(candidate).is_file():
            if java:
                return {"available": True, "kind": "jar", "path": candidate, "java": java}
        elif Path(candidate).is_file():
            return {"available": True, "kind": "bin", "path": candidate, "java": java}
    return {"available": False, "reason": "未找到 EPUBCheck 可执行文件或 jar", "java": java}


def _run_epubcheck(path: str | Path, *, report_path: str | Path | None = None) -> dict:
    """运行 EPUBCheck 并把官方 JSON 报告解析成可读摘要。

    EPUBCheck 4.x / 5.x 的 JSON 参数是 ``--json <file>``；``--format json``
    不是官方选项，会被 CLI 拒绝，从而把「参数错误」误判成「书不合规」。
    ``report_path`` 会把**完整**官方 JSON（含全部 locations 与
    additionalLocations）留在任务产物旁，供 §22.3 差异归因使用。
    """

    found = find_epubcheck()
    if not found.get("available"):
        return {"ran": False, "status": "not_run", "reason": found.get("reason", "不可用")}
    java = found.get("java")
    if found["kind"] == "jar":
        version_cmd = [java, "-jar", found["path"], "--version"]
    else:
        version_cmd = [found["path"], "--version"]
    version = ""
    try:
        result = subprocess.run(version_cmd, capture_output=True, text=True, timeout=60)
        version = (result.stdout or result.stderr or "").strip().splitlines()[0][:120]
    except Exception:  # noqa: BLE001
        version = "未知"

    with tempfile.TemporaryDirectory(prefix="vocab-epubcheck-") as tmp:
        report_file = Path(tmp) / "assessment.json"
        # EPUBCheck 只认 .epub 后缀：发布门槛校验的是 ``*.epub.pending``，
        # 直接传过去会不产报告，必须先用同字节的临时副本跑。
        inspected = Path(path)
        if inspected.suffix.lower() != ".epub":
            staged = Path(tmp) / "candidate.epub"
            shutil.copyfile(inspected, staged)
            inspected = staged
        if found["kind"] == "jar":
            command = [java, "-jar", found["path"], str(inspected),
                       "--json", str(report_file), "-q"]
        else:
            command = [found["path"], str(inspected), "--json", str(report_file), "-q"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            return {"ran": False, "status": "tool_error", "reason": "无法执行 EPUBCheck"}
        except subprocess.TimeoutExpired:
            return {"ran": False, "status": "tool_error", "reason": "EPUBCheck 超时"}
        payload: dict = {}
        if report_file.is_file():
            try:
                payload = json.loads(report_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if report_path and payload:
                target = Path(report_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_target = target.with_name(target.name + f".part{os.getpid()}")
                try:
                    temp_target.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(temp_target, target)
                except Exception:
                    temp_target.unlink(missing_ok=True)
                    raise
        if not payload or "checker" not in payload:
            return {
                "ran": False,
                "status": "tool_error",
                "reason": "官方报告缺失或无法解析（不是书籍不合规）",
                "output_tail": (result.stdout or result.stderr or "")[-800:],
            }

    checker = payload.get("checker") or {}
    counts = {
        key: checker[key]
        for key in ("nError", "nWarning", "nFatal", "nUsage")
        if key in checker
    }
    messages = []
    for item in (payload.get("messages") or [])[:10]:
        if not isinstance(item, dict):
            continue
        locations = item.get("locations") or []
        first = locations[0] if locations and isinstance(locations[0], dict) else {}
        messages.append({
            "severity": item.get("severity"),
            # EPUBCheck 官方 JSON 用大写 ID；旧版/自定义报告可能用小写 id
            "id": item.get("ID") or item.get("id"),
            "location": str(first.get("path") or "")[:160],
            "message": (item.get("message") or "")[:300],
        })
    return {
        "ran": True,
        "ok": result.returncode == 0,
        "status": "passed" if result.returncode == 0 else "failed",
        "version": version,
        "returncode": result.returncode,
        "counts": counts,
        "messages": messages,
        "output_tail": (result.stdout or result.stderr or "")[-1500:],
    }

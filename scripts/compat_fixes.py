"""§22.5：不规范原书的**有限、确定性**兼容处理（vocab-compat-fixes/v1）。

边界（不要扩成通用修书工具）：

* 输入只读；仅在必要时创建工作副本，输出变更清单与原路径/ID → 新路径/ID 映射；
* 只处理已见且能确定解释的问题：日期格式、引用 URI 空格、缺 NCX 补最小导航；
* 图片 alt、非法但唯一的 ID、旧式外观属性、含空格文件名**只记录不修**，
  不编造描述、不批量加前缀、不重排正文、不为零报错重命名整书；
* 修复后必须复核可见正文、阅读顺序、资源、链接；源输入与未改二进制资源哈希不变；
* 工作副本会改变书籍哈希：按 §22.3，已有任务必须重新建立 v6 内容绑定并
  重新识别 / 审查，**不得手改旧哈希掩盖失效**。

默认链路不对「遗留瑕疵可接受」的书创建工作副本；本模块是显式调用的工具：
``python -m scripts.compat_fixes --book <书> --out <工作副本> --report <清单>``。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import posixpath
import re
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/compat_fixes.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from lxml import etree  # noqa: E402

from .console import configure_stdio  # noqa: E402
from .epub_common import (  # noqa: E402
    MIMETYPE,
    local_name,
    open_epub,
    parse_xml,
    qualified,
    relative_href,
    resolve_href,
    serialize_xml,
)

__all__ = ["SCHEMA", "analyze", "apply_fixes", "verify_work_copy"]

SCHEMA = "vocab-compat-fixes/v1"

_DC_NS = "http://purl.org/dc/elements/1.1/"
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_DATE_WITH_SPACE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2}|Z)?$"
)
_XML_NAME = re.compile(r"^[A-Za-z_][\w.\-]*$")
_LEGACY_ATTRS = {"clear", "link", "vlink", "alink", "bgcolor", "text", "background", "align"}
_MEDIA_ATTRS = ("href", "src", "poster")
_EXTERNAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:|^//")


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _is_external(href: str) -> bool:
    return bool(_EXTERNAL.match(href)) or href.startswith("#")


def _encode_spaces(href: str) -> str:
    """只把引用 URI 里的裸空格编码成 %20；不动实际 ZIP 文件名、不双重编码。"""

    if not href or _is_external(href) or " " not in href:
        return href
    return href.replace(" ", "%20")


def _visible_text(data: bytes) -> str:
    tree = parse_xml(data, "XHTML")
    root = tree.getroot()
    body = next((el for el in root.iter() if local_name(el) == "body"), root)
    return " ".join("".join(body.itertext()).split())


def analyze(book_path: str | Path) -> dict:
    """只读分析：哪些能确定性修复，哪些只记录，哪些影响完整处理必须阻断。"""

    source = Path(book_path)
    archive, package = open_epub(source)
    result: dict = {
        "schema": SCHEMA,
        "source_path": str(source),
        "source_sha256": _sha256(source),
        "version": package.version,
        "applicable": [],
        "recorded": [],
        "blocking": [],
        "work_copy_needed": False,
    }
    try:
        opf_tree = package.tree
        # 1) 日期格式：明确可解析才标准化，不猜不明日期
        for el in opf_tree.getroot().iter():
            if local_name(el) != "date" or not (el.text or "").strip():
                continue
            raw = el.text.strip()
            match = _DATE_WITH_SPACE.match(raw)
            if match:
                fixed = f"{match.group(1)}T{match.group(2)}{match.group(3) or ''}"
                result["applicable"].append(
                    {
                        "rule": "date_format",
                        "path": package.opf_path,
                        "before": raw,
                        "after": fixed,
                        "reason": "日期与时间之间的空格改为 T，保留时间和时区",
                    }
                )
            elif not re.match(r"^\d{4}-\d{2}-\d{2}", raw):
                result["recorded"].append(
                    {"rule": "date_unknown", "path": package.opf_path, "value": raw,
                     "reason": "日期无法明确解析，仅影响元数据，保留为遗留瑕疵"}
                )

        # 2) 引用 URI 空格（含 manifest 与各 XHTML）
        for item in package.items.values():
            if " " in item.href:
                result["applicable"].append(
                    {"rule": "uri_spaces", "path": package.opf_path,
                     "before": item.href, "after": _encode_spaces(item.href),
                     "reason": "仅编码引用中的裸空格，不重命名实际文件"}
                )
        for item in package.xhtml_items():
            data = archive.read(item.path)
            tree = parse_xml(data, item.path)
            for el in tree.getroot().iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in _MEDIA_ATTRS:
                    value = el.get(attr) or ""
                    if " " in value and not _is_external(value):
                        result["applicable"].append(
                            {"rule": "uri_spaces", "path": item.path,
                             "before": value, "after": _encode_spaces(value),
                             "reason": "编码引用 URI 空格，不动片段与外链"}
                        )

        # 3) 缺 NCX / spine toc：spine 完整才补最小导航或补登记
        spine_el = package.spine_element()
        has_toc = bool(spine_el is not None and spine_el.get("toc"))
        if not package.is_epub3 and not has_toc and package.spine:
            if package.ncx_item is None:
                first = package.spine[0].item
                result["applicable"].append(
                    {"rule": "missing_ncx", "path": package.opf_path,
                     "before": "spine 无 toc 且无 NCX",
                     "after": "最小 NCX（单一入口，指向 spine 第一项）",
                     "reason": "spine 完整，补最小有效导航，不虚构章节",
                     "target": relative_href(first.path, package.opf_dir)}
                )
            else:
                result["applicable"].append(
                    {"rule": "spine_toc_attr", "path": package.opf_path,
                     "before": "spine 无 toc 但包内已有 NCX",
                     "after": f"spine toc={package.ncx_item.id}",
                     "reason": "只补登记引用，不新增导航、不虚构章节",
                     "target": package.ncx_item.id}
                )

        # 4) 只记录的遗留项
        ids: dict[str, int] = {}
        missing_alt = 0
        legacy_attr = 0
        for item in package.xhtml_items():
            tree = parse_xml(archive.read(item.path), item.path)
            for el in tree.getroot().iter():
                if not isinstance(el.tag, str):
                    continue
                ident = el.get("id")
                if ident:
                    ids[ident] = ids.get(ident, 0) + 1
                if local_name(el) == "img" and el.get("alt") is None:
                    missing_alt += 1
                if any(attr in el.attrib for attr in _LEGACY_ATTRS):
                    legacy_attr += 1
        duplicates = sorted(k for k, v in ids.items() if v > 1)
        invalid_ids = sorted(k for k in ids if not _XML_NAME.match(k))
        if duplicates:
            result["blocking"].append(
                {"rule": "duplicate_id", "ids": duplicates[:10],
                 "reason": "重复 ID 导致引用目标歧义，不能靠批量加前缀掩盖"}
            )
        if invalid_ids:
            result["recorded"].append(
                {"rule": "invalid_id", "count": len(invalid_ids), "sample": invalid_ids[:5],
                 "reason": "非法但唯一且引用可解析，保留；修复需同步全量引用"}
            )
        if missing_alt:
            result["recorded"].append(
                {"rule": "missing_alt", "count": missing_alt,
                 "reason": "不为规范零错误编造描述，也不把重要插图一律设空"}
            )
        if legacy_attr:
            result["recorded"].append(
                {"rule": "legacy_presentation_attr", "count": legacy_attr,
                 "reason": "无需为非关键规范瑕疵重排书籍，保留版式"}
            )
        spaced_names = [n for n in archive.names if " " in n and not n.endswith("/")]
        if spaced_names:
            result["recorded"].append(
                {"rule": "filename_spaces", "count": len(spaced_names),
                 "sample": spaced_names[:5],
                 "reason": "资源可解析，保留文件名，不为消除警告重命名整书"}
            )
        result["work_copy_needed"] = bool(result["applicable"]) and not result["blocking"]
        result["note"] = (
            "工作副本会改变书籍哈希；按 §22.3 必须重新建立 v6 内容绑定并重新识别/审查。"
            if result["work_copy_needed"]
            else "所有问题均为可接受遗留或无法确定性修复，未创建工作副本。"
        )
    finally:
        archive.close()
    return result


def _unique_path(archive, wanted: str) -> str:
    base, ext = posixpath.splitext(wanted)
    candidate, index = wanted, 1
    while candidate in archive.names:
        candidate = f"{base}-{index}{ext}"
        index += 1
    return candidate


def _minimal_ncx(package, target: str) -> bytes:
    title = package.title or "正文"
    uid = package.identifier or "urn:uuid:vocab-compat-ncx"
    root = etree.Element(f"{{{_NCX_NS}}}ncx", nsmap={None: _NCX_NS})
    root.set("version", "2005-1")
    head = etree.SubElement(root, f"{{{_NCX_NS}}}head")
    meta = etree.SubElement(head, f"{{{_NCX_NS}}}meta")
    meta.set("name", "dtb:uid")
    meta.set("content", uid)
    doc = etree.SubElement(root, f"{{{_NCX_NS}}}docTitle")
    etree.SubElement(doc, f"{{{_NCX_NS}}}text").text = title
    navmap = etree.SubElement(root, f"{{{_NCX_NS}}}navMap")
    point = etree.SubElement(navmap, f"{{{_NCX_NS}}}navPoint")
    point.set("id", "navpoint-1")
    point.set("playOrder", "1")
    label = etree.SubElement(point, f"{{{_NCX_NS}}}navLabel")
    # 用原标题或中性「正文」入口，不伪造章节
    etree.SubElement(label, f"{{{_NCX_NS}}}text").text = title
    content = etree.SubElement(point, f"{{{_NCX_NS}}}content")
    content.set("src", _encode_spaces(target))
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _unique_id(package, wanted: str) -> str:
    candidate, index = wanted, 1
    while candidate in package.items:
        candidate = f"{wanted}-{index}"
        index += 1
    return candidate


def apply_fixes(
    book_path: str | Path,
    work_path: str | Path,
    *,
    report_path: str | Path | None = None,
) -> dict:
    """创建工作副本，只做确定性修复，并保存变更清单与复核结果。"""

    source, target = Path(book_path), Path(work_path)
    if target.exists():
        raise FileExistsError(f"工作副本已存在，拒绝覆盖：{target}")
    if source.resolve() == target.resolve():
        raise ValueError("工作副本不能与源书同路径")
    before_sha = _sha256(source)
    info = analyze(source)
    if info["blocking"]:
        raise ValueError("存在影响完整处理的问题，拒绝创建工作副本：" + str(info["blocking"]))
    if not info["applicable"]:
        info["work_copy_needed"] = False
        info["note"] = "没有需要确定性修复的项，未创建工作副本（遗留项只记录）。"
        return info

    archive, package = open_epub(source)
    try:
        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}
        mapping: dict = {"path": {}, "id": {}}
        applied: list[dict] = []

        # OPF：日期、manifest href、缺 NCX
        opf_tree = package.tree
        changed_opf = False
        for el in opf_tree.getroot().iter():
            if local_name(el) == "date" and (el.text or "").strip():
                match = _DATE_WITH_SPACE.match(el.text.strip())
                if match:
                    el.text = f"{match.group(1)}T{match.group(2)}{match.group(3) or ''}"
                    changed_opf = True
            for attr in ("href",):
                value = el.get(attr) or ""
                if " " in value and not _is_external(value):
                    el.set(attr, _encode_spaces(value))
                    changed_opf = True

        toc_attr_rules = [r for r in info["applicable"] if r["rule"] == "spine_toc_attr"]
        for rule in toc_attr_rules:
            package.spine_element().set("toc", rule["target"])
            mapping["id"]["(spine.toc)"] = rule["target"]
            changed_opf = True
            applied.append({"rule": "spine_toc_attr", "path": package.opf_path,
                            "manifest_id": rule["target"], "navpoints": 0})

        ncx_rules = [r for r in info["applicable"] if r["rule"] == "missing_ncx"]
        if ncx_rules:
            # NCX 与 OPF 同目录：NCX 的 content src 相对自身所在目录解析，
            # 放到包根目录会产生 RSC-007（引用找不到资源）。
            ncx_path = _unique_path(
                archive, posixpath.join(package.opf_dir, "toc.ncx")
            )
            additions[ncx_path] = _minimal_ncx(package, ncx_rules[0]["target"])
            manifest = package.manifest_element()
            item = etree.SubElement(manifest, qualified(manifest, "item"))
            item_id = _unique_id(package, "vocab-ncx")
            item.set("id", item_id)
            item.set("href", relative_href(ncx_path, package.opf_dir))
            item.set("media-type", "application/x-dtbncx+xml")
            package.spine_element().set("toc", item_id)
            mapping["id"]["(spine.toc)"] = item_id
            changed_opf = True
            applied.append({"rule": "missing_ncx", "path": ncx_path,
                            "manifest_id": item_id, "navpoints": 1})

        if changed_opf:
            replacements[package.opf_path] = serialize_xml(opf_tree)

        # XHTML：引用 URI 空格（不改文本节点）
        for item in package.xhtml_items():
            tree = parse_xml(archive.read(item.path), item.path)
            changed = False
            for el in tree.getroot().iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in _MEDIA_ATTRS:
                    value = el.get(attr) or ""
                    if " " in value and not _is_external(value):
                        el.set(attr, _encode_spaces(value))
                        changed = True
            if changed:
                replacements[item.path] = serialize_xml(tree)

        for rule in info["applicable"]:
            if rule["rule"] in ("date_format", "uri_spaces"):
                applied.append(rule)
        info["applied"] = applied
        info["mapping"] = mapping
        info["changed_files"] = sorted(replacements)
        info["added_files"] = sorted(additions)

        temp = target.with_name(target.name + f".part{os.getpid()}")
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as out:
                if "mimetype" in archive.names:
                    zinfo = copy.copy(archive.info("mimetype"))
                else:
                    zinfo = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
                zinfo.compress_type = zipfile.ZIP_STORED
                out.writestr(zinfo, MIMETYPE.encode("ascii") if isinstance(MIMETYPE, str) else MIMETYPE)
                for name in archive.names:
                    if name == "mimetype":
                        continue
                    data = replacements.get(name)
                    if data is None:
                        data = archive.read(name)
                    out.writestr(copy.copy(archive.info(name)), data)
                for name, data in additions.items():
                    zinfo = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    zinfo.compress_type = zipfile.ZIP_DEFLATED
                    out.writestr(zinfo, data)
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    finally:
        archive.close()

    info["work_path"] = str(target)
    info["work_sha256"] = _sha256(target)
    info["verification"] = verify_work_copy(source, target, set(info["changed_files"]))
    info["verification"]["input_sha256_unchanged"] = _sha256(source) == before_sha
    info["verification"]["hash_binding_notice"] = (
        "书籍哈希已改变：必须重新 emit/identify 并重新审查，不得复用旧 scope/review。"
    )
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return info


def verify_work_copy(source: Path, work: Path, changed_files: set[str]) -> dict:
    """复核可见正文、阅读顺序、二进制资源、链接与新增导航。"""

    result: dict = {
        "visible_text_identical": False,
        "spine_order_identical": False,
        "binary_resources_identical": False,
        "links_resolvable": False,
        "ncx_navpoints": 0,
        "input_sha256_unchanged": True,
    }
    src_arch, src_pkg = open_epub(source)
    work_arch, work_pkg = open_epub(work)
    try:
        src_order = [s.item.id for s in src_pkg.spine]
        work_order = [s.item.id for s in work_pkg.spine]
        result["spine_order_identical"] = src_order == work_order

        texts_equal = True
        for s in src_pkg.spine:
            if not s.item.is_xhtml:
                continue
            if s.item.path not in work_arch.names:
                texts_equal = False
                break
            if _visible_text(src_arch.read(s.item.path)) != _visible_text(
                work_arch.read(s.item.path)
            ):
                texts_equal = False
                break
        result["visible_text_identical"] = texts_equal

        untouched = [
            name for name in src_arch.names
            if name not in changed_files and name in work_arch.names
            and not name.lower().endswith((".xhtml", ".html", ".htm", ".opf", ".xml"))
        ]
        result["binary_resources_identical"] = all(
            src_arch.read(name) == work_arch.read(name) for name in untouched
        )

        missing = []
        for item in work_pkg.items.values():
            path = resolve_href(work_pkg.opf_dir, item.href)
            if path not in work_arch.names:
                missing.append(item.href)
        result["links_resolvable"] = not missing
        result["unresolved_links"] = missing[:10]

        if work_pkg.ncx_item:
            data = work_arch.read(work_pkg.ncx_item.path)
            result["ncx_navpoints"] = len(
                [el for el in parse_xml(data, "NCX").getroot().iter()
                 if local_name(el) == "navPoint"]
            )
    finally:
        src_arch.close()
        work_arch.close()
    return result


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="§22.5 不规范 EPUB 的有限兼容处理")
    parser.add_argument("--book", required=True, metavar="EPUB")
    parser.add_argument("--out", default="", metavar="EPUB", help="工作副本路径")
    parser.add_argument("--report", default="", metavar="JSON", help="变更清单路径")
    parser.add_argument("--analyze-only", action="store_true", help="只分析，不创建工作副本")
    args = parser.parse_args(argv)

    book = Path(args.book)
    if not book.is_file():
        print(f"输入不存在：{book}", file=sys.stderr)
        return 2
    if args.analyze_only or not args.out:
        info = analyze(book)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    info = apply_fixes(book, Path(args.out), report_path=args.report or None)
    print(json.dumps({
        "work_path": info["work_path"],
        "applied": len(info["applied"]),
        "recorded": len(info["recorded"]),
        "verification": info["verification"],
        "note": info["note"],
    }, ensure_ascii=False, indent=2))
    return 0 if all(
        info["verification"][key]
        for key in (
            "visible_text_identical", "spine_order_identical",
            "binary_resources_identical", "links_resolvable",
            "input_sha256_unchanged",
        )
    ) else 4


if __name__ == "__main__":
    raise SystemExit(main())

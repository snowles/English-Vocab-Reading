"""§22.4：EPUB 2 合规 DOM 与随任务保存的标注映射（vocab-reader-map/v1）。

问题背景：EPUB 2 要求 XHTML 1.1，本 Skill 早期把内部信息直接写成
``data-*`` 属性、章末词条用 HTML5 ``<section>``。EPUBCheck 5.4.0 实测两本
样例各净增 12 类 RSC-005 错误，全部来自这两类写法。

处理原则：

* **EPUB 内部**只保留 XHTML 1.1 合法的 ``id`` / ``class`` / ``href``；
  维持定位、跳转、回链和读者可见文本，不依赖任何外部文件即可阅读。
* **内部标注信息**剥离后存到 EPUB 包外的映射文件（``*.vocab-map.json``），
  以稳定 ``id`` 对应正文节点；HTML 阅读页生成时再把 ``data-*`` 注入内存 DOM。
* 不把 JSON 塞进 EPUB 包，也不对源书原有属性做全局清洗：只处理
  ``VOCAB_ATTRS`` 白名单里的、由本 Skill 写入的属性。
* 映射文件记录产物 SHA-256，校验器按**最终产物的实际节点**核对映射，
  不能只检查旁边 JSON 自洽。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from lxml import etree

from .epub_common import local_name, parse_xml, qualified, serialize_xml

__all__ = [
    "SCHEMA",
    "VOCAB_ATTRS",
    "binding_path_for",
    "is_annotated",
    "strip_vocab_metadata",
    "write_binding",
    "load_binding",
    "apply_binding",
    "audit_binding",
]

SCHEMA = "vocab-reader-map/v1"

#: 仅这些属性会被剥离；源书自带的其它 data-* 一律不动。
VOCAB_ATTRS = frozenset(
    {
        "data-vocab-id",
        "data-entry-id",
        "data-note",
        "data-chapter",
        "data-source",
        "data-label-scope",
        "data-unit",
        "data-importance",
        "data-gloss",
        "data-head",
        "data-confidence",
        "data-sense-id",
    }
)

#: 净化后追加的合法 class，兼作「已处理」标记（替代旧的 data-vocab-id）。
MARK_CLASS = "vocab-skill"

_XHTML_SUFFIXES = (".xhtml", ".html", ".htm")


def is_xhtml(path: str) -> bool:
    return path.lower().endswith(_XHTML_SUFFIXES)


def _strip_pending(name: str) -> str:
    return name[: -len(".pending")] if name.endswith(".pending") else name


def binding_path_for(epub_path: str | Path, *, pending: bool = False) -> Path:
    """映射文件路径。``*.epub.pending`` 与最终 ``*.epub`` 共用同一基准名。"""

    path = Path(epub_path)
    base = path.with_name(_strip_pending(path.name) + ".vocab-map.json")
    return base.with_name(base.name + ".pending") if pending else base


def is_annotated(data: bytes) -> bool:
    """重复标注检测：同时识别新标记与旧版 ``data-vocab-id`` 产物。"""

    return MARK_CLASS.encode("ascii") in data or b"data-vocab-id" in data


def _doc_token(doc_path: str) -> str:
    return hashlib.sha256(doc_path.encode("utf-8")).hexdigest()[:8]


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())[:160]


def strip_vocab_metadata(data: bytes, doc_path: str) -> tuple[bytes, dict[str, dict]]:
    """从 XHTML 剥离本工具写入的 data-*，把章末 section 换成 div。

    返回（净化后的字节、``id -> 属性记录``）。没有可剥离内容时原样返回，
    避免无意义的二次序列化改变原书字节。
    """

    if not any(attr.encode("ascii") in data for attr in VOCAB_ATTRS) and (
        b"vocab-chapter-notes" not in data or b"<section" not in data
    ):
        return data, {}

    tree = parse_xml(data, doc_path)
    root = tree.getroot()
    records: dict[str, dict] = {}
    changed = False
    seq = 0

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        classes = (el.get("class") or "").split()
        if local_name(el) == "section" and "vocab-chapter-notes" in classes:
            el.tag = qualified(el, "div")
            changed = True

        found = {key: value for key, value in el.attrib.items() if key in VOCAB_ATTRS}
        if not found:
            continue

        ident = el.get("id")
        if not ident:
            seq += 1
            ident = f"vb-{_doc_token(doc_path)}-{seq}"
            el.set("id", ident)
        original_classes = " ".join(classes)
        if MARK_CLASS not in classes:
            el.set("class", (original_classes + " " + MARK_CLASS).strip())

        record = {
            "_doc": doc_path,
            "_tag": local_name(el),
            "_class": original_classes,
            "_text": _norm(el.text),
        }
        record.update(found)
        records[ident] = record
        for key in found:
            del el.attrib[key]
        changed = True

    if not changed:
        return data, {}
    return serialize_xml(tree), records


def write_binding(path: str | Path, epub_path: str | Path, records: dict[str, dict]) -> Path:
    """把映射写到 EPUB 包外；记录产物 SHA-256 绑定，原子落盘。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "epub_sha256": hashlib.sha256(Path(epub_path).read_bytes()).hexdigest(),
        "epub_name": _strip_pending(Path(epub_path).name),
        "attrs": records,
    }
    temp = target.with_name(target.name + f".part{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return target


def load_binding(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"映射文件 schema 不符：{payload.get('schema')!r}")
    if not isinstance(payload.get("attrs"), dict):
        raise ValueError("映射文件缺少 attrs")
    return payload


def _root_of(tree):
    return tree.getroot() if hasattr(tree, "getroot") else tree


def _index_roots(roots) -> tuple[dict, dict]:
    """建两套索引：``(文档, id) -> 节点`` 与 ``id -> [(文档, 节点)]``。

    文档名未知时（传入的是裸根节点列表），``_doc`` 侧为空，只能靠候选数量
    与 ``_tag`` / ``_class`` 特征消歧，见 ``_pick_node()``。
    """

    by_doc: dict[tuple[str, str], object] = {}
    candidates: dict[str, list[tuple[str, object]]] = {}
    if isinstance(roots, dict):
        pairs = [(path, _root_of(tree)) for path, tree in roots.items()]
    elif hasattr(roots, "iter"):
        pairs = [("", _root_of(roots))]
    else:
        pairs = [("", _root_of(tree)) for tree in roots]
    for path, root in pairs:
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            ident = el.get("id")
            if not ident:
                continue
            if path:
                by_doc[(path, ident)] = el
            candidates.setdefault(ident, []).append((path, el))
    return by_doc, candidates


def _shape_matches(el, record: dict) -> bool:
    """候选节点是否与映射记录的形状特征一致（标签、class 子集）。"""

    tag = record.get("_tag")
    if tag and local_name(el) != tag:
        return False
    expected = set((record.get("_class") or "").split())
    if not expected:
        return True
    return expected.issubset(set((el.get("class") or "").split()))


def _pick_node(ident: str, record: dict, by_doc: dict, candidates: dict):
    """按记录定位最终产物节点，返回 ``(文档, 节点, 状态)``。

    状态为 ``None``（唯一命中）、``"missing"``、``"ambiguous"``。
    同名 ID 在多个文档里合法，所以先按 ``_doc`` 精确命中；没有文档名时
    才退化为唯一候选或形状特征筛选，避免错绑到另一章的同名节点。
    """

    doc = record.get("_doc") or ""
    if doc:
        el = by_doc.get((doc, ident))
        if el is not None:
            return doc, el, None
        others = candidates.get(ident) or []
        if others:
            return others[0][0], others[0][1], None  # 文档不一致由调用方报错
        return None, None, "missing"
    hits = candidates.get(ident) or []
    if not hits:
        return None, None, "missing"
    if len(hits) == 1:
        return hits[0][0], hits[0][1], None
    shaped = [hit for hit in hits if _shape_matches(hit[1], record)]
    if len(shaped) == 1:
        return shaped[0][0], shaped[0][1], None
    return None, None, "ambiguous"


def apply_binding(roots, binding: dict) -> dict:
    """把映射里的 data-* 注入内存 DOM（HTML 生成 / 校验前调用）。

    ``roots`` 可以是单个节点、多个文档的根节点列表，或
    ``{文档路径: 树}`` 映射。给出文档路径时按 ``_doc`` 精确定位，
    避免原书同名 ID 与生成节点互相错绑。
    必须在给 id 加书内前缀**之前**调用，否则稳定 ID 对不上。
    """

    by_doc, candidates = _index_roots(roots)
    stats = {
        "matched": 0,
        "missing": 0,
        "ambiguous": 0,
        "attrs": 0,
        "total": len(binding["attrs"]),
    }
    for ident, record in binding["attrs"].items():
        _, el, state = _pick_node(ident, record, by_doc, candidates)
        if state == "ambiguous":
            stats["ambiguous"] += 1
            continue
        if el is None:
            stats["missing"] += 1
            continue
        for key, value in record.items():
            if key.startswith("data-"):
                el.set(key, value)
                stats["attrs"] += 1
        stats["matched"] += 1
    return stats


def audit_binding(trees: dict, binding: dict, epub_sha256: str = "") -> list[str]:
    """按最终产物实际节点核对映射，防止「旁边 JSON 自洽但产物不符」。

    * 映射里的每个稳定 ID 必须在产物中存在，且文档、标签、class、直接文本一致；
    * 产物里每个 ``vocab-skill`` 节点都必须在映射中有记录（防漏记）；
    * 短义、编号、词头等可从实际节点文本复核的字段必须与映射一致；
    * 提供 epub_sha256 时核对映射与产物字节一致。

    ID 唯一性按**单个 XHTML 文档**判定：不同章节各有一个 ``id="p1"`` 是合法
    输入（EPUB 内锚点本就只在本文档内解析），同文档内重复仍视为产物损坏。
    """

    errors: list[str] = []
    if epub_sha256 and binding.get("epub_sha256") != epub_sha256:
        errors.append("标注映射与 EPUB 产物字节不一致（SHA-256 不匹配）")

    by_doc, candidates = _index_roots(trees)
    duplicates: set[tuple[str, str]] = set()
    seen: dict[str, set[str]] = {}
    for path, tree in trees.items():
        for el in _root_of(tree).iter():
            if not isinstance(el.tag, str):
                continue
            ident = el.get("id")
            if not ident:
                continue
            bucket = seen.setdefault(path, set())
            if ident in bucket:
                duplicates.add((path, ident))
            bucket.add(ident)

    for path, ident in sorted(duplicates):
        errors.append(f"产物在 {path} 内存在重复 id：{ident}")

    records = binding.get("attrs") or {}
    for ident, record in records.items():
        path, el, state = _pick_node(ident, record, by_doc, candidates)
        if state == "ambiguous":
            errors.append(
                f"{ident}: 产物中多个文档存在同名 id 且映射缺少 _doc，无法确定绑定"
            )
            continue
        if el is None:
            errors.append(f"映射记录了产物中不存在的节点：{ident}")
            continue
        if record.get("_doc") and record["_doc"] != path:
            errors.append(f"{ident}: 映射文档 {record['_doc']} 与产物 {path} 不一致")
        if record.get("_tag") and record["_tag"] != local_name(el):
            errors.append(f"{ident}: 映射标签 {record['_tag']} 与产物 {local_name(el)} 不一致")
        expected_classes = set((record.get("_class") or "").split())
        actual_classes = set((el.get("class") or "").split())
        if not expected_classes.issubset(actual_classes):
            errors.append(f"{ident}: 映射 class 与产物 class 不一致")
        if record.get("_text") != _norm(el.text):
            errors.append(f"{ident}: 映射记录的节点文本与产物不一致")

        # 可从实际节点复核的字段：短义、编号、章节、词头
        gloss = record.get("data-gloss")
        if gloss and "vocab-sense" in actual_classes:
            body = _norm("".join(el.itertext()))
            if gloss not in body:
                errors.append(f"{ident}: 章末词条文本与映射短义不一致")
        note = record.get("data-note")
        if note and "vocab" in actual_classes:
            nxt = el.getnext()
            if nxt is not None and "vocab-note" in ((nxt.get("class") or "").split()):
                if f"[{note}]" not in "".join(nxt.itertext()):
                    errors.append(f"{ident}: 正文括注编号与映射不一致")
        chapter = record.get("data-chapter")
        if chapter and ident.startswith("vref-") and not ident.startswith(f"vref-{chapter}-"):
            errors.append(f"{ident}: 章节编号与稳定 ID 不一致")
        head = record.get("data-head")
        if head and "vocab-item" in actual_classes:
            lemmas = [
                _norm("".join(node.itertext()))
                for node in el.iter()
                if isinstance(node.tag, str)
                and "vocab-lemma" in ((node.get("class") or "").split())
            ]
            if lemmas and head not in lemmas:
                errors.append(f"{ident}: 章末词头与映射可信原形不一致")

    marked: set[str] = set()
    for tree in trees.values():
        for el in _root_of(tree).iter():
            if (
                isinstance(el.tag, str)
                and MARK_CLASS in ((el.get("class") or "").split())
                and el.get("id")
            ):
                marked.add(el.get("id"))
    for ident in sorted(marked - set(records)):
        errors.append(f"产物存在未登记的标注节点：{ident}")
    return errors

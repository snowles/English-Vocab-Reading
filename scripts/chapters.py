"""逻辑章节识别（§16.5）。

不能默认"一个 spine XHTML 文件 = 一章"：
    * 优先按原书 nav（EPUB3）/ NCX（EPUB2）的目标位置切章；
    * 目标位置可以在同一文件内部（同文件多章），也可以跨文件（一章跨多文件）；
    * 没有导航信息时，整本书作为一章（绝不用"每 1200 词"之类窗口冒充章节）；
    * 第一个章节起点之前的内容（封面、献词、版权）视为前置页，不参与标注。

每个章节带两个位置：
    start_path / start_id          章节起点
    end_path  / end_before_id      词注块插入位置（章节真正结束处）
`end_before_id is None` 表示插到该文件 body 末尾。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .epub_common import Archive, Package, local_name, parse_xml, resolve_href

OPS_NS = "http://www.idpf.org/2007/ops"

__all__ = ["Chapter", "detect_chapters", "toc_targets"]


@dataclass(frozen=True)
class Chapter:
    index: int
    label: str
    start_path: str
    start_id: str | None
    end_path: str
    end_before_id: str | None

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "start": self.start_path + (f"#{self.start_id}" if self.start_id else ""),
            "notes_at": self.end_path + (f"#{self.end_before_id}" if self.end_before_id else " (body end)"),
        }


def _nav_anchor_targets(archive: Archive, package: Package) -> list[tuple[str, str | None, str]]:
    nav = package.nav_item
    if nav is None or not archive.exists(nav.path):
        return []
    tree = parse_xml(archive.read(nav.path), nav.path)
    root = tree.getroot()
    scope = None
    for el in root.iter():
        if local_name(el) != "nav":
            continue
        types = (el.get(f"{{{OPS_NS}}}type") or "").split()
        if "toc" in types:
            scope = el
            break
    scope = scope if scope is not None else root

    out: list[tuple[str, str | None, str]] = []
    base = posixpath.dirname(nav.path)
    for el in scope.iter():
        if local_name(el) != "a":
            continue
        href = el.get("href")
        if not href:
            continue
        path, _, fragment = href.partition("#")
        label = " ".join("".join(el.itertext()).split())
        out.append((resolve_href(base, path or nav.path), fragment or None, label))
    return out


def _ncx_targets(archive: Archive, package: Package) -> list[tuple[str, str | None, str]]:
    ncx = package.ncx_item
    if ncx is None or not archive.exists(ncx.path):
        return []
    tree = parse_xml(archive.read(ncx.path), ncx.path)
    root = tree.getroot()
    navmap = None
    for el in root.iter():
        if local_name(el) == "navMap":
            navmap = el
            break
    if navmap is None:
        return []

    base = posixpath.dirname(ncx.path)
    out: list[tuple[str, str | None, str]] = []
    for point in navmap.iter():
        if local_name(point) != "navPoint":
            continue
        src = None
        label = ""
        for child in point:
            name = local_name(child)
            if name == "content" and child.get("src"):
                src = child.get("src")
            elif name == "navLabel":
                label = " ".join("".join(child.itertext()).split())
        if not src:
            continue
        path, _, fragment = src.partition("#")
        out.append((resolve_href(base, path), fragment or None, label))
    return out


def toc_targets(archive: Archive, package: Package) -> list[tuple[str, str | None, str]]:
    """取目录目标（路径, 片段, 标题），保持原书顺序。"""

    targets = _nav_anchor_targets(archive, package)
    if not targets:
        targets = _ncx_targets(archive, package)
    return targets


def detect_chapters(archive: Archive, package: Package) -> list[Chapter]:
    spine_paths = [s.item.path for s in package.spine]
    if not spine_paths:
        return []

    targets = [
        (path, fragment, label)
        for path, fragment, label in toc_targets(archive, package)
        if path in spine_paths
    ]

    # 去重：连续指向同一位置只保留第一个
    deduped: list[tuple[str, str | None, str]] = []
    for entry in targets:
        if deduped and deduped[-1][:2] == entry[:2]:
            continue
        deduped.append(entry)

    if not deduped:
        # 无导航：整本当一章；前置页不存在，从第一个 spine 文件开始
        return [
            Chapter(
                index=1,
                label=package.title or "全文",
                start_path=spine_paths[0],
                start_id=None,
                end_path=spine_paths[-1],
                end_before_id=None,
            )
        ]

    chapters: list[Chapter] = []
    for i, (path, fragment, label) in enumerate(deduped):
        next_entry = deduped[i + 1] if i + 1 < len(deduped) else None
        if next_entry is None:
            end_path, end_before = spine_paths[-1], None
        elif next_entry[1]:
            end_path, end_before = next_entry[0], next_entry[1]
        else:
            # 下一章从某个文件开头开始 -> 本章在它前一个文件末尾结束
            try:
                position = spine_paths.index(next_entry[0])
            except ValueError:
                position = len(spine_paths) - 1
            end_path, end_before = spine_paths[max(0, position - 1)], None
        chapters.append(
            Chapter(
                index=i + 1,
                label=label or f"第 {i + 1} 章",
                start_path=path,
                start_id=fragment,
                end_path=end_path,
                end_before_id=end_before,
            )
        )
    return chapters


def doc_chapter_map(
    chapters: list[Chapter], spine_paths: list[str]
) -> dict[str, tuple[int, dict[str, int]]]:
    """每个文件 -> (文件开始时的章节号, {元素 id: 章节号})。

    0 表示"前置页"（第一个章节起点之前，不标注）。
    """

    starts: dict[str, list[tuple[str | None, int]]] = {}
    for chapter in chapters:
        starts.setdefault(chapter.start_path, []).append((chapter.start_id, chapter.index))

    result: dict[str, tuple[int, dict[str, int]]] = {}
    current = 0
    for path in spine_paths:
        entries = starts.get(path, [])
        default = current
        id_map: dict[str, int] = {}
        for fragment, index in entries:
            if fragment is None:
                default = index
            else:
                id_map[fragment] = index
        if entries:
            # 本文件里最后一个开始的章节，会成为下一个文件开始时的"当前章节"
            current = entries[-1][1]
        result[path] = (default, id_map)
    return result

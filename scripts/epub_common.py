"""EPUB 容器与包结构解析的共享底层（只读，安全默认）。

三条硬规则：
    * 只解析、不联网：`no_network=True`，不加载外部 DTD；
    * 不碰 DRM：发现 `META-INF/encryption.xml` 或 `rights.xml` 一律报错；
    * 不猜测：任何结构异常都抛 `EpubError`，由调用方决定失败策略。
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

from lxml import etree

__all__ = [
    "NS",
    "EpubError",
    "Archive",
    "ManifestItem",
    "SpineItem",
    "Package",
    "local_name",
    "parse_xml",
    "serialize_xml",
    "resolve_href",
    "relative_href",
    "is_excluded",
    "TextSlot",
    "iter_text_slots",
    "open_epub",
]

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ops": "http://www.idpf.org/2007/ops",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "svg": "http://www.w3.org/2000/svg",
}

XHTML_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "application/xml",
    "text/xml",
}

MIMETYPE = b"application/epub+zip"


class EpubError(Exception):
    """EPUB 结构不可处理（损坏、加密、缺少必要部件）。"""


# ---------------------------------------------------------------------------
# XML 解析 / 序列化
# ---------------------------------------------------------------------------

#: 部分老书用命名实体但没有可访问的 DTD。这里只在**解析失败后**补一份内部子集，
#: 不做正文替换；实体本身在序列化时原样保留。
_HTML_ENTITIES: dict[str, int] = {
    "nbsp": 160, "iexcl": 161, "cent": 162, "pound": 163, "curren": 164,
    "yen": 165, "brvbar": 166, "sect": 167, "uml": 168, "copy": 169,
    "ordf": 170, "laquo": 171, "not": 172, "shy": 173, "reg": 174,
    "macr": 175, "deg": 176, "plusmn": 177, "sup2": 178, "sup3": 179,
    "acute": 180, "micro": 181, "para": 182, "middot": 183, "cedil": 184,
    "sup1": 185, "ordm": 186, "raquo": 187, "frac14": 188, "frac12": 189,
    "frac34": 190, "iquest": 191, "times": 215, "divide": 247,
    "ndash": 8211, "mdash": 8212, "lsquo": 8216, "rsquo": 8217,
    "sbquo": 8218, "ldquo": 8220, "rdquo": 8221, "bdquo": 8222,
    "dagger": 8224, "Dagger": 8225, "bull": 8226, "hellip": 8230,
    "permil": 8240, "prime": 8242, "Prime": 8243, "lsaquo": 8249,
    "rsaquo": 8250, "oline": 8254, "frasl": 8260, "euro": 8364,
    "trade": 8482, "larr": 8592, "uarr": 8593, "rarr": 8594, "darr": 8595,
    "harr": 8596, "ensp": 8194, "emsp": 8195, "thinsp": 8201,
    "zwnj": 8204, "zwj": 8205, "lrm": 8206, "rlm": 8207,
}


def _internal_subset() -> str:
    return "".join(
        f'<!ENTITY {name} "&#{code};">' for name, code in _HTML_ENTITIES.items()
    )


def _inject_entity_subset(data: bytes) -> bytes:
    """给文档补一个只含 HTML 命名实体的内部 DTD 子集。"""

    text = data.decode("utf-8", errors="surrogateescape")
    subset = _internal_subset()
    m = re.search(r"<!DOCTYPE[^>\[]*\[", text, re.IGNORECASE)
    if m:
        text = text[: m.end()] + subset + text[m.end() :]
    else:
        m = re.search(r"<!DOCTYPE[^>]*>", text, re.IGNORECASE)
        if m:
            doctype = m.group(0)[:-1] + f" [{subset}]>"
            text = text[: m.start()] + doctype + text[m.end() :]
        else:
            m = re.search(r"<(?![?!])", text)
            if not m:
                raise EpubError("无法为文档定位根元素")
            text = text[: m.start()] + f"<!DOCTYPE html [{subset}]> " + text[m.start() :]
    return text.encode("utf-8", errors="surrogateescape")


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        remove_blank_text=False,
        huge_tree=True,
        recover=False,
    )


def parse_xml(data: bytes, what: str = "XML") -> etree._ElementTree:
    """解析 XML/XHTML。实体未定义时补一份内部子集重试一次。"""

    try:
        # fromstring 保留注释与 DOCTYPE（在 docinfo 中）
        root = etree.fromstring(data, parser=_parser())
    except etree.XMLSyntaxError as exc:
        message = str(exc)
        if "Entity" not in message:
            raise EpubError(f"{what} 无法解析：{message}") from exc
        try:
            root = etree.fromstring(_inject_entity_subset(data), parser=_parser())
        except etree.XMLSyntaxError as exc2:
            raise EpubError(f"{what} 无法解析：{exc2}") from exc2
    return root.getroottree()


def serialize_xml(tree: etree._ElementTree) -> bytes:
    """回写为 UTF-8 字节，保留 XML 声明与 DOCTYPE。"""

    doctype = tree.docinfo.doctype or None
    return etree.tostring(
        tree,
        xml_declaration=True,
        encoding="utf-8",
        doctype=doctype,
        pretty_print=False,
    )


def local_name(element) -> str:
    tag = element.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def qualified(element, name: str) -> str:
    """生成与 `element` 同命名空间的标签名。"""

    tag = element.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[: tag.index("}") + 1] + name
    return name


def iter_local(element, name: str) -> Iterator:
    for child in element.iter():
        if local_name(child) == name:
            yield child


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def resolve_href(base_dir: str, href: str) -> str:
    """把 manifest href 解析为容器内绝对路径（去锚点、解百分号编码）。"""

    clean = unquote(href.split("#", 1)[0]).strip()
    if not clean:
        return base_dir
    clean = clean.lstrip("/")
    joined = posixpath.join(base_dir, clean) if base_dir else clean
    return posixpath.normpath(joined)


def relative_href(target: str, base_dir: str) -> str:
    """求 `base_dir` 下引用 `target` 的相对路径（用于 href 属性）。"""

    if not base_dir:
        return target
    return posixpath.relpath(target, base_dir)


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------


class Archive:
    """只读 ZIP 视图。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise EpubError(f"文件不存在：{self.path}")
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
        except zipfile.BadZipFile as exc:
            raise EpubError(f"不是合法的 ZIP/EPUB 容器：{exc}") from exc
        self._infos = {i.filename: i for i in self._zip.infolist()}
        self.names = [i.filename for i in self._zip.infolist()]

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._zip.close()

    def exists(self, name: str) -> bool:
        return name in self._infos

    def read(self, name: str) -> bytes:
        try:
            return self._zip.read(name)
        except KeyError as exc:
            raise EpubError(f"容器内缺少资源：{name}") from exc

    def info(self, name: str) -> zipfile.ZipInfo:
        return self._infos[name]

    @property
    def zipfile(self) -> zipfile.ZipFile:
        return self._zip


# ---------------------------------------------------------------------------
# 包结构
# ---------------------------------------------------------------------------


@dataclass
class ManifestItem:
    id: str
    href: str
    media_type: str
    properties: str
    path: str

    @property
    def is_xhtml(self) -> bool:
        return self.media_type in XHTML_TYPES or self.path.lower().endswith(
            (".xhtml", ".html", ".htm")
        )

    @property
    def is_nav(self) -> bool:
        return "nav" in self.properties.split()

    @property
    def is_encrypted(self) -> bool:
        return "encrypted" in self.properties.split()


@dataclass
class SpineItem:
    index: int
    idref: str
    linear: bool
    item: ManifestItem


@dataclass
class Package:
    opf_path: str
    opf_dir: str
    version: str
    title: str
    identifier: str
    tree: etree._ElementTree
    items: dict[str, ManifestItem] = field(default_factory=dict)
    spine: list[SpineItem] = field(default_factory=list)
    nav_item: ManifestItem | None = None
    ncx_item: ManifestItem | None = None

    # -- 查询 ---------------------------------------------------------

    def item_by_path(self, path: str) -> ManifestItem | None:
        for item in self.items.values():
            if item.path == path:
                return item
        return None

    def xhtml_items(self) -> list[ManifestItem]:
        return [i for i in self.items.values() if i.is_xhtml]

    @property
    def is_epub3(self) -> bool:
        return self.version.strip().startswith("3")

    def manifest_element(self):
        for el in self.tree.getroot():
            if local_name(el) == "manifest":
                return el
        raise EpubError("OPF 缺少 manifest")

    def spine_element(self):
        for el in self.tree.getroot():
            if local_name(el) == "spine":
                return el
        raise EpubError("OPF 缺少 spine")


def open_epub(path: str | Path) -> tuple[Archive, Package]:
    """打开 EPUB，返回 (归档, 包结构)。任何异常都抛 `EpubError`。"""

    archive = Archive(path)
    try:
        package = load_package(archive)
    except Exception:
        archive.close()
        raise
    return archive, package


def load_package(archive: Archive) -> Package:
    _check_mimetype(archive)
    _check_drm(archive)

    if not archive.exists("META-INF/container.xml"):
        raise EpubError("缺少 META-INF/container.xml，不是合法 EPUB")
    container_data = archive.read("META-INF/container.xml")
    container_tree = parse_xml(container_data, "META-INF/container.xml")

    rootfiles = [
        el
        for el in container_tree.getroot().iter()
        if local_name(el) == "rootfile"
    ]
    opf_path = None
    for el in rootfiles:
        candidate = el.get("full-path")
        media = el.get("media-type") or "application/oebps-package+xml"
        if candidate and media == "application/oebps-package+xml":
            opf_path = resolve_href("", candidate)
            break
    if not opf_path:
        if not rootfiles:
            raise EpubError("container.xml 未声明 rootfile")
        opf_path = resolve_href("", rootfiles[0].get("full-path", ""))
    if not opf_path or not archive.exists(opf_path):
        raise EpubError(f"OPF 文件不存在：{opf_path}")

    opf_tree = parse_xml(archive.read(opf_path), opf_path)
    root = opf_tree.getroot()
    version = (root.get("version") or "2.0").strip()
    opf_dir = posixpath.dirname(opf_path)

    title = ""
    identifier = ""
    for el in root.iter():
        name = local_name(el)
        if name == "title" and not title and (el.text or "").strip():
            title = el.text.strip()
        elif name == "identifier" and not identifier and (el.text or "").strip():
            identifier = el.text.strip()

    package = Package(
        opf_path=opf_path,
        opf_dir=opf_dir,
        version=version,
        title=title,
        identifier=identifier,
        tree=opf_tree,
    )

    manifest_el = package.manifest_element()
    for el in manifest_el:
        if local_name(el) != "item":
            continue
        item_id = el.get("id") or ""
        href = el.get("href") or ""
        if not item_id or not href:
            continue
        item = ManifestItem(
            id=item_id,
            href=href,
            media_type=(el.get("media-type") or "").strip(),
            properties=(el.get("properties") or "").strip(),
            path=resolve_href(opf_dir, href),
        )
        if item.id in package.items:
            raise EpubError(f"manifest 中出现重复 id：{item.id}")
        package.items[item.id] = item
        if item.is_nav:
            package.nav_item = item
        if item.media_type == "application/x-dtbncx+xml":
            package.ncx_item = item

    spine_el = package.spine_element()
    toc_id = spine_el.get("toc")
    if package.ncx_item is None and toc_id and toc_id in package.items:
        package.ncx_item = package.items[toc_id]

    seen: set[str] = set()
    for index, el in enumerate(spine_el):
        if local_name(el) != "itemref":
            continue
        idref = el.get("idref") or ""
        if not idref:
            continue
        if idref not in package.items:
            raise EpubError(f"spine 引用了不存在的 manifest id：{idref}")
        item = package.items[idref]
        if item.is_encrypted:
            raise EpubError(f"spine 项被加密/混淆，拒绝处理：{item.path}")
        package.spine.append(
            SpineItem(
                index=index,
                idref=idref,
                linear=(el.get("linear") or "yes").lower() != "no",
                item=item,
            )
        )
        seen.add(item.path)

    if not package.spine:
        raise EpubError("spine 为空，无法确定阅读顺序")
    return package


def _check_mimetype(archive: Archive) -> None:
    if not archive.exists("mimetype"):
        return  # 缺 mimetype 的书很常见，按警告处理，由校验器标注
    info = archive.info("mimetype")
    data = archive.read("mimetype").strip()
    if data != MIMETYPE:
        raise EpubError(
            f"mimetype 内容非法：{data[:40]!r}（应为 application/epub+zip）"
        )
    if info.compress_type != zipfile.ZIP_STORED:
        raise EpubError("mimetype 必须不压缩（ZIP_STORED）")


def _check_drm(archive: Archive) -> None:
    for name in ("META-INF/rights.xml", "META-INF/sinf.xml"):
        if archive.exists(name):
            raise EpubError(
                f"检测到 DRM 相关文件 {name}，本技能不做解密，已停止"
            )
    if not archive.exists("META-INF/encryption.xml"):
        return
    try:
        tree = parse_xml(archive.read("META-INF/encryption.xml"), "encryption.xml")
    except EpubError:
        raise EpubError("META-INF/encryption.xml 无法解析，无法确认是否加密")
    encrypted_targets = [
        el.get("URI") or "" for el in tree.getroot().iter() if local_name(el) == "CipherReference"
    ]
    # 关3 压测修正：有 encryption.xml 却声明不出任何加密目标，属结构不可辨认
    # （也可能是别的 DRM 变体用 RetryMethod/EncryptedData 直接包裹）。此时无法
    # 确认是字体混淆还是内容加密，按保守策略停止——宁可拒处理，不产残缺书。
    if not encrypted_targets:
        raise EpubError(
            "META-INF/encryption.xml 未声明任何可识别的加密目标，"
            "无法确认是字体混淆还是内容加密；本技能不做解密，已停止"
        )
    # 字体混淆常见于正常商业书，但内容加密必须拒绝
    if encrypted_targets:
        readable = [t for t in encrypted_targets if not t.lower().endswith((".otf", ".ttf", ".woff", ".woff2"))]
        if readable:
            raise EpubError(
                "EPUB 含加密资源（" + "、".join(readable[:3]) + "），本技能不做解密，已停止"
            )


# ---------------------------------------------------------------------------
# 正文可见性判定
# ---------------------------------------------------------------------------

#: 一律不扫描的容器标签
EXCLUDED_TAGS = {
    "script", "style", "head", "title", "nav", "code", "pre", "kbd", "samp",
    "textarea", "svg", "math", "audio", "video", "ruby", "rt", "rp",
    "sup", "sub",  # 脚注编号/注音等上下标
}

#: 表示隐藏的 class 名（保守列举）
HIDDEN_CLASSES = {"hidden", "sr-only", "visually-hidden", "visuallyhidden", "offscreen"}

#: 原有脚注容器（不当作正文标注，避免脚注里再长脚注）
NOTE_TYPES = {"footnote", "endnote", "rearnote", "footnotes", "endnotes", "rearnotes"}
NOTE_CLASSES = {"footnote", "endnote", "footnotes", "endnotes", "note", "notes", "fn"}


def is_excluded(element) -> str:
    """向上检查祖先链，返回排除原因；可见则返回空串。"""

    current = element
    while current is not None and isinstance(current.tag, str):
        name = local_name(current)
        if name in EXCLUDED_TAGS:
            return f"tag:{name}"
        if current.get("hidden") is not None:
            return "hidden"
        if (current.get("aria-hidden") or "").lower() == "true":
            return "aria-hidden"
        style = (current.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return "css-hidden"
        classes = set((current.get("class") or "").split())
        if classes & HIDDEN_CLASSES:
            return "class-hidden"
        if classes & {"vocab", "vocab-notes", "vocab-glossary"}:
            return "already-vocab"
        epub_type = (current.get("{http://www.idpf.org/2007/ops}type") or "").split()
        if set(epub_type) & NOTE_TYPES:
            return "existing-note"
        if name in ("aside", "div", "section") and classes & NOTE_CLASSES:
            return "existing-note"
        if name == "a":
            # 原书链接内的文字不改写；本技能写入的标注链接自然不会重复扫描
            return "inside-link"
        current = current.getparent()
    return ""


@dataclass
class TextSlot:
    """一个可写入的文本槽：`parent.text` 或 `owner.tail`。"""

    parent: object
    owner: object | None  # None 表示写入 parent.text
    text: str
    excluded: str = ""

    @property
    def writable(self) -> bool:
        return not self.excluded


def iter_text_slots(root) -> Iterator[TextSlot]:
    """按文档顺序枚举文本槽（含被排除的，便于统计跳过原因）。"""

    def walk(element, excluded: str) -> Iterator[TextSlot]:
        reason = excluded or is_excluded(element)
        if element.text:
            yield TextSlot(element, None, element.text, reason)
        for child in element:
            if not isinstance(child.tag, str):
                continue
            child_excluded = excluded or is_excluded(child)
            yield from walk(child, child_excluded)
            if child.tail:
                yield TextSlot(element, child, child.tail, child_excluded)

    yield from walk(root, "")


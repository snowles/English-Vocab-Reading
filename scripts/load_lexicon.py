"""词表 CSV 读取、校验与归一化（阶段 1）。

设计约束（见设计文档 §6）：
    * 按列名映射，不按固定列序号读取；
    * 编码自动识别 UTF-8 / UTF-8 BOM / UTF-16 / GB18030；
    * 分隔符自动探测，也可显式指定；
    * 匹配键 Unicode NFC + casefold，保留原始显示形式；
    * 合并完全重复词头，保留不同等级/词性/释义的独立词条；
    * 空词头、空释义、坏行报告后跳过；
    * 多词短语用最长匹配优先。

本模块不做任何网络访问，也不依赖第三方库。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .lexicon_vault import VaultError, is_vault, unpack_bytes
from .phonetic_l1 import normalize_phonetic

__all__ = [
    "LexiconError",
    "LexiconIssue",
    "Entry",
    "Lexicon",
    "FormInfo",
    "load_form_map",
    "COLUMN_ALIASES",
    "GRADE_SHORT",
    "DEFAULT_LABEL_ORDER",
    "detect_encoding",
    "detect_delimiter",
    "normalize_key",
    "parse_grades",
    "ordered_labels",
    "pick_label",
    "parse_label_order",
    "load_lexicon",
    "load_lexicon_bytes",
    "lexicon_content_sha256",
]

#: 词库标签 -> 正文简称（§18.2）。这是**显示**映射，不是难度排序。
GRADE_SHORT: dict[str, str] = {
    "四级": "4",
    "六级": "6",
    "雅思": "ielts",
    "托福": "toefl",
    "GRE": "GRE",
    "牛津3000": "牛津",
    "AWL": "AWL",
}

#: 默认显示次序（§18.2，可配置，仅是显示偏好）
DEFAULT_LABEL_ORDER: tuple[str, ...] = (
    "四级",
    "六级",
    "雅思",
    "托福",
    "GRE",
    "牛津3000",
    "AWL",
)

#: 规范列名 -> 可接受别名（小写比较，去首尾空格）
COLUMN_ALIASES: dict[str, set[str]] = {
    "word": {"单词", "词头", "word", "headword", "head word", "term", "词汇"},
    "grade": {"单词等级", "等级", "level", "grade", "tag", "级别"},
    "pos": {"词性", "pos", "part of speech", "词类"},
    "gloss": {"中文释义", "释义", "gloss", "definition", "meaning", "中文意思"},
    "example": {"例句", "example", "sentence", "示例"},
    "extra": {"扩展说明", "扩展", "note", "notes", "remark", "备注"},
    # 层 A 字段增强（11-11 P1-b）：新增列一律可选，缺列不影响旧词表。
    "phonetic": {"音标", "读音", "ipa", "phonetic", "pronunciation"},
    "root": {"词根", "root", "lemma"},
    "form_info": {"词形信息", "词形变换", "form info", "forms"},
    "bnc": {"词频bnc", "bnc", "bnc词频"},
    "frq": {"词频当代", "当代词频", "frq", "frequency"},
    "collins": {"柯林斯", "collins", "collins星级"},
    "oxford": {"牛津3000", "oxford", "oxford3000"},
    "en_definition": {"英义", "英文释义", "definition_en", "english definition"},
    "source": {"数据来源", "来源", "source", "provenance"},
}

REQUIRED_COLUMNS = ("word", "gloss")

#: 只做展示与统计的可选列；数值类列做轻量坏值校验，不阻断加载。
NUMERIC_COLUMNS = ("bnc", "frq", "collins", "oxford")

#: 层 B 词形映射表（随包容器）默认位置
FORM_MAP_FILE = "lexicon.forms.vault"

#: 等级标签切分（`四级+雅思` / `四级/雅思` / `四级、雅思`）
_GRADE_SPLIT_RE = re.compile(r"[+＋/／、,，;；|]+")
#: 词头内的并列写法（如 `percent/per cent`、`science fiction/sci-fi`）
_VARIANT_SPLIT_RE = re.compile(r"\s*/\s*")
_WS_RE = re.compile(r"\s+")

_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF8 = b"\xef\xbb\xbf"


class LexiconError(Exception):
    """词表无法读取或结构不可用（缺少必需列、编码无法识别等）。"""


@dataclass
class LexiconIssue:
    """一条词表问题记录，供报告输出。"""

    row: int
    column: str
    reason: str
    sample: str = ""

    def as_dict(self) -> dict:
        return {
            "row": self.row,
            "column": self.column,
            "reason": self.reason,
            "sample": self.sample,
        }


def normalize_key(text: str) -> str:
    """匹配键归一化：NFC + 折叠空白 + casefold。"""

    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text)).strip().casefold()


def parse_grades(raw: str) -> frozenset[str]:
    """把 `四级+雅思` 拆成 {'四级', '雅思'}；空值返回空集合。"""

    if not raw:
        return frozenset()
    parts = (p.strip() for p in _GRADE_SPLIT_RE.split(raw))
    return frozenset(p for p in parts if p)


def parse_label_order(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """把用户配置的显示次序（简称或全名）解析成词库标签序列。

    只接受已知标签；未知项保留原样并由调用方报告，不猜成已有等级。
    """

    if not raw:
        return DEFAULT_LABEL_ORDER
    items = [raw] if isinstance(raw, str) else list(raw)
    by_short = {v.lower(): k for k, v in GRADE_SHORT.items()}
    order: list[str] = []
    for item in items:
        for piece in str(item).replace("，", ",").replace("、", ",").split(","):
            name = piece.strip()
            if not name:
                continue
            canonical = name if name in GRADE_SHORT else by_short.get(name.lower(), "")
            if canonical and canonical not in order:
                order.append(canonical)
    return tuple(order) or DEFAULT_LABEL_ORDER


def ordered_labels(
    labels: Iterable[str], order: Sequence[str] = DEFAULT_LABEL_ORDER
) -> list[str]:
    """按显示次序排列标签；未知标签按原顺序追加在末尾（保留原值）。"""

    present = [label for label in dict.fromkeys(labels) if label]
    ordered = [name for name in order if name in present]
    return ordered + [name for name in present if name not in ordered]


def pick_label(
    labels: Iterable[str],
    *,
    allowed: Iterable[str] | None = None,
    order: Sequence[str] = DEFAULT_LABEL_ORDER,
) -> str:
    """按显示次序选一个正文标签（§18.2）。

    选定等级筛选时，先取该词**实际命中**的所选标签，再按次序决定；
    绝不返回词库没有的标签，也不会把筛选外的标签顶上来。
    """

    present = ordered_labels(labels, order)
    if allowed:
        allowed_set = {a for a in allowed if a}
        present = [name for name in present if name in allowed_set]
    return GRADE_SHORT.get(present[0], present[0]) if present else ""


def detect_encoding(data: bytes) -> str:
    """按 BOM 与实际可解码性判断编码，返回 Python codec 名。"""

    if data.startswith(_BOM_UTF16_LE) or data.startswith(_BOM_UTF16_BE):
        return "utf-16"
    if data.startswith(_BOM_UTF8):
        return "utf-8-sig"
    for codec in ("utf-8", "gb18030"):
        try:
            data.decode(codec)
            return codec
        except UnicodeDecodeError:
            continue
    raise LexiconError("无法识别词表编码（已尝试 UTF-8 / UTF-16 / GB18030）")


def detect_delimiter(sample: str) -> str:
    """探测分隔符；逗号优先作为兜底。"""

    candidates = ",\t;|"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=candidates)
        if dialect.delimiter in candidates:
            return dialect.delimiter
    except csv.Error:
        pass
    counts = {d: sample.count(d) for d in candidates}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


@dataclass(frozen=True)
class FormInfo:
    """层 B：一个词形 → 词根的映射，带**该词形自己**的 L1 音标。

    `Lived` 的音标是 `/livd/`，绝不回填词头 `live` 的 `/laiv/`。
    """

    surface: str
    root: str
    phonetic: str = ""
    form_info: str = ""

    @property
    def key(self) -> str:
        return normalize_key(self.surface)


def _clean_ipa(raw: str) -> tuple[str, str]:
    """把 `/…/` 包裹剥掉后走 L1 归一（幂等）；返回 (成品, 去包裹原值)。"""

    s = (raw or "").strip()
    if len(s) > 2 and s.startswith("/") and s.endswith("/"):
        s = s[1:-1]
    return normalize_phonetic(s), s


def load_form_map(path: str | Path) -> tuple[dict[str, FormInfo], list[LexiconIssue]]:
    """加载层 B 词形表（明文 CSV 或随包容器），做坏值与歧义校验。

    只作为候选来源；`matching.lemma_candidates()` 仍要求词根落在词表内，
    且 §21 的 token 定位与词形硬校验照常执行。
    """

    p = Path(path)
    data = p.read_bytes()
    if is_vault(data):
        data = unpack_bytes(data)
    encoding = detect_encoding(data)
    text = data.decode(encoding).lstrip("\ufeff")
    sample = text[:4096]
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=detect_delimiter(sample))
    if not reader.fieldnames:
        raise LexiconError("词形表为空")
    header = {_WS_RE.sub(" ", unicodedata.normalize("NFC", str(h))).strip().lower(): h
              for h in reader.fieldnames if h}
    aliases = {
        "surface": ("词形", "surface", "form", "word"),
        "root": ("词根", "root", "lemma", "head"),
        "phonetic": ("词形音标", "音标", "ipa", "phonetic"),
        "form_info": ("词形信息", "form info", "forms"),
    }
    cols: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in header:
                cols[canonical] = header[name]
                break
    if "surface" not in cols or "root" not in cols:
        raise LexiconError(f"词形表缺少必需列（词形/词根）：{reader.fieldnames}")

    out: dict[str, FormInfo] = {}
    issues: list[LexiconIssue] = []
    for offset, row in enumerate(reader, start=2):
        surface = (row.get(cols["surface"]) or "").strip()
        root = (row.get(cols["root"]) or "").strip()
        if not surface or not root:
            continue
        if normalize_key(surface) == normalize_key(root):
            continue
        raw_ipa = (row.get(cols.get("phonetic", "")) or "").strip() if "phonetic" in cols else ""
        ipa, bare = _clean_ipa(raw_ipa)
        if bare and not ipa:
            issues.append(LexiconIssue(offset, "phonetic", "脏音标整条省略", surface))
        info = (row.get(cols.get("form_info", "")) or "").strip() if "form_info" in cols else ""
        item = FormInfo(surface=surface, root=root, phonetic=ipa, form_info=info)
        previous = out.get(item.key)
        if previous is None:
            out[item.key] = item
        elif previous.root.casefold() != root.casefold():
            issues.append(LexiconIssue(offset, "surface", "词形多词根歧义，保留首次映射", surface))
    return out, issues


@dataclass
class Entry:
    """一条词表词条。"""

    head: str
    key: str
    gloss: str
    grade_raw: str = ""
    grades: frozenset[str] = frozenset()
    pos: str = ""
    example: str = ""
    extra: str = ""
    row: int = 0
    variants: tuple[str, ...] = ()
    # 层 A 增强字段（全部可选，缺省为空串）
    phonetic: str = ""
    root: str = ""
    form_info: str = ""
    bnc: str = ""
    frq: str = ""
    collins: str = ""
    oxford: str = ""
    en_definition: str = ""
    source: str = ""

    @property
    def entry_id(self) -> str:
        """可追溯 ID：`key|等级|词性`（见 schema.md）。"""

        grade = self.grade_raw or "-"
        pos = self.pos or "-"
        return f"{self.key}|{grade}|{pos}"

    @property
    def grade_label(self) -> str:
        return self.grade_raw or "未分级"

    @property
    def labels(self) -> list[str]:
        """规范化标签（按默认显示次序），未知标签保留原值。"""

        return ordered_labels(self.grades)

    @property
    def labels_short(self) -> str:
        """正文用简称，如 `4` / `toefl`（多标签时取第一个）。"""

        return pick_label(self.grades)

    @property
    def labels_full(self) -> str:
        """章末用完整标签，如 `四级、雅思、牛津3000`。"""

        return "、".join(self.labels)

    @property
    def is_phrase(self) -> bool:
        return " " in self.key

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "head": self.head,
            "key": self.key,
            "gloss": self.gloss,
            "grade": self.grade_raw,
            "pos": self.pos,
        }


@dataclass
class Lexicon:
    """加载完成的词表及其查询索引。

    `entries`/`index` 是**筛选后**视图（用于匹配与标注）；
    `all_entries`/`index_all` 是**完整词库**视图（用于来源判定）。
    两者分开，才能区分「真正未收录」与「被等级筛掉」。
    """

    path: str = ""
    encoding: str = ""
    delimiter: str = ","
    columns: dict[str, str] = field(default_factory=dict)
    entries: list[Entry] = field(default_factory=list)
    index: dict[str, list[Entry]] = field(default_factory=dict)
    all_entries: list[Entry] = field(default_factory=list)
    index_all: dict[str, list[Entry]] = field(default_factory=dict)
    filter_grades: frozenset[str] = frozenset()
    label_order: tuple[str, ...] = DEFAULT_LABEL_ORDER
    issues: list[LexiconIssue] = field(default_factory=list)
    rows_total: int = 0
    rows_used: int = 0
    rows_dropped_by_grade: int = 0
    rows_duplicate: int = 0
    ordered_grade_labels: list[str] = field(default_factory=list)
    # 层 B 词形映射（随包容器自动加载；内存构造的词表默认为空）
    form_map: dict[str, FormInfo] = field(default_factory=dict)
    form_map_path: str = ""
    form_issues: list[LexiconIssue] = field(default_factory=list)

    # -- 查询 ---------------------------------------------------------

    def form_of(self, surface: str) -> FormInfo | None:
        """按被标注词形查层 B 映射；没有返回 None（不猜、不回退词头）。"""

        return self.form_map.get(normalize_key(surface))

    def phonetic_for(self, surface: str, entry: Entry | None = None) -> str:
        """词条块音标：**优先被标注词形自己的音标**。

        词形 != 词头时，只用层 B 里该词形的 L1 音标；层 B 没有就返回空串
        （整行省略），绝不拿词头音标顶替。词形 == 词头时用层 A 的音标。
        """

        key = normalize_key(surface)
        head = normalize_key(entry.head) if entry else ""
        if entry and (head == key or not head):
            return entry.phonetic
        info = self.form_of(surface)
        return info.phonetic if info else ""

    def form_info_for(self, surface: str, entry: Entry | None = None) -> str:
        """词条块的中文可读词形信息，口径与 `phonetic_for()` 一致。

        词形 != 词头时只给层 B 该词形自己的描述；层 B 没有就整行省略，
        不拿词头的词形信息顶替。
        """

        key = normalize_key(surface)
        head = normalize_key(entry.head) if entry else ""
        if entry and (head == key or not head):
            return entry.form_info
        info = self.form_of(surface)
        return info.form_info if info else ""

    def lookup(self, key: str) -> list[Entry]:
        """按归一化键返回**筛选后**词条（同一词头可能有多个词性/等级）。"""

        return self.index.get(key, [])

    def lookup_any(self, key: str) -> list[Entry]:
        """按归一化键返回完整词库里的词条（不受等级筛选影响）。"""

        return self.index_all.get(key, [])

    def entry_by_id(self, entry_id: str) -> Entry | None:
        for entry in self.all_entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def status(self, key: str) -> str:
        """来源判定：`exact`（筛选内）/ `filtered`（被筛掉）/ `absent`。"""

        if key in self.index:
            return "exact"
        if key in self.index_all:
            return "filtered"
        return "absent"

    def __contains__(self, key: str) -> bool:
        return key in self.index

    def __len__(self) -> int:
        return len(self.entries)

    # -- 统计 ---------------------------------------------------------

    @property
    def heads(self) -> int:
        """不同词头（词条主键）数量；并列写法的变体不算独立词头。"""

        return len({e.key for e in self.entries})

    @property
    def phrases(self) -> int:
        return sum(1 for e in self.entries if e.is_phrase)

    @property
    def max_phrase_words(self) -> int:
        best = 1
        for key in self.index:
            if " " in key:
                best = max(best, len(key.split(" ")))
        return best

    @property
    def grades(self) -> list[str]:
        """词表中出现过的全部等级标签（按字符序，便于稳定报告）。"""

        seen: set[str] = set()
        for entry in self.entries:
            seen.update(entry.grades)
        return sorted(seen)

    def entry_count_by_grade(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self.entries:
            for g in entry.grades or {"未分级"}:
                out[g] = out.get(g, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> dict:
        return {
            "path": self.path,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "columns": dict(self.columns),
            "rows_total": self.rows_total,
            "entries": len(self.entries),
            "all_entries": len(self.all_entries),
            "filter_grades": sorted(self.filter_grades),
            "label_order": list(self.label_order),
            "heads": self.heads,
            "phrases": self.phrases,
            "grades": self.grades,
            "entry_count_by_grade": self.entry_count_by_grade(),
            "rows_dropped_by_grade": self.rows_dropped_by_grade,
            "rows_duplicate": self.rows_duplicate,
            "issues": len(self.issues),
            "enhanced": {
                "with_phonetic": sum(1 for e in self.entries if e.phonetic),
                "with_example": sum(1 for e in self.entries if e.example),
                "with_form_info": sum(1 for e in self.entries if e.form_info),
            },
            "form_map": {
                "path": self.form_map_path,
                "entries": len(self.form_map),
                "with_phonetic": sum(1 for f in self.form_map.values() if f.phonetic),
                "issues": len(self.form_issues),
            },
        }


def _map_header(header: Sequence[str]) -> tuple[dict[str, int], list[str]]:
    """把表头映射到规范列名。返回 (列名->序号, 未识别表头)。"""

    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for idx, raw in enumerate(header):
        if raw is None:
            continue
        name = _WS_RE.sub(" ", unicodedata.normalize("NFC", raw)).strip().lower()
        if not name:
            continue
        matched = None
        for canonical, aliases in COLUMN_ALIASES.items():
            if name == canonical or name in aliases:
                matched = canonical
                break
        if matched is None:
            unknown.append(raw)
        elif matched not in mapping:
            mapping[matched] = idx
    return mapping, unknown


def load_lexicon(
    path: str | Path,
    grades: Iterable[str] | None = None,
    *,
    delimiter: str | None = None,
    label_order: str | Iterable[str] | None = None,
    load_forms: bool = True,
) -> Lexicon:
    """从文件加载词表。`grades` 非空时只保留命中这些等级的条目（用于匹配/展示）。

    `load_forms=True` 时自动附带随包层 B 词形表；它是增强数据，缺失或损坏
    不阻断出书，但会记入 `form_issues` 与报告，绝不静默当作「没有词形」。
    """

    p = Path(path)
    if not p.is_file():
        raise LexiconError(f"词表文件不存在：{p}")
    data = p.read_bytes()
    if is_vault(data):
        # 随包词库容器：自动解封，用户无需任何配置；明文 CSV 仍照常支持。
        try:
            data = unpack_bytes(data)
        except VaultError as exc:
            raise LexiconError(f"词库容器无法读取：{exc}") from exc
    lex = load_lexicon_bytes(
        data, grades=grades, delimiter=delimiter, label_order=label_order
    )
    lex.path = str(p)
    if load_forms:
        assets = Path(__file__).resolve().parent.parent / "assets"
        form_path = assets / FORM_MAP_FILE
        if form_path.is_file():
            try:
                lex.form_map, lex.form_issues = load_form_map(form_path)
                lex.form_map_path = str(form_path)
            except (LexiconError, VaultError, OSError, UnicodeError) as exc:
                lex.form_issues.append(
                    LexiconIssue(0, "form_map", f"词形表不可用（{exc}）", str(form_path))
                )
        else:
            lex.form_issues.append(
                LexiconIssue(0, "form_map", "随包词形表缺失，词形增强未启用", str(form_path))
            )
    return lex


def lexicon_content_sha256(path: str | Path) -> str:
    """词库**内容**哈希（不是容器文件哈希），用于 v6 内容版本绑定。

    容器每次打包使用随机 nonce，文件字节必然变化；若按文件哈希绑定，
    同一份词表重新封装就会让已有 scope / ann / review 全部失效。
    解封后按 CSV 原始字节计算，自备明文 CSV 与随包容器因此等价。
    """

    data = Path(path).read_bytes()
    if is_vault(data):
        data = unpack_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_lexicon_bytes(
    data: bytes,
    grades: Iterable[str] | None = None,
    *,
    delimiter: str | None = None,
    label_order: str | Iterable[str] | None = None,
) -> Lexicon:
    """从字节加载词表（便于测试编码分支）。"""

    encoding = detect_encoding(data)
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:  # pragma: no cover - detect_encoding 已保证
        raise LexiconError(f"词表解码失败（{encoding}）：{exc}") from exc
    if text.startswith("\ufeff"):
        text = text[1:]

    sample = text[:8192]
    delim = delimiter or detect_delimiter(sample)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise LexiconError(f"词表 CSV 无法解析：{exc}") from exc
    if not rows:
        raise LexiconError("词表为空（没有任何行）")

    header = rows[0]
    mapping, unknown = _map_header(header)
    missing = [c for c in REQUIRED_COLUMNS if c not in mapping]
    if missing:
        readable = {"word": "单词", "gloss": "中文释义"}
        names = "、".join(readable.get(c, c) for c in missing)
        raise LexiconError(
            f"词表缺少必需列：{names}。识别到的表头：{header}"
        )

    requested = {g.strip() for g in (grades or []) if g and g.strip()}
    lex = Lexicon(
        encoding=encoding,
        delimiter=delim,
        columns={k: header[v] for k, v in mapping.items()},
        filter_grades=frozenset(requested),
        label_order=parse_label_order(label_order),
        ordered_grade_labels=[],
    )
    seen: set[tuple[str, str, str, str]] = set()

    for offset, row in enumerate(rows[1:], start=2):  # 行号按 1 基、含表头
        if not any((cell or "").strip() for cell in row):
            continue  # 空行不计入总数
        lex.rows_total += 1
        if len(row) < len(header):
            row = list(row) + [""] * (len(header) - len(row))
        raw_word = (row[mapping["word"]] or "").strip()
        raw_gloss = (row[mapping["gloss"]] or "").strip()
        if not raw_word:
            lex.issues.append(LexiconIssue(offset, "word", "空词头"))
            continue
        if not raw_gloss:
            lex.issues.append(LexiconIssue(offset, "gloss", "空释义", raw_word))
            continue

        def cell(name: str) -> str:
            return row[mapping[name]].strip() if name in mapping else ""

        grade_raw = cell("grade")
        pos = cell("pos")
        example = cell("example")
        extra = cell("extra")
        # 层 A：音标统一走 L1（幂等，已归一的 `/…/` 先剥包裹再归一）；
        # 脏音标整条省略并记问题，绝不带病值进成品。
        ipa, bare = _clean_ipa(cell("phonetic"))
        if bare and not ipa:
            lex.issues.append(
                LexiconIssue(offset, "phonetic", "脏音标整条省略", raw_word)
            )
        numbers: dict[str, str] = {}
        for name in NUMERIC_COLUMNS:
            value = cell(name)
            if value and not re.fullmatch(r"\d+(?:\.\d+)?", value):
                lex.issues.append(
                    LexiconIssue(offset, name, "非数值内容已忽略", raw_word)
                )
                value = ""
            numbers[name] = value

        grades_set = parse_grades(grade_raw)

        key = normalize_key(raw_word)
        if not key:
            lex.issues.append(LexiconIssue(offset, "word", "词头归一化后为空", raw_word))
            continue

        dedup_key = (key, grade_raw, pos, raw_gloss)
        if dedup_key in seen:
            lex.rows_duplicate += 1
            continue
        seen.add(dedup_key)

        variants = tuple(
            v for v in (normalize_key(x) for x in _VARIANT_SPLIT_RE.split(raw_word)) if v
        ) or (key,)
        # 主键取第一个写法（`percent/per cent` -> `percent`），保证索引可回查
        primary = variants[0]

        entry = Entry(
            head=raw_word,
            key=primary,
            gloss=raw_gloss,
            grade_raw=grade_raw,
            grades=grades_set,
            pos=pos,
            example=example,
            extra=extra,
            row=offset,
            variants=variants,
            phonetic=ipa,
            root=cell("root"),
            form_info=cell("form_info"),
            bnc=numbers["bnc"],
            frq=numbers["frq"],
            collins=numbers["collins"],
            oxford=numbers["oxford"],
            en_definition=cell("en_definition"),
            source=cell("source"),
        )
        # 完整词库视图：来源判定必须能看到被筛掉的词
        lex.all_entries.append(entry)
        for variant in variants:
            lex.index_all.setdefault(variant, []).append(entry)

        if requested and not (grades_set & requested):
            lex.rows_dropped_by_grade += 1
            continue

        lex.entries.append(entry)
        lex.rows_used += 1
        for variant in variants:
            lex.index.setdefault(variant, []).append(entry)

    if lex.entries and unknown:
        lex.issues.append(
            LexiconIssue(0, "header", "未识别的表头列（已忽略）", "、".join(unknown))
        )

    if requested and not lex.entries:
        available = sorted({g for e in lex.all_entries for g in e.grades})
        raise LexiconError(
            f"按等级筛选后没有可用词条（请求：{'、'.join(sorted(requested))}；"
            f"词表可用等级：{'、'.join(available) or '无'}）"
        )

    return lex


def _all_grade_labels(data: bytes, encoding: str, delim: str) -> list[frozenset[str]]:
    """辅助：只为错误提示收集所有等级标签。"""

    text = data.decode(encoding, errors="replace").lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    rows = list(reader)
    if not rows:
        return []
    mapping, _ = _map_header(rows[0])
    if "grade" not in mapping:
        return []
    idx = mapping["grade"]
    out: list[frozenset[str]] = []
    for row in rows[1:]:
        if idx < len(row):
            out.append(parse_grades(row[idx].strip()))
    return out

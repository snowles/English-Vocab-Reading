"""词边界、短语最长优先与词形还原匹配（阶段 1，完全离线）。

设计约束（见设计文档 §7.1）：
    * Unicode-aware token 边界：`art` 不得命中 `article` / `part`；
    * 撇号、连字符、缩写保持整体（`art's`、`well-known`）；
    * 多词短语最长优先，短词不得抢占短语内部词；
    * 词形还原**只有还原结果落在词表内才接受**，不做粗暴前后缀裁剪；
    * 跨节点（被标签打断）的短语不强行拼接，只计数上报。

`SEP` 是内部哨兵字符，用于在拼接多个文本节点后仍能识别"跨节点"边界。
它不会出现在正常书籍文本中。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .load_lexicon import Entry, FormInfo, Lexicon, normalize_key

__all__ = [
    "SEP",
    "TOKEN_RE",
    "Match",
    "MatchResult",
    "find_matches",
    "lemma_candidates",
    "strip_possessive",
]

#: 文本节点拼接哨兵（跨节点边界）
SEP = "\x00"

#: 单词 token：字母开头，允许内部撇号/连字符连接（`art's`、`well-known`、`sci-fi`）
TOKEN_RE = re.compile(r"[^\W\d_]+(?:['\u2019\-][^\W\d_]+)*", re.UNICODE)

#: URL / 裸网址，命中区间内的 token 一律跳过
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE | re.UNICODE)

#: 不规则动词/名词 -> 原形。仅当原形在词表内才生效。
IRREGULAR_FORMS: dict[str, str] = {
    # be / have / do
    "am": "be", "is": "be", "are": "be", "was": "be", "were": "be",
    "been": "be", "being": "be", "has": "have", "had": "have", "having": "have",
    "does": "do", "did": "do", "done": "do", "doing": "do",
    # 常见不规则动词
    "went": "go", "gone": "go", "saw": "see", "seen": "see",
    "took": "take", "taken": "take", "came": "come", "got": "get",
    "gotten": "get", "gave": "give", "given": "give", "made": "make",
    "said": "say", "found": "find", "thought": "think", "told": "tell",
    "became": "become", "left": "leave", "felt": "feel", "brought": "bring",
    "began": "begin", "begun": "begin", "kept": "keep", "held": "hold",
    "wrote": "write", "written": "write", "stood": "stand", "heard": "hear",
    "meant": "mean", "met": "meet", "ran": "run", "paid": "pay",
    "sat": "sit", "spoke": "speak", "spoken": "speak", "led": "lead",
    "grew": "grow", "grown": "grow", "lost": "lose", "fell": "fall",
    "fallen": "fall", "sent": "send", "built": "build", "drew": "draw",
    "drawn": "draw", "broke": "break", "broken": "break", "spent": "spend",
    "rose": "rise", "risen": "rise", "drove": "drive", "driven": "drive",
    "bought": "buy", "wore": "wear", "worn": "wear", "chose": "choose",
    "chosen": "choose", "ate": "eat", "eaten": "eat", "threw": "throw",
    "thrown": "throw", "caught": "catch", "taught": "teach",
    "fought": "fight", "flew": "fly", "flown": "fly", "forgot": "forget",
    "forgotten": "forget", "hid": "hide", "hidden": "hide", "hung": "hang",
    "shot": "shoot", "slept": "sleep", "sold": "sell", "sought": "seek",
    "struck": "strike", "swam": "swim", "swum": "swim", "woke": "wake",
    "woken": "wake", "won": "win", "sang": "sing", "sung": "sing",
    "drank": "drink", "drunk": "drink", "rang": "ring", "rung": "ring",
    "sank": "sink", "sunk": "sink", "shook": "shake", "shaken": "shake",
    "bore": "bear", "borne": "bear", "beat": "beat", "beaten": "beat",
    "bound": "bind", "bit": "bite", "bitten": "bite", "bled": "bleed",
    "blew": "blow", "blown": "blow", "bred": "breed", "cast": "cast",
    "crept": "creep", "dealt": "deal", "dug": "dig", "fed": "feed",
    "fled": "flee", "froze": "freeze", "frozen": "freeze", "laid": "lay",
    "lain": "lie", "lent": "lend", "lit": "light", "mistook": "mistake",
    "mistaken": "mistake", "overcame": "overcome", "rode": "ride",
    "ridden": "ride", "shone": "shine", "shrank": "shrink",
    "shrunk": "shrink", "sped": "speed",
    "spilt": "spill", "spread": "spread", "sprang": "spring",
    "stole": "steal", "stolen": "steal", "stuck": "stick", "stung": "sting",
    "stank": "stink", "swore": "swear", "sworn": "swear",
    "swept": "sweep", "swelled": "swell", "tore": "tear", "torn": "tear",
    "undertook": "undertake", "undertaken": "undertake", "upset": "upset",
    "withdrew": "withdraw", "withdrawn": "withdraw", "wrung": "wring",
    "wound": "wind",
    # 不规则复数
    "children": "child", "men": "man", "women": "woman", "feet": "foot",
    "teeth": "tooth", "mice": "mouse", "geese": "goose", "people": "person",
    "lives": "life", "wives": "wife", "knives": "knife", "leaves": "leaf",
    "halves": "half", "shelves": "shelf", "wolves": "wolf", "thieves": "thief",
    "criteria": "criterion", "phenomena": "phenomenon", "analyses": "analysis",
    "crises": "crisis", "theses": "thesis", "hypotheses": "hypothesis",
    "data": "datum", "media": "medium", "bacteria": "bacterium",
}

_VOWELS = set("aeiou")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_span(span: str) -> str:
    """匹配键：哨兵视作空格 + NFC + 折叠空白 + casefold。"""

    return normalize_key(span.replace(SEP, " "))


def strip_possessive(key: str) -> str | None:
    """`art's` / `art’s` -> `art`；不是所有格返回 None。"""

    for suffix in ("'s", "\u2019s", "'", "\u2019"):
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)]
    return None


def lemma_candidates(
    token: str, form_map: dict[str, FormInfo] | None = None
) -> list[str]:
    """给出可能的原形候选（按可信度排序），由调用方逐个查词表验证。

    `form_map` 是层 B 词形映射（数据驱动、唯一词根），放在最前；
    它**只是候选来源**，后续仍须逐个 `lexicon.lookup()` 验证词根在词表内，
    §21 的 token 定位与词形硬校验不因此放宽。
    """

    t = token.casefold()
    out: list[str] = []

    def add(value: str) -> None:
        if value and value != t and value not in out:
            out.append(value)

    if form_map:
        info = form_map.get(normalize_key(t))
        if info:
            add(info.root.casefold())

    irregular = IRREGULAR_FORMS.get(t)
    if irregular:
        add(irregular)

    if len(t) > 3 and t.endswith("ies"):
        add(t[:-3] + "y")
    if len(t) > 3 and t.endswith("es"):
        add(t[:-2])
        add(t[:-1])
    if len(t) > 2 and t.endswith("s") and not t.endswith(("ss", "us", "is", "as", "os")):
        add(t[:-1])

    if t.endswith("ied") and len(t) > 4:
        add(t[:-3] + "y")
    if t.endswith("ed") and len(t) > 3:
        add(t[:-2])
        add(t[:-1])
        if len(t) > 4 and t[-3] == t[-4] and t[-3] not in _VOWELS:
            add(t[:-3])

    if t.endswith("ing") and len(t) > 5:
        add(t[:-3])
        add(t[:-3] + "e")
        if t.endswith("ying"):
            add(t[:-4] + "ie")
        if len(t) > 5 and t[-4] == t[-5] and t[-4] not in _VOWELS:
            add(t[:-4])

    if t.endswith("est") and len(t) > 5:
        add(t[:-3])
        add(t[:-2])
    if t.endswith("er") and len(t) > 4:
        add(t[:-2])
        add(t[:-1])
        if len(t) > 4 and t[-3] == t[-4] and t[-3] not in _VOWELS:
            add(t[:-3])

    return out


@dataclass
class Match:
    """一次命中。`start`/`end` 是相对传入文本的偏移。"""

    start: int
    end: int
    surface: str
    entries: tuple[Entry, ...]
    kind: str  # word | phrase | lemma

    @property
    def key(self) -> str:
        """命中表面的归一化键（用于统计与去重）。"""

        return normalize_span(self.surface)

    @property
    def entry_key(self) -> str:
        """词条主键：`studies` 与 `study` 共用同一个脚注。"""

        return self.entries[0].key

    @property
    def head(self) -> str:
        return self.entries[0].head


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    tokens: int = 0
    url_tokens: int = 0
    cross_node_phrases: int = 0


def _in_ranges(pos: int, ranges: Sequence[tuple[int, int]]) -> bool:
    for start, end in ranges:
        if start <= pos < end:
            return True
    return False


def find_matches(
    text: str,
    lexicon: Lexicon,
    *,
    reduce_forms: bool = True,
) -> MatchResult:
    """在一段文本（可含 `SEP` 哨兵）中查找词表命中。"""

    result = MatchResult()
    tokens = list(TOKEN_RE.finditer(text))
    result.tokens = len(tokens)
    if not tokens:
        return result

    url_spans = [(m.start(), m.end()) for m in URL_RE.finditer(text)]
    max_words = lexicon.max_phrase_words
    i = 0
    total = len(tokens)
    while i < total:
        tok = tokens[i]
        if _in_ranges(tok.start(), url_spans):
            result.url_tokens += 1
            i += 1
            continue

        found = False
        # ---- 短语：最长优先 -------------------------------------------
        if max_words > 1:
            for n in range(min(max_words, total - i), 1, -1):
                last = tokens[i + n - 1]
                if _in_ranges(last.start(), url_spans):
                    continue
                span = text[tok.start() : last.end()]
                key = normalize_span(span)
                if SEP in span:
                    # 句子被标签打断：不强行拼接，只统计
                    if key in lexicon:
                        result.cross_node_phrases += 1
                    continue
                entries = lexicon.lookup(key)
                if entries:
                    result.matches.append(
                        Match(tok.start(), last.end(), span, tuple(entries), "phrase")
                    )
                    i += n
                    found = True
                    break
        if found:
            continue

        # ---- 单词 / 所有格 / 词形还原 ---------------------------------
        surface = tok.group()
        key = normalize_key(surface)
        entries = lexicon.lookup(key)
        kind = "word"
        if not entries:
            base = strip_possessive(key)
            if base:
                entries = lexicon.lookup(base)
                if entries:
                    kind = "lemma"
        if not entries and reduce_forms:
            for candidate in lemma_candidates(key, lexicon.form_map):
                entries = lexicon.lookup(candidate)
                if entries:
                    kind = "lemma"
                    break
        if entries:
            result.matches.append(
                Match(tok.start(), tok.end(), surface, tuple(entries), kind)
            )
        i += 1

    return result

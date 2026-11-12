"""稳定切词表：段落 → 句子 → token（ID + 原文 + 半开区间 + DOM 槽位）。

§19 的核心：**位置由程序生成，模型只选 token ID**。

    * 模型不再自己数偏移、截拼写；
    * token 是完整词边界单位，`taki` 这种子串根本无法成为 token；
    * token ID 绑定源书哈希 + 段落版本 + 切词配置，换书/换配置自动失效；
    * 跨 DOM 标签的词（`tak<em>ing</em>`）会标记 cross_slot，交由上层明确跳过。

切词规则（显式，可回归）：

    1. 以 Unicode 字母/数字为主，内部允许直/弯撇号（don't / boy’s）；
    2. 连字符是分隔符（well-known → well + known），短语可选择跨连字符的 token 区间；
    3. 缩写整体成词（U.S. / a.m. / e.g.）；
    4. 其余标点、空白都是分隔符，不进入 token；
    5. 索引口径统一为 Python 字符（Unicode 码点），不做 UTF-16 换算。
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field

TOKENIZER_VERSION = "v1"

#: 缩写在前，避免 `U.S.` 被拆成 U / S；单词允许内部撇号
TOKEN_RE = re.compile(
    r"""
      (?P<abbr>(?:[^\W\d_]{1,3}\.){2,})              # U.S. / a.m. / e.g.
    | (?P<word>[^\W_]+(?:[’'][^\W_]+)*)              # taking / boy’s / don't / 84
    """,
    re.UNICODE | re.VERBOSE,
)

#: 句子边界：句末标点 + 可选收尾引号/括号，后面必须是空白或段末
_SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'”’)]*")


@dataclass(frozen=True)
class Token:
    """一个完整词边界单位。"""

    id: str
    index: int
    text: str
    start: int
    end: int
    slot: int = 0
    cross_slot: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "i": self.index,
            "t": self.text,
            "start": self.start,
            "end": self.end,
            "slot": self.slot,
            **({"cross_slot": True} if self.cross_slot else {}),
        }


@dataclass
class Sentence:
    sid: str
    start: int
    end: int
    token_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sid": self.sid,
            "start": self.start,
            "end": self.end,
            "token_ids": list(self.token_ids),
        }


def token_id(
    book_sha256: str,
    doc: str,
    paragraph_index: int,
    start: int,
    end: int,
    text: str,
) -> str:
    raw = f"{TOKENIZER_VERSION}|{book_sha256}|{doc}|{paragraph_index}|{start}|{end}|{text}"
    return "tk-" + hashlib.blake2s(raw.encode("utf-8"), digest_size=5).hexdigest()


def sentence_id(book_sha256: str, doc: str, paragraph_index: int, start: int) -> str:
    raw = f"sn|{TOKENIZER_VERSION}|{book_sha256}|{doc}|{paragraph_index}|{start}"
    return "sn-" + hashlib.blake2s(raw.encode("utf-8"), digest_size=5).hexdigest()


def _slot_of(offsets: list[int], start: int) -> int:
    """二分定位字符所属 DOM 槽位。"""

    return max(0, bisect.bisect_right(offsets, start) - 1)


def tokenize(
    text: str,
    *,
    doc: str,
    paragraph_index: int,
    book_sha256: str,
    offsets: list[int],
) -> list[Token]:
    """把段落文本切成 token；偏移全部指向段落拼接文本（左闭右开）。"""

    tokens: list[Token] = []
    for index, match in enumerate(TOKEN_RE.finditer(text)):
        start, end = match.start(), match.end()
        first = _slot_of(offsets, start)
        last = _slot_of(offsets, max(start, end - 1))
        tokens.append(
            Token(
                id=token_id(book_sha256, doc, paragraph_index, start, end, match.group(0)),
                index=index,
                text=match.group(0),
                start=start,
                end=end,
                slot=first,
                cross_slot=first != last,
            )
        )
    return tokens


def split_sentences(
    text: str,
    tokens: list[Token],
    *,
    doc: str,
    paragraph_index: int,
    book_sha256: str,
) -> list[Sentence]:
    """按句末标点切句，句子带完整 token ID 列表。"""

    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if end < len(text) and not text[end].isspace():
            continue
        bounds.append((start, end))
        start = end
    if start < len(text):
        bounds.append((start, len(text)))

    sentences: list[Sentence] = []
    for begin, end in bounds:
        if not text[begin:end].strip():
            continue
        members = [t.id for t in tokens if begin <= t.start and t.end <= end]
        if not members:
            continue
        sentences.append(
            Sentence(
                sid=sentence_id(book_sha256, doc, paragraph_index, begin),
                start=begin,
                end=end,
                token_ids=members,
            )
        )
    return sentences


def build_tokens(paragraph) -> list[Token]:
    """按段落已有字段生成 token（供 Paragraph 初始化时调用）。"""

    return tokenize(
        paragraph.text,
        doc=paragraph.doc,
        paragraph_index=paragraph.index,
        book_sha256=paragraph.book_sha256,
        offsets=paragraph.offsets,
    )


__all__ = [
    "TOKENIZER_VERSION",
    "TOKEN_RE",
    "Token",
    "Sentence",
    "token_id",
    "sentence_id",
    "tokenize",
    "split_sentences",
    "build_tokens",
]

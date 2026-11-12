"""ECDICT / 现有词库音标的 L1 字符归一（技能本体唯一实现，试点脚本通过 shim 引用）。

读者 11-11 拍板口径：
  * 只做 L1 字符归一，L2 音素现代化不做（`/ˈɒksaid/` 保持原样，不改 `/ˈɒksaɪd/`）。
  * 词现在是什么样就标什么音标：`Lived` 标 `/livd/`，不拿词头 `live` 的音顶替。
  * 没有就整行省略，不强行补、不乱造。

L1 = **字符级等价归一**：只修编码混乱（同一个音被写成西里尔 / 希腊 /
旧式符号），不改任何音素，可逆、可复核。

对含义不明的脏字符（`^`、`\\`、`?`、`@`、CJK 汉字、私用区、全角标点等）
一律整条返回空字符串，绝不猜测其音值。ECDICT 里 `geese` 写作 `^i:s`、
`Aalborg` 写作 `'ɔ:lbɔ:^`，这类词条宁可不标音标也不编造。
"""

from __future__ import annotations

import re
import unicodedata

#: 有明确依据的字符等价归一（编码混乱，音素不变）。
#: 依据：ECDICT 里同一个音位在 bear/bare/chair 写作 є(U+0454)、
#: 在 -arian/Aaronite 写作 ε(U+03B5)、在 high chair 写作 ɛ(U+025B)。
L1_CHAR_MAP: dict[str, str] = {
    "\u04d9": "\u0259",  # ә CYRILLIC SCHWA      -> ə LATIN SCHWA
    "\u0454": "\u025b",  # є CYRILLIC UKRAINIAN IE -> ɛ OPEN E
    "\u03b5": "\u025b",  # ε GREEK EPSILON       -> ɛ OPEN E
    "\u0456": "i",       # і CYRILLIC I          -> i
    "\u0439": "j",       # й CYRILLIC SHORT I    -> j
    "\u03b1": "\u0251",  # α GREEK ALPHA         -> ɑ LATIN ALPHA
    "\u02ca": "\u02c8",  # ˊ MODIFIER ACUTE      -> ˈ PRIMARY STRESS
    "\u00b4": "\u02c8",  # ´ ACUTE ACCENT        -> ˈ
    "\u02b9": "\u02c8",  # ʹ MODIFIER PRIME      -> ˈ
    "\u2019": "\u02c8",  # ’ RIGHT SINGLE QUOTE  -> ˈ
    "\u02c8": "\u02c8",  # ˈ 已是标准，保留
    "\u02cc": "\u02cc",  # ˌ 已是标准，保留
    "'": "\u02c8",       # ASCII 撇号 -> 主重音
    ",": "\u02cc",       # ASCII 逗号 -> 次重音
    ":": "\u02d0",       # ASCII 冒号 -> 长音
}

#: 出现即整条省略的字符：含义不明，无法在不猜测的前提下归一。
#: 依据 ECDICT 770,611 行全量字符扫描。
L1_REJECT_CHARS: frozenset[str] = frozenset(
    "^\\?@=[]()<>{}|/／"
    "0123456789"
    "\uff08\uff09\uff1b\uff0c\u3000\u2011"
    "\u4e00-\u9fff"  # 区间由 L1_REJECT_RE 处理，这里只放单字符
)

#: 允许出现在归一后音标里的字符（白名单，比黑名单更稳）。
#: ASCII 字母 + IPA 扩展段 + 常用 IPA 符号 + 重音/长音/次重音 + 连字符/点。
_L1_ALLOWED_RE = re.compile(
    r"^[a-zA-Z\u00e6\u00f0\u0100-\u024f\u0250-\u02af\u02b0-\u02ff"
    r"\u0300-\u036f\u03b8\u2011\.\-ˈˌːˑ ]+$"
)

#: 明确拒绝：这些字符说明该条音标是脏数据或格式损坏。
_L1_REJECT_RE = re.compile(
    r"[\^\\?@=\[\]\(\)<>\{\}0-9\uff08\uff09\uff1b\uff0c\u3000"
    r"\u4e00-\u9fff\ue000-\uf8ff]"
)

#: 多读音分隔：句点+空白（ECDICT 常见 `livd. 'laivd`）、分号、竖线、全角斜杠。
_SPLIT_RE = re.compile(r"\.\s+|[;|／；]")

#: 可选音括号：IPA 宽式标音惯例，`beə(r)` 表示 r 可读可不读、`(h)w` 表示 h 可省。
#: 直接删掉整个括号组（不编造新音素，也不擅自替读者选美式/英式），这是有依据的
#: 格式清理，不是猜测音值。仅允许括号里是音标字符，避免误吞注解文字。
_OPTIONAL_RE = re.compile(r"\(([a-zA-Z\u0250-\u02af\u00e6\u00f0\u02d0:]{1,3})\)")

#: 音节分隔点（老式 IPA 用 `.` 分音节，它不是音素）。
_SYLLABLE_DOT_RE = re.compile(r"(?<![.\s])\.(?=[a-zA-Z\u0250-\u02af])")


def normalize_phonetic(raw: str) -> str:
    """返回 `/…/` 包裹的 L1 音标；无音标或脏数据则返回空字符串。

    只做字符归一，不改音素，不猜测、不回退到词头 / 词根音标。
    """
    return diagnose(raw)["normalized"]


def diagnose(raw: str) -> dict:
    """L1 归一并给出可复核的诊断信息（供执行报告统计，不进页面）。"""
    src = unicodedata.normalize("NFC", (raw or "").strip())
    out: dict = {"raw": raw or "", "normalized": "", "status": "", "mapped": []}
    if not src:
        out["status"] = "empty"
        return out

    # CSV 转义残留的首尾 ASCII 双引号：纯格式清理，不涉及音值
    src = src.strip('"').strip()
    parts = _SPLIT_RE.split(src, maxsplit=1)
    multi = len(parts) > 1
    first = parts[0].strip()
    if multi:
        out["status_candidate"] = "multi_pronunciation_first_only"

    # 可选音括号 / 音节分隔点：先清理再归一，避免被脏字符闸门误杀。
    # 注意音节分隔点不算多读音（`'liθәu.sfiә` 是一个读音），不能用它改 status。
    first = _OPTIONAL_RE.sub("", first)
    first = _SYLLABLE_DOT_RE.sub("", first)

    mapped: list[str] = []
    buf: list[str] = []
    for c in first:
        if c in L1_CHAR_MAP and L1_CHAR_MAP[c] != c:
            mapped.append(f"U+{ord(c):04X}->U+{ord(L1_CHAR_MAP[c]):04X}")
        buf.append(L1_CHAR_MAP.get(c, c))
    s = "".join(buf)
    out["mapped"] = sorted(set(mapped))

    # 含义不明的字符：整条省略，不猜
    if _L1_REJECT_RE.search(s):
        out["status"] = "rejected_dirty"
        return out

    # 去掉尾部的孤立重音 / 空白，中间空白压掉（复合词音标常含空格）
    s = re.sub(r"[\s\u2018\u2019ˈˌ]+$", "", s.strip())
    s = re.sub(r"\s+", "", s)
    if not s:
        out["status"] = "empty_after_norm"
        return out
    if not _L1_ALLOWED_RE.match(s):
        out["status"] = "rejected_non_ipa"
        return out
    if not re.search(r"[a-zA-Z\u0250-\u02af\u00e6\u00f0]", s):
        out["status"] = "rejected_no_phoneme"
        return out

    out["normalized"] = "/" + s + "/"
    out["status"] = "ok_multi_first" if multi else "ok"
    return out


if __name__ == "__main__":
    cases = [
        ("livd. 'laivd", "Lived 取第一个读音"),
        ("'bєә", "bear 西里尔 є + ә"),
        ("'εəriən", "-arian 希腊 ε"),
        ("ˊæbәtsi", "abbotcy 旧式重音 ˊ"),
        ("'hai tʃɛə", "high chair 标准 ɛ"),
        ("'lɑ:st", "last 老式长音"),
        ("^i:s", "geese 脏字符 ^"),
        ("әbajt't\\\\:n", "about-turn 脏字符 \\"),
        ("'ɒksaid", "oxide L2 不做，保持 aid"),
        ("", "空"),
    ]
    for raw, note in cases:
        print(f"{raw!r:<26} -> {normalize_phonetic(raw)!r:<20} {note}")

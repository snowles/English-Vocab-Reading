"""阅读提示偏好（设计文档第 20 节 A 批）。

三件事，只影响「显示什么」，不改标注内容、词库和审查结论：

1. 三档提示量 ``light / standard / detailed``，用可解释的 ``core/useful/transparent``
   三类，不做打分模型；
2. 本地熟词名单 ``mastered / focus``，复用词形还原匹配，组合与新义不被误删；
3. 记录每条被主动筛除的原因，和「缺审 / 无效」这类质量问题分开。

同章同义去重不在这里做：渲染器已有 ``_dedup_key`` + ``state.seen``，本模块只负责
「哪些条目值得提示」，去重仍由 ``--density`` 控制。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .matching import lemma_candidates, normalize_span
from .units import Annotation

LEVELS = {"light": "轻提示", "standard": "标准", "detailed": "详细"}
DEFAULT_LEVEL = "standard"

#: 可解释的提示必要性类别。 §21.2：不再用「说明是否为空」推断难度。
#: 真正的判断来自识别字段 ``reading_need``，这里只把它映射成三分类；
#: 没有可靠判断时（unknown）一律保守保留。
IMPORTANCE = {
    "core": "必须提示：required，或被标记为新义/特殊用法保护",
    "useful": "值得提示：useful，或缺少可靠判断（unknown）但属于单字",
    "transparent": "明确可省：optional（识别给出、审查核对的判断）",
}

STATUS_ALIASES = {
    "mastered": "mastered",
    "已掌握": "mastered",
    "已会": "mastered",
    "known": "mastered",
    "focus": "focus",
    "重点学习": "focus",
    "重点": "focus",
    "learning": "focus",
}

#: 文档 20.5 建议的导出字段，顺序固定
EXPORT_FIELDS = [
    "surface",
    "lemma",
    "gloss",
    "sentence",
    "book",
    "chapter",
    "locator",
    "labels",
]


@dataclass
class WordList:
    mastered: set[str] = field(default_factory=set)
    focus: set[str] = field(default_factory=set)
    path: str = ""
    issues: list[str] = field(default_factory=list)

    def status(self, annotation: Annotation) -> str:
        """focus 优先于 mastered；返回 ``focus`` / ``mastered`` / 空串。"""

        keys = _form_keys(annotation)
        if keys & self.focus:
            return "focus"
        if keys & self.mastered:
            return "mastered"
        return ""

    def summary(self) -> dict:
        return {
            "path": self.path,
            "mastered": len(self.mastered),
            "focus": len(self.focus),
            "issues": self.issues[:10],
        }


def _form_keys(annotation: Annotation) -> set[str]:
    """标注可能命中的名单键：原形、原文词形及其还原候选。"""

    keys: set[str] = set()
    for raw in (annotation.lemma, annotation.surface):
        if not raw:
            continue
        norm = normalize_span(raw)
        if not norm:
            continue
        keys.add(norm)
        for candidate in lemma_candidates(norm):
            candidate = normalize_span(candidate)
            if candidate:
                keys.add(candidate)
    return keys


def _add_word(target: set[str], word: str) -> None:
    norm = normalize_span(word)
    if norm:
        target.add(norm)


def load_word_list(path: str | Path | None) -> WordList:
    """读取本地熟词名单。支持 CSV（word,status）与 JSON（对象或列表）。"""

    result = WordList()
    if not path:
        return result
    file = Path(path)
    result.path = str(file)
    if not file.exists():
        result.issues.append(f"名单文件不存在，已忽略：{file}")
        return result
    raw = file.read_text(encoding="utf-8-sig")
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        _load_json(raw, result)
    else:
        _load_csv(raw, result)
    return result


def _load_json(raw: str, result: WordList) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        result.issues.append(f"JSON 无法解析：{exc}")
        return
    if isinstance(payload, dict):
        for key, bucket in (("mastered", result.mastered), ("focus", result.focus)):
            for word in payload.get(key) or []:
                if isinstance(word, str):
                    _add_word(bucket, word)
        for item in payload.get("words") or []:
            if not isinstance(item, dict):
                continue
            status = STATUS_ALIASES.get(str(item.get("status") or "").strip())
            word = str(item.get("word") or "").strip()
            if status and word:
                _add_word(
                    result.mastered if status == "mastered" else result.focus, word
                )
        return
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            status = STATUS_ALIASES.get(str(item.get("status") or "").strip())
            word = str(item.get("word") or "").strip()
            if status and word:
                _add_word(
                    result.mastered if status == "mastered" else result.focus, word
                )
        return
    result.issues.append("JSON 顶层既不是对象也不是列表")


def _load_csv(raw: str, result: WordList) -> None:
    reader = csv.reader(io.StringIO(raw))
    header_seen = False
    for row in reader:
        if not row:
            continue
        cells = [cell.strip() for cell in row]
        if not header_seen:
            header_seen = True
            lowered = {cell.lower() for cell in cells}
            if "word" in lowered or "单词" in cells or "status" in lowered or "状态" in cells:
                continue  # 跳过表头
        word = cells[0] if cells else ""
        status_raw = cells[1] if len(cells) > 1 else "mastered"
        status = STATUS_ALIASES.get(status_raw.strip().lower()) or STATUS_ALIASES.get(
            status_raw.strip()
        )
        if not word:
            continue
        if not status:
            result.issues.append(f"未知状态「{status_raw}」，按已掌握处理：{word}")
            status = "mastered"
        _add_word(result.mastered if status == "mastered" else result.focus, word)


def export_word_list(
    path: str | Path, mastered: Iterable[str], focus: Iterable[str]
) -> Path:
    """导出 CSV：``word,status``。HTML 的临时标记可存成同格式再导入。"""

    file = Path(path)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["word", "status"])
    for word in sorted({w for w in mastered if w}):
        writer.writerow([word, "mastered"])
    for word in sorted({w for w in focus if w}):
        writer.writerow([word, "focus"])
    file.write_text(buffer.getvalue(), encoding="utf-8")
    return file


def importance(annotation: Annotation) -> str:
    """把识别给出的 ``reading_need`` 映射成三分类；不看词库等级。

    - ``required`` 或 ``sense_protected`` → ``core``；
    - ``optional`` → ``transparent``（只有这种才允许轻提示/名单省略）；
    - ``useful`` → ``useful``；
    - ``unknown`` 或缺字段 → 沿用结构性保护，单字按 ``useful`` 保守保留。

    「写没写读者说明」不再单独决定必要性—— §21.2 明确这是错误的推断。
    """

    if annotation.sense_protected:
        return "core"
    need = (annotation.reading_need or "").strip().lower()
    if need == "required":
        return "core"
    if need == "optional":
        return "transparent"
    if need == "useful":
        return "useful"
    # unknown：保守保留。组合/专名/未收录/带类别仍按结构判难点
    if annotation.unit_type in ("phrase", "proper_name"):
        return "core"
    if annotation.components:
        return "core"
    if annotation.source_status in ("out_of_lexicon", "form_match_failed"):
        return "core"
    if annotation.usage_category:
        return "core"
    if annotation.label_scope != "whole":
        return "core"
    return "useful"


@dataclass
class ReadingDecision:
    kept: list[Annotation] = field(default_factory=list)
    filtered: list[dict] = field(default_factory=list)
    importance: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    focus_hits: int = 0
    level: str = DEFAULT_LEVEL

    def report(self, word_list: WordList | None, density: str) -> dict:
        return {
            "level": self.level,
            "level_label": LEVELS.get(self.level, self.level),
            "density": density,
            "importance": self.importance,
            "filtered_counts": self.counts,
            "filtered": self.filtered[:80],
            "filtered_total": len(self.filtered),
            "focus_hits": self.focus_hits,
            "word_list": (word_list or WordList()).summary(),
        }


def decide(
    annotations: Iterable[Annotation],
    *,
    level: str = DEFAULT_LEVEL,
    word_list: WordList | None = None,
) -> ReadingDecision:
    """按档位与名单决定显示哪些条目，并记录每条主动筛除原因。

    - ``focus``（重点学习）优先：即使被标为透明词也保留，且不受 ``mastered`` 影响；
    - ``mastered`` 只剔除「透明常见词」，组合、未收录、语境义一律保留；
    - ``light`` 剔除透明常见词；``standard`` / ``detailed`` 不按类别剔除。
    """

    if level not in LEVELS:
        raise ValueError(f"未知提示档位：{level}")
    words = word_list or WordList()
    decision = ReadingDecision(level=level)
    importance_count = {key: 0 for key in IMPORTANCE}

    for annotation in annotations:
        if annotation.action != "annotate":
            continue
        rank = importance(annotation)
        status = words.status(annotation)
        if status == "focus":
            decision.focus_hits += 1
            if rank == "transparent":
                rank = "useful"
        importance_count[rank] = importance_count.get(rank, 0) + 1
        # 供渲染器写入 data-importance，HTML 端无需重算规则
        annotation.reading_rank = rank

        if status == "focus":
            decision.kept.append(annotation)
            continue
        # 只有「明确可省」的条目才允许被名单或轻提示省略；
        # unknown 与 useful 都保留（§21.2 要求未知保守处理）。
        omittable = rank == "transparent" and (
            (annotation.reading_need or "").strip().lower() == "optional"
        )
        if status == "mastered" and omittable:
            decision.filtered.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "surface": annotation.surface,
                    "reason": "mastered",
                    "importance": rank,
                }
            )
            continue
        if level == "light" and omittable:
            decision.filtered.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "surface": annotation.surface,
                    "reason": "level",
                    "importance": rank,
                }
            )
            continue
        decision.kept.append(annotation)

    counts: dict[str, int] = {}
    for item in decision.filtered:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    decision.importance = importance_count
    decision.counts = counts
    return decision


def effective_density(level: str, density: str) -> tuple[str, bool]:
    """§21.4：档位与密度彼此独立，档位不再改写密度。

    ``reading-level`` 只控制提示内容范围，``--density`` 只控制重复频率；
    需要同章同义反复提示时由用户显式传 ``--density all``。
    保留函数是为了让调用点显式表达「没有隐式改写」。
    """

    return density, False

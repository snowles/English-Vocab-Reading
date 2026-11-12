"""阶段 2：harness-in-the-loop 语境消歧的协议层（本模块不含任何模型调用）。

设计要点：
    * Skill 只负责**产出请求**和**校验结果**；判断由宿主智能体（harness）完成——
      Codex / WorkBuddy / 豆包工作版自己读请求、自己给结论，再写回 decisions；
    * 不引入 SDK、不发网络请求、不猜默认模型路径；
    * 请求只带**最小上下文**（命中词所在句子）与候选释义，不带整章、整书、文件路径；
    * 决策必须可追溯：`entry_id` 来自词表，`short_gloss_zh` 必须能在词表释义里找到，
      否则判为非法并回退。

两类文件：

    requests/part-0001.json    由 Skill 生成，给 harness 读
    decisions/part-0001.json   由 harness 写，给 Skill 校验并应用

ID 是**确定性哈希**：`c-{blake2s(chapter|entry_key|occurrence)[:10]}`，
这样生成请求和应用结果两次运行能对上，不需要保留中间状态。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "SCHEMA_REQUEST",
    "SCHEMA_DECISION",
    "SCHEMA_REQUEST_V2",
    "SCHEMA_DECISION_V2",
    "PROTOCOL_V2",
    "DEFAULT_MIN_CONFIDENCE",
    "SENSE_SPLIT",
    "Candidate",
    "SenseDecision",
    "DecisionSet",
    "candidate_id",
    "candidate_id_v2",
    "sense_variants",
    "is_faithful_gloss",
    "load_decisions",
    "request_payload",
]

SCHEMA_REQUEST = "vocab-disambiguation-request/v1"
SCHEMA_DECISION = "vocab-disambiguation-decisions/v1"
#: 阶段 2（§16）协议：全文语境审查，带动作与义项标识
SCHEMA_REQUEST_V2 = "vocab-disambiguation-request/v2"
SCHEMA_DECISION_V2 = "vocab-disambiguation-decisions/v2"
PROTOCOL_V2 = "v2"
DEFAULT_MIN_CONFIDENCE = 0.55

#: 中文释义里的义项分隔符（不用 `/`，避免把 `n./v.` 之类误切）
SENSE_SPLIT = re.compile(r"[；;。|｜]+")

INSTRUCTION = (
    "你是英语词汇标注助手。每个 candidate 给出一句原文上下文，以及该词在词表里"
    "可能的释义。请判断上下文实际使用的是哪个释义，并按 response_schema 输出 JSON。"
    "规则：short_gloss_zh 必须从 options 的 gloss 里选择或忠实缩短，不得添加词表之外的解释；"
    "无法判断时把 is_target_sense 设为 false 或 confidence 低于阈值，由 Skill 保守跳过。"
)

RESPONSE_SCHEMA = {
    "decisions": [
        {
            "id": "c-3f2a91b0c4",
            "is_target_sense": True,
            "selected_entry_ids": ["study|四级+雅思|n./v."],
            "short_gloss_zh": "学习；研究",
            "confidence": 0.9,
            "reason_code": "context_match",
        }
    ]
}


def candidate_id(chapter: str, entry_key: str, occurrence: int) -> str:
    """确定性的候选 ID：同一次匹配在任何一次运行里都得到同一个 ID。"""

    raw = f"{chapter}|{entry_key}|{occurrence}".encode("utf-8")
    return "c-" + hashlib.blake2s(raw, digest_size=5).hexdigest()


def candidate_id_v2(doc: str, chapter: int, entry_key: str, occurrence: int) -> str:
    """v2 候选 ID：doc + 逻辑章节 + 词条 + 章内出现序号，确定性。"""

    raw = f"v2|{doc}|{chapter}|{entry_key}|{occurrence}".encode("utf-8")
    return "c-" + hashlib.blake2s(raw, digest_size=5).hexdigest()


def sense_variants(gloss: str) -> list[str]:
    """把 `工作；起作用` 拆成 ['工作', '起作用']；单义项返回单元素。"""

    parts = [p.strip() for p in SENSE_SPLIT.split(gloss)]
    return [p for p in parts if p] or [gloss.strip()]


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def is_faithful_gloss(short: str, glosses: Iterable[str]) -> bool:
    """`short` 必须是某个释义（或其义项）的子串，即"选择或忠实缩短"。"""

    candidate = _squeeze(short)
    if not candidate:
        return False
    for gloss in glosses:
        for variant in sense_variants(gloss):
            if candidate in _squeeze(variant):
                return True
        if candidate in _squeeze(gloss):
            return True
    return False


@dataclass
class Candidate:
    """一条待消歧的命中。"""

    id: str
    chapter: str
    chapter_index: int
    surface: str
    head: str
    entry_key: str
    occurrence: int
    context: str
    options: list[dict] = field(default_factory=list)
    reason: str = "multi_entry"  # multi_entry | multi_sense

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "chapter": self.chapter,
            "chapter_index": self.chapter_index,
            "surface": self.surface,
            "head": self.head,
            "occurrence": self.occurrence,
            "reason": self.reason,
            "context": self.context,
            "options": list(self.options),
        }


@dataclass
class SenseDecision:
    id: str
    is_target_sense: bool = True
    selected_entry_ids: list[str] = field(default_factory=list)
    short_gloss_zh: str = ""
    confidence: float = 0.0
    reason_code: str = ""
    # --- v2（§16）字段 ---
    action: str = "annotate"        # annotate | skip | uncertain
    sense_id: str = ""              # 稳定的义项标识（用于同义复用编号）
    context_pos: str = ""           # 该词在本句的词性
    usage_note: str = ""            # 用法说明，如 "fished 为过去式"
    protocol: str = ""              # v1 / v2

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "is_target_sense": self.is_target_sense,
            "selected_entry_ids": list(self.selected_entry_ids),
            "sense_id": self.sense_id,
            "short_gloss_zh": self.short_gloss_zh,
            "context_pos": self.context_pos,
            "usage_note": self.usage_note,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }


@dataclass
class DecisionSet:
    by_id: dict[str, SenseDecision] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    book_sha256: str = ""
    lexicon_sha256: str = ""
    config_hash: str = ""

    def __len__(self) -> int:
        return len(self.by_id)

    def get(self, key: str) -> SenseDecision | None:
        return self.by_id.get(key)

    def summary(self) -> dict:
        return {
            "files": len(self.files),
            "decisions": len(self.by_id),
            "issues": len(self.issues),
            "issues_sample": self.issues[:5],
        }


def _iter_decision_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(
            p
            for p in path.rglob("*.json")
            if p.name != "index.json" and not p.name.startswith(".")
        )
    if path.is_file():
        return [path]
    raise FileNotFoundError(f"决策文件或目录不存在：{path}")


def load_decisions(path: str | Path) -> DecisionSet:
    """读取 harness 写回的 decisions（单文件或目录），做结构校验。"""

    target = Path(path)
    result = DecisionSet()
    for file in _iter_decision_files(target):
        result.files.append(str(file))
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            result.issues.append({"file": str(file), "reason": f"JSON 无法解析：{exc}"})
            continue
        if not isinstance(payload, dict):
            result.issues.append({"file": str(file), "reason": "顶层必须是对象"})
            continue
        schema = payload.get("schema")
        protocol = ""
        if schema == SCHEMA_DECISION_V2:
            protocol = PROTOCOL_V2
        elif schema == SCHEMA_DECISION:
            protocol = "v1"
        elif schema:
            result.issues.append(
                {"file": str(file), "reason": f"未知协议版本，拒绝应用：{schema}"}
            )
            continue
        if payload.get("book_sha256") and not result.book_sha256:
            result.book_sha256 = str(payload["book_sha256"])
        if payload.get("lexicon_sha256") and not result.lexicon_sha256:
            result.lexicon_sha256 = str(payload["lexicon_sha256"])
        scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else {}
        if scan.get("hash") and not result.config_hash:
            result.config_hash = str(scan["hash"])
        items = payload.get("decisions")
        if not isinstance(items, list):
            result.issues.append({"file": str(file), "reason": "缺少 decisions 数组"})
            continue
        for item in items:
            if not isinstance(item, dict):
                result.issues.append({"file": str(file), "reason": "decision 不是对象"})
                continue
            decision = _parse_one(item, str(file), result.issues)
            if decision is None:
                continue
            decision.protocol = protocol
            if decision.id in result.by_id:
                result.issues.append(
                    {"file": str(file), "reason": f"重复的 id：{decision.id}"}
                )
                continue
            result.by_id[decision.id] = decision
    return result


def _parse_one(item: dict, file: str, issues: list[dict]) -> SenseDecision | None:
    identifier = str(item.get("id") or "").strip()
    if not identifier:
        issues.append({"file": file, "reason": "decision 缺少 id"})
        return None
    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        issues.append({"file": file, "reason": f"{identifier}: confidence 不是数字"})
        confidence = 0.0
    selected = item.get("selected_entry_ids") or []
    if not isinstance(selected, list):
        issues.append({"file": file, "reason": f"{identifier}: selected_entry_ids 不是数组"})
        selected = []
    action = str(item.get("action") or "annotate").strip()
    if action not in ("annotate", "skip", "uncertain"):
        issues.append({"file": file, "reason": f"{identifier}: 非法 action={action}"})
        action = "uncertain"
    return SenseDecision(
        id=identifier,
        is_target_sense=bool(item.get("is_target_sense", True)),
        selected_entry_ids=[str(s) for s in selected],
        short_gloss_zh=str(item.get("short_gloss_zh") or "").strip(),
        confidence=confidence,
        reason_code=str(item.get("reason_code") or ""),
        action=action,
        sense_id=str(item.get("sense_id") or "").strip(),
        context_pos=str(item.get("context_pos") or "").strip(),
        usage_note=str(item.get("usage_note") or "").strip(),
    )


def request_payload(
    candidates: list[Candidate],
    *,
    book_title: str,
    book_sha256: str,
    lexicon_sha256: str,
    part: int,
    parts: int,
    min_confidence: float,
    max_context_chars: int,
) -> dict:
    """构造一个请求分片（给 harness 读的完整自描述文件）。"""

    return {
        "schema": SCHEMA_REQUEST,
        "part": part,
        "parts": parts,
        "instruction": INSTRUCTION,
        "response_schema": RESPONSE_SCHEMA,
        "book": {"title": book_title, "sha256": book_sha256},
        "lexicon": {"sha256": lexicon_sha256},
        "min_confidence": min_confidence,
        "max_context_chars": max_context_chars,
        "candidates": [c.as_dict() for c in candidates],
    }

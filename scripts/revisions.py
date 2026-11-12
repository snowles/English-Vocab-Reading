"""v6 内容版本与审查绑定（审计整改 P1）。

分工：
    * ``annotation_id`` 只回答「这是正文哪一处」，由 pid + token_ids 决定；
    * ``annotation_revision_hash`` 回答「审查针对的是哪一份内容」，由
      书籍/词库/配置/tokenizer/协议/提示版本/上下文包 + 规范化内容共同决定。

任何会影响读者输出或采用决定的字段变化（短义、用法、成员、词条、类别、
动作、义项键、依据）都会产生新哈希；时间戳、文件路径、展示编号、审查意见、
审计备注不参与哈希。

规范化规则固定：``sort_keys=True``、UTF-8、``separators=(",", ":")``、
数组顺序保留（成员和 token 的顺序有语义）。不做去标点、截断或模糊归一化。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL = "v6"
REVISION_VERSION = "rev-v1"

#: 提示词版本：修改指令文本时必须同步提升，否则旧审查会被误认为仍有效
IDENTIFY_PROMPT_VERSION = "id-v6.1"
REVIEW_PROMPT_VERSION = "rv-v6.1"

SCHEMA_ANN_REQUEST = "vocab-annotation-request/v6"
SCHEMA_DECISIONS = "vocab-annotation-decisions/v6"
SCHEMA_REVIEW_REQUEST = "vocab-annotation-review-request/v6"
SCHEMA_REVIEW = "vocab-annotation-review/v6"
SCHEMA_SCOPE = "vocab-annotation-scope/v6"

BINDING_KEYS = (
    "protocol",
    "tokenizer",
    "book_sha256",
    "lexicon_sha256",
    "config_hash",
    "identify_prompt",
    "review_prompt",
    "context_pack_hash",
)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return "th-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def window_hash(window_id: str, paragraphs: list[dict], hints: list[dict]) -> str:
    """单个窗口的上下文哈希：段落文本、token、词库提示都算输入。"""

    return "wh-" + digest(
        {
            "revision": REVISION_VERSION,
            "window_id": window_id,
            "paragraphs": paragraphs,
            "program_hints": hints,
        }
    )[:32]


def context_pack_hash(window_hashes: list[str]) -> str:
    return "cp-" + digest({"revision": REVISION_VERSION, "windows": window_hashes})[:32]


def task_id(scope: dict) -> str:
    payload = {k: v for k, v in scope.items() if k not in ("task_id", "created_at", "next_step")}
    return "task-" + digest(payload)[:24]


def binding_payload(binding: dict) -> dict:
    return {key: str(binding.get(key) or "") for key in BINDING_KEYS}


def annotation_content(fingerprint: dict) -> dict:
    """只有会影响读者输出或采用决定的字段参与内容哈希。

    调用方负责传入已规范化的字典（见 units.annotation_fingerprint）。
    这里再做一次白名单裁剪，避免以后新增展示字段时意外改变哈希语义。
    """

    components = []
    for component in fingerprint.get("components") or []:
        components.append(
            {
                "token_ids": list(component.get("token_ids") or []),
                "entry_ids": list(component.get("entry_ids") or []),
            }
        )
    return {
        "annotation_id": str(fingerprint.get("annotation_id") or ""),
        "pid": str(fingerprint.get("pid") or ""),
        "token_ids": list(fingerprint.get("token_ids") or []),
        "unit_type": str(fingerprint.get("unit_type") or ""),
        "source_type": str(fingerprint.get("source_type") or ""),
        "whole_entry_ids": list(fingerprint.get("whole_entry_ids") or []),
        "components": components,
        "short_gloss_zh": str(fingerprint.get("short_gloss_zh") or ""),
        "reader_usage_note": str(fingerprint.get("reader_usage_note") or ""),
        "usage_category": str(fingerprint.get("usage_category") or ""),
        "usage_domain": str(fingerprint.get("usage_domain") or ""),
        "sense_key": str(fingerprint.get("sense_key") or ""),
        "action": str(fingerprint.get("action") or ""),
        "reason": str(fingerprint.get("reason") or ""),
        "skip_reason": str(fingerprint.get("skip_reason") or ""),
        "evidence": str(fingerprint.get("evidence") or ""),
    }


def revision_hash(fingerprint: dict, binding: dict) -> str:
    payload = {
        "revision": REVISION_VERSION,
        "binding": binding_payload(binding),
        "content": annotation_content(fingerprint),
    }
    return "rh-" + digest(payload)[:40]


def review_verdict_hash(verdict: dict) -> str:
    """审查意见自身的指纹，用于发现同一版本上的矛盾判定。"""

    return "vh-" + digest(
        {
            "annotation_id": str(verdict.get("annotation_id") or ""),
            "reviewed_revision_hash": str(verdict.get("reviewed_revision_hash") or ""),
            "review_status": str(verdict.get("review_status") or ""),
            "issues": list(verdict.get("issues") or []),
            "revised": verdict.get("revised") or {},
        }
    )[:24]

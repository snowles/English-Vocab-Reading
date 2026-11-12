"""本地纠错反馈（设计文档 §20.7）。

网页把「意思不对 / 组合范围不对 / 解释太多」记成待复核条目并导出 JSON；
这里负责读取与校验，把反馈转成「需要重审的 annotation 列表」。

反馈**不是**正确答案：调用方只应据此让旧审查失效、生成复核清单，
真正的修改仍要走「改识别 → 独立审查 → 硬校验」原链路，不得直接写回正文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "vocab-reader-feedback/v1"

#: 与网页三个按钮一致
KINDS = {
    "sense": "意思不对",
    "span": "组合范围不对",
    "too_much": "解释太多",
}


@dataclass
class FeedbackSet:
    items: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def annotation_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            ident = item.get("annotation_id") or ""
            if ident and ident not in seen:
                seen.append(ident)
        return seen

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.items:
            kind = item.get("kind") or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
        return {
            "files": len(self.files),
            "items": len(self.items),
            "kinds": counts,
            "issues": self.issues[:10],
        }


def _load_one(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        items = payload.get("items")
        if items is None:
            items = [payload] if payload.get("annotation_id") else []
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def load_feedback(path: str | Path | None) -> FeedbackSet:
    """读取单文件或目录下的 ``*.json`` 反馈。"""

    result = FeedbackSet()
    if not path:
        return result
    target = Path(path)
    if not target.exists():
        result.issues.append(f"反馈文件不存在：{target}")
        return result
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    for file in files:
        result.files.append(str(file))
        try:
            raw_items = _load_one(file)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            result.issues.append(f"{file.name} 无法解析：{exc}")
            continue
        for item in raw_items:
            ident = str(item.get("annotation_id") or "").strip()
            if not ident:
                result.issues.append(f"{file.name}: 反馈缺少 annotation_id，已忽略")
                continue
            kind = str(item.get("kind") or "").strip()
            if kind not in KINDS:
                result.issues.append(f"{file.name}: {ident} 的反馈类型「{kind}」未知，按 sense 处理")
                kind = "sense"
            result.items.append(
                {
                    "annotation_id": ident,
                    "surface": str(item.get("surface") or ""),
                    "lemma": str(item.get("lemma") or ""),
                    "chapter": str(item.get("chapter") or ""),
                    "gloss": str(item.get("gloss") or ""),
                    "sentence": str(item.get("sentence") or ""),
                    "book": str(item.get("book") or ""),
                    "version": str(item.get("version") or ""),
                    "kind": kind,
                    "kind_label": KINDS[kind],
                    "note": str(item.get("note") or ""),
                }
            )
    return result


def write_review_list(feedback: FeedbackSet, path: str | Path) -> Path:
    """把待复核清单写成 JSON，供人工/模型按原链路复审。"""

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "vocab-feedback-review/v1",
        "note": (
            "这些条目只是待复核。修改必须回到识别/审查/硬校验链路，"
            "不得直接改写正文或按反馈自动采纳。"
        ),
        "items": feedback.items,
        "issues": feedback.issues,
    }
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return file

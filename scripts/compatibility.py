"""§22.2/§22.3/§22.6：官方校验基线、差异归因与兼容/严格发布判定。

旧做法是「EPUBCheck 有 ERROR 就不交付」，一刀切。本节改成：

* 源书与产物各跑一次**完整**官方 JSON，按源书哈希 + 工具版本缓存基线；
* 差异按 ``(级别, 错误码, 归一化消息, 资源路径, 元素/属性目标)`` 的**多重集**
  比较，行号列号变化不算新问题；不能用净错误数下降或前 10 条摘要证明无新增；
* 问题分五类：可接受遗留 / 已修复 / 新增 / 阻断 / 未解决；
* ``compatible``（默认）：语义与内容完整、新增 ERROR/FATAL 为零、剩余均属
  可接受遗留 → 可交付，但 ``epubcheck.ok`` 仍如实为 false；
* ``strict``：还要求官方检查确实运行且 ERROR/FATAL 为零。

工具缺失与工具出错分开：``not_run`` 明确标注外部校验未完成；``tool_error``
一律阻断，不自动退化成 ``--no-epubcheck``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from .validate_epub import _run_epubcheck, find_epubcheck

__all__ = [
    "SCHEMA_BASELINE",
    "baseline_path_for",
    "load_or_create_baseline",
    "compare_reports",
    "classify_issue",
    "evaluate",
    "summary_text",
]

SCHEMA_BASELINE = "vocab-epubcheck-baseline/v1"

#: 严重度：只有这些算门槛级问题。
_BLOCKING_SEVERITIES = {"ERROR", "FATAL"}


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_version(tool: dict | None = None) -> str:
    """从官方 JSON / CLI 版本输出里取稳定的版本标识。"""

    found = tool or find_epubcheck()
    if not found.get("available"):
        return "not-installed"
    jar = Path(found["path"])
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", jar.parent.name)
    return match.group(1) if match else jar.parent.name or "unknown"


def baseline_path_for(artifact_dir: str | Path, source_sha: str, version: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", version)
    return (
        Path(artifact_dir)
        / "_epubcheck_baseline"
        / f"{source_sha[:16]}-{safe}.baseline.json"
    )


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".part{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return path


def load_or_create_baseline(
    source_path: str | Path, artifact_dir: str | Path, *, refresh: bool = False
) -> dict:
    """源书基线：命中缓存直接复用，否则跑官方检查并把完整 JSON 留在产物旁。"""

    source_sha = _sha256(source_path)
    version = tool_version()
    path = baseline_path_for(artifact_dir, source_sha, version)
    if path.is_file() and not refresh:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") == SCHEMA_BASELINE:
            payload["cached"] = True
            return payload

    path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = path.with_name(path.stem + ".official.json")
    run = _run_epubcheck(source_path, report_path=raw_path)
    official = (
        json.loads(raw_path.read_text(encoding="utf-8"))
        if raw_path.is_file() and run.get("ran")
        else None
    )
    payload = {
        "schema": SCHEMA_BASELINE,
        "source_sha256": source_sha,
        "tool_version": version,
        "source_path": str(source_path),
        "official": official,
        "run": run,
        "cached": False,
    }
    _write_json(path, payload)
    return payload


# ---------------------------------------------------------------------------
# 差异归因
# ---------------------------------------------------------------------------

_CONTAINER_PATH = re.compile(r"^/[A-Za-z]:/|^/[\w/]+/\S+\.epub$")


def _norm_path(path: str) -> str:
    """包内路径归一化；整包级（指向 epub 文件本身）统一成一个哨兵。"""

    value = (path or "").strip()
    if not value or _CONTAINER_PATH.match(value) or value.lower().endswith(".epub"):
        return "(container)"
    value = value.lstrip("./")
    return value.replace("\\", "/")


def _norm_message(message: str) -> str:
    """归一化消息：去掉行列号、书名路径、多余空白，保留属性/元素名。"""

    text = " ".join((message or "").split())
    text = re.sub(r"line\s+\d+|column\s+\d+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"/[A-Za-z]:/[^\s\"']+\.epub", "(container)", text)
    return text.strip()


def _target(message: str) -> str:
    """抽出可定位的元素 / 属性 / 引用目标，作为匹配维度之一。"""

    patterns = (
        r'attribute\s+"([^"]+)"',
        r'element\s+"([^"]+)"',
        r'value of attribute\s+"([^"]+)"',
        r"<([a-zA-Z][\w-]*)>",
        r'"([^"]+\.(?:xhtml|html|opf|ncx|css|jpg|png))"',
    )
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return ""


def _key(message: dict) -> tuple:
    locs = message.get("locations") or []
    merged = int(message.get("additionalLocations") or 0) > 0
    paths = (
        ("(merged)",)
        if merged
        else tuple(sorted({_norm_path(loc.get("path", "")) for loc in locs}))
    )
    norm = _norm_message(message.get("message", ""))
    return (
        (message.get("severity") or "").upper(),
        (message.get("ID") or "").upper(),
        norm,
        _target(message.get("message", "")),
        paths,
    )


def _occurrences(message: dict) -> int:
    """实际出现次数：列出位置 + 被归并的额外位置。不能只看消息条数。"""

    return len(message.get("locations") or []) + int(
        message.get("additionalLocations") or 0
    )


def _counter(report: dict | None) -> Counter:
    counter: Counter = Counter()
    for message in (report or {}).get("messages") or []:
        counter[_key(message)] += _occurrences(message)
    return counter


def _describe(key: tuple, count: int) -> dict:
    severity, code, message, target, paths = key
    return {
        "severity": severity,
        "code": code,
        "message": message[:300],
        "target": target,
        "resources": [p for p in paths if p != "(merged)"][:8],
        "locations_merged": "(merged)" in paths,
        "count": count,
    }


def compare_reports(
    source_report: dict | None,
    output_report: dict | None,
    *,
    mapping: dict | None = None,
) -> dict:
    """按多重集比较；行号变化不入 key，归并位置如实标注。

    ``mapping`` 是 §22.3 工作副本的「原路径/ID → 新路径/ID」映射，
    目前只用于把修复后的资源路径回写为源书路径，避免误判成新问题。
    """

    src = _counter(source_report)
    out = _counter(output_report)
    if mapping:
        rename = {_norm_path(k): _norm_path(v) for k, v in mapping.items()}
        remapped: Counter = Counter()
        for key, count in out.items():
            paths = tuple(rename.get(p, p) for p in key[4])
            remapped[key[:4] + (tuple(sorted(set(paths))),)] += count
        out = remapped

    new = out - src
    fixed = src - out
    inherited = src & out
    return {
        "source_total": sum(src.values()),
        "output_total": sum(out.values()),
        "new": [_describe(k, v) for k, v in sorted(new.items(), key=lambda kv: str(kv[0]))],
        "fixed": [_describe(k, v) for k, v in sorted(fixed.items(), key=lambda kv: str(kv[0]))],
        "inherited": [
            _describe(k, v) for k, v in sorted(inherited.items(), key=lambda kv: str(kv[0]))
        ],
        "unresolved": [],
        "comparable": source_report is not None and output_report is not None,
    }


# ---------------------------------------------------------------------------
# 五级分类
# ---------------------------------------------------------------------------

#: (错误码, 消息正则, 类别, 理由)。顺序即优先级；未匹配的一律不自动放行。
LEGACY_RULES: tuple[tuple[str, str, str, str], ...] = (
    ("OPF-054", r"Date value", "date_format", "元数据日期格式不标准，仅影响元数据"),
    ("PKG-010", r"contains spaces", "filename_spaces", "资源文件名含空格但可解析，保留原文件名"),
    ("RSC-020", r"not a valid URL.*space", "uri_spaces", "含空格的资源引用可解析，内部链接校验已覆盖"),
    ("RSC-005", r'attribute "(clear|link|vlink|alink|bgcolor|text|background)" not allowed',
     "legacy_presentation_attr", "旧式外观属性，不重排正文与版式"),
    ("RSC-005", r'element "br" not allowed', "legacy_element", "旧式换行位置，保留版式"),
    ("RSC-005", r'element "spine" missing required attribute "toc"', "missing_navigation",
     "缺 NCX / spine toc，spine 与阅读顺序完整"),
    ("RSC-005", r'element "img" missing required attribute "alt"', "missing_alt",
     "图片缺 alt，不编造描述、保留图片"),
    ("RSC-005", r'value of attribute "(id|idref)" is invalid', "invalid_id",
     "非法但唯一且引用可解析的 ID，内部链接/ID 校验已覆盖"),
)

#: 本工具写入的 DOM 问题：即使原书也有同错误码，也绝不归入兼容例外。
ANNOTATION_DOM = re.compile(
    r'attribute "data-|element "section"|element "aside"|vocab-chapter-notes',
    re.IGNORECASE,
)

#: 影响完整处理的问题：无论来自原书还是产物都不能放行。
CONTENT_BLOCKERS = re.compile(
    r"spine.*(?:points to|itemref.*missing)|item.*not found|resource.*not found|"
    r"duplicate.*id|unable to parse|could not parse|"
    r"missing required.*(?:src|href)|text.*lost|content.*lost",
    re.IGNORECASE,
)


def classify_issue(issue: dict) -> tuple[str, str]:
    """返回 (类别, 理由)。类别：acceptable_legacy / new / blocking / unresolved。"""

    code = (issue.get("code") or "").upper()
    message = issue.get("message") or ""
    if ANNOTATION_DOM.search(message):
        return "blocking", "标注写入的非法 DOM（data-* / section），必须修实现"
    if CONTENT_BLOCKERS.search(message):
        return "blocking", "影响内容完整性或引用歧义，不能仅因原书已有而放行"
    for rule_code, pattern, category, reason in LEGACY_RULES:
        if code == rule_code and re.search(pattern, message, re.IGNORECASE):
            return "acceptable_legacy", reason
    return "unresolved", "未覆盖的官方问题类型，无法确认影响，需人工判定"


# ---------------------------------------------------------------------------
# 发布判定
# ---------------------------------------------------------------------------


def _status(run: dict | None) -> str:
    if not run:
        return "not_run"
    if run.get("ran"):
        return "passed" if run.get("ok") else "failed"
    reason = str(run.get("reason") or "")
    if "未找到" in reason or "不可用" in reason:
        return "not_run"
    return "tool_error"


def evaluate(
    *,
    source_path: str | Path,
    output_path: str | Path,
    artifact_dir: str | Path,
    policy: str = "compatible",
    run_epubcheck: bool = True,
    internal_ok: bool = True,
    mapping: dict | None = None,
    report_name: str | None = None,
) -> dict:
    """跑产物检查 + 源书基线 → 差异归因 → 分类 → 交付判定。"""

    artifact_dir = Path(artifact_dir)
    policy = "strict" if policy == "strict" else "compatible"
    result: dict = {
        "policy": policy,
        "official": {
            "status": "not_run",
            "ran": False,
            "version": tool_version(),
            "nError": None,
            "nWarning": None,
            "nFatal": None,
            "source_report": "",
            "output_report": "",
        },
        "issues": {
            "acceptable_legacy": [],
            "fixed": [],
            "new": [],
            "blocking": [],
            "unresolved": [],
        },
        "delivery_kind": "blocked",
        "publishable": False,
        "external_validation_incomplete": True,
        "work_copy": {
            "created": False,
            "reason": (
                "兼容模式默认接纳可读的不规范原书（§22.1）。可接受的遗留瑕疵允许不修；"
                "如需确定性修复须显式调用 compat_fixes 创建工作副本，并按 §22.3 "
                "重新建立 v6 内容绑定与审查，本轮未执行。"
            ),
        },
        "summary": "",
    }
    run: dict = {"ran": False, "status": "not_run", "reason": "--no-epubcheck"}
    result["official"]["run"] = run

    if not run_epubcheck:
        result["official"]["status"] = "not_run"
        result["issues"]["unresolved"].append(
            {"reason": "显式跳过官方检查（--no-epubcheck），外部校验未完成"}
        )
        result["external_validation_incomplete"] = True
        if policy == "strict":
            result["issues"]["blocking"].append(
                {"severity": "ERROR", "message": "严格模式要求官方检查实际运行"}
            )
            result["summary"] = "严格模式未运行官方校验，拒绝交付；详情见报告。"
            return result
        # 兼容模式：只能靠内部硬校验，明确披露未完成外部检查
        result["publishable"] = bool(internal_ok)
        result["delivery_kind"] = "compatible_unverified" if internal_ok else "blocked"
        result["summary"] = (
            "已生成产物；官方规范校验未运行（仅内部检查），详情见报告。"
            if internal_ok
            else "内部检查未通过，拒绝交付；详情见报告。"
        )
        return result

    output_report_path = artifact_dir / (
        (report_name or Path(output_path).name) + ".epubcheck.json"
    )
    run = _run_epubcheck(output_path, report_path=output_report_path)
    result["official"]["run"] = run
    status = _status(run)
    result["official"].update(
        {
            "status": status,
            "ran": bool(run.get("ran")),
            "version": tool_version(),
            "output_report": str(output_report_path) if output_report_path.is_file() else "",
            "nError": (run.get("counts") or {}).get("nError"),
            "nWarning": (run.get("counts") or {}).get("nWarning"),
            "nFatal": (run.get("counts") or {}).get("nFatal"),
        }
    )
    if status == "tool_error":
        result["issues"]["blocking"].append(
            {"severity": "ERROR", "message": f"官方检查工具出错：{run.get('reason')}"}
        )
        result["summary"] = "官方校验工具出错，拒绝交付；详情见报告。"
        return result
    if status == "not_run":
        result["external_validation_incomplete"] = True
        result["issues"]["unresolved"].append(
            {"reason": f"官方检查工具未运行：{run.get('reason')}"}
        )
        if policy == "strict":
            result["issues"]["blocking"].append(
                {"severity": "ERROR", "message": "严格模式要求官方检查实际运行"}
            )
            result["summary"] = "严格模式无可用官方校验，拒绝交付；详情见报告。"
            return result
        result["publishable"] = bool(internal_ok)
        result["delivery_kind"] = "compatible_unverified" if internal_ok else "blocked"
        result["summary"] = (
            "已生成产物；本机无官方校验工具，仅完成内部检查，详情见报告。"
            if internal_ok
            else "内部检查未通过，拒绝交付；详情见报告。"
        )
        return result

    output_report = (
        json.loads(output_report_path.read_text(encoding="utf-8"))
        if output_report_path.is_file()
        else None
    )
    try:
        baseline = load_or_create_baseline(source_path, artifact_dir)
    except (OSError, ValueError) as exc:
        result["issues"]["blocking"].append(
            {"severity": "ERROR", "message": f"源书基线无法建立：{exc}"}
        )
        result["summary"] = "源书基线无法建立，拒绝交付；详情见报告。"
        return result

    result["official"]["source_report"] = str(
        baseline_path_for(artifact_dir, baseline["source_sha256"], baseline["tool_version"])
    )
    result["official"]["source_cached"] = bool(baseline.get("cached"))
    baseline_run = baseline.get("run") or {}
    if _status(baseline_run) in ("tool_error", "not_run") or not baseline.get("official"):
        result["issues"]["unresolved"].append(
            {"reason": "源书官方基线缺失或工具出错，无法完成差异归因"}
        )
        result["summary"] = "源书基线不可用，无法确认有无新增问题；详情见报告。"
        return result

    diff = compare_reports(baseline["official"], output_report, mapping=mapping)
    issues = result["issues"]
    issues["fixed"] = diff["fixed"]
    for item in diff["new"]:
        category, reason = classify_issue(item)
        item["reason"] = reason
        if category == "blocking" or item["severity"] in _BLOCKING_SEVERITIES:
            issues["blocking"].append(item)
        else:
            issues["new"].append(item)  # 新增 WARNING 记录但不阻断
    for item in diff["inherited"]:
        category, reason = classify_issue(item)
        item["reason"] = reason
        if category == "acceptable_legacy":
            issues["acceptable_legacy"].append(item)
        elif category == "blocking":
            issues["blocking"].append(item)
        else:
            issues["unresolved"].append(item)
    if not diff["comparable"]:
        issues["unresolved"].append({"reason": "官方报告缺失，差异无法定位"})
    if any(item.get("locations_merged") for item in diff["inherited"]):
        # 位置被归并：不声称「逐一定位已完成」，靠内部 DOM 扫描补充证据
        result["diagnostic_note"] = (
            "部分官方诊断位置被归并截断；已用内部 16+2 项结构/内容校验与 "
            "EPUB 2 DOM 扫描补充核对，未逐一定位每个被归并位置。"
        )
    result["external_validation_incomplete"] = False

    n_error = (result["official"]["nError"] or 0) + (result["official"]["nFatal"] or 0)
    strict_failed = policy == "strict" and n_error > 0
    if strict_failed:
        issues["blocking"].append(
            {
                "severity": "ERROR",
                "code": "STRICT_POLICY",
                "message": f"严格模式要求官方 ERROR/FATAL 为零，实测 {n_error}",
                "reason": "严格模式如实拒绝非零 ERROR；可改用默认 compatible 策略",
            }
        )
    result["official"]["strict_error_count"] = n_error
    blocking = bool(issues["blocking"]) or not internal_ok or strict_failed
    # 未分类且影响判断的遗留问题：不能静默放行
    if issues["unresolved"]:
        blocking = True
    result["publishable"] = not blocking
    if blocking:
        result["delivery_kind"] = "blocked"
    elif policy == "strict":
        result["delivery_kind"] = "spec_passed"
    else:
        result["delivery_kind"] = "compatible_delivery"
    result["summary"] = summary_text(result)
    return result


def summary_text(result: dict) -> str:
    """§22.6：默认只给读者一句话，不逐项弹确认、不在词注里加告警。"""

    issues = result["issues"]
    if not result["publishable"]:
        reasons = []
        if result.get("policy") == "strict" and result["official"].get("strict_error_count"):
            reasons.append(
                f"严格模式仍有 {result['official']['strict_error_count']} 项 ERROR/FATAL"
            )
        if issues["blocking"]:
            reasons.append(f"{len(issues['blocking'])} 项须处理的问题")
        if issues["unresolved"]:
            reasons.append(f"{len(issues['unresolved'])} 项无法确认")
        return f"交付被阻断（{'；'.join(reasons) or '内部检查未通过'}），详情见报告。"
    legacy = len(issues["acceptable_legacy"])
    new = len(issues["new"])
    fixed = len(issues["fixed"])
    if result["delivery_kind"] == "spec_passed":
        return "已生成产物；官方规范校验通过。详情见报告。"
    if result["external_validation_incomplete"]:
        return "已生成产物；官方规范校验未运行（仅内部检查），详情见报告。"
    tail = f"已修复 {fixed} 项" if fixed else "未发现本次新增问题"
    new_part = f"；另有 {new} 项新增提示" if new else ""
    return (
        f"已生成产物；原书有 {legacy} 类格式瑕疵，已保留可读内容，{tail}{new_part}。详情见报告。"
    )

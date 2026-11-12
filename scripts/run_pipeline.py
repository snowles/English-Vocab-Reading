"""命令行入口：预检 -> 词表 -> (消歧请求/应用) -> 标注 -> 校验 -> 报告。

阶段 1（无模型）：
    python -m scripts.run_pipeline --book <书.epub> --lexicon <词表.csv>

阶段 2（由 harness 做语境消歧，两步走）：
    # 1) 产出请求，交给 Codex / WorkBuddy / 豆包工作版判断
    python -m scripts.run_pipeline --book <书> --lexicon <词表> --emit-requests out/requests
    # 2) 应用 harness 写回的 decisions（目录或单文件）
    python -m scripts.run_pipeline --book <书> --lexicon <词表> --apply-decisions out/decisions

退出码：
    0  成功（完整交付且校验通过）
    2  输入错误（格式不支持、结构损坏、词表不可用、版本绑定错配）
    3  输入已含本技能标注，拒绝重复处理
    4  输出校验未通过（结构检查失败，不提交最终文件）
    5  未预期的内部错误
    6  发布门槛未通过（缺审、待复审、范围缺失、硬校验失败；未生成成品）
    7  仅部分交付（--allow-partial 显式丢弃未完成项，publishable=false）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/run_pipeline.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .annotate_epub import (  # noqa: E402
    DENSITIES,
    AlreadyAnnotatedError,
    AnnotationOptions,
    annotate_epub,
    collect_candidates,
)
from .console import configure_stdio  # noqa: E402
from .disambiguation import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    load_decisions,
    request_payload,
)
from .epub_common import EpubError  # noqa: E402
from .inspect_book import inspect_book, sha256_file  # noqa: E402
from .load_lexicon import (  # noqa: E402
    LexiconError,
    lexicon_content_sha256,
    load_lexicon,
)
from .reader_mode import (  # noqa: E402
    READER_MODE_LABELS,
    READER_MODE_UNSPECIFIED,
    ReaderModeError,
    build_translation_request,
    load_translations,
    paragraph_records,
    parse_reader_mode,
)
from .validate_epub import validate_output  # noqa: E402

SKILL_NAME = "vocab-reading-skill"
STAGE = 4
MATCHING_MODE = "no-model+harness-semantic-units"

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_ALREADY_ANNOTATED = 3
EXIT_VALIDATION = 4
EXIT_INTERNAL = 5
EXIT_BLOCKED = 6
EXIT_PARTIAL = 7

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEXICON = ROOT_DIR / "assets" / "lexicon.vault"


def _positive_int(raw: str) -> int:
    """窗口大小必须 ≥1。

    关3 压测发现 `--window-paragraphs 0` 会被静默接受，退化成"每段一窗"，
    窗口数暴涨而用户拿到的还是"成功"。这里直接拒掉。
    """

    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要整数：{raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"必须 ≥ 1：{value}")
    return value


def _non_negative_int(raw: str) -> int:
    """段落范围必须 ≥0。

    负数会让 `paragraphs[-5:]` / `paragraphs[:-5]` 从尾部切片，
    静默得到与"前 N 段"相反的片段，属于会误导用户的静默错误。
    """

    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要整数：{raw!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"必须 ≥ 0：{value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocab-reading",
        description=(
            "英语词汇伴读：在 EPUB 正文中标注词表词汇并生成脚注。"
            "语境消歧由宿主智能体（harness）完成，本工具不含模型调用。"
        ),
    )
    parser.add_argument("--book", required=True, help="输入 EPUB 路径（只读）")
    parser.add_argument(
        "--lexicon",
        default="",
        help=(
            "词表 CSV 路径（可选）。默认使用随包词库 assets/lexicon.vault，"
            "零配置即可运行；传入自己的 CSV 时按明文读取。"
        ),
    )
    parser.add_argument(
        "--grades", default="", help="只保留这些等级，逗号/顿号分隔（如 四级,雅思）"
    )
    parser.add_argument(
        "--label-order",
        default="",
        help=(
            "正文标签显示次序（默认 4,6,ielts,toefl,GRE,牛津,AWL）；"
            "只影响显示，不影响词库来源判定"
        ),
    )
    parser.add_argument(
        "--no-stoplist",
        action="store_true",
        help="不排除虚词（that/but/and 等默认不标，认为无释义价值）",
    )
    parser.add_argument(
        "--max-note-chars",
        type=int,
        default=12,
        help="inline 括注最大长度（默认 12 字，超出截断加省略号；0 = 不截断）",
    )
    parser.add_argument(
        "--presentation",
        choices=("inline-notes", "footnote", "inline"),
        default="inline-notes",
        help=(
            "呈现方式：inline-notes=正文短注+章末完整词条（默认）、"
            "inline=纯正文括注、footnote=章末脚注"
        ),
    )
    parser.add_argument(
        "--segment-words",
        type=int,
        default=1200,
        help="--density segment 时每段词数（默认 1200，约两页）",
    )
    parser.add_argument(
        "--density",
        choices=DENSITIES,
        default="chapter",
        help=(
            "标注密度：chapter=每章首次（默认）、book=全书首次、"
            "segment=每 N 词首次（单文件长书用）、all=全部命中"
        ),
    )
    parser.add_argument("--glossary", action="store_true", help="额外生成书末词表页")
    parser.add_argument(
        "--no-reduce", action="store_true", help="关闭词形还原，只做原形匹配"
    )
    parser.add_argument("--out", default="", help="输出目录（默认与源书同目录）")
    parser.add_argument(
        "--report", default="", help="任务报告 JSON 路径（默认放在输出目录）"
    )
    parser.add_argument("--no-epubcheck", action="store_true", help="跳过 EPUBCheck 探测")
    parser.add_argument(
        "--html",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "同时生成离线阅读页（HTML）。只写 --html 时与 EPUB 同名同目录；"
            "给目录则放在该目录。章节、标签、目录全自动推导"
        ),
    )
    parser.add_argument(
        "--html-chapters",
        default="",
        metavar="RANGE",
        help="阅读页章节范围，如 1-2 或 3；默认全书（按原书目录的逻辑章节）",
    )
    parser.add_argument(
        "--html-force", action="store_true", help="允许覆盖已存在的阅读页"
    )
    parser.add_argument(
        "--check-policy",
        choices=("compatible", "strict"),
        default="compatible",
        help=(
            "§22 官方校验策略：compatible=默认，允许带披露地交付可读的不规范原书；"
            "strict=要求 EPUBCheck 实际运行且 ERROR/FATAL 为零"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="只输出最终结果行")

    mode = parser.add_argument_group("出书阅读档位（宿主必须先问用户）")
    mode.add_argument(
        "--reader-mode",
        default="",
        metavar="MODE",
        help=(
            "basic=初级（自然段后带中文译文）/ advanced=高级（不带译文）。"
            "正式出书前宿主必须主动问用户；未确认档位时成品不标记为可发布"
        ),
    )
    mode.add_argument(
        "--translations",
        default="",
        metavar="PATH",
        help="初级模式的宿主译文分片（文件或目录），schema vocab-para-translations/v1",
    )
    mode.add_argument(
        "--emit-translations",
        default="",
        metavar="DIR",
        help="导出待翻译的段落请求给宿主（不写 EPUB）",
    )
    mode.add_argument(
        "--require-reader-mode",
        action="store_true",
        help=(
            "宿主正式出书链路必带：未确认档位时直接拦下不出成品。"
            "手动调试/兼容旧脚本不带此开关时行为不变"
        ),
    )

    group = parser.add_argument_group("阶段 2：harness 语境消歧")
    group.add_argument(
        "--engine",
        choices=("units", "legacy"),
        default="units",
        help="units=§17 语义单位流程（默认）；legacy=旧的逐词 v2 流程",
    )
    group.add_argument(
        "--emit-annotations",
        default="",
        metavar="DIR",
        help="产出 §17 段落窗口请求（identify），不写 EPUB",
    )
    group.add_argument(
        "--apply-annotations",
        default="",
        metavar="DIR",
        help="应用 ann-*.json（识别）+ review-*.json（审查）",
    )
    group.add_argument(
        "--emit-review",
        default="",
        metavar="DIR",
        help=(
            "冻结识别内容并导出独立审查包（review-request-*.json），不写 EPUB；"
            "审查者写回 review-*.json 后再运行 --apply-annotations"
        ),
    )
    group.add_argument(
        "--review-round",
        type=int,
        default=1,
        help="审查轮次（modify 后的复审用 2、3…）",
    )
    group.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "允许丢弃未通过硬校验的标注后继续出书（默认严格：有 invalid 就不生成成品）"
        ),
    )
    group.add_argument(
        "--reading-level",
        choices=("light", "standard", "detailed"),
        default="standard",
        help=(
            "提示量档位：light=只留影响理解的难点，standard=沿用现有尺度（默认），"
            "detailed=提示内容最全（含搭配与易混用法）。**不改变重复频率**——"
            "档位只控制标注什么，要同章反复提示请显式加 --density all"
        ),
    )
    group.add_argument(
        "--resume",
        action="store_true",
        help=(
            "断点续作：同一任务（scope 哈希一致）已有 windows-*.json 时不再重写，"
            "只补缺失分片；已有识别与审查保持不变"
        ),
    )
    group.add_argument(
        "--feedback",
        default="",
        metavar="PATH",
        help=(
            "网页导出的纠错反馈 JSON（文件或目录）。相关条目会被标记待复核、"
            "旧审查作废，但不自动采纳反馈内容"
        ),
    )
    group.add_argument(
        "--feedback-review",
        default="",
        metavar="PATH",
        help="把待复核清单写到该 JSON，供按原链路复审",
    )
    group.add_argument(
        "--invalidate",
        action="append",
        default=[],
        metavar="ANNOTATION_ID",
        help=(
            "让指定条目的旧审查失效、强制重审（可重复）。失效后该条变为待审，"
            "发布门槛会拦住，直到写出针对新内容版本的 verdict"
        ),
    )
    group.add_argument(
        "--word-list",
        default="",
        metavar="PATH",
        help=(
            "本地熟词名单 CSV（word,status）或 JSON；status 取 mastered / focus。"
            "已掌握只减少透明常见词，组合与新义仍提示"
        ),
    )
    group.add_argument(
        "--comparison",
        default="",
        metavar="PATH",
        help="输出「原文—区间—短义—等级—章末—依据—审查」对照表 HTML",
    )
    group.add_argument(
        "--window-paragraphs",
        type=_positive_int,
        default=4,
        help="每个上下文窗口的段落数（≥1）",
    )
    group.add_argument(
        "--max-paragraphs",
        type=_non_negative_int,
        default=0,
        help="只处理前 N 段（小样用，0=全部）",
    )
    group.add_argument(
        "--paragraph-start",
        type=_non_negative_int,
        default=0,
        help="从第 N 段开始（抽查未参与调试的片段用，0=从头）",
    )
    group.add_argument(
        "--emit-requests",
        default="",
        metavar="DIR",
        help="只产出消歧请求分片到该目录，不生成 EPUB",
    )
    group.add_argument(
        "--apply-decisions",
        default="",
        metavar="PATH",
        help="应用 harness 写回的 decisions（目录或单个 JSON）",
    )
    group.add_argument(
        "--include-multi-sense",
        action="store_true",
        help="把「单条多义项释义」也纳入消歧（请求量会显著增加）",
    )
    group.add_argument(
        "--undecided",
        choices=("skip", "all-senses", "lexicon"),
        default="skip",
        help=(
            "无决策/低置信时的策略：skip=不标注（默认）、"
            "all-senses=列出全部释义（legacy 模式）、"
            "lexicon=用词表首个义项并明确标为未消歧版本"
        ),
    )
    group.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help=f"采纳决策的最低置信度（默认 {DEFAULT_MIN_CONFIDENCE}）",
    )
    group.add_argument(
        "--batch-size", type=int, default=50, help="每个请求分片包含的候选数（默认 50）"
    )
    group.add_argument(
        "--max-context-chars",
        type=int,
        default=300,
        help="每条上下文最大字符数（默认 300）",
    )
    return parser


def _split_grades(raw: str) -> list[str]:
    for separator in ("、", "，", ";"):
        raw = raw.replace(separator, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _log(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


def _config(args, grades: list[str]) -> dict:
    return {
        "grades": grades,
        "label_order": _split_grades(args.label_order) if args.label_order else [],
        "density": args.density,
        "segment_words": args.segment_words,
        "presentation": args.presentation,
        "reduce_forms": not args.no_reduce,
        "stoplist": not args.no_stoplist,
        "include_multi_sense": args.include_multi_sense,
        "min_confidence": args.min_confidence,
        # 阅读档位改变成品内容，必须进内容版本绑定，不能与另一档位复用 decisions
        "reader_mode": parse_reader_mode(getattr(args, "reader_mode", "")),
    }


def _config_hash(config: dict) -> str:
    import hashlib

    payload = json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _options(args: argparse.Namespace, grades: list[str]) -> AnnotationOptions:
    return AnnotationOptions(
        glossary=args.glossary,
        reduce_forms=not args.no_reduce,
        grades=grades,
        density=args.density,
        presentation=args.presentation,
        use_stoplist=not args.no_stoplist,
        max_note_chars=args.max_note_chars,
        segment_words=args.segment_words,
        undecided=args.undecided,
        min_confidence=args.min_confidence,
        include_multi_sense=args.include_multi_sense,
        max_context_chars=args.max_context_chars,
    )


def _reader_mode(args) -> str:
    return parse_reader_mode(getattr(args, "reader_mode", ""))


def _reader_summary(args, translations=None, coverage: dict | None = None) -> dict:
    mode = _reader_mode(args)
    out: dict = {
        "mode": mode,
        "label": READER_MODE_LABELS.get(mode, "未确认（宿主须先问用户）"),
        "confirmed": mode != READER_MODE_UNSPECIFIED,
        "has_chinese_translation": mode == "basic",
        "translation_prefix": "参考译文：" if mode == "basic" else "",
        "translation_disclaimer": (
            "段落译文由宿主模型生成，仅供参考；正文只在每段前加「参考译文：」，"
            "不逐段重复免责声明，免责说明记录在本报告里。"
            if mode == "basic" else ""
        ),
        "required_by_host": bool(getattr(args, "require_reader_mode", False)),
    }
    if coverage is not None:
        out["translation_coverage"] = coverage
    if translations is not None:
        out["translations"] = {
            "provided": len(translations),
            "files": translations.files,
            "issues": translations.issues[:10],
        }
    return out


def _prepare_reader_mode(args, book: Path, sha_before: str):
    """出书前把档位与译文数据准备好；不合格就不让成品出门。"""

    mode = _reader_mode(args)
    if mode == READER_MODE_UNSPECIFIED and getattr(args, "require_reader_mode", False):
        raise ReaderModeError(
            "正式出书前宿主必须主动问用户「初级（带中文译文）」还是「高级（不带）」，"
            "并把选择固定到本次链路；未确认档位时不生成成品。"
        )
    translations = None
    if getattr(args, "translations", ""):
        translations = load_translations(args.translations, book_sha256=sha_before)
        if mode == "advanced":
            raise ReaderModeError(
                "高级模式不带中文译文，却提供了译文文件；请改选初级或去掉译文。"
            )
    coverage = None
    if mode == "basic":
        if translations is None:
            raise ReaderModeError(
                "初级模式必须提供段落译文：请先导出译文请求，由宿主逐段翻译后写回。"
            )
        coverage = translations.coverage(paragraph_records(book))
        if coverage["ratio"] < 0.95:
            raise ReaderModeError(
                f"初级模式译文覆盖不足（{coverage['covered']}/{coverage['total']}，"
                f"缺 {coverage['missing']} 段）；不交付名不副实的初级成品。"
            )
    args.reader_translations = translations
    args.reader_coverage = coverage
    return translations


def _load_decisions(args, book: Path, lexicon_path: Path, precheck):
    decisions = load_decisions(args.apply_decisions)
    for issue in decisions.issues[:5]:
        print(f"决策文件问题：{issue}", file=sys.stderr)
    book_sha = sha256_file(book)
    if decisions.book_sha256 and decisions.book_sha256 != book_sha:
        raise EpubError(
            "决策不是为这本书生成的（book_sha256 不一致），拒绝应用；"
            "请对同一本书重新生成请求。"
        )
    config_hash = _config_hash(_config(args, _split_grades(args.grades)))
    if decisions.config_hash and decisions.config_hash != config_hash:
        raise EpubError(
            "决策对应的扫描配置与当前不一致（密度/呈现/等级等已变，"
            f"{decisions.config_hash} != {config_hash}），拒绝应用；请重新生成请求。"
        )
    if decisions.lexicon_sha256:
        # 主链路按解封后 CSV 内容哈希绑定；旧版请求分片写的是容器文件哈希，
        # 两种都认，但词库内容一变则两者同时失效，旧 decisions 不能沿用。
        accepted = {
            lexicon_content_sha256(lexicon_path),
            sha256_file(lexicon_path),
        }
        if decisions.lexicon_sha256 not in accepted:
            raise EpubError(
                "决策对应的词表与当前词表不一致（lexicon_sha256 不匹配），拒绝应用。"
            )
    _log(
        args.quiet,
        f"      决策：{len(decisions)} 条/ {len(decisions.files)} 个文件"
        f"（问题 {len(decisions.issues)}）",
    )
    return decisions


def _emit_units(args, book: Path, lexicon, precheck, lexicon_sha: str) -> int:
    from .revisions import (
        IDENTIFY_PROMPT_VERSION,
        PROTOCOL,
        REVIEW_PROMPT_VERSION,
    )
    from .tokens import TOKENIZER_VERSION
    from .units import (
        SCHEMA_ANN_REQUEST,
        build_request,
        build_scope,
        collect_windows,
    )

    config = _config(args, _split_grades(args.grades))
    config_hash = _config_hash(config)
    windows, meta = collect_windows(
        book,
        lexicon,
        window_paragraphs=args.window_paragraphs,
        max_paragraphs=args.max_paragraphs or None,
        paragraph_start=args.paragraph_start or 0,
    )
    if not windows:
        # 与「不交付空书」同一口径：导不出窗口就不算这一步成功，
        # 否则宿主会拿着空目录往下走，最后才在出书阶段报 0 标注。
        print(
            "未生成识别请求：这本书里没有可标注的正文段落"
            "（可能全部落在前置页/目录）。请确认是正文版 EPUB，"
            "或调整 --paragraph-start / --max-paragraphs。",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    target = Path(args.emit_annotations)
    target.mkdir(parents=True, exist_ok=True)
    _assert_own_emit_dir(target)
    scope_file = target / "scope.json"
    previous: dict = {}
    if getattr(args, "resume", False) and scope_file.exists():
        try:
            previous = json.loads(scope_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous = {}
    binding = {
        "protocol": PROTOCOL,
        "tokenizer": TOKENIZER_VERSION,
        "book_sha256": precheck.sha256,
        "lexicon_sha256": lexicon_sha,
        "config_hash": config_hash,
        "identify_prompt": IDENTIFY_PROMPT_VERSION,
        "review_prompt": REVIEW_PROMPT_VERSION,
        "context_pack_hash": "",
    }
    scope = build_scope(
        windows,
        lexicon,
        binding,
        max_paragraphs=args.max_paragraphs or 0,
        window_paragraphs=args.window_paragraphs,
        paragraph_start=args.paragraph_start or 0,
    )
    # §20.6 断点续作：只有任务清单哈希完全一致才复用已有分片，
    # 书、词库、配置、窗口范围任一变化都会重建，避免把旧识别当成新任务。
    resume_ok = bool(previous) and previous.get("task_id") == scope["task_id"]
    if resume_ok:
        _log(
            args.quiet,
            f"      续作：任务清单一致，复用已有窗口（{len(previous.get('windows') or [])} 个）",
        )
    elif getattr(args, "resume", False):
        _log(args.quiet, "      已有清单与当前任务不一致，重新生成全部窗口")
    if not resume_ok:
        for old in target.glob("windows-*.json"):
            old.unlink()
    scope_file.write_text(
        json.dumps(scope, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    reused = 0
    for index, window in enumerate(windows, start=1):
        window_file = target / f"windows-{index:04d}.json"
        if resume_ok and window_file.exists():
            reused += 1
            continue
        payload = build_request(
            meta.get("title", ""),
            precheck.sha256,
            lexicon_sha,
            config,
            config_hash,
            [window.as_dict(lexicon)],
            scope=scope,
            reading_level=args.reading_level,
        )
        window_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    (target / "index.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA_ANN_REQUEST,
                "protocol": PROTOCOL,
                "task_id": scope["task_id"],
                "context_pack_hash": scope["context_pack_hash"],
                "book": {"title": meta.get("title", ""), "sha256": precheck.sha256},
                "config": config,
                "config_hash": config_hash,
                "reading_level": args.reading_level,
                "paragraphs": meta.get("paragraphs", 0),
                "windows": len(windows),
                "next_step": (
                    "逐个 windows-*.json 完成 identify，写出 ann-*.json"
                    "（顶层必须带 windows 覆盖记录）；"
                    "然后运行 --apply-annotations <目录> --emit-review <目录> 导出审查包；"
                    "审查者写回 review-*.json 后再 --apply-annotations <目录> 出书"
                ),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    _log(
        args.quiet,
        f"[3/3] 已产出 {len(windows)} 个窗口 / {meta.get('paragraphs', 0)} 段"
        f"{f'（复用 {reused} 个）' if reused else ''} -> {target}",
    )
    return EXIT_OK


def _attach_compatibility(gate: dict, args, validation) -> None:
    """§22.6：把官方状态、问题归因与交付类型分列，不混成一个布尔值。"""

    compat = (getattr(validation, "compatibility", None) if validation else None) or {}
    official = compat.get("official") or {}
    issues = compat.get("issues") or {}
    gate["check_policy"] = getattr(args, "check_policy", "compatible")
    gate["official_check"] = {
        "status": official.get("status", "not_run"),
        "ran": bool(official.get("ran")),
        "version": official.get("version", ""),
        "nError": official.get("nError"),
        "nWarning": official.get("nWarning"),
        "nFatal": official.get("nFatal"),
        "source_report": official.get("source_report", ""),
        "output_report": official.get("output_report", ""),
    }
    gate["compatibility"] = {
        "delivery_kind": compat.get("delivery_kind", "not_evaluated"),
        "publishable": bool(compat.get("publishable")),
        "external_validation_incomplete": bool(
            compat.get("external_validation_incomplete", True)
        ),
        "issues": {
            key: list(issues.get(key) or [])
            for key in (
                "acceptable_legacy",
                "fixed",
                "new",
                "blocking",
                "unresolved",
            )
        },
        "summary": compat.get("summary", ""),
        "diagnostic_note": compat.get("diagnostic_note", ""),
        "work_copy": compat.get("work_copy") or {"created": False, "reason": "未评估"},
    }


def _html_target(epub_path: Path, spec: str) -> Path:
    """``--html`` 给目录就放同名 .html，给文件名就用它。"""

    raw = (spec or "").strip()
    if not raw:
        return epub_path.with_suffix(".html")
    target = Path(raw).expanduser()
    if target.is_dir() or target.suffix.lower() not in (".html", ".htm"):
        return target / (epub_path.stem + ".html")
    return target


def _build_html(args, epub_path: Path) -> dict:
    """生成离线阅读页；失败如实记录，不冒充两种格式都已完成。"""

    target = _html_target(epub_path, args.html)
    try:
        from .build_reader_html import build_single

        info = build_single(
            epub_path,
            target,
            chapters=args.html_chapters,
            force=args.html_force,
        )
    except SystemExit as exc:
        _log(args.quiet, f"      阅读页未生成：{exc}")
        return {"requested": True, "ok": False, "path": str(target), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        _log(args.quiet, f"      阅读页未生成：{exc}")
        return {
            "requested": True,
            "ok": False,
            "path": str(target),
            "error": f"{type(exc).__name__}: {exc}",
        }
    _log(args.quiet, f"      阅读页 {info['path']}（{info['sections']} 节）")
    return {"requested": True, **info}


def _release_report(
    args,
    book: Path,
    lexicon,
    lexicon_sha: str,
    grades: list[str],
    precheck,
    sha_before: str,
    *,
    outcome,
    validation,
    gate: dict,
    decisions=None,
    review=None,
    elapsed: float = 0.0,
    deliverables: dict | None = None,
) -> dict:
    annotation = outcome.as_dict() if outcome is not None else {}
    if outcome is None:
        annotation = {
            "blocked": True,
            "decisions": decisions.summary() if decisions is not None else {},
            "review": review.summary() if review is not None else {},
        }
    if validation is not None and "disambiguation" in annotation:
        annotation["disambiguation"]["publishable"] = bool(gate.get("publishable"))
    report = {
        "skill": SKILL_NAME,
        "stage": STAGE,
        "matching_mode": MATCHING_MODE,
        "timestamp": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "grades_requested": grades,
        "grades_applied": (
            f"{lexicon.rows_total} -> {len(lexicon)} 词条" if grades else "全部词条"
        ),
        "lexicon": lexicon.summary(),
        "lexicon_sha256": lexicon_sha,
        "reader_mode": _reader_summary(
            args,
            getattr(args, "reader_translations", None),
            getattr(args, "reader_coverage", None),
        ),
        "precheck": precheck.as_dict(),
        "annotation": annotation,
        "validation": validation.as_dict() if validation is not None else {
            "ok": None,
            "checks": {},
            "errors": ["未生成输出，结构检查未执行"],
            "warnings": [],
        },
        "release": gate,
        "output": {
            "path": outcome.output if outcome is not None else "",
            "sha256": (
                sha256_file(outcome.output)
                if outcome is not None and Path(outcome.output).exists()
                else ""
            ),
            "glossary": outcome.glossary_path if outcome is not None else "",
            "css": outcome.css_path if outcome is not None else "",
        },
        "input_sha256_before": sha_before,
        "input_sha256_after": sha256_file(book),
        "elapsed_sec": round(elapsed, 3),
    }
    if deliverables:
        report["deliverables"] = deliverables
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return report


def _chapter_progress(scope: dict, decisions, review, annotated=None) -> dict:
    """按逻辑章节汇总窗口状态：识别、已审、已生成分别计数。

    审查只认与本窗口哈希相同的覆盖结论；产物存在不代表已通过审查。
    """

    identified = {str(w.get("window_id")) for w in decisions.windows}
    reviewed: set[str] = set()
    for window in scope.get("windows") or []:
        wid = str(window.get("window_id"))
        items = review.windows.get(wid) or []
        if any(
            str(item.get("reviewed_window_hash") or "") == str(window.get("hash") or "")
            for item in items
        ):
            reviewed.add(wid)

    chapters: dict[tuple, dict] = {}
    for window in scope.get("windows") or []:
        wid = str(window.get("window_id"))
        key = (window.get("chapter"), window.get("chapter_label"))
        entry = chapters.setdefault(
            key,
            {
                "chapter": window.get("chapter"),
                "label": window.get("chapter_label") or "",
                "windows": 0,
                "identified": 0,
                "reviewed": 0,
            },
        )
        entry["windows"] += 1
        if wid in identified:
            entry["identified"] += 1
        if wid in reviewed:
            entry["reviewed"] += 1

    # 条目级：产物存在 ≠ 已通过审查，待审条目要单独计数
    pid_chapter: dict[str, int] = {}
    for window in scope.get("windows") or []:
        for pid in window.get("pids") or []:
            pid_chapter[pid] = window.get("chapter")
    for annotation in getattr(annotated, "annotations", []) or []:
        if annotation.action != "annotate":
            continue
        chapter = pid_chapter.get(annotation.pid)
        key = next((k for k in chapters if k[0] == chapter), None)
        entry = chapters.get(key) if key else None
        if entry is None:
            continue
        entry.setdefault("items", 0)
        entry.setdefault("passed", 0)
        entry.setdefault("pending", 0)
        entry["items"] += 1
        if annotation.review_status == "pass":
            entry["passed"] += 1
        else:
            entry["pending"] += 1

    rows = list(chapters.values())
    for row in rows:
        row.setdefault("items", 0)
        row.setdefault("passed", 0)
        row.setdefault("pending", 0)
    return {
        "chapters": rows,
        "windows": sum(row["windows"] for row in rows),
        "identified": sum(row["identified"] for row in rows),
        "reviewed": sum(row["reviewed"] for row in rows),
        "items": sum(row["items"] for row in rows),
        "passed": sum(row["passed"] for row in rows),
        "pending": sum(row["pending"] for row in rows),
        "complete": bool(rows)
        and all(row["reviewed"] == row["windows"] for row in rows)
        and all(row["pending"] == 0 for row in rows),
    }


def _apply_units(
    args,
    book: Path,
    lexicon,
    lexicon_path: Path,
    precheck,
    sha_before: str,
    lexicon_sha: str,
    grades: list[str],
    started: float,
) -> int:
    """v6：加载任务清单 → 冻结内容版本 →（可选导出审查包）→ 应用审查 → 门槛 → 出书。"""

    import os

    from .annotate_epub import choose_output_path
    from .binding import binding_path_for
    from .reading_prefs import LEVELS, effective_density, load_word_list
    from .render_units import render_units
    from .revisions import PROTOCOL
    from .tokens import TOKENIZER_VERSION
    from .units import (
        apply_review,
        build_review_package,
        collect_windows,
        evaluate_gate,
        filter_publishable,
        freeze_revisions,
        load_annotations,
        load_review,
        load_scope,
        precheck_units,
    )

    config = _config(args, grades)
    config_hash = _config_hash(config)
    scope = load_scope(args.apply_annotations)
    expected = {
        "protocol": PROTOCOL,
        "tokenizer": TOKENIZER_VERSION,
        "book_sha256": sha_before,
        "lexicon_sha256": lexicon_sha,
        "config_hash": config_hash,
        "identify_prompt": str(scope.get("identify_prompt") or ""),
        "review_prompt": str(scope.get("review_prompt") or ""),
        "context_pack_hash": str(scope.get("context_pack_hash") or ""),
    }
    decisions = load_annotations(args.apply_annotations, expected_binding=expected)
    freeze_revisions(decisions, scope)
    _log(
        args.quiet,
        f"      识别 {len(decisions.annotations)} 条"
        f"（{len(decisions.files)} 个分片，窗口覆盖 {len(decisions.windows)}）",
    )

    # 第 20 节 A 批：提示量档位与熟词名单属于展示偏好，不进入识别/审查的
    # config_hash，因此不会让已完成的审查失效；只影响本次渲染显示哪些条目。
    reading_level = args.reading_level
    word_list = load_word_list(args.word_list or None)
    density, _density_widened = effective_density(reading_level, args.density)
    for issue in word_list.issues:
        print(f"        名单：{issue}", file=sys.stderr)
    _log(
        args.quiet,
        f"      提示档位 {LEVELS.get(reading_level, reading_level)}"
        f"（{reading_level}）｜名单 已掌握 {len(word_list.mastered)} / "
        f"重点 {len(word_list.focus)}｜重复频率 {density}",
    )

    if args.emit_review:
        windows, _ = collect_windows(
            book,
            lexicon,
            window_paragraphs=int(
                scope.get("window_paragraphs") or args.window_paragraphs
            ),
            max_paragraphs=int(scope.get("max_paragraphs") or 0) or None,
            paragraph_start=int(scope.get("paragraph_start") or 0),
        )
        payloads = build_review_package(
            windows, decisions, scope, lexicon, review_round=args.review_round
        )
        target = Path(args.emit_review)
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("review-request-*.json"):
            old.unlink()
        names = []
        items = 0
        window_count = 0
        for index, payload in enumerate(payloads, start=1):
            name = f"review-request-{index:04d}.json"
            (target / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            names.append(name)
            items += sum(len(w["proposed"]) for w in payload["windows"])
            window_count += len(payload["windows"])
        (target / "review-index.json").write_text(
            json.dumps(
                {
                    "schema": "vocab-annotation-review-index/v6",
                    "protocol": PROTOCOL,
                    "task_id": scope.get("task_id", ""),
                    "context_pack_hash": scope.get("context_pack_hash", ""),
                    "review_round": args.review_round,
                    "files": names,
                    "windows": window_count,
                    "items": items,
                    "next_step": (
                        "审查者读取 review-request-*.json，写回 review-*.json；"
                        "每个 verdict 必须带 reviewed_revision_hash，"
                        "每个窗口 verdict 必须带 reviewed_window_hash"
                    ),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        _log(
            args.quiet,
            f"      审查包：{len(payloads)} 个分片 / {window_count} 个窗口"
            f" / {items} 条待审 -> {target}",
        )
        return EXIT_OK

    review = load_review(args.apply_annotations, expected_binding=expected)

    # §20.7 本地纠错：反馈只让旧审查失效并生成待复核清单，不直接改正文
    from .feedback import FeedbackSet, load_feedback, write_review_list

    feedback_set = FeedbackSet()
    if getattr(args, "feedback", ""):
        feedback_set = load_feedback(args.feedback)
        feedback_ids = feedback_set.annotation_ids()
        args.invalidate = list(
            dict.fromkeys(list(args.invalidate) + feedback_ids)
        )
        _log(
            args.quiet,
            f"      本地反馈：{len(feedback_set.items)} 条，涉及 "
            f"{len(feedback_ids)} 个条目（旧审查作废，待复核）",
        )
        for issue in feedback_set.issues[:3]:
            print(f"        反馈：{issue}", file=sys.stderr)
        if getattr(args, "feedback_review", ""):
            written = write_review_list(feedback_set, args.feedback_review)
            _log(args.quiet, f"      待复核清单：{written}")

    # §20.6 局部更新：让指定条目的旧审查失效，强制重审。
    # 只删审查结论，不改标注内容；失效条会变成 pending，被发布门槛拦住。
    if args.invalidate:
        requested = list(dict.fromkeys(args.invalidate))
        removed = [ident for ident in requested if review.verdicts.pop(ident, None) is not None]
        _log(
            args.quiet,
            f"      局部更新：{len(removed)}/{len(requested)} 条审查已失效，需重新审查",
        )
        missing_ids = [ident for ident in requested if ident not in removed]
        if missing_ids:
            _log(
                args.quiet,
                f"      注意：{len(missing_ids)} 条没有可失效的审查记录"
                f"（{', '.join(missing_ids[:3])}）",
            )

    annotated = apply_review(decisions, review)
    # 先做结构/词形/来源预校验，让门槛能看到 invalid，而不是等渲染阶段才失败
    usable, stats, _ = precheck_units(book, lexicon, annotated)
    gate = evaluate_gate(
        annotated,
        scope,
        review,
        allow_partial=bool(args.allow_partial),
        usable_count=len(usable),
    )
    gate["structural_valid"] = None
    gate["validation"] = {
        "invalid": stats["invalid"],
        "invalid_codes": dict(stats["invalid_codes"]),
        "accepted": stats["accepted"],
        "unreviewed": stats["unreviewed"],
        "pending_review": stats["pending_review"],
        "rejected_review": stats["rejected_review"],
        "stale_review": stats["stale_review"],
        "skipped_unsupported": stats["skipped_unsupported"],
    }
    # §20.6 章节进度：识别 / 已审 / 已生成分开，产物存在不等于质量通过
    gate["progress"] = _chapter_progress(scope, decisions, review, annotated)
    # §20.7 反馈明细只进报告，且明确标注「待复核」
    gate["feedback"] = {
        "summary": feedback_set.summary(),
        "items": feedback_set.items[:50],
        "status": "pending_review" if feedback_set.items else "none",
    }
    _attach_compatibility(gate, args, None)
    if gate["delivery_status"] == "blocked":
        _release_report(
            args,
            book,
            lexicon,
            lexicon_sha,
            grades,
            precheck,
            sha_before,
            outcome=None,
            validation=None,
            gate=gate,
            decisions=decisions,
            review=review,
            elapsed=time.time() - started,
        )
        detail = (
            f"缺范围 {len(gate['missing_scope'])}、待审 {len(gate['pending_review'])}、"
            f"无效 {len(gate['invalid_items'])}"
        )
        _log(args.quiet, f"      未生成成品：发布门槛未通过（{detail}）")
        for item in gate["pending_review"][:5]:
            print(
                f"        待审：{item.get('surface') or item.get('annotation_id')}"
                f"（{item.get('status')}）",
                file=sys.stderr,
            )
        for item in gate["missing_scope"][:5]:
            print(
                f"        缺失范围：{item.get('window_id')}（{item.get('reason')}）",
                file=sys.stderr,
            )
        return EXIT_BLOCKED
    if gate["delivery_status"] == "no_annotations":
        _release_report(
            args,
            book,
            lexicon,
            lexicon_sha,
            grades,
            precheck,
            sha_before,
            outcome=None,
            validation=None,
            gate=gate,
            decisions=decisions,
            review=review,
            elapsed=time.time() - started,
        )
        _log(args.quiet, "      完整审查后没有值得标注的内容：no_annotations（不复制原书）")
        return EXIT_OK
    if gate["delivery_status"] == "partial":
        annotated = filter_publishable(annotated, gate, usable)
        if not annotated.annotations:
            # 显式允许部分交付，但过滤后没有任何有效项：仍属未完成，不能出空书
            gate["delivery_status"] = "blocked"
            gate["publishable"] = False
            _release_report(
                args,
                book,
                lexicon,
                lexicon_sha,
                grades,
                precheck,
                sha_before,
                outcome=None,
                validation=None,
                gate=gate,
                decisions=decisions,
                review=review,
                elapsed=time.time() - started,
            )
            _log(args.quiet, "      --allow-partial 过滤后没有有效项：仍按 blocked 处理")
            return EXIT_BLOCKED

    out_dir = Path(args.out).expanduser() if args.out else book.parent
    final_path = choose_output_path(book, out_dir)
    if gate["delivery_status"] == "partial":
        final_path = final_path.with_name(
            f"{final_path.stem}_partial{final_path.suffix}"
        )
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    outcome = render_units(
        book,
        lexicon,
        annotated,
        density=density,
        output_path=pending_path,
        glossary=args.glossary,
        comparison_path=args.comparison or None,
        allow_partial=bool(args.allow_partial),
        reading_level=reading_level,
        word_list=word_list,
        translations=getattr(args, "reader_translations", None),
        reader_mode=_reader_mode(args),
    )
    pending_binding = binding_path_for(pending_path, pending=True)
    final_binding = binding_path_for(final_path)
    _log(args.quiet, f"[3/4] 结构校验（临时输出）：{pending_path.name}")
    validation = validate_output(
        book,
        pending_path,
        modified_paths=set(outcome.modified_paths),
        run_epubcheck=not args.no_epubcheck,
        lexicon=lexicon,
        presentation=args.presentation,
        policy=args.check_policy,
        artifact_dir=final_path.parent,
        artifact_name=final_path.name,
    )
    _attach_compatibility(gate, args, validation)
    compat = validation.compatibility or {}
    compat_blocked = not compat.get("publishable", True) or bool(
        (compat.get("issues") or {}).get("blocking")
    )
    if not validation.ok or compat_blocked:
        pending_path.unlink(missing_ok=True)
        pending_binding.unlink(missing_ok=True)
        gate["structural_valid"] = bool(validation.ok)
        gate["publishable"] = False
        _release_report(
            args,
            book,
            lexicon,
            lexicon_sha,
            grades,
            precheck,
            sha_before,
            outcome=outcome,
            validation=validation,
            gate=gate,
            decisions=decisions,
            review=review,
            elapsed=time.time() - started,
        )
        _log(args.quiet, "      校验未通过（结构 / 兼容判定），已删除临时输出，不提交成品")
        _log(args.quiet, f"      {compat.get('summary', '')}")
        return EXIT_VALIDATION
    os.replace(pending_path, final_path)
    if pending_binding.exists():
        # §22.4：映射与 EPUB 一起原子提交；校验失败时两者一起删除。
        os.replace(pending_binding, final_binding)
        if validation.binding and validation.binding.get("present"):
            validation.binding["path"] = str(final_binding)
    outcome.output = str(final_path)
    gate["structural_valid"] = True
    gate["publishable"] = gate["delivery_status"] == "complete" and bool(
        compat.get("publishable")
    )
    deliverables = {
        "epub": {
            "requested": True,
            "ok": True,
            "path": str(final_path),
            "sha256": sha256_file(final_path),
        }
    }
    if args.html is not None:
        deliverables["html"] = _build_html(args, final_path)
    report = _release_report(
        args,
        book,
        lexicon,
        lexicon_sha,
        grades,
        precheck,
        sha_before,
        outcome=outcome,
        validation=validation,
        gate=gate,
        decisions=decisions,
        review=review,
        elapsed=time.time() - started,
        deliverables=deliverables,
    )
    _log(args.quiet, f"[3/4] 校验：{final_path}")
    _log(args.quiet, f"[4/4] 报告：{args.report}")
    _log(
        args.quiet,
        f"      状态 {report['release']['delivery_status']}"
        f"｜范围完整 {report['release']['scope_complete']}"
        f"｜审查完整 {report['release']['review_complete']}"
        f"｜publishable {report['release']['publishable']}"
        f"｜策略 {args.check_policy}"
        f"｜{gate['compatibility']['delivery_kind']}",
    )
    # §22.6：默认只给读者一句话，不逐项弹确认、不在词注里加技术告警
    _log(args.quiet, f"      {compat.get('summary', '')}")
    html_info = deliverables.get("html")
    if html_info is not None and not html_info.get("ok"):
        _log(
            args.quiet,
            "      EPUB 已交付；阅读页未生成（见上方原因），可修好后单独重跑阅读页",
        )
        return EXIT_PARTIAL
    return EXIT_OK if gate["delivery_status"] == "complete" else EXIT_PARTIAL


def _assert_own_emit_dir(target: Path) -> None:
    """v6 导出目录的外来文件守卫（关3 压测补齐）。

    legacy `--emit-requests` 走 `_clear_emit_dir()`（会删旧产物、遇外来文件拒绝），
    v6 `--emit-annotations` 之前没有这道守卫，产物会直接混进用户目录。
    这里**只检查、不删除**：出现非本任务产物就拒绝，让用户换目录或先移走。
    """

    known_files = {"scope.json", "index.json"}
    known_prefixes = ("windows-", "ann-", "review-")
    known_dirs = {"review_requests"}

    foreign: list[str] = []
    for child in target.iterdir():
        name = child.name
        if child.is_dir() and name in known_dirs:
            continue
        if child.is_file() and (
            name in known_files or name.startswith(known_prefixes)
        ):
            continue
        foreign.append(name)
    if foreign:
        raise EpubError(
            f"{target} 里还有非本程序导出的文件（{'、'.join(sorted(foreign)[:5])}），"
            "为避免与识别产物混淆已停止。请换一个空目录，或先把这些文件移走再重跑。"
        )


def _clear_emit_dir(target: Path) -> None:
    """清理上一次导出的产物，但**不碰用户的其它文件**。

    安全红线：不得对用户给的路径做无条件递归删除。这里只认本程序写过的
    文件名（`windows-*` / `scope.json` / `index.json` / `ann-*` / `review-*`，
    以及 `review_requests/` 子目录），只要目录里还躺着别的文件就拒绝清理并报错，
    让用户自己决定——误删用户资料是不可逆的。
    """

    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return
    if not target.is_dir():
        raise EpubError(f"--emit-annotations 的目标不是目录：{target}")

    known_files = ("scope.json", "index.json")
    known_prefixes = ("windows-", "ann-", "review-")
    known_dirs = ("review_requests",)

    stale: list[Path] = []
    foreign: list[str] = []
    for child in target.iterdir():
        name = child.name
        if child.is_dir() and name in known_dirs:
            stale.append(child)
        elif child.is_file() and (
            name in known_files or name.startswith(known_prefixes)
        ):
            stale.append(child)
        else:
            foreign.append(name)

    if foreign:
        raise EpubError(
            f"{target} 里还有非本次导出的文件（{'、'.join(sorted(foreign)[:5])}），"
            "为避免误删已停止清理。请换一个空目录，或先把这些文件移走再重跑。"
        )
    for item in stale:
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    target.mkdir(parents=True, exist_ok=True)


def _emit(args, book: Path, lexicon, lexicon_path: Path, precheck, lexicon_sha: str) -> int:
    options = _options(args, _split_grades(args.grades))
    _log(args.quiet, "[2/3] 扫描候选（不写出 EPUB）")
    candidates, meta = collect_candidates(book, lexicon, options)
    if not candidates:
        _log(args.quiet, "      没有需要消歧的候选，可以直接不带 --apply-decisions 运行")
        return EXIT_OK

    target = Path(args.emit_requests)
    _clear_emit_dir(target)

    size = max(1, args.batch_size)
    parts = [candidates[i : i + size] for i in range(0, len(candidates), size)]
    index_rows = []
    config = _config(args, _split_grades(args.grades))
    config_hash = _config_hash(config)
    for number, chunk in enumerate(parts, start=1):
        if args.presentation == "inline-notes":
            from .inline_notes import request_payload_v2

            payload = request_payload_v2(
                chunk,
                book_title=meta.get("title", ""),
                book_sha256=precheck.sha256,
                lexicon_sha256=lexicon_sha,
                config=config,
                config_hash=config_hash,
                part=number,
                parts=len(parts),
                min_confidence=args.min_confidence,
                max_context_chars=args.max_context_chars,
            )
        else:
            payload = request_payload(
                chunk,
                book_title=meta.get("title", ""),
                book_sha256=precheck.sha256,
                lexicon_sha256=lexicon_sha,
                part=number,
                parts=len(parts),
                min_confidence=args.min_confidence,
                max_context_chars=args.max_context_chars,
            )
        file = target / f"part-{number:04d}.json"
        file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        index_rows.append({"file": file.name, "candidates": len(chunk)})

    (target / "index.json").write_text(
        json.dumps(
            {
                "schema": "vocab-disambiguation-request-index/v1",
                "book": {"title": meta.get("title", ""), "sha256": precheck.sha256},
                "chapters_scanned": meta.get("chapters_scanned", 0),
                "density": args.density,
                "protocol": "v2" if args.presentation == "inline-notes" else "v1",
                "config": config,
                "config_hash": config_hash,
                "total_candidates": len(candidates),
                "parts": index_rows,
                "next_step": (
                    "逐个 part 判断后写出同结构 decisions JSON（含 schema、"
                    "book_sha256、lexicon_sha256、decisions 数组），"
                    "再运行 --apply-decisions <目录>"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _log(
        args.quiet,
        f"[3/3] 已产出 {len(parts)} 个请求分片 / {len(candidates)} 个候选 -> {target}",
    )
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    started = time.time()
    book = Path(args.book).expanduser()
    lexicon_path = (
        Path(args.lexicon).expanduser() if args.lexicon else DEFAULT_LEXICON
    )
    if not lexicon_path.is_file():
        raise LexiconError(
            f"未找到词库：{lexicon_path}。请确认随包 assets/lexicon.vault 存在，"
            "或用 --lexicon 指定自己的词表 CSV。"
        )
    grades = _split_grades(args.grades)

    total = 3 if args.emit_requests else 4
    _log(args.quiet, f"[1/{total}] 预检：{book}")
    lexicon = load_lexicon(
        lexicon_path, grades or None, label_order=args.label_order or None
    )
    precheck = inspect_book(book, lexicon, reduce_forms=not args.no_reduce)
    sha_before = precheck.sha256
    _log(
        args.quiet,
        f"      结构 {precheck.version or '未知'}｜章节 {precheck.chapters_total}"
        f"｜可见词 {precheck.tokens_total}｜候选 {precheck.candidates_estimate_total}",
    )
    if precheck.already_annotated_files:
        raise AlreadyAnnotatedError(
            f"输入已包含本技能标注（{precheck.already_annotated_files} 个章节），"
            "拒绝重复处理；请使用原始文件。"
        )
    if precheck.unprocessable_chapters:
        detail = "；".join(
            f"{item['path']}（{item['reason']}）"
            for item in precheck.unprocessable_chapters[:3]
        )
        raise EpubError(f"存在无法处理的章节，按约定整体失败：{detail}")

    _log(args.quiet, f"[2/{total}] 词表：{len(lexicon)} 词条 / {lexicon.heads} 词头")
    # 按词库内容（解封后的 CSV 字节）绑定，不按容器文件字节：
    # 容器随机 nonce 会让重新封装改变文件哈希，但词库内容没变。
    lexicon_sha = lexicon_content_sha256(lexicon_path)

    if getattr(args, "emit_translations", ""):
        from .reader_mode import SCHEMA_REQUEST

        target = Path(args.emit_translations).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        payload = build_translation_request(book, sha_before)
        (target / "translation-request.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _log(
            args.quiet,
            f"      已导出 {payload['paragraph_count']} 个自然段的译文请求"
            f"（{SCHEMA_REQUEST}）→ {target / 'translation-request.json'}",
        )
        return EXIT_OK

    if args.emit_annotations:
        return _emit_units(args, book, lexicon, precheck, lexicon_sha)

    if args.emit_requests:
        return _emit(args, book, lexicon, lexicon_path, precheck, lexicon_sha)

    mode = _reader_mode(args)
    if mode == READER_MODE_UNSPECIFIED and not getattr(args, "require_reader_mode", False):
        _log(
            args.quiet,
            "      提醒：本次未确认阅读档位（调试/兼容路径）；正式出书须由宿主先问"
            "「初级（带中文译文）」还是「高级（不带）」。",
        )
    try:
        _prepare_reader_mode(args, book, sha_before)
    except ReaderModeError as exc:
        print(f"未生成成品：{exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.apply_annotations:
        return _apply_units(
            args, book, lexicon, lexicon_path, precheck, sha_before,
            lexicon_sha, grades, started,
        )

    if mode == "basic":
        print(
            "未生成成品：初级（带中文译文）只在完整链路（识别 → 独立审查 → 出书）可用；"
            "词表直注路径不交付初级成品。",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    options = _options(args, grades)
    if args.apply_decisions:
        options.decisions = _load_decisions(args, book, lexicon_path, precheck)

    outcome = annotate_epub(book, lexicon, options, out_dir=args.out or None)
    if outcome.as_dict().get("total_matches", 0) == 0:
        # 不交付空书（2026-11-06 实测：默认 inline-notes 无宿主判断会 0 标注
        # 却报「已完成」）。按**实际产出**判定，不靠参数猜测——任何能出标注的
        # 合法路径（footnote / inline / --undecided lexicon / apply-decisions）
        # 都不受影响。删除刚写出的产物后给可执行的二选一指引。
        from .binding import binding_path_for

        for path in (
            Path(outcome.output),
            binding_path_for(Path(outcome.output), pending=True),
        ):
            if path.is_file():
                path.unlink()
        print(
            "未生成成品：本次链路产出 0 处标注，已删除空标注产物，源文件未修改。\n"
            "  常见原因：默认「语境短注 + 章末词条」需要 AI 助手完成语境判断，\n"
            "            单独直接运行不会写入标注。请二选一：\n"
            "  A（推荐）在 WorkBuddy / Codex 等助手里说「把这本书做成伴读版」，\n"
            "           助手会按 SKILL.md 自动完成识别、独立审查与出书。\n"
            "  B（立即出书）加 --presentation inline 或 --presentation footnote，\n"
            "           或加 --undecided lexicon 使用词表直注；不做语境消歧、标注较密。",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    return _finish(
        args, book, lexicon, lexicon_path, precheck, sha_before, outcome,
        lexicon_sha, grades, started,
    )


def _finish(args, book, lexicon, lexicon_path, precheck, sha_before, outcome,
            lexicon_sha, grades, started) -> int:
    import os

    from .binding import binding_path_for

    _log(args.quiet, f"[3/4] 校验：{outcome.output}")
    if sha256_file(book) != sha_before:
        raise EpubError("源文件在处理过程中被改动（SHA-256 不一致），请检查磁盘/权限")
    validation = validate_output(
        book,
        outcome.output,
        modified_paths=set(outcome.modified_paths),
        run_epubcheck=not args.no_epubcheck,
        lexicon=lexicon,
        presentation=args.presentation,
        policy=args.check_policy,
        artifact_dir=Path(outcome.output).parent,
    )
    compat = validation.compatibility or {}
    ok = validation.ok and bool(compat.get("publishable", True))
    # 直注档也写出与主链路同形的 release 摘要，宿主按编排契约读
    # release.publishable 时不会因路径不同而拿不到结论。
    gate_summary: dict = {}
    _attach_compatibility(gate_summary, args, validation)
    release = {
        "mode": "direct",
        "publishable": bool(ok),
        "check_policy": gate_summary["check_policy"],
        "official_check": gate_summary["official_check"],
        "compatibility": gate_summary["compatibility"],
    }
    legacy_pending_binding = binding_path_for(Path(outcome.output), pending=True)
    if validation.ok and legacy_pending_binding.exists():
        final_binding = binding_path_for(Path(outcome.output))
        os.replace(legacy_pending_binding, final_binding)
        if validation.binding and validation.binding.get("present"):
            validation.binding["path"] = str(final_binding)

    report = {
        "skill": SKILL_NAME,
        "stage": STAGE,
        "matching_mode": MATCHING_MODE,
        "timestamp": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "grades_requested": grades,
        "grades_applied": (
            f"{lexicon.rows_total} -> {len(lexicon)} 词条" if grades else "全部词条"
        ),
        "lexicon": lexicon.summary(),
        "lexicon_sha256": lexicon_sha,
        "reader_mode": _reader_summary(
            args,
            getattr(args, "reader_translations", None),
            getattr(args, "reader_coverage", None),
        ),
        "precheck": precheck.as_dict(),
        "annotation": outcome.as_dict(),
        "validation": validation.as_dict(),
        "check_policy": args.check_policy,
        "compatibility": compat,
        "release": release,
        "output": {
            "path": outcome.output,
            "sha256": sha256_file(outcome.output)
            if Path(outcome.output).exists()
            else "",
            "glossary": outcome.glossary_path,
            "css": outcome.css_path,
        },
        "input_sha256_before": sha_before,
        "input_sha256_after": sha256_file(book),
        "elapsed_sec": round(time.time() - started, 3),
    }
    deliverables = {
        "epub": {
            "requested": True,
            "ok": Path(outcome.output).is_file(),
            "path": outcome.output,
            "sha256": sha256_file(outcome.output)
            if Path(outcome.output).is_file()
            else "",
        }
    }
    if args.html is not None and Path(outcome.output).is_file():
        deliverables["html"] = _build_html(args, Path(outcome.output))
    report["deliverables"] = deliverables

    report_path = Path(args.report).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _log(args.quiet, f"[4/4] 报告：{report_path}")
    _print_summary(report, quiet=args.quiet)

    if not ok:
        return EXIT_VALIDATION
    html_info = deliverables.get("html")
    if html_info is not None and not html_info.get("ok"):
        _log(args.quiet, "      EPUB 已交付；阅读页未生成（见上方原因）")
        return EXIT_PARTIAL
    return EXIT_OK


def _print_summary(report: dict, *, quiet: bool) -> None:
    annotation = report["annotation"]
    validation = report["validation"]
    disambiguation = annotation.get("disambiguation", {})
    checks = validation.get("checks", {})
    # §22.6：epubcheck 是官方维度，单独一行如实显示；内部硬校验不因它变 False
    failed = [
        name for name, value in checks.items() if value is False and name != "epubcheck"
    ]
    if quiet:
        print(report["output"]["path"], flush=True)
        return
    applied = disambiguation.get("decisions_applied", 0)
    pending = disambiguation.get("candidates", 0)
    sources = ""
    if disambiguation.get("mode") == "harness-v5":
        sources = (
            f"\n  来源      整体命中 {disambiguation.get('exact_entry', 0)}"
            f"｜仅成员命中 {disambiguation.get('component_entries', 0)}"
            f"｜未收录 {disambiguation.get('out_of_lexicon', 0)}"
            f"｜被筛选 {disambiguation.get('filtered_out', 0)}"
            f"｜组合 {disambiguation.get('merged_units', 0)}"
            f"\n  校验      invalid {disambiguation.get('invalid', 0)}"
            f"｜跳过(跨标签) {disambiguation.get('skipped_unsupported', 0)}"
            f"｜可发布 {'是' if disambiguation.get('publishable') else '否'}"
        )
    elif disambiguation.get("mode") == "harness-v4":
        sources = (
            f"\n  来源      整体命中 {disambiguation.get('exact_entry', 0)}"
            f"｜仅成员命中 {disambiguation.get('component_entries', 0)}"
            f"｜未收录 {disambiguation.get('out_of_lexicon', 0)}"
            f"｜被筛选 {disambiguation.get('filtered_out', 0)}"
            f"｜组合 {disambiguation.get('merged_units', 0)}"
        )
    compat = report.get("compatibility") or {}
    epub_label = _epubcheck_label(validation.get("epubcheck"))
    if compat.get("publishable") and not (compat.get("issues") or {}).get("new"):
        counts = (validation.get("epubcheck") or {}).get("counts") or {}
        n_error = counts.get("nError", 0)
        if n_error:
            epub_label = (
                f"原书遗留 {n_error} 处格式瑕疵，不影响阅读（已保留，详见报告）"
            )
    lines = [
        "已完成：源文件未修改，输出为新文件。",
        f"  输出      {report['output']['path']}",
        f"  标注      {annotation['total_matches']} 处 / "
        f"{len(annotation['per_entry'])} 个词条 / "
        + (
            f"{annotation['total_footnotes']} 条脚注"
            if annotation.get("total_footnotes")
            else "正文括注"
        )
        + f"（密度 {annotation.get('density', '-')}"
        + (
            f" / {disambiguation.get('segment_words')} 词"
            if disambiguation.get("segment_words")
            else ""
        )
        + "）",
        f"  消歧      待判定 {pending}｜已应用 {applied}"
        + (f"｜模式 {disambiguation['mode']}" if disambiguation.get("mode") else "")
        + sources,
        f"  内部校验  {'通过' if validation['ok'] else '未通过'}"
        + (f"（失败项：{', '.join(failed)}）" if failed else ""),
        f"  格式检查  {epub_label}",
    ]
    if compat:
        issues = compat.get("issues") or {}
        if compat.get("publishable") and not issues.get("new"):
            if compat.get("external_validation_incomplete"):
                delivery = "可交付：内部校验通过；官方格式检查未运行（详见报告）"
            else:
                delivery = "可交付：内容完整，未引入新的格式问题"
        elif compat.get("publishable"):
            delivery = "可交付，但有需要留意的问题（详见报告）"
        else:
            delivery = "未交付：" + (compat.get("summary") or "校验未通过，详见报告")
        lines.append(f"  交付      {delivery}")
    found = report.get("deliverables") or {}
    epub = found.get("epub")
    if epub and epub.get("ok"):
        lines.append(f"  成品      EPUB  {epub['path']}")
    html = found.get("html")
    if html is not None:
        if html.get("ok"):
            lines.append(
                f"  成品      阅读页 {html['path']}（{html.get('sections', 0)} 节）"
            )
        else:
            reason = str(html.get("error", "未知原因")).splitlines()[0]
            lines.append(f"  成品      阅读页未生成（{reason}）")
    print("\n".join(lines), flush=True)


def _epubcheck_label(info: dict | None) -> str:
    if not info:
        return "未运行"
    if not info.get("ran"):
        return f"未运行（{info.get('reason', '不可用')}）"
    if info.get("ok"):
        return "通过"
    counts = info.get("counts") or {}
    detail = "、".join(f"{key}={value}" for key, value in counts.items() if value)
    return f"报告错误（{detail}）" if detail else "报告错误"


def _normalize_paths(args) -> None:
    """输出统一落到独立任务目录：默认书旁 `<书名>_伴读/`，且自动创建。

    不写进技能安装目录、也不写进当前工作目录；``--out`` 给了不存在的目录时
    直接创建，避免用户先手工建目录。
    """

    if args.out:
        out_dir = Path(args.out).expanduser()
    else:
        book = Path(args.book).expanduser()
        out_dir = book.parent / f"{book.stem}_伴读"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建输出目录 {out_dir}：{exc}") from exc
    args.out = str(out_dir)
    if not args.report:
        args.report = str(out_dir / "vocab_task_report.json")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_policy == "strict" and args.no_epubcheck:
        print(
            "参数冲突：--check-policy strict 要求 EPUBCheck 实际运行，"
            "不能同时指定 --no-epubcheck。",
            file=sys.stderr,
        )
        return EXIT_INPUT
    _normalize_paths(args)
    try:
        return run(args)
    except AlreadyAnnotatedError as exc:
        print(f"拒绝处理：{exc}", file=sys.stderr)
        return EXIT_ALREADY_ANNOTATED
    except (EpubError, LexiconError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    except KeyboardInterrupt:
        print("已中断；源文件未改动。", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001
        print(f"内部错误：{exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

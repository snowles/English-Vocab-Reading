"""阶段 3/4：Markdown / TXT / 可提取文本 PDF -> 中间 EPUB。

**阶段 1 未实现**。本模块只提供接口与明确的报错，避免调用方以为功能可用。

阶段 1 的处理链路是「直接编辑原 EPUB 包内 XHTML」，任何需要先转换格式的来源都不在
本阶段范围内；转换会带来格式损失，且不承诺复刻 PDF 固定版式。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["convert_source", "SUPPORTED_SOURCES", "PLANNED_STAGES"]

SUPPORTED_SOURCES = {".md": 3, ".markdown": 3, ".txt": 3, ".pdf": 4}

PLANNED_STAGES = {
    3: "Markdown / TXT 导入：语法解析 -> 中间 EPUB（标题、段落、链接、本地图片）",
    4: "可提取文本 PDF 导入：封装 PDF 转 EPUB，明确重排版预期，不做 OCR",
}


class NotSupportedInStage1(NotImplementedError):
    """该输入格式需要阶段 3/4 的转换能力。"""


def convert_source(path: str | Path, *, stage: int = 1) -> Path:
    """把非 EPUB 来源转换为中间 EPUB。

    阶段 1 一律抛 `NotSupportedInStage1`，并在消息中说明属于哪个阶段。
    """

    suffix = Path(path).suffix.lower()
    planned = SUPPORTED_SOURCES.get(suffix)
    if planned is None:
        raise NotSupportedInStage1(
            f"不支持的输入格式：{suffix or '未知'}；阶段 1 仅支持 EPUB 2/3"
        )
    reason = PLANNED_STAGES.get(planned, "")
    raise NotSupportedInStage1(
        f"{suffix} 导入属于阶段 {planned}（{reason}），"
        f"阶段 1（no-model 离线原型）尚未实现。请先提供 EPUB 文件。"
    )

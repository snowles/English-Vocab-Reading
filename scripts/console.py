"""CLI 输出编码契约（§P0-2）。

背景：Windows 上 Python 把**被重定向**的 stdout/stderr 按本地代码页
（简中环境通常是 cp936）编码。宿主或测试按 UTF-8 读取时，中文提示就会变成
乱码甚至解码失败——表现成「0 处标注」这类断言误报，而不是真实缺陷。

这里的约定只作用于当前进程，不修改系统区域设置、不改用户环境变量：

* 输出被重定向（宿主捕获、管道、写日志）→ 一律 UTF-8；
* 直接连终端（isatty）→ 保持终端自身编码，避免 cp936 控制台出现乱码。

宿主捕获时按 UTF-8 解码即可，与 ``PYTHONIOENCODING=utf-8`` 等价，
但不要求用户为我们的脚本去改全局环境。
"""

from __future__ import annotations

import sys

__all__ = ["configure_stdio"]


def configure_stdio() -> None:
    """按输出目标统一 stdout/stderr 编码，幂等且失败静默。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None:
                continue
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding == "utf8":
                continue
            if stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue

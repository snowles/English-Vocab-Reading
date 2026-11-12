"""测试辅助：把项目根目录加入 sys.path，并提供共享 fixture 加载器。

不依赖当前工作目录，也不依赖网络。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.binding import binding_path_for, load_binding  # noqa: E402
from scripts.load_lexicon import Lexicon, load_lexicon  # noqa: E402


def load_binding_for(epub_path) -> dict:
    """§22.4：内部标注属性已移到包外映射，测试从映射核对而非从 XHTML 找。"""

    path = Path(epub_path)
    for candidate in (binding_path_for(path), binding_path_for(path, pending=True)):
        if candidate.is_file():
            return load_binding(candidate)
    raise AssertionError(f"找不到 {path} 的 vocab-reader-map 映射")


def binding_html(epub_path) -> str:
    """把映射属性渲染成 ``data-xxx="值"`` 文本，便于沿用原有断言。

    无标注的产物没有映射文件，此时返回空串（不视为错误）。
    """

    path = Path(epub_path)
    if not (
        binding_path_for(path).is_file()
        or binding_path_for(path, pending=True).is_file()
    ):
        return ""
    payload = load_binding_for(epub_path)
    return "\n".join(
        f'{key}="{value}"'
        for record in payload["attrs"].values()
        for key, value in record.items()
        if key.startswith("data-")
    )


def load_sample_lexicon(**kwargs) -> Lexicon:
    return load_lexicon(FIXTURES / "lexicon_sample.csv", **kwargs)


def load_tiny_lexicon(*rows: str) -> Lexicon:
    """构造只含指定词条的词表，便于隔离边界用例。

    每行格式：`单词,等级,词性,释义`。
    """

    from scripts.load_lexicon import load_lexicon_bytes

    header = "单词,单词等级,词性,中文释义\n"
    lines = []
    for row in rows:
        parts = [p.strip() for p in row.split(",")]
        while len(parts) < 4:
            parts.append("")
        lines.append(",".join(parts))
    return load_lexicon_bytes((header + "\n".join(lines) + "\n").encode("utf-8"))

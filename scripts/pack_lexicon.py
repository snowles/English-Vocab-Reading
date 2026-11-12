"""开发期工具：把明文 CSV 打包成随包词库容器。

    python -m scripts.pack_lexicon --csv <原始.csv> --out assets/lexicon.vault

原始明文 CSV 只留在开发目录，**不要放进分发包**。
容器是纯文本，运行时由 load_lexicon.py 自动嗅探解封，用户无需任何配置。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/pack_lexicon.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from .console import configure_stdio  # noqa: E402
from .lexicon_vault import (  # noqa: E402
    SCHEMA,
    VaultError,
    pack_file,
    unpack_bytes,
)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        prog="pack-lexicon",
        description="把明文词表 CSV 封装成随包词库容器（防顺手查看，不防有动机者）",
    )
    parser.add_argument("--csv", required=True, help="开发目录中的明文 CSV")
    parser.add_argument(
        "--out", default="assets/lexicon.vault", help="输出容器路径（纯文本）"
    )
    args = parser.parse_args(argv)
    try:
        target, size = pack_file(args.csv, args.out)
        raw = Path(args.csv).read_bytes()
        if unpack_bytes(target.read_bytes()) != raw:
            raise VaultError("往返校验不一致")
    except (VaultError, OSError) as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 2
    print(f"已生成 {target}（{SCHEMA}）")
    print(
        f"  源词表 {size} bytes / SHA-256 "
        f"{hashlib.sha256(raw).hexdigest()[:16]}…"
    )
    print(f"  容器 {target.stat().st_size} bytes；往返校验通过")
    print("  提醒：原始明文 CSV 不要放进分发包；分发前确认词表再分发许可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

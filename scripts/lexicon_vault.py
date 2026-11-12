"""词库轻量封装：随包分发、零配置、防顺手查看。

用户口径（2026-11-06）：只防君子不防小人；词表随包、零配置；
稍微设个门槛即可；本意是让大家更好地学习，产物完整词条必须保留。

本模块做到：
  * 明文 CSV 不直接出现在分发包里——双击 / 文本搜索看不到词条；
  * 运行零配置：不需要密码、不联网、不激活、不新增第三方依赖（只用标准库）；
  * HMAC-SHA256 完整性校验：容器被改动会明确报错，不静默产出坏词库；
  * 容器本身是**纯文本**（magic 行 + base64），可打包、可 diff、可审阅。

如实边界（不得对外宣称更强）：
  * 密钥由源码中的分散片段派生，能读源码的人仍可还原；
  * 这是**门槛**，不是密码学保密，不防有动机的攻击者；
  * 不防内存截获，也不防从产物章末词条反推词库——后者由产品口径决定保留。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

__all__ = [
    "VaultError",
    "SCHEMA",
    "MAGIC",
    "is_vault",
    "pack_bytes",
    "unpack_bytes",
    "pack_file",
]

SCHEMA = "vocab-lexicon-vault/v1"
MAGIC = b"VOCABLEX1"
_NONCE_LEN = 16
_TAG_LEN = 32
_LINE_WIDTH = 96
_SALT = b"vocab-reading|study|v1"
# 分散片段：源码里不出现一整串可直接复制的密钥
_PARTS = (
    b"lexicon",
    b"companion",
    b"reader",
    b"local-only",
)


class VaultError(Exception):
    """词库容器无法解封或完整性校验失败。"""


def _key() -> bytes:
    return hashlib.sha256(_SALT + b"|" + b"|".join(_PARTS)).digest()


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(
            hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        )
        counter += 1
    return bytes(out[:size])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def pack_bytes(data: bytes) -> str:
    """把词表 CSV 字节封装成纯文本容器。"""

    key = _key()
    nonce = os.urandom(_NONCE_LEN)
    cipher = _xor(data, _keystream(key, nonce, len(data)))
    body = nonce + cipher
    tag = hmac.new(key, MAGIC + body, hashlib.sha256).digest()
    payload = base64.b64encode(body + tag).decode("ascii")
    lines = [MAGIC.decode("ascii")]
    lines.extend(
        payload[i : i + _LINE_WIDTH] for i in range(0, len(payload), _LINE_WIDTH)
    )
    return "\n".join(lines) + "\n"


def is_vault(data: bytes) -> bool:
    """按文件头判断是否为词库容器（明文 CSV 永远返回 False）。"""

    return data[: len(MAGIC)] == MAGIC


def unpack_bytes(data: bytes) -> bytes:
    """解封词库容器；校验失败抛 VaultError，绝不返回部分内容。"""

    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise VaultError(f"容器不是 {SCHEMA} 文本：{exc}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != MAGIC.decode("ascii"):
        raise VaultError(f"容器头不匹配，期望 {SCHEMA}")
    try:
        raw = base64.b64decode("".join(lines[1:]), validate=True)
    except Exception as exc:  # noqa: BLE001 - base64 异常类型跨版本不一致
        raise VaultError(f"容器内容损坏：{exc}") from exc
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise VaultError("容器过短，可能已被截断")
    body, tag = raw[:-_TAG_LEN], raw[-_TAG_LEN:]
    key = _key()
    expect = hmac.new(key, MAGIC + body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise VaultError("完整性校验失败：词库容器可能被改动")
    nonce, cipher = body[:_NONCE_LEN], body[_NONCE_LEN:]
    return _xor(cipher, _keystream(key, nonce, len(cipher)))


def pack_file(csv_path: str | Path, out_path: str | Path) -> tuple[Path, int]:
    """开发期：把明文 CSV 打包成随包容器，返回 (容器路径, 源字节数)。"""

    src = Path(csv_path)
    if not src.is_file():
        raise VaultError(f"源词表不存在：{src}")
    data = src.read_bytes()
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pack_bytes(data), encoding="ascii", newline="\n")
    return target, len(data)

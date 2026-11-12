"""H2 契约：用户自备词表的列名模板 ↔ 脚本解析列名逐字对齐。

模板是给用户抄的。列名一旦和 `COLUMN_ALIASES` 脱节，用户照着填回去就被判
「词表缺少必需列」——这是典型的假 ERROR，也是 H2 要防的东西。

这里钉住四件事：模板随包存在、每个表头列名都能被解析器识别、必需列齐全、
模板本身能被完整加载（带 BOM，Windows Excel 打开不乱码）。
"""

from __future__ import annotations

import unittest

from tests._bootstrap import ROOT

from scripts.load_lexicon import (
    COLUMN_ALIASES,
    REQUIRED_COLUMNS,
    load_lexicon,
    normalize_key,
)

TEMPLATE = ROOT / "assets" / "lexicon-template.csv"


def _header() -> list[str]:
    text = TEMPLATE.read_bytes().decode("utf-8-sig")
    return [cell.strip() for cell in text.splitlines()[0].split(",")]


def _canonical(name: str) -> str | None:
    """把一个表头列名映射回 `COLUMN_ALIASES` 的规范键，识别不了返回 None。"""

    key = normalize_key(name)
    for canonical, aliases in COLUMN_ALIASES.items():
        if key == normalize_key(canonical):
            return canonical
        if any(key == normalize_key(alias) for alias in aliases):
            return canonical
    return None


class TemplateContractTests(unittest.TestCase):
    def test_template_ships_in_release(self) -> None:
        self.assertTrue(TEMPLATE.is_file(), "assets/lexicon-template.csv 缺失")
        manifest = (ROOT / "scripts" / "pack_release.py").read_text(encoding="utf-8")
        self.assertIn(
            "assets/lexicon-template.csv", manifest, "模板没进发布清单，用户拿不到"
        )

    def test_every_header_column_is_recognized(self) -> None:
        for name in _header():
            self.assertIsNotNone(
                _canonical(name),
                f"模板列名 {name!r} 不在 COLUMN_ALIASES 里——用户照抄会被判缺少列",
            )

    def test_required_columns_present(self) -> None:
        mapped = {_canonical(name) for name in _header()}
        for need in REQUIRED_COLUMNS:
            self.assertIn(need, mapped, f"模板缺必需列 {need}")

    def test_template_loads_end_to_end(self) -> None:
        lexicon = load_lexicon(TEMPLATE)
        self.assertEqual(len(lexicon.entries), 2, "模板示例行应能被完整加载")
        first = lexicon.entries[0]
        self.assertEqual(first.head, "abandon")
        self.assertTrue(first.gloss, "释义列没被解析出来")
        self.assertEqual(first.example, "He abandoned the plan.")
        self.assertTrue(first.phonetic, "音标列没被解析出来")
        self.assertIn("四级", first.grades)

    def test_has_utf8_bom(self) -> None:
        self.assertTrue(
            TEMPLATE.read_bytes().startswith(b"\xef\xbb\xbf"),
            "无 BOM 时 Windows Excel 打开会显示乱码，用户会以为模板坏了",
        )


if __name__ == "__main__":
    unittest.main()

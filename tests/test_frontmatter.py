"""SKILL.md frontmatter 的上架必填字段（关2 闸门 F1/F7/P3 的防回归断言）。

平台按角色区分严格度：独立技能包的 SKILL.md 是**严格**的，缺字段直接判解析失败。
这里把「易漏的字段」变成单测，避免下次改文档时又漏回去。
"""

from __future__ import annotations

import unittest

from tests._bootstrap import ROOT

from scripts.pack_release import (
    NAME,
    REQUIRED_FRONTMATTER,
    VERSION,
    check_frontmatter,
    read_frontmatter,
)

REQUIRED = (
    "name",
    "display_name",
    "display_name_en",
    "description",
    "description_zh",
    "description_en",
    "version",
    "author",
    "category",
    "allowed-tools",
)


class FrontmatterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fm = read_frontmatter(ROOT / "SKILL.md")

    def test_parsed(self) -> None:
        self.assertTrue(self.fm, "SKILL.md frontmatter 读不出来")

    def test_required_fields_present_and_non_empty(self) -> None:
        for key in REQUIRED:
            self.assertIn(key, self.fm, f"缺字段 {key}")
            self.assertTrue(self.fm[key].strip(), f"字段 {key} 为空")

    def test_gate_list_matches_test_list(self) -> None:
        # 打包脚本与单测的必填清单必须同源，避免两处漂移
        self.assertEqual(tuple(REQUIRED), tuple(REQUIRED_FRONTMATTER))

    def test_name_and_version_in_sync(self) -> None:
        self.assertEqual(self.fm["name"], NAME)
        self.assertEqual(self.fm["version"], VERSION, "SKILL.md 版本与 pack_release 分叉")

    def test_version_is_semver(self) -> None:
        parts = self.fm["version"].split(".")
        self.assertEqual(len(parts), 3, "版本必须是 x.y.z")
        for part in parts:
            self.assertTrue(part.isdigit(), f"版本段不是数字：{part}")

    def test_author_not_placeholder(self) -> None:
        author = self.fm["author"].strip()
        self.assertNotIn(author.lower(), {"", "todo", "tbd", "your name", "author", "unknown"})

    def test_allowed_tools_allows_bash(self) -> None:
        tools = self.fm["allowed-tools"]
        self.assertIn("Read", tools)
        self.assertIn("Bash", tools, "包内有 scripts/，不放行 Bash 运行期调不动")

    def test_no_ascii_quote_escape_needed(self) -> None:
        """值用单引号整体包裹：含冒号的字段不能裸写，否则平台 Go-YAML 报 mapping values。"""
        raw = (ROOT / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        for line in raw.splitlines():
            if line[:1] in (" ", "\t", "") or ":" not in line:
                continue
            value = line.partition(":")[2].strip()
            if "：" in value or ":" in value:
                self.assertIn(
                    value[0], "'\"", f"含冒号的值必须加引号：{line[:40]}"
                )

    def test_check_frontmatter_clean(self) -> None:
        self.assertEqual(check_frontmatter(), [])

    def test_reader_raw_variants(self) -> None:
        """极简解析器要能处理引号、空行与缩进行，不把续行当键。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\nname: demo\n\n  indented: not-a-key\ndescription: 'a: b'\n---\nbody\n",
                encoding="utf-8",
            )
            fm = read_frontmatter(path)
        self.assertEqual(fm["name"], "demo")
        self.assertEqual(fm["description"], "a: b")
        self.assertNotIn("indented", fm)


if __name__ == "__main__":
    unittest.main()

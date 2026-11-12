"""段后词条分组规则回归（11-11 读者规格，外机试跑 v1 的版式口径）。

长段单独成组；短段最多连续 4 段 / 110 词出一组；章界不跨。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts.render_units import (
    PARA_GROUP_WORDS,
    PARA_MAX_SHORT,
    PARA_SHORT_WORDS,
    _group_paragraphs,
    _para_word_count,
)


def para(pid: str, words: int, chapter: int = 1):
    return SimpleNamespace(
        pid=pid,
        chapter=chapter,
        text=" ".join(["word"] * words),
        element=None,
    )


class ParagraphGroupingTests(unittest.TestCase):
    def test_word_count_ignores_non_latin_tokens(self) -> None:
        self.assertEqual(_para_word_count(para("a", 3)), 3)
        self.assertEqual(
            _para_word_count(SimpleNamespace(text="one 中文 二", chapter=1)), 1
        )

    def test_long_paragraph_stands_alone(self) -> None:
        groups = _group_paragraphs([
            para("short-a", 3),
            para("long", PARA_SHORT_WORDS),
            para("short-b", 3),
        ])
        self.assertEqual(
            [[p.pid for p in g] for g in groups],
            [["short-a"], ["long"], ["short-b"]],
        )

    def test_short_paragraphs_merged_up_to_max(self) -> None:
        groups = _group_paragraphs(
            [para(f"p{i}", 3) for i in range(PARA_MAX_SHORT + 2)]
        )
        self.assertEqual([len(g) for g in groups], [PARA_MAX_SHORT, 2])

    def test_group_words_cap(self) -> None:
        each = PARA_GROUP_WORDS // 3 + 1
        groups = _group_paragraphs([para(f"p{i}", each) for i in range(4)])
        self.assertGreaterEqual(sum(len(g) for g in groups), 4)
        self.assertTrue(all(len(g) <= PARA_MAX_SHORT for g in groups))

    def test_chapter_boundary_never_merges(self) -> None:
        groups = _group_paragraphs([
            para("a", 3, 1), para("b", 3, 1), para("c", 3, 2),
        ])
        self.assertEqual(
            [[p.pid for p in g] for g in groups], [["a", "b"], ["c"]]
        )

    def test_non_chapter_paragraphs_skipped(self) -> None:
        groups = _group_paragraphs([para("front", 3, 0), para("body", 3, 1)])
        self.assertEqual([[p.pid for p in g] for g in groups], [["body"]])


if __name__ == "__main__":
    unittest.main()

"""匹配测试（设计文档 §11.1 功能验收）。"""

from __future__ import annotations

import unittest

from tests._bootstrap import load_sample_lexicon, load_tiny_lexicon
from scripts.matching import (
    SEP,
    find_matches,
    lemma_candidates,
    strip_possessive,
)


def surfaces(text: str, lex, **kwargs) -> list[str]:
    return [m.surface for m in find_matches(text, lex, **kwargs).matches]


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lex = load_tiny_lexicon("art,四级,n.,艺术", "part,四级,n.,部分")

    def test_no_substring_hit(self) -> None:
        # article 不在词表内，绝不能因为包含 art 而命中
        self.assertEqual(surfaces("article", self.lex), [])
        self.assertEqual(surfaces("particle", self.lex), [])
        self.assertEqual(surfaces("apart", self.lex), [])

    def test_exact_word_hits(self) -> None:
        self.assertEqual(surfaces("art", self.lex), ["art"])

    def test_article_and_part_are_separate_words(self) -> None:
        got = surfaces("article part art", self.lex)
        self.assertEqual(got, ["part", "art"])

    def test_case_insensitive(self) -> None:
        self.assertEqual(surfaces("Art ART aRt", self.lex), ["Art", "ART", "aRt"])

    def test_unicode_punctuation(self) -> None:
        got = surfaces("art\u2014part\u2026art", self.lex)
        self.assertEqual(got, ["art", "part", "art"])

    def test_possessive(self) -> None:
        self.assertEqual(surfaces("art's value", self.lex), ["art's"])
        self.assertEqual(surfaces("art\u2019s value", self.lex), ["art\u2019s"])

    def test_hyphenated_compound_not_split(self) -> None:
        lex = load_tiny_lexicon("well-known,六级,adj.,著名的", "well,四级,adv.,好地")
        got = surfaces("a well-known fact", lex)
        self.assertEqual(got, ["well-known"])

    def test_strip_possessive_helper(self) -> None:
        self.assertEqual(strip_possessive("art's"), "art")
        self.assertEqual(strip_possessive("art\u2019s"), "art")
        self.assertIsNone(strip_possessive("art"))

    def test_url_skipped(self) -> None:
        res = find_matches("see https://example.com/art for art", self.lex)
        self.assertEqual([m.surface for m in res.matches], ["art"])
        self.assertGreaterEqual(res.url_tokens, 1)

    def test_duplicate_occurrences_all_matched(self) -> None:
        self.assertEqual(len(surfaces("art art art", self.lex)), 3)


class PhraseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lex = load_tiny_lexicon(
            "give,四级,v.,给",
            "give up,四级+雅思,phr.,放弃",
            "look forward to,雅思,phr.,期待",
        )

    def test_longest_phrase_wins(self) -> None:
        matches = find_matches("please give up now", self.lex).matches
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].surface, "give up")
        self.assertEqual(matches[0].kind, "phrase")

    def test_three_word_phrase(self) -> None:
        matches = find_matches("they look forward to it", self.lex).matches
        self.assertEqual([m.surface for m in matches], ["look forward to"])

    def test_single_word_still_matches_alone(self) -> None:
        matches = find_matches("please give now", self.lex).matches
        self.assertEqual([m.surface for m in matches], ["give"])

    def test_cross_node_phrase_not_forced(self) -> None:
        text = f"please give{SEP}up now"
        res = find_matches(text, self.lex)
        self.assertEqual(res.cross_node_phrases, 1)
        # 不成短语，但各自单词仍可独立命中
        self.assertEqual([m.surface for m in res.matches], ["give"])

    def test_phrase_case_and_spacing(self) -> None:
        self.assertEqual(surfaces("GIVE   UP", self.lex), ["GIVE   UP"])

    def test_entry_key_unifies_inflections(self) -> None:
        lex = load_tiny_lexicon("study,四级,n.,学习")
        match = find_matches("studies", lex).matches[0]
        self.assertEqual(match.surface, "studies")
        self.assertEqual(match.entry_key, "study")


class LemmaTests(unittest.TestCase):
    def test_regular_plural(self) -> None:
        lex = load_tiny_lexicon("study,四级,n.,学习", "book,四级,n.,书")
        self.assertEqual(surfaces("studies books", lex), ["studies", "books"])

    def test_regular_past_and_ing(self) -> None:
        lex = load_tiny_lexicon("work,四级,v.,工作", "stop,四级,v.,停止", "give,四级,v.,给")
        self.assertEqual(surfaces("worked stopping giving", lex), ["worked", "stopping", "giving"])

    def test_irregular_verb(self) -> None:
        lex = load_tiny_lexicon("go,四级,v.,去", "child,四级,n.,孩子")
        self.assertEqual(surfaces("went children", lex), ["went", "children"])

    def test_irregular_requires_lemma_in_lexicon(self) -> None:
        # dove 不在不规则表内、规则还原也到不了 dive，必须不命中
        lex = load_tiny_lexicon("dive,四级,v.,潜水")
        self.assertEqual(surfaces("dove", lex), [])

    def test_known_irregular_is_mapped(self) -> None:
        lex = load_tiny_lexicon("sink,四级,v.,下沉")
        self.assertEqual(surfaces("sunk", lex), ["sunk"])

    def test_reduction_disabled(self) -> None:
        lex = load_tiny_lexicon("study,四级,n.,学习")
        self.assertEqual(surfaces("studies", lex, reduce_forms=False), [])

    def test_no_bogus_suffix_hit(self) -> None:
        lex = load_tiny_lexicon("article,四级,n.,文章")
        # art 不在词表，articles 不能靠裁剪命中 article 之外的东西
        self.assertEqual(surfaces("articles", lex), ["articles"])
        lex2 = load_tiny_lexicon("art,四级,n.,艺术")
        self.assertEqual(surfaces("articles", lex2), [])

    def test_lemma_candidates_ordering(self) -> None:
        cands = lemma_candidates("studies")
        self.assertIn("study", cands)
        self.assertLess(cands.index("study"), len(cands))

    def test_short_tokens_not_reduced(self) -> None:
        lex = load_tiny_lexicon("a,四级,art.,一个")
        # 'as' 不能被还原成 'a'
        self.assertEqual(surfaces("as", lex), [])


class VisibilityTests(unittest.TestCase):
    """隐藏/链接/代码区的跳过规则（DOM 层，直接看槽位判定）。"""

    SNIPPET = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
      <p>visible art</p>
      <p><a href="#x">linked art</a></p>
      <div hidden="hidden"><p>hidden art</p></div>
      <p style="display:none">css hidden art</p>
      <div aria-hidden="true"><p>aria art</p></div>
      <pre>pre art</pre>
      <p>inline <code>code art</code> here</p>
      <p>note<sup>1</sup> art</p>
      <aside epub:type="footnote" xmlns:epub="http://www.idpf.org/2007/ops"><p>footnote art</p></aside>
      <nav><p>nav art</p></nav>
      <p>tail art</p>
    </body></html>"""

    def setUp(self) -> None:
        from scripts.epub_common import iter_text_slots, parse_xml

        tree = parse_xml(self.SNIPPET.encode("utf-8"), "snippet.xhtml")
        body = next(el for el in tree.getroot().iter() if str(el.tag).endswith("body"))
        self.slots = list(iter_text_slots(body))

    def _texts(self, *, writable: bool) -> list[str]:
        return [s.text for s in self.slots if s.writable is writable]

    def test_visible_text_only(self) -> None:
        joined = " ".join(self._texts(writable=True))
        self.assertIn("visible art", joined)
        self.assertIn("tail art", joined)
        for hidden in (
            "linked art",
            "hidden art",
            "css hidden art",
            "aria art",
            "code art",
            "pre art",
            "footnote art",
            "nav art",
        ):
            self.assertNotIn(hidden, joined, f"{hidden} 不应可写")

    def test_reasons_reported(self) -> None:
        reasons = {s.excluded for s in self.slots if s.excluded}
        self.assertIn("inside-link", reasons)
        self.assertIn("hidden", reasons)
        self.assertIn("css-hidden", reasons)
        self.assertIn("aria-hidden", reasons)
        self.assertIn("tag:code", reasons)
        self.assertIn("tag:sup", reasons)
        self.assertIn("existing-note", reasons)
        self.assertIn("tag:nav", reasons)

    def test_matches_only_in_visible_slots(self) -> None:
        lex = load_tiny_lexicon("art,四级,n.,艺术")
        text = SEP.join(self._texts(writable=True))
        matched = find_matches(text, lex)
        self.assertEqual(len(matched.matches), 2)  # visible + tail


class StatsTests(unittest.TestCase):
    def test_token_count(self) -> None:
        lex = load_sample_lexicon()
        res = find_matches("art and article", lex)
        self.assertEqual(res.tokens, 3)

    def test_empty_text(self) -> None:
        lex = load_sample_lexicon()
        res = find_matches("", lex)
        self.assertEqual(res.matches, [])
        self.assertEqual(res.tokens, 0)

    def test_sample_lexicon_traps(self) -> None:
        lex = load_sample_lexicon()
        got = surfaces("art article part", lex)
        self.assertEqual(got, ["art", "article", "part"])


if __name__ == "__main__":
    unittest.main()

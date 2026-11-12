"""§19 切词表测试：词边界、稳定 ID、跨节点、句子归属。"""

from __future__ import annotations

import unittest

from scripts.tokens import (
    TOKENIZER_VERSION,
    tokenize,
    split_sentences,
    token_id,
)


def toks(text: str, offsets=None, book="book-sha"):
    return tokenize(
        text, doc="OPS/book.xhtml", paragraph_index=0, book_sha256=book,
        offsets=offsets or [0],
    )  # fmt: skip


class TokenBoundaryTests(unittest.TestCase):
    def test_never_produces_substrings(self) -> None:
        """taki 不是 token；article 里的 art 也不是独立 token。"""

        words = [t.text for t in toks("taking an article about art")]
        self.assertIn("taking", words)
        self.assertIn("article", words)
        self.assertIn("art", words)  # 独立的 art 才是 token
        self.assertNotIn("taki", words)
        self.assertEqual(len([w for w in words if w == "art"]), 1)

    def test_hyphen_splits_and_phrase_can_span(self) -> None:
        tokens = toks("eighty-four well-known")
        self.assertEqual([t.text for t in tokens], ["eighty", "four", "well", "known"])
        span = (tokens[0].start, tokens[1].end)
        self.assertEqual("eighty-four"[span[0] : span[1]], "eighty-four")

    def test_apostrophes_and_possessives(self) -> None:
        tokens = toks("don't boy’s boy's")
        self.assertEqual([t.text for t in tokens], ["don't", "boy’s", "boy's"])

    def test_abbreviations_stay_whole(self) -> None:
        tokens = toks("at 9 a.m. in the U.S. today")
        self.assertEqual([t.text for t in tokens][:5], ["at", "9", "a.m.", "in", "the"])
        self.assertIn("U.S.", [t.text for t in tokens])

    def test_unicode_letters(self) -> None:
        tokens = toks("café naïve 北京")
        self.assertEqual([t.text for t in tokens], ["café", "naïve", "北京"])

    def test_offsets_are_exact(self) -> None:
        text = "He fished alone."
        for token in toks(text):
            self.assertEqual(text[token.start : token.end], token.text)


class TokenIdTests(unittest.TestCase):
    def test_ids_are_deterministic(self) -> None:
        a = [t.id for t in toks("taking a fish", book="sha-1")]
        b = [t.id for t in toks("taking a fish", book="sha-1")]
        c = [t.id for t in toks("taking a fish", book="sha-2")]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)  # 绑定源书哈希

    def test_id_changes_with_position(self) -> None:
        first = token_id("sha", "doc", 0, 0, 4, "fish")
        second = token_id("sha", "doc", 0, 10, 14, "fish")
        self.assertNotEqual(first, second)  # 同段重复词也不会撞 ID

    def test_tokenizer_version_is_part_of_contract(self) -> None:
        self.assertTrue(TOKENIZER_VERSION)


class CrossSlotTests(unittest.TestCase):
    def test_cross_inline_word_flagged(self) -> None:
        # 两个 DOM 槽：'He was tak' + 'ing the fish'
        text = "He was taking the fish"
        tokens = toks(text, offsets=[0, len("He was tak")])
        taking = next(t for t in tokens if t.text == "taking")
        self.assertTrue(taking.cross_slot)
        self.assertFalse(next(t for t in tokens if t.text == "fish").cross_slot)

    def test_normal_slots_are_not_flagged(self) -> None:
        text = "plain words here"
        tokens = toks(text, offsets=[0])
        self.assertFalse(any(t.cross_slot for t in tokens))


class SentenceTests(unittest.TestCase):
    def test_sentences_cover_all_tokens(self) -> None:
        text = "He fished. She took a fish! Did he?"
        tokens = toks(text)
        sentences = split_sentences(
            text, tokens, doc="OPS/book.xhtml", paragraph_index=0, book_sha256="book-sha"
        )
        self.assertEqual(len(sentences), 3)
        covered = [tid for sentence in sentences for tid in sentence.token_ids]
        self.assertEqual(covered, [t.id for t in tokens])

    def test_sentence_bounds_include_punctuation(self) -> None:
        text = "One. Two."
        tokens = toks(text)
        sentences = split_sentences(
            text, tokens, doc="d", paragraph_index=0, book_sha256="b"
        )
        self.assertEqual(text[sentences[0].start : sentences[0].end], "One.")


if __name__ == "__main__":
    unittest.main()

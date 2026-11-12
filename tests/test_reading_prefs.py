"""第 20/21 节：密度档位、熟词名单、提示必要性与筛选原因。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.reading_prefs import (
    decide,
    effective_density,
    export_word_list,
    importance,
    load_word_list,
)
from scripts.chapters import Chapter
from scripts.render_units import ChapterState, _dedup_key, _render_paragraph
from scripts.units import (
    Annotation,
    Component,
    _parse_annotation,
    document_paragraphs,
)


def ann(
    surface: str,
    *,
    lemma: str = "",
    unit: str = "word",
    source: str = "exact_entry",
    label_scope: str = "whole",
    reader: str = "",
    category: str = "",
    components: list[Component] | None = None,
    gloss: str = "释义",
    need: str = "",
    protect: bool = False,
) -> Annotation:
    return Annotation(
        annotation_id=f"a-{surface}",
        pid="doc#0",
        surface=surface,
        lemma=lemma or surface,
        unit_type=unit,
        source_type=source,
        source_status=source,
        label_scope=label_scope,
        reader_usage_note=reader,
        usage_category=category,
        components=components or [],
        gloss=gloss,
        action="annotate",
        review_status="pass",
        reading_need=need,
        sense_protected=protect,
    )


class ImportanceTests(unittest.TestCase):
    def test_required_is_core(self) -> None:
        self.assertEqual(importance(ann("prey", need="required")), "core")

    def test_optional_is_transparent(self) -> None:
        self.assertEqual(importance(ann("hat", need="optional")), "transparent")

    def test_useful_is_useful(self) -> None:
        self.assertEqual(importance(ann("swallowing", need="useful")), "useful")

    def test_missing_need_stays_conservative(self) -> None:
        # §21.2：没有可靠判断时不能当成可省
        self.assertEqual(importance(ann("raft")), "useful")
        self.assertEqual(importance(ann("magnificent")), "useful")

    def test_reader_note_no_longer_decides(self) -> None:
        # 写不写说明不影响必要性：两条同为 optional，结论一致
        self.assertEqual(
            importance(ann("a", reader="有说明", need="optional")),
            importance(ann("b", reader="", need="optional")),
        )

    def test_protected_always_core(self) -> None:
        self.assertEqual(importance(ann("order", need="optional", protect=True)), "core")

    def test_structural_core_without_need(self) -> None:
        members = [Component(token_ids=["t1"], lemma="fish", entry_ids=["fish|x"])]
        self.assertEqual(
            importance(ann("taking a fish", unit="phrase", components=members)), "core"
        )
        self.assertEqual(
            importance(ann("skiff", source="out_of_lexicon", label_scope="none")),
            "core",
        )
        self.assertEqual(importance(ann("Santiago", unit="proper_name")), "core")


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = ann("taking a fish", unit="phrase", need="required")
        self.useful = ann("swallowing", need="useful")
        self.optional = ann("hat", need="optional")
        self.unknown = ann("raft")
        self.items = [self.core, self.useful, self.optional, self.unknown]

    def test_light_drops_only_optional(self) -> None:
        decision = decide(self.items, level="light")
        surfaces = [a.surface for a in decision.kept]
        self.assertIn("taking a fish", surfaces)
        self.assertIn("swallowing", surfaces)
        self.assertIn("raft", surfaces)  # unknown 保守保留
        self.assertNotIn("hat", surfaces)
        self.assertEqual(decision.counts, {"level": 1})

    def test_standard_and_detailed_keep_everything(self) -> None:
        for level in ("standard", "detailed"):
            decision = decide(self.items, level=level)
            self.assertEqual(len(decision.kept), 4)
            self.assertEqual(decision.filtered, [])

    def test_mastered_drops_optional_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text(
                "word,status\nhat,mastered\nraft,mastered\n", encoding="utf-8"
            )
            words = load_word_list(path)
            decision = decide(self.items, level="standard", word_list=words)
            surfaces = [a.surface for a in decision.kept]
            self.assertNotIn("hat", surfaces)
            self.assertIn("raft", surfaces)  # unknown 不受名单影响
            self.assertEqual(decision.counts, {"mastered": 1})

    def test_mastered_take_keeps_taking_a_fish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("word,status\ntake,mastered\n", encoding="utf-8")
            words = load_word_list(path)
            items = [
                ann("taking a fish", unit="phrase", need="required"),
                ann("taking", lemma="take", need="optional"),
            ]
            decision = decide(items, level="standard", word_list=words)
            surfaces = [a.surface for a in decision.kept]
            self.assertIn("taking a fish", surfaces)
            self.assertNotIn("taking", surfaces)

    def test_focus_wins_over_mastered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("word,status\nhat,mastered\nhat,focus\n", encoding="utf-8")
            words = load_word_list(path)
            decision = decide(self.items, level="light", word_list=words)
            self.assertIn("hat", [a.surface for a in decision.kept])
            self.assertEqual(decision.focus_hits, 1)

    def test_focus_lifts_optional_in_light(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("word,status\nhat,focus\n", encoding="utf-8")
            words = load_word_list(path)
            decision = decide(
                [ann("hat", need="optional")], level="light", word_list=words
            )
            self.assertEqual(len(decision.kept), 1)
            self.assertEqual(decision.importance.get("useful"), 1)


class WordListTests(unittest.TestCase):
    def test_json_and_csv_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "a.csv"
            csv_path.write_text(
                "word,status\ntake,mastered\nfish,focus\n", encoding="utf-8"
            )
            csv_words = load_word_list(csv_path)
            self.assertIn("take", csv_words.mastered)
            self.assertIn("fish", csv_words.focus)

            json_path = Path(tmp) / "b.json"
            json_path.write_text(
                '{"mastered": ["take"], "focus": ["fish"]}', encoding="utf-8"
            )
            json_words = load_word_list(json_path)
            self.assertIn("take", json_words.mastered)
            self.assertIn("fish", json_words.focus)

    def test_possessive_matches_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("word,status\npainter,mastered\n", encoding="utf-8")
            words = load_word_list(path)
            self.assertEqual(words.status(ann("painter's", lemma="painter")), "mastered")

    def test_inflection_matches_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("word,status\nswallow,focus\ncatch,mastered\n", encoding="utf-8")
            words = load_word_list(path)
            self.assertEqual(words.status(ann("swallowing", lemma="swallow")), "focus")
            self.assertEqual(words.status(ann("caught", lemma="catch")), "mastered")

    def test_missing_file_reports_issue(self) -> None:
        self.assertTrue(load_word_list(Path("no-such-file.csv")).issues)

    def test_export_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            export_word_list(path, {"take"}, {"fish"})
            reloaded = load_word_list(path)
            self.assertIn("take", reloaded.mastered)
            self.assertIn("fish", reloaded.focus)

    def test_phrase_key_stays_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text(
                'word,status\n"taking my engine apart",focus\n', encoding="utf-8"
            )
            words = load_word_list(path)
            self.assertEqual(
                words.status(ann("taking my engine apart", unit="phrase")), "focus"
            )


class ParseTests(unittest.TestCase):
    def test_unknown_need_falls_back_to_unknown(self) -> None:
        issues: list[dict] = []
        parsed = _parse_annotation(
            {
                "annotation_id": "a-1",
                "pid": "doc#0",
                "token_ids": ["t1"],
                "reading_need": "bogus",
                "sense_protected": "true",
            },
            "test",
            issues,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.reading_need, "unknown")
        self.assertTrue(parsed.sense_protected)
        self.assertTrue(issues)

    def test_missing_need_stays_empty(self) -> None:
        parsed = _parse_annotation(
            {"annotation_id": "a-1", "pid": "doc#0", "token_ids": ["t1"]}, "test", []
        )
        self.assertEqual(parsed.reading_need, "")
        self.assertFalse(parsed.sense_protected)


def make_paragraphs(pairs, id_map=None):
    body = "".join(f'<p id="{pid}">{text}</p>' for pid, text in pairs)
    raw = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        + body
        + "</body></html>"
    ).encode("utf-8")
    _, paragraphs = document_paragraphs(raw, "doc.xhtml", 1, id_map or {})
    return paragraphs


def word_ann(paragraph, word, gloss, order=0, lemma="catch"):
    tokens = [t for t in paragraph.tokens if t.text == word]
    token = tokens[order]
    return Annotation(
        annotation_id=f"a-{paragraph.pid}-{order}",
        pid=paragraph.pid,
        token_ids=[token.id],
        span=(token.start, token.end),
        surface=token.text,
        lemma=lemma,
        unit_type="word",
        source_type="exact_entry",
        source_status="exact_entry",
        label_scope="whole",
        gloss=gloss,
        action="annotate",
        review_status="pass",
        reading_need="required",
    )


def chapter(index: int) -> Chapter:
    return Chapter(
        index=index,
        label=f"Ch{index}",
        start_path="doc.xhtml",
        start_id=None,
        end_path="doc.xhtml",
        end_before_id=None,
    )


class DedupTests(unittest.TestCase):
    """§21.4：同章同义去重、同词异义、跨章再现各一个针对性样例。"""

    def test_same_word_same_sense_twice_marks_once(self) -> None:
        paragraph = make_paragraphs(
            [("p1", "He caught a fish. He caught a fish.")]
        )[0]
        first = word_ann(paragraph, "caught", "捕到", order=0)
        second = word_ann(paragraph, "caught", "捕到", order=1)
        state = ChapterState(chapter=chapter(1), density="chapter")
        _render_paragraph(paragraph, [first, second], state)
        self.assertEqual(state.marks, 1)
        self.assertEqual(state.skipped_density, 1)

    def test_same_word_different_sense_appears_twice(self) -> None:
        first_para, second_para = make_paragraphs(
            [("p1", "He caught a fish."), ("p2", "He caught the ball.")]
        )
        state = ChapterState(chapter=chapter(1), density="chapter")
        _render_paragraph(
            first_para, [word_ann(first_para, "caught", "捕到")], state
        )
        _render_paragraph(
            second_para, [word_ann(second_para, "caught", "抓住")], state
        )
        self.assertEqual(state.marks, 2)
        self.assertEqual(state.skipped_density, 0)

    def test_same_sense_in_new_chapter_appears_again(self) -> None:
        first_para, second_para = make_paragraphs(
            [("p1", "He caught a fish."), ("p2", "He caught a fish.")],
            {"p2": 2},
        )
        state1 = ChapterState(chapter=chapter(1), density="chapter")
        state2 = ChapterState(chapter=chapter(2), density="chapter")
        _render_paragraph(
            first_para, [word_ann(first_para, "caught", "捕到")], state1
        )
        _render_paragraph(
            second_para, [word_ann(second_para, "caught", "捕到")], state2
        )
        self.assertEqual(state1.marks, 1)
        self.assertEqual(state2.marks, 1)

    def test_phrase_key_uses_whole_surface_not_member(self) -> None:
        base = Annotation(
            annotation_id="a-1",
            pid="doc#0",
            surface="taking a fish",
            unit_type="phrase",
            lemma="take",
            gloss="捕鱼",
        )
        same = Annotation(
            annotation_id="a-2",
            pid="doc#0",
            surface="taking a fish",
            unit_type="phrase",
            lemma="take",
            gloss="捕鱼",
        )
        other_sense = Annotation(
            annotation_id="a-3",
            pid="doc#0",
            surface="taking a fish",
            unit_type="phrase",
            lemma="take",
            gloss="拿鱼",
        )
        self.assertEqual(_dedup_key(base), _dedup_key(same))
        self.assertNotEqual(_dedup_key(base), _dedup_key(other_sense))
        self.assertNotEqual(_dedup_key(base), _dedup_key(
            Annotation(
                annotation_id="a-4",
                pid="doc#0",
                surface="taking",
                unit_type="word",
                lemma="take",
                gloss="捕鱼",
            )
        ))


class DensityTests(unittest.TestCase):
    def test_level_no_longer_rewrites_density(self) -> None:
        # §21.4：档位与密度独立，不再把 chapter 自动改成 all
        self.assertEqual(effective_density("detailed", "chapter"), ("chapter", False))
        self.assertEqual(effective_density("detailed", "all"), ("all", False))
        self.assertEqual(effective_density("light", "chapter"), ("chapter", False))


if __name__ == "__main__":
    unittest.main()

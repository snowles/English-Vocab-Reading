"""词表加载测试：编码、分隔符、列名映射、等级组合、坏行与去重。"""

from __future__ import annotations

import unittest

from tests._bootstrap import FIXTURES, load_sample_lexicon
from scripts.load_lexicon import (
    DEFAULT_LABEL_ORDER,
    GRADE_SHORT,
    LexiconError,
    detect_delimiter,
    detect_encoding,
    load_lexicon,
    load_lexicon_bytes,
    normalize_key,
    ordered_labels,
    parse_grades,
    parse_label_order,
    pick_label,
)

HEADER = "单词,单词等级,词性,中文释义,例句,扩展说明\n"
BODY = (
    "art,四级,n.,艺术；美术,,\n"
    "give up,四级+雅思,phr.,放弃,,\n"
    "work,四级,n.,作品,,\n"
    "work,四级+雅思,v.,工作,,\n"
)


def _csv(*rows: str, header: str = HEADER) -> bytes:
    return (header + "".join(rows)).encode("utf-8")


class EncodingTests(unittest.TestCase):
    def test_detect_utf8_bom(self) -> None:
        self.assertEqual(detect_encoding(b"\xef\xbb\xbfabc"), "utf-8-sig")

    def test_detect_utf8(self) -> None:
        self.assertEqual(detect_encoding("单词".encode("utf-8")), "utf-8")

    def test_detect_gb18030(self) -> None:
        self.assertEqual(detect_encoding("单词".encode("gb18030")), "gb18030")

    def test_detect_utf16(self) -> None:
        self.assertEqual(detect_encoding("单词".encode("utf-16")), "utf-16")

    def test_non_utf8_falls_back_to_gb18030(self) -> None:
        # 不完整 UTF-8 序列必须回退到 GB18030，而不是抛错或乱码
        self.assertEqual(detect_encoding(b"\xe4\xb8"), "gb18030")


class DelimiterTests(unittest.TestCase):
    def test_comma(self) -> None:
        self.assertEqual(detect_delimiter("单词,等级,释义\nart,四级,艺术"), ",")

    def test_tab(self) -> None:
        self.assertEqual(detect_delimiter("单词\t等级\t释义\nart\t四级\t艺术"), "\t")

    def test_tab_parsed_when_forced(self) -> None:
        data = "单词\t单词等级\t中文释义\nart\t四级\t艺术\n".encode("utf-8")
        lex = load_lexicon_bytes(data, delimiter="\t")
        self.assertEqual(len(lex), 1)
        self.assertEqual(lex.entries[0].gloss, "艺术")


class HeaderMappingTests(unittest.TestCase):
    def test_chinese_header(self) -> None:
        lex = load_lexicon_bytes(_csv("art,四级,n.,艺术,,\n"))
        self.assertEqual(lex.columns["word"], "单词")
        self.assertEqual(lex.columns["gloss"], "中文释义")

    def test_english_alias_header(self) -> None:
        data = "word,level,pos,definition,example,note\nart,四级,n.,艺术,,\n".encode()
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.entries[0].gloss, "艺术")
        self.assertEqual(lex.entries[0].grade_raw, "四级")

    def test_missing_required_column_raises(self) -> None:
        data = "单词,单词等级,词性\nart,四级,n.\n".encode("utf-8")
        with self.assertRaises(LexiconError) as ctx:
            load_lexicon_bytes(data)
        self.assertIn("中文释义", str(ctx.exception))

    def test_column_order_irrelevant(self) -> None:
        data = "中文释义,单词,单词等级\n艺术,art,四级\n".encode("utf-8")
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.entries[0].head, "art")
        self.assertEqual(lex.entries[0].gloss, "艺术")


class GradeTests(unittest.TestCase):
    def test_parse_combo(self) -> None:
        self.assertEqual(parse_grades("四级+雅思"), frozenset({"四级", "雅思"}))
        self.assertEqual(parse_grades("四级/六级"), frozenset({"四级", "六级"}))
        self.assertEqual(parse_grades(""), frozenset())

    def test_filter_by_single_grade(self) -> None:
        lex = load_lexicon_bytes(_csv(BODY), grades=["雅思"])
        heads = {e.head for e in lex.entries}
        # 四级 被过滤；四级+雅思 与 四级+雅思 组合保留
        self.assertNotIn("art", heads)
        self.assertIn("give up", heads)
        self.assertIn("work", {e.head for e in lex.entries if e.grade_raw == "四级+雅思"})

    def test_filter_no_match_raises_with_available(self) -> None:
        with self.assertRaises(LexiconError) as ctx:
            load_lexicon_bytes(_csv(BODY), grades=["考研"])
        self.assertIn("词表可用等级", str(ctx.exception))

    def test_filter_dropped_counted(self) -> None:
        lex = load_lexicon_bytes(_csv(BODY), grades=["雅思"])
        self.assertGreater(lex.rows_dropped_by_grade, 0)


class StructureTests(unittest.TestCase):
    def test_duplicate_row_merged(self) -> None:
        data = _csv("art,四级,n.,艺术,,\n", "art,四级,n.,艺术,,\n")
        lex = load_lexicon_bytes(data)
        self.assertEqual(len(lex), 1)
        self.assertEqual(lex.rows_duplicate, 1)

    def test_same_head_different_sense_kept(self) -> None:
        lex = load_lexicon_bytes(_csv(BODY))
        work = lex.lookup("work")
        self.assertEqual(len(work), 2)
        self.assertEqual({e.pos for e in work}, {"n.", "v."})

    def test_empty_head_and_gloss_reported(self) -> None:
        data = _csv(",四级,n.,艺术,,\n", "art,四级,n.,,,\n", "buy,四级,v.,买,,\n")
        lex = load_lexicon_bytes(data)
        self.assertEqual(len(lex), 1)
        reasons = {i.reason for i in lex.issues}
        self.assertIn("空词头", reasons)
        self.assertIn("空释义", reasons)

    def test_short_row_padded(self) -> None:
        # 缺列不右移错位：缺的是“例句/扩展说明”，词条仍应正确入库
        data = "单词,单词等级,词性,中文释义,例句,扩展说明\nart,四级,n.,艺术\n".encode("utf-8")
        lex = load_lexicon_bytes(data)
        self.assertEqual(len(lex), 1)
        self.assertEqual(lex.entries[0].gloss, "艺术")
        self.assertEqual(lex.entries[0].example, "")

    def test_short_row_missing_gloss_reported(self) -> None:
        data = "单词,单词等级,词性,中文释义,例句,扩展说明\nart,四级\n".encode("utf-8")
        lex = load_lexicon_bytes(data)
        self.assertEqual(len(lex), 0)
        self.assertEqual({i.reason for i in lex.issues}, {"空释义"})

    def test_quoted_comma_in_gloss(self) -> None:
        data = '单词,中文释义\nart,"艺术，美术, 技巧"\n'.encode("utf-8")
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.entries[0].gloss, "艺术，美术, 技巧")

    def test_bom_stripped(self) -> None:
        data = b"\xef\xbb\xbf" + _csv("art,四级,n.,艺术,,\n")
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.entries[0].head, "art")

    def test_gbk_file_loads(self) -> None:
        data = (HEADER + "art,四级,n.,艺术,,\n").encode("gb18030")
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.entries[0].gloss, "艺术")

    def test_crlf_line_endings(self) -> None:
        data = HEADER.replace("\n", "\r\n") + "art,四级,n.,艺术,,\r\n"
        lex = load_lexicon_bytes(data.encode("utf-8"))
        self.assertEqual(len(lex), 1)


class KeyTests(unittest.TestCase):
    def test_normalize_case_and_space(self) -> None:
        self.assertEqual(normalize_key("  Look   Forward  To "), "look forward to")
        self.assertEqual(normalize_key("ART"), "art")

    def test_variant_slash_head(self) -> None:
        data = _csv("percent/per cent,六级,n.,百分比,,\n")
        lex = load_lexicon_bytes(data)
        self.assertEqual(len(lex.lookup("percent")), 1)
        self.assertEqual(len(lex.lookup("per cent")), 1)
        self.assertEqual(lex.heads, 1)

    def test_phrase_count_and_max_words(self) -> None:
        data = _csv("a,四级,n.,一\n", "look forward to,雅思,phr.,期待\n")
        lex = load_lexicon_bytes(data)
        self.assertEqual(lex.phrases, 1)
        self.assertEqual(lex.max_phrase_words, 3)

    def test_entry_id_traceable(self) -> None:
        lex = load_lexicon_bytes(_csv("study,四级,n.,学习,,\n"))
        self.assertEqual(lex.entries[0].entry_id, "study|四级|n.")


class RealLexiconTests(unittest.TestCase):
    """真实词表结构抽查（文件存在时执行，用于防止回归）。"""

    def test_sample_fixture_loads(self) -> None:
        lex = load_sample_lexicon()
        self.assertGreaterEqual(len(lex), 15)
        self.assertIn(lex.encoding, {"utf-8", "utf-8-sig"})
        self.assertIn("雅思", lex.grades)

    def test_fixture_file_exists(self) -> None:
        self.assertTrue((FIXTURES / "lexicon_sample.csv").is_file())

    def test_load_missing_file_raises(self) -> None:
        with self.assertRaises(LexiconError):
            load_lexicon(FIXTURES / "not_exists.csv")


class LabelMappingTests(unittest.TestCase):
    """§18.2：标签映射、显示次序、筛选只影响展示。"""

    def _lexicon(self, grades=None):
        data = (
            HEADER
            + "benevolent,托福,adj.,慈善的,,"
            + "\nskin,四级+雅思+牛津3000,n.,皮肤,,"
            + "\nmast,六级+托福,n.,桅杆,,"
            + "\nnormal,四级+雅思+牛津3000+AWL,adj.,正常的,,"
            + "\n"
        ).encode("utf-8")
        return load_lexicon_bytes(data, grades=grades)

    def test_short_labels_cover_all_seven(self) -> None:
        self.assertEqual(
            GRADE_SHORT,
            {
                "四级": "4",
                "六级": "6",
                "雅思": "ielts",
                "托福": "toefl",
                "GRE": "GRE",
                "牛津3000": "牛津",
                "AWL": "AWL",
            },
        )

    def test_default_order_is_display_preference(self) -> None:
        self.assertEqual(
            ordered_labels({"AWL", "托福", "四级", "牛津3000"}),
            ["四级", "托福", "牛津3000", "AWL"],
        )
        self.assertEqual(pick_label({"雅思", "托福"}), "ielts")
        self.assertEqual(pick_label({"托福", "雅思"}, order=("托福", "雅思")), "toefl")

    def test_pick_label_never_invents_label(self) -> None:
        self.assertEqual(pick_label(set()), "")
        self.assertEqual(pick_label({"未知标签"}), "未知标签")  # 保留原值，不猜

    def test_label_order_accepts_short_names(self) -> None:
        self.assertEqual(parse_label_order("雅思,托,4").count("四级"), 1)
        self.assertEqual(parse_label_order("雅思,toefl"), ("雅思", "托福"))
        self.assertEqual(parse_label_order(""), DEFAULT_LABEL_ORDER)

    def test_filtered_status_distinct_from_absent(self) -> None:
        lex = self._lexicon(grades=["雅思"])
        self.assertEqual(lex.status("skin"), "exact")
        self.assertEqual(lex.status("benevolent"), "filtered")  # 完整词库有，被筛掉
        self.assertEqual(lex.status("skiff"), "absent")          # 真正没有
        self.assertEqual(len(lex.entries), 2)
        self.assertEqual(len(lex.all_entries), 4)

    def test_filtered_entry_keeps_labels_for_audit(self) -> None:
        lex = self._lexicon(grades=["雅思"])
        entries = lex.lookup_any("mast")
        self.assertTrue(entries)
        self.assertEqual(entries[0].labels_full, "六级、托福")
        self.assertEqual(lex.lookup("mast"), [])


if __name__ == "__main__":
    unittest.main()

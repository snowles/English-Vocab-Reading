"""层 A + 层 B 接入回归（11-11 读者拍板，P1-b / 0.10.0）。

口径：音标只做 L1 字符归一、按被标注词形自身标注、无则整行省略；
例句有就有不编造；词条数不变；词形表只作为候选来源，硬校验不放宽。
"""

from __future__ import annotations

import hashlib
import json
import unittest

from tests._bootstrap import ROOT

from scripts.lexicon_vault import unpack_bytes
from scripts.load_lexicon import (
    FormInfo,
    lexicon_content_sha256,
    load_form_map,
    load_lexicon,
    load_lexicon_bytes,
)
from scripts.matching import find_matches, lemma_candidates
from scripts.phonetic_l1 import diagnose, normalize_phonetic

ASSET = ROOT / "assets" / "lexicon.vault"
FORMS = ROOT / "assets" / "lexicon.forms.vault"
SOURCE_JSON = ROOT / "assets" / "lexicon.source.json"


class BundledEnhancedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lexicon(ASSET)

    def test_entries_and_enhanced_columns(self):
        self.assertEqual(len(self.lex), 6861)
        fish = self.lex.lookup("fish")[0]
        self.assertEqual(fish.phonetic, "/fiʃ/")
        self.assertTrue(fish.source)
        summary = self.lex.summary()
        self.assertEqual(summary["enhanced"]["with_phonetic"], 6757)
        self.assertEqual(summary["enhanced"]["with_example"], 3624)
        self.assertEqual(summary["columns"]["phonetic"], "音标")
        self.assertEqual(summary["columns"]["form_info"], "词形信息")

    def test_example_sentences_not_invented(self):
        # 词条数与例句数按层 A 全量统计固定，层 C/D 未开。
        self.assertEqual(len(self.lex), 6861)
        self.assertEqual(sum(1 for e in self.lex.all_entries if e.example), 3624)

    def test_phonetic_l1_is_idempotent(self):
        for entry in self.lex.all_entries:
            if entry.phonetic:
                self.assertEqual(normalize_phonetic(entry.phonetic.strip("/")),
                                 entry.phonetic, entry.head)

    def test_dirty_phonetics_are_dropped(self):
        for word in ("discernible", "yogurt", "arouse", "introverted",
                     "linguistic", "universally"):
            entries = self.lex.lookup(word)
            if entries:
                self.assertEqual(entries[0].phonetic, "", word)

    def test_form_map_loaded(self):
        summary = self.lex.summary()
        self.assertEqual(summary["form_map"]["entries"], 11825)
        self.assertEqual(summary["form_map"]["with_phonetic"], 7219)
        self.assertEqual(summary["form_map"]["issues"], 0)
        self.assertEqual(len(self.lex.form_issues), 0)

    def test_provenance_record_matches_bundled(self):
        record = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(record["bundled"]["content_sha256"],
                         lexicon_content_sha256(ASSET))
        self.assertEqual(record["enhanced"]["layer_b"]["content_sha256"],
                         hashlib.sha256(unpack_bytes(FORMS.read_bytes())).hexdigest())

    def test_license_files_present(self):
        for name in ("LICENSE", "references/ECDICT_LICENSE.txt"):
            self.assertIn("MIT License", (ROOT / name).read_text(encoding="utf-8"), name)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("MIT", notices)
        self.assertIn("待确认", notices)


class SurfacePhoneticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lexicon(ASSET)

    def test_surface_phonetic_wins(self):
        fish = self.lex.lookup("fish")[0]
        live = self.lex.lookup("live")[0]
        self.assertEqual(self.lex.phonetic_for("fished", fish), "/fiʃt/")
        self.assertEqual(self.lex.phonetic_for("Lived", live), "/livd/")
        self.assertEqual(self.lex.phonetic_for("picked"), "/pikt/")
        self.assertEqual(self.lex.phonetic_for("taking"), "/ˈteikiŋ/")

    def test_head_phonetic_only_for_head_form(self):
        fish = self.lex.lookup("fish")[0]
        live = self.lex.lookup("live")[0]
        self.assertEqual(self.lex.phonetic_for("fish", fish), "/fiʃ/")
        self.assertEqual(self.lex.phonetic_for("live", live), "/laiv/")

    def test_no_fallback_when_surface_has_no_ipa(self):
        # 音标没有就整行省略；词形信息是描述文字，层 B 有就照给，不算「回退词头」。
        for word in ("destroyed", "craning", "geese"):
            self.assertEqual(self.lex.phonetic_for(word), "", word)
        self.assertIn("词根 destroy", self.lex.form_info_for("destroyed"))

    def test_form_info_is_chinese_readable(self):
        info = self.lex.form_info_for("fished", self.lex.lookup("fish")[0])
        self.assertIn("词根 fish", info)
        self.assertNotIn("0:", info)
        self.assertNotIn("/1:", info)


class LemmaMapTests(unittest.TestCase):
    def test_form_map_candidate_comes_first(self):
        lex = load_lexicon(ASSET)
        self.assertEqual(lemma_candidates("lived", lex.form_map)[0], "live")
        self.assertEqual(lemma_candidates("caught", lex.form_map)[0], "catch")

    def test_form_map_is_only_a_candidate_source(self):
        # 词形表说了算也不能跳过「词根必须落在词表内」的硬校验。
        lex = load_lexicon_bytes("单词,中文释义\nfish,鱼\n".encode("utf-8"))
        lex.form_map = {"zzzz": FormInfo(surface="zzzz", root="not-in-lexicon")}
        self.assertEqual(find_matches("zzzz", lex).matches, [])
        self.assertEqual(lemma_candidates("zzzz", lex.form_map)[0], "not-in-lexicon")

    def test_regular_rules_still_work_without_form_map(self):
        lex = load_lexicon_bytes(
            "单词,中文释义\nfish,鱼\n".encode("utf-8")
        )
        self.assertTrue(find_matches("fished", lex).matches)


class BadValueTests(unittest.TestCase):
    def _load(self, header: str, row: str):
        return load_lexicon_bytes((header + "\n" + row + "\n").encode("utf-8"))

    def test_dirty_phonetic_dropped_with_issue(self):
        lex = self._load(
            "单词,中文释义,音标,词频BNC",
            "fish,鱼,'^i:s,abc",
        )
        entry = lex.lookup("fish")[0]
        self.assertEqual(entry.phonetic, "")
        self.assertEqual(entry.bnc, "")
        reasons = {i.reason for i in lex.issues}
        self.assertIn("脏音标整条省略", reasons)
        self.assertIn("非数值内容已忽略", reasons)

    def test_old_six_column_csv_still_loads(self):
        lex = self._load("单词,单词等级,词性,中文释义,例句,扩展说明",
                         "fish,四级,n.,鱼,We fish,音标：fɪʃ")
        self.assertEqual(len(lex), 1)
        self.assertEqual(lex.lookup("fish")[0].phonetic, "")
        self.assertEqual(lex.summary()["form_map"]["entries"], 0)

    def test_l1_diagnose_rejects_unknown_symbols(self):
        self.assertEqual(normalize_phonetic("^i:s"), "")
        self.assertEqual(diagnose("livd. 'laivd")["normalized"], "/livd/")
        self.assertEqual(normalize_phonetic("'bєә"), "/ˈbɛə/")

    def test_form_map_rejects_bad_rows(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forms.csv"
            path.write_text(
                "词形,词根,词形音标,词形信息\n"
                "lived,live,/livd/,词根 live\n"
                ",live,/livd/,空词形\n"
                "same,same,/x/,词根同词形\n"
                "lived,live,/livd/,重复\n"
                "bad,root,'^i:s,脏音标\n",
                encoding="utf-8",
            )
            mapping, issues = load_form_map(path)
        self.assertEqual(mapping["lived"].phonetic, "/livd/")
        self.assertEqual(len(mapping), 2)  # lived + bad
        self.assertEqual(mapping["bad"].phonetic, "")
        self.assertTrue(any(i.reason == "脏音标整条省略" for i in issues))


if __name__ == "__main__":
    unittest.main()

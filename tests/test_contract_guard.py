"""契约条款守护测试（H3-T8 回归，0.10.5）。

T8 失守的根因不是程序 bug，而是 `SKILL.md` 一条豁免被模型泛化：
「用户说先简单标一下 / 不用语境判断 → 词表直注」被读成「用户赶时间 → 可以跳过识别与审查」。

这类纯文档约束没法用行为单测覆盖（技能自身不调模型），能做的是把条款钉住：
条款被删、被放松、被挪到别处，测试立刻红。

**局限（必须写明）**：本测试只保证「约束还在文档里」，不保证模型一定遵守。
模型是否遵守由 H3 的 T8 题面实跑验证；这里防的是「下次改文档时把红线改没了」。
"""

from __future__ import annotations

import unittest

from tests._bootstrap import ROOT

SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
H1 = (ROOT / "references" / "h1-coverage.md").read_text(encoding="utf-8")

REDLINE_MARKER = "不得把「赶时间」当成降级授权"


def _section(text: str, heading: str) -> str:
    """取某个 `## ` 小节的正文（到下一个同级标题为止）。"""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def _bullet(text: str, marker: str) -> str:
    """取以 marker 开头的那条 bullet 全文（含两空格缩进的续行）。"""
    start = text.index(marker)
    lines = text[start:].splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith("- ") or line.startswith("## ") or line.startswith("**"):
            break
        if not line.strip():
            break
        out.append(line)
    return "\n".join(out)


class T8ContractGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = _section(SKILL, "## 编排契约")
        self.limits = _section(SKILL, "## 已知限制")

    def test_redline_exists(self) -> None:
        self.assertIn(
            REDLINE_MARKER, self.contract,
            "「赶时间不构成降级授权」这条红线被删了——T8 会重新失守",
        )

    def test_redline_defines_degraded_delivery(self) -> None:
        block = _bullet(self.contract, REDLINE_MARKER)
        for word in ("降级交付", "没有语境短义", "没有独立审查"):
            self.assertIn(word, block, f"红线缺少降级产物的定义要素：{word}")

    def test_redline_says_time_pressure_is_not_permission(self) -> None:
        block = _bullet(self.contract, REDLINE_MARKER)
        self.assertIn("不构成豁免", block, "红线没有明确否定「赶时间」作为豁免")
        for trigger in ("赶时间", "别审查", "直接出书"):
            self.assertIn(trigger, block, f"红线未点名该拒绝的措辞：{trigger}")

    def test_redline_gives_fastest_path(self) -> None:
        block = _bullet(self.contract, REDLINE_MARKER)
        self.assertIn("1 章小样", block, "红线没有给「最快可行路径」，只拒绝等于没说怎么办")

    def test_redline_forbids_calling_it_companion_edition(self) -> None:
        block = _bullet(self.contract, REDLINE_MARKER)
        self.assertIn("浅标版", block, "降级产物必须限定自称「浅标版」")
        self.assertIn("不得说成", block, "没有禁止把降级产物说成伴读版")

    def test_legacy_lenient_clause_removed(self) -> None:
        """旧的宽松触发句必须消失，否则模型仍会从旧句推回「说一句宽泛理由就能跳审」。"""
        legacy = '用户明确说"先简单标一下 / 不用语境判断"时，才用'
        self.assertNotIn(legacy, self.contract, "旧豁免条款仍在，T8 修复被回退")

    def test_direct_paths_marked_degraded(self) -> None:
        block = _section(SKILL, "## 执行步骤")
        self.assertIn("三条直出路径", block)
        self.assertIn("这三条都属降级交付", block, "直出路径没标注降级身份，会被当成正式交付")
        self.assertIn("不要把直出路径当作跳过识别与审查的捷径", block)

    def test_exit_code_six_flags_downgrade(self) -> None:
        block = _section(SKILL, "## 退出码")
        self.assertIn("降级交付", block, "退出码 6 的指引没有提醒改走直注是降级")

    def test_known_limits_entry(self) -> None:
        self.assertIn(
            "词表直注不是「跳过审查」的替代品", self.limits,
            "已知限制缺少这条，降级路径会重新被理解为「跳过审查的省事做法」",
        )

    def test_choice_wording_must_explain_difference(self) -> None:
        self.assertIn(
            "这个二选一必须把差异说清", SKILL,
            "「精细 / 快标」二选一没要求说明差异，等于默许用户误选降级版",
        )

    def test_h1_matrix_matches_this_fix(self) -> None:
        self.assertIn("H3-T8", H1, "H1 覆盖矩阵没记录这次修复")
        self.assertIn("假覆盖", H1, "H1 应如实写明这一格此前是假覆盖")
        self.assertIn("tests/test_contract_guard.py", H1, "H1 应指向本守护测试")


if __name__ == "__main__":
    unittest.main()

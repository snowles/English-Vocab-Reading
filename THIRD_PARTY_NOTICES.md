# 许可与第三方声明（统一 MIT 体系）

> 编号 `VR-LIC-2026-1111-01`　·　11-11 读者拍板：「词表再分发许可统一设计一下，还是遵循 MIT 协议」。
> 本文件与 `LICENSE`、`references/ECDICT_LICENSE.txt` 一起随发布包分发。

## 1. 统一口径

整个项目按 **MIT 体系**设计，不引入第二套许可证：

| 层次 | 内容 | 许可 | 依据文件 |
|---|---|---|---|
| ① 本项目代码 / 脚本 / 文档 / 协议定义 | `scripts/*.py`、`SKILL.md`、`schema.md`、`README.md`、`INSTALL.md`、`styles.md`、`references/*`、`tests/*` | **MIT** | `LICENSE` |
| ② 本项目派生的词库结构（15 列增强词表、4 列词形映射、容器封装格式） | 字段编排、L1 音标归一规则、词形筛选口径 | **MIT**（作为本项目作品） | `LICENSE` |
| ③ 第三方数据 ECDICT | 音标 / 英义 / 词频 / 柯林斯 / 牛津 3000 / `lemma.en.txt` 词形映射 | **MIT**，Copyright (c) 2025 Linwei | `references/ECDICT_LICENSE.txt` |
| ④ 原始中文词表 `四级六级雅思词汇表.csv` | 词头、等级、词性、中文释义、例句（原 5 列原值） | **MIT**，Copyright (c) 2026 FunInSpring（权利人即本项目维护者） | `LICENSE` + §3 |

MIT 允许使用、复制、修改、合并、发布、分发、再授权与销售，条件是**保留版权声明与许可声明**。
因此发布包必须同时携带 `LICENSE` 与 `references/ECDICT_LICENSE.txt`；二者缺一，③ 的再分发条件即不成立。

## 2. ECDICT 使用范围（可复核）

- 来源：`skywind3000/ECDICT`，<https://github.com/skywind3000/ECDICT>，MIT，Copyright (c) 2025 Linwei。
- 只取 `ecdict.csv` 的 `phonetic / definition / pos / collins / oxford / tag / bnc / frq / exchange` 与 `lemma.en.txt` 的词形映射，用于**给现有 6,861 条词条补字段**与**生成词形→词根映射**。
- 未引入 77 万条全量词条（层 C 不做）、短语、网络释义、`detail` / `audio` 空列。
- 词条数恒为 6,861，例句 3,624 条全部来自原词表，未用 ECDICT 或原书句子补写。
- 词形映射只保留「唯一词根 + 词根在现有词库 + 词形自身不在词库」的 11,825 条。

## 3. 原始中文词表的再分发授权（D-05，**已结案**）

**结案结论：原始词表的著作权人就是本项目维护者本人，已于 2026-11-12 明确按 MIT 授权随本项目再分发。**

| 项 | 内容 |
|---|---|
| 词表 | `四级六级雅思词汇表.csv`（增强后为 6,861 条 / 15 列） |
| 权利人 | **FunInSpring（本项目维护者，即词表作者本人）** |
| 授权方式 | **MIT**（与本项目同一许可，不引入第二套） |
| 确认时间 | 2026-11-12 |
| 确认原话 | 「四级/六级雅思词表是我自己整理的，也按照 MIT 授权给大家」 |
| 登记位置 | `assets/lexicon.source.json` → `license.original_wordlist`（`status: closed`）与 `license.lexicon_redistribution` |

权利人与本项目维护者是同一人，因此 ④ 不再存在"第三方授权缺失"问题：词表可按 MIT
使用、复制、修改、分发、再授权与销售，条件是**保留版权声明与本许可声明**——即随包携带
`LICENSE` 与本文件。

历史口径说明：`VR-ACC-2026-1109-01` 当时把 D-05 记为"权利人授权未确认"，原因是本机没有
授权凭证记录、且不能替权利人下结论。**该结论已于 2026-11-12 由权利人本人确认后结案**；
此前"待确认"的谨慎表述不再适用于当前版本，历史报告的原文保留不改（那是当时的真实状态）。

> 封装（`assets/lexicon.vault`）依旧不是授权手段，只是"只防君子不防小人"的轻量门槛；
> 它的存在不改变上述许可状态。

## 4. 分发清单（发布包必须包含）

| 文件 | 作用 |
|---|---|
| `LICENSE` | 本项目 MIT 原文 |
| `THIRD_PARTY_NOTICES.md` | 本声明（分层口径 + D-05 结案说明） |
| `references/ECDICT_LICENSE.txt` | ECDICT MIT 原文 + 出处登记 |
| `assets/lexicon.source.json` | 词库来源、内容哈希与 `license` 字段 |
| `assets/lexicon.vault` / `assets/lexicon.forms.vault` | 随包数据容器 |

## 5. 用户自备词表

用户用 `--lexicon` 指向自己的 CSV 时，该词表的许可由用户自行负责；本项目不复制、不上传、
不把它写进发布包。程序只在本地读取并按需生成成品。

## 6. 与原验收报告的关系

`外机测试验收报告.md` D-05 的状态更新为：**ECDICT 来源已登记并合规（MIT 原文随包）；
原始中文词表已由权利人本人确认按 MIT 授权（2026-11-12 结案）**。其余结论不受本声明影响。

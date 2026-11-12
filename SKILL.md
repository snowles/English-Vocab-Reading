---
name: vocab-reading
display_name: 英语词汇伴读
display_name_en: English Vocabulary Reading Companion
description: '英语词汇伴读——用随包词库（6861 条 15 列增强词表，含四级/六级/雅思标签与音标、词形信息）标注英文原版 EPUB：正文写成 fished(捕鱼,[1]) 这种不带等级的语境短注，自然段后按编号给出完整词条（含完整释义、例句、按被标注词形自身标注的音标、中文可读词形信息；短段落合并成一组）；组合（如 benevolent skin cancer）整体解释、成员信息分开保存，整体不继承成员等级。正式出书前宿主必须主动问用户选「初级（带中文译文）」还是「高级（不带）」，初级译文由宿主按自然段写回、程序只校验与渲染。也支持用户自备含托福/GRE/牛津3000/AWL 标签的 CSV。词库零配置、无需用户提供词表；语境由宿主智能体（Codex / WorkBuddy / 豆包工作版）判断并独立审查，Skill 自身不含模型调用、脚本不主动联网。当用户想把英文书做成「边读边背单词」的版本、说」把这本书做成伴读版」、要求给 EPUB 正文标生词或加词注时使用。不用于 PDF/OCR 重排、DRM 解密；不改源文件、不覆盖既有输出。'
description_zh: '英语词汇伴读：把英文原版 EPUB 标注成边读边背单词的版本——正文语境短注 (短义,[n])，自然段后按编号给出完整词条（完整释义、例句、按被标注词形自身标注的音标、中文可读的词形信息）。随包 6861 条词库（四级/六级/雅思）与 11825 条词形表，用户无需准备词表。正式出书前必问读者选初级（带中文参考译文）还是高级（不带）；书名、章节名、标题不翻译。语境判断与独立审查由宿主完成，技能自身不调用模型、不联网。'
description_en: 'Vocabulary reading companion for English EPUBs: inline contextual gloss notes such as fished(gloss,[1]) in the body, plus numbered entries after each natural paragraph with the full gloss, example sentence, IPA of the inflected form actually used, and readable morphology. Ships with a bundled 6,861-entry lexicon (CET-4/6, IELTS) and an 11,825-entry inflected-form map, so no word list is required. The host must ask the reader to choose Basic (with Chinese reference translations) or Advanced (no translation) before generating; book titles, chapter titles and headings are never translated. Context disambiguation and independent review are done by the host agent; the skill calls no model and no network.'
version: 0.10.7
author: FunInSpring
category: content
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# 英语词汇伴读 Skill（当前实现：§16–§23，含 §22 兼容交付与 §23 省心化）

在阅读英文原版书时标注生词：正文里在词后加一行浅色短注（语境义 + 章内编号，**不再显示等级**），
自然段后按编号列出完整学习内容（短段合并成一组；含音标与词形信息）。**不改写、不摘编英文正文；
不修改源文件；不改原目录与章节顺序；不覆盖既有输出。**
中文译文只在用户选择「初级」时作为**独立的自然段附注**出现，由宿主提供，段前只加
「参考译文：」，不逐段重复免责声明（免责说明记在报告里），绝不替换或混入英文原文。
**书名、章节名、标题一律不翻译**，它们通常出现在最前面。

## 触发条件

- "把这本书做成伴读版 / 边读边背单词的版本"（**最常见，用户不会再给词表**）
- "用四级/雅思词表标注这本书 / 这个 EPUB"
- "把 D:\Books\x.epub 做成伴读版"
- "标注成 fished(钓鱼,[1]) 这种，自然段后给完整词条"
- 用户提供 `.epub` + 自己的词表 `.csv`，希望产出新的 `.epub`

不适用：脱离伴读标注的独立全文翻译、PDF 重排、OCR、DRM 解密。
（初级模式的段落中文译文属于伴读附注，不在此列。）

## 编排契约（§23，宿主必须遵守）

**用户用自然语言提需求，宿主负责把意图变成完整链路，不能把中间步骤丢回给用户。**
用户说「把这本书按雅思做成伴读版」时，宿主应自行抽出书路径、等级档位、输出位置，
然后自动依次跑完下列 ①–⑤；每一步都用退出码与产物校验，失败立即给人话指引：

| 步骤 | 宿主动作 | 完成判据 |
|---|---|---|
| ⓪ | **主动问用户阅读档位**：「这本要做初级（每段带中文译文）还是高级（只看原文＋词注）？」用自然语言给两个选项，不要让用户记参数。用户没明确回答就**不得出书** | 得到一个明确选择，并从 ② 起整条链路固定用同一档位（档位进 `config_hash`，中途改档位会让识别/审查结果失效） |
| ① | 从自然语言里定位 EPUB 路径（必要时询问一次），默认 `--grades` 按用户提到的档位 | 文件存在、非 DRM |
| ② | 跑 `--emit-annotations <units>` 导出窗口与任务清单；带上 ⓪ 选定的 `--reader-mode` 与 `--require-reader-mode` | 退出码 0，窗口分片齐全 |
| ③ | **宿主自己充当识别者**，逐窗口写 `ann-*.json`（token ID、语境义、词条来源）。若选初级，另跑 `--emit-translations <dir>` 导出自然段请求，逐段写回 `vocab-para-translations/v1` 译文分片 | 每个窗口都写了 `windows` 完成记录；初级模式译文覆盖率 ≥ 95% |
| ④ | 跑 `--apply-annotations <units> --emit-review <review>`；**另起一轮独立审查**，逐条写 `review-*.json`（每条带 `reviewed_revision_hash`） | `modify` 的版本必须复审；无 pending |
| ⑤ | 跑最终 `--apply-annotations <units> --out <目录> --html <目录或.html>`（初级另带 `--translations`），读报告 `release.publishable`、`reader_mode` 与 `deliverables`，只把成品路径与一句人话结论交给用户 | `publishable=true`、`reader_mode.confirmed=true`、初级时 `has_chinese_translation=true`、`deliverables.epub.ok=true`、`deliverables.html.ok=true` |

红线（逐条适用；用户催促、时间压力都不构成豁免）：

- **不得跳过 ⓪**：正式出书必须主动问「初级 / 高级」，没得到明确选择不得生成成品。
  宿主链路带 `--require-reader-mode`，程序会在未确认档位时直接拦下（exit 6）。
  初级模式缺译文或覆盖不足同样不出书，绝不交付「名为初级、实则无译文」的假成品。
- **不得停在 ②**：只导出请求就说"已完成"等于没交付。
- **不得直接跑无 decisions 的一键命令**：默认档（语境短注 + 段后词条）在没有宿主判断时
  会产出 0 标注；程序现在会拦下并删除空书（exit 6），但宿主不该把用户带进这条死路。
- **不得把「赶时间」当成降级授权**（H3-T8 实测修正）：词表直注
  （`--presentation inline` / `--undecided lexicon`）是**降级交付**——只有词表义项、
  没有语境短义、没有独立审查、不写正文括注与段后词条。只有用户**明确接受降级产物**
  （话里指向"先简单标一下 / 不用语境判断 / 只要词表意思就行"）时才可走这条路。
  用户仅以「赶时间 / 别搞那些步骤 / 别审查 / 直接出书」为由要求跳步，**不构成豁免**；
  此时必须：① 说清跳步只能得到浅标降级版、不是正式伴读版；② 给最快可行路径——
  先出 1 章小样走完整链路看效果、再决定是否全本；③ 用户听完仍明确选降级版才可执行，
  且交付时只能称"浅标版"，**不得说成"伴读版 / 生词版已做好"**。
- **不得要求用户提供词表**：§23 起 `assets/lexicon.vault` 随包，零配置即用；
  `--lexicon` 只用于用户主动换自己的 CSV。
- **不得只说 EPUB 就了事**：默认产品是 **EPUB + 离线阅读页**；只给一种时必须是用户
  明确说只要一种。`--html` 省略值即与 EPUB 同目录同名；阅读页章节自动按原书目录推导，
  用户说"前两章"就传 `--html-chapters 1-2`，**不要用段落数冒充章节**。
  HTML 失败时如实说明「EPUB 已交付、阅读页未生成（原因）」，exit 7，不得声称两种都完成。

**输出纪律（面向用户的回话）**：

- 终端摘要里 `可交付` / `格式检查` 是给人看的；`release.*`、`validation.*`、
  `delivery_kind` 等工程字段只进 JSON 报告，不要原样转述给用户。
- **不要把命令行、参数、脚本名贴给用户**。用户不需要、也看不懂
  `--presentation` / `--lexicon` / `--apply-annotations` 这类东西；
  回话只说三件事：成品在哪、这次标了多少词、有没有需要他决定的事。
- 需要用户决策时，用自然语言给选项（如「要精细到按语境判断，还是先快速标一遍？」），
  由宿主自己翻译成对应参数，不要要求用户记住命令。**但这个二选一必须把差异说清**：
  「快速标一遍」是**浅标降级版**（只有词表义项、不做语境判断、没有独立审查、
  词条不进正文括注与段后），不是同一份成品的快慢版本。

面向人的快速上手见 `README.md`，本文件是给宿主看的。

## 工作方式（§19）：程序切词 → 模型选 token ID → 程序回填 → 硬校验 → 写回

**位置不由模型决定**。程序先把每段切成 token（稳定 ID + 原文 + 区间 + DOM 槽位），
模型只返回 token ID；`taki`、`article` 里的 `art` 这类子串根本不构成 token。

1. **段落理解**：一次读 3–5 个连续段落，理解人物、动作、指代与场景。
2. **选 token**：单词选一个完整 token；短语选同段内连续 token 区间
   （可跨空格、标点、连字符）。程序据 ID 回填 surface/span，模型若附带 `surface`
   只做一致性检查，不一致即 `surface_mismatch`。合并后短义必须覆盖整个区间
   （`skin cancer` 写"皮肤癌"，不能只写"癌"）。
3. **查完整词库**（顺序：原词 → 词形还原/拼写变体 → 完整短语 → 组成词）：
   - 整条表达命中 → `source_type=exact_entry`，给 `whole_entry_ids`；
   - 只查得到组成词 → `source_type=component_entries`，在 `components` 里逐个写成员；
   - 原词与还原形都查不到 → `source_type=out_of_lexicon`。
   **查不到本句义项 ≠ 查不到这个词**：`benevolent` 用"不凶险的"这种语境义时，
   仍保留托福来源，不能降为未收录。
4. **冻结内容版本**：识别文件只声明「改了哪里、写了什么」；程序按任务清单
   （`scope.json`：窗口/段落/哈希/绑定）计算每条 `annotation_revision_hash`。
   改短义、用法、成员、词条、类别、动作或义项键都会产生新哈希。
5. **独立审查**：程序导出审查包（完整原句、未选中正文、拟输出内容、哈希），
   另一轮读取后给 `pass / modify / pending / reject`，每条必须回填 `reviewed_revision_hash`。
   `pass` 只对同一哈希有效；`modify` 生成新版本与新哈希，**必须再审新版本**；
   审查确认的 `reject` 视为合理跳过，不计入「待审」。
6. **硬校验 + 确定性写回**：token 必须存在、同段、连续；成员必须落在父区间内且互不重叠；
   所选词条必须与原文词形相关（taking→take、caught→catch）。
   只对「审查 pass + 硬校验通过」的条目做密度、编号、短注、段后词条块（章末索引隐藏）；
   正文标签由程序按词库条目生成，**绝不采信模型填写的等级**。

命令分四步（判断由宿主完成，Skill 不含模型调用）。**§23 起 `--lexicon` 可省略**，
默认读取随包 `assets/lexicon.vault`；用户主动提供自己的 CSV 时再显式指定：

```bash
# ① 出段落窗口 + 任务清单（窗口、段落、哈希、绑定）
python scripts/run_pipeline.py --book <书> --grades 雅思 \
    --emit-annotations out/units [--max-paragraphs 24 --window-paragraphs 4]
# ② 宿主写 ann-*.json（识别；顶层 windows 记录每个窗口的完成情况）
# ③ 冻结版本并导出独立审查包
python scripts/run_pipeline.py --book <书> --grades 雅思 \
    --apply-annotations out/units --emit-review out/units/review_requests \
    [--max-paragraphs 24 --window-paragraphs 4]
# ④ 审查者写 review-*.json（每条带 reviewed_revision_hash，每窗口带 reviewed_window_hash）
python scripts/run_pipeline.py --book <书> --grades 雅思 \
    --apply-annotations out/units --comparison out/comparison.html --out out
```

没有完整审查、缺窗口结果、`modify` 未复审、结构校验失败时**不生成成品**，
退出码 6；`--allow-partial` 只输出有效项，文件名带 `_partial`，退出码 7。

标错的判定会被程序拒绝并**分开计数**：

| 计数 / 错误码 | 含义 |
|---|---|
| `missing_token_ids` | 没有 token ID（旧版 span/surface 记录直接拒收） |
| `unknown_token` / `cross_paragraph` / `non_contiguous` | token 不存在 / 跨段 / 不连续 |
| `surface_mismatch` | 模型附带的 surface 与 token 原文不一致 |
| `member_outside_parent` / `member_overlap` | 成员越出父区间 / 成员互相重叠 |
| `entry_form_mismatch` | 所选词条与原文词形无可靠关系（taking 配 sail） |
| `filtered_out` | 词在完整词库中属被筛掉的等级（不能包装成未收录） |
| `overlap` / `missing_gloss` / `duplicate_annotation_id` | 区间重叠 / 无短义 / ID 重复 |
| `unbound_window` / `window_mismatch` | 标注找不到所属窗口 / 与 pid 所属窗口不一致 |
| `revision_hash_mismatch` | 识别文件声明的内容哈希与重算结果不一致 |
| `stale_review` | 审查针对旧内容版本，不能放行 |
| `skipped_unsupported` | 跨内联标签的完整词（`tak<em>ing</em>`），明确跳过、不输出残缺词 |

**默认发布策略是严格的**：只要存在 `invalid`，就不生成最终 EPUB（退出码 2），
并列出错误码。确实要出书时用 `--allow-partial`：丢弃无效项、重新编号，
报告里保留 `invalid` 与 `invalid_codes`，不得假装全部通过。
invalid 记录**不会**在后续轮次被自动重新关联（错误不复活）。

## 来源、标签与用法类别（§18 核心）

**解析层**支持 7 类标签，用户自备含这些标签的 CSV 时照常解析：

| 词库标签 | 正文简称 |
|---|---|
| 四级 / 六级 / 雅思 / 托福 | `4` / `6` / `ielts` / `toefl` |
| GRE / 牛津3000 / AWL | `GRE` / `牛津` / `AWL` |

**随包词库实际只含三级：四级 / 六级 / 雅思**（6861 条，见
`assets/lexicon.source.json` 的内容哈希与来源记录）。对外说明时不得宣称
随包词库覆盖托福 / GRE / 牛津3000 / AWL——那是解析能力，不是随包内容。

- 单词默认显示**一个**标签，次序 `4 → 6 → ielts → toefl → GRE → 牛津 → AWL`，
  可用 `--label-order` 改（这是显示偏好，不是难度排序）；完整标签全部留在段后词条块。
- 选定等级筛选时，先取该词**实际命中的所选标签**，再按次序决定；不展示词库没有的标签。
- 只命中组成词的组合：**正文不显示等级**，段后词条块分别列出成员的标签与词表原义；
  整体绝不继承成员等级，也不为凑标签而强拆短语。
- 词库未收录：只有确实影响阅读且能确定类别时才用 `usage_category`
  （`专` / `书` / `俚` / `方` / `名`，`专` 附 `usage_domain`）；
  普通词正文只显示 `词语(语境义,[n])`。**不再使用"补充/扩展"占位**。

## 呈现方式

默认 `--presentation inline-notes`：**正文短注 + 自然段后完整词条**（设计文档 §16 / §18）。

词条块就近放在自然段后（11-11 读者规格）：英文词数 ≥ 60 的长段自己成一组；
短段最多连续 4 段、或累计 110 词出一组，词条块插在**该组最后一段之后**。
初级档的中文译文也在段后，DOM 顺序固定为「正文 → 中文译文 → 段后词条」。
章末的 `vocab-chapter-notes` 只保留为**隐藏数据层**（`vocab-archive`，CSS `display:none`），
供结构校验、包外映射与阅读页词表名单使用；正文 `[n]` 链接指向就近的 `vpara-*` 词条，
不指向隐藏索引，读者不会看到两份重复内容。

```text
正文：fished(捕鱼,[1])  alone(独自一人,[2])  skiff(小渔舟,[3])
      Gulf Stream(墨西哥湾流,[4])  benevolent skin cancer(不凶险的皮肤癌,[17])
      ↑词形        ↑语境义          ↑章内编号（11-11 起正文不显示等级/用法类别）

自然段后词条块（整体命中）：
[1] fished → fish | 四级、雅思、牛津3000
    音标：/fiʃt/          ← fished 自己的音标，不是词头 fish 的 /fiʃ/
    词形信息：词根 fish · 本词是词根的过去式/过去分词
    本句意思：捕鱼（fished 为过去式）
    词表词性：n./v.
    词表完整释义：鱼/钓鱼
    词表例句：We caught three little fish
    返回正文

自然段后词条块（仅成员命中，一个编号展开全部成员）：
[5] taking a fish
    整体理解：捕到鱼
    taking → take｜四级、牛津3000
    词表原义：拿；取；花费；乘坐；认为
    扩展说明（take）：音标：teɪk
    fish｜四级、雅思、牛津3000
    词表原义：鱼/钓鱼
    词表例句（fish）：We caught three little fish
    返回正文
```

- **正文不显示等级**（11-11 拍板）：等级 / 标签 / 用法类别统一放到段后词条块头部一次展示，
  正文短注恒为 `(语境义,[n])`。等级由程序从词库读取，绝不采信模型填写。
- **音标按被标注词形自身**（11-11 拍板）：词条块优先取层 B 里该词形自己的 L1 音标，
  `Lived → /livd/`、`fished → /fiʃt/`、`picked → /pikt/`、`taking → /ˈteikiŋ/`；
  没有就**整行省略**，不回退词头音标、不编造（`destroyed` / `craning` / `geese` 都不显示音标行）。
  词形信息给中文可读描述（如 `词根 fish · 本词是词根的过去式/过去分词`），不暴露 `0:fine/1:r` 这类类型码。
- 例句**有就有、没有不强行加、不乱造**，且只来自词表本身；不展示「书中原句」。
- 编号在**逻辑章节**内从 1 开始，按被采用标注的首次出现顺序；同「词条+义项」复用编号，
  异义新增编号；跨章重新从 1 开始，内部锚点 id 仍全书唯一。
- 章节边界按 nav/NCX 目标识别（同文件多章、跨文件一章都支持），**不把一个 xhtml 当一章**，
  也不拿"每 N 词"当章节；编号按章重启，词条块的位置只按自然段分组，不按章节位置。
- 段后词条块区分三种来源（分开写，不混为一谈）：
  **整体命中**（显示完整标签 + 词库原义）/ **仅成员命中**（按成员逐条列 `词形 → 词头`、
  标签、完整原义、例句与扩展说明）/ **未收录**（不生成假例句、假音标、假等级）。
  词表原义**不做定长截断**；短义与词库原义不同时，另起一行标"语境解释"。
- 面向实现者的说明（"整体未收录该组合""正文不显示整体等级"等）只进审计报告，
  **不写进读者词注**；校验器 `note_no_impl_notes` 会拦。
- 语境判断是默认流程（见下）：缺失/低置信/非法决策一律不标并计数；
  只有显式加 `--undecided lexicon` 才用词表首个义项，并在报告中标记为**未消歧版本**。

兼容选项：`--presentation footnote`（章末脚注）与 `--presentation inline`
（纯括注，无词注块）保留，行为不变；`--engine legacy` 也仍是章末词条。
**默认 `--engine units` 才走上面的段后规格**，别把兼容项当成当前交付形态。

## 两个能力层

| 层 | 输入 | 输出 | 是否需要模型 |
|---|---|---|---|
| **阶段 1：离线标注** | EPUB + 词库 | 新 EPUB（含标注与词注） | 不需要，纯确定性匹配 |
| **阶段 2：语义判断** | 段落窗口 | 意义单位 + 短义 + 词库关联 + 审查 | **由 harness 判断**，Skill 只出请求/校验结果 |

阶段 2 不需要任何 SDK、API Key 或网络：**判断由当前对话的智能体自己完成**。
Skill 负责三件事——切出段落窗口生成请求、校验 harness 给的结论、把结论写进 EPUB。

⚠️ 阶段 2 不能等同于"模型推理在本机"：如果宿主是云端模型，句子上下文实际由该宿主处理。
Skill 自身不发网络请求，但上下文出没出本机取决于宿主。

**整本书不送审时**：`--undecided lexicon` 会用词表首个义项标注全部命中，
报告写 `undecided_version: true` 并附警告。这是**检索式查阅版，不是语境标注成品**：
短义是词表原义（可能很长、带多个义项），密度高（《小王子》17,407 词 → 4,042 处）。
要语境短义、组合整体解释与独立审查，必须走 §19 的 `--emit-annotations → ann/review → --apply-annotations`。

## 标注密度（重要）

| `--density` | 行为 |
|---|---|
| `chapter`（**默认**） | 每个词条**每章只标首次出现**，脚注注明"本章出现 N 次" |
| `book` | 每个词条全书只标首次出现 |
| `segment` | 每 `--segment-words`（默认 1200，约两页）重新计数，每段首次 |
| `all` | 所有命中都标（密度很高，一般不建议） |

默认 `chapter`：读者每章只看一次标记，不打断阅读节奏。

**单文件长书要用 `segment`**：很多 EPUB（尤其 Calibre 转换的）把整本正文塞进一个
xhtml，此时 `chapter` 会退化成"全书首次"，标记全挤在开头几页、后面几乎为空。
实测《老人与海》单文件 27,000 词：`chapter` 只有 681 处且 40% 篇幅零标记；
`--density segment --segment-words 1200` 得到 2,034 处、分布均匀。

### 提示量档位 `--reading-level`（§20 A 批）

与 `--density` 正交：`density` 管「同一词义隔多远能再出现」，`reading-level` 管「哪些条目值得提示」。

| 档位 | 行为 |
|---|---|
| `light` | 只省**明确可省**的条目（`reading_need=optional`），难点、组合与新义一律保留 |
| `standard`（**默认**） | 沿用现有尺度，全部保留 |
| `detailed` | 全部保留；档位只管内容范围，**不改编密度**（要重复提示请显式 `--density all`） |

**必要性来自识别字段，不由「说明是否为空」推断**（§21.2）：
每条标注带 `reading_need`（`required / useful / optional / unknown`）和 `sense_protected`。
`required` 或 `sense_protected` → 必留；`optional` → 可省；`unknown`/缺字段 → **保守保留**。
`light` 与 `mastered` 都只动 `optional`，被删原因（`level` / `mastered`）写进报告
`reading.filtered`，与「缺审/无效」分开。组合、专名、未收录词、语境义永远不受档位影响。

`reading_need` 影响是否省略，因此**参与内容版本哈希**：改了这个判断必须重新审查，
旧 `pass` 不会延续。只切换档位（不改判断）不需要重跑模型。

**档位与名单不进 config_hash**，所以换档位不会让已完成的审查失效。但要真正「增加」条目，
必须在识别阶段用 `--emit-annotations --reading-level detailed` 重新识别并重新审查——
已审查集永远是任何档位的上限，渲染阶段不补未审条目。

### 本地熟词名单 `--word-list`

CSV（`word,status`）或 JSON（`{"mastered": [...], "focus": [...]}`），`status` 取 `mastered` / `focus`
（也认「已掌握 / 重点学习」）。

- `mastered` 只减少**明确可省**（`optional`）的条目；组合、未收录、语境义、
  `unknown` 一律保留。`take` 在名单里，`taking a fish` 整体解释照旧显示；
  `order` 的「订单」已掌握，也不会自动隐藏 `orders` 的「吩咐」用法。
- `focus` 优先于 `mastered`，并会把 `optional` 提升为保留，轻提示档也不会删。
- 只影响展示，不改词库与标注内容。名单只作用于后续生成，已导出的 EPUB / HTML 不会自动变。

网页里标记「已掌握/重点学习」时，导出键用**已核验原形**（隐藏章末索引的 `data-head`）：
标记 `swallowing` 导出 `swallow`，`caught` 导出 `catch`；词组按整体保存
（`taking my engine apart` 不拆成 `take`）。没有可信原形的未收录词保留原文词形。

## 阅读页（HTML，§20 A 批）

```bash
python scripts/build_reader_html.py --config examples/preview_two_chapters/reader_config.json
```

配置里每本书给 `epub`（`--apply-annotations` 的产物）、`prefix`（锚点前缀，避免多本书 id 冲突）
和 `sections`（`all: true` 取整章；或 `wanted_source` 指向 units 目录、用 `start/take` 取连续段落）。
正文、括注、词条、插图都从产物里取，不重新渲染、不调用模型；图片转 data URI，单文件离线可开。

- 中文提示开关：关掉只隐藏 `.vocab-note`，英文原文和词语下划线不变；
- 点词或键盘 Enter/Space 打开详解，内容克隆自隐藏章末索引里的完整词条；
- 阅读位置按书存 `localStorage`，锚点优先段落 id，`data-version`（产物哈希）不一致时回章节开头；
- 段后词条块直接平铺显示在正文里，不折叠、不依赖点击；隐藏索引不再另起 `<details>` 词表；
- 详解里可临时标「已掌握 / 重点学习」，导出 `word,status` CSV，可直接作为 `--word-list` 输入；
- 渐进增强：**默认所有书顺序可读、依赖脚本的控件隐藏**；脚本可用时才切单书、
  显示开关与弹层。无脚本时正文、括注、目录、段后词条、`[n]` 回链都可用，
  只是不记忆位置；存储被禁但脚本可用时功能照常，只有位置记忆失效。

生成后建议用 `reader.html?notes=0` 检查一次「先读原文」效果。

## 长书处理与纠错（§20 B 批）

```bash
# 断点续作：任务清单一致才复用已有窗口
python scripts/run_pipeline.py ... --emit-annotations <units> --resume

# 局部更新：让某条审查失效，改完重审再出书
python scripts/run_pipeline.py ... --apply-annotations <units> \
    --invalidate a-xxxxxxxxxx --out <out>

# 本地纠错：网页导出的反馈 → 作废旧审查 + 待复核清单
python scripts/run_pipeline.py ... --apply-annotations <units> \
    --feedback vocab-feedback.json --feedback-review review.json
```

- 报告 `release.progress` 按章给出 `windows/identified/reviewed` 与 `items/passed/pending`；
  `complete` 要求窗口全覆盖**且**待审条目为 0，产物存在不等于通过。
- `--resume` 只认 `task_id` 完全一致；书、词库、配置、窗口范围变化都会重建。
- `--invalidate` 只删审查结论，条目不删；失效后变 `pending`，发布门槛拦住。
- 反馈**不自动采纳**：它只让旧结论失效，修改仍要回到识别 → 独立审查 → 硬校验。
- 局部更新当前是整本重渲染（编号与回链正确），没有做只重建受影响章节的增量优化。

## 需要向用户确认的信息

1. **阅读档位（唯一必问项，⓪）**：用自然语言问「这本要做初级（每段带中文译文）还是
   高级（只看原文＋词注）？」用户明确回答前不得出书。选初级时译文由宿主逐自然段翻译，
   成品每段后直接显示，段前统一写「参考译文：」；**不再逐段重复「仅供参考」**，
   免责说明只记在报告 `reader_mode.translation_disclaimer` 里。选高级时完全不带中文译文。
   **书名、章节名、标题不翻译**：译文请求已自动排除这类段落（`h1`–`h6`、`epub:type`
   标题语义、class 含 title、章首与章节名同文），宿主不必也不得自己补译。
2. **书**：EPUB 绝对路径（确认存在、非加密/DRM、非已标注文件）。
3. **词表**：**默认用随包 `assets/lexicon.vault`，不必询问、也不要猜用户目录**。
   只有用户主动说「用我自己的词表」时才问 CSV 路径并传 `--lexicon`。
4. **等级筛选**（可选）：如只看 `四级,雅思`；不筛选则全标签参与。
5. **标签显示次序**（可选，默认 `4,6,ielts,toefl,GRE,牛津,AWL`）。
6. **标注密度**（可选，默认每章首次；单文件长书用 `--density segment`）。
7. **词表页 / 输出位置**（可选）。

## 执行步骤

词库**随包、零配置**：`--lexicon` 现在是可选参数，默认用
`assets/lexicon.vault`（纯文本封装，运行时自动解封，见下节）。
只有在用户主动给出自己的 CSV 时才需要 `--lexicon`。

```bash
# 0) 安装自检（宿主在新环境首次使用前跑一次；输出「结论：就绪」即可用）
python -m scripts.doctor --smoke

# —— 词表直注（降级交付：只有词表义项、无语境短义、无独立审查）——
# 仅当用户明确接受浅标版时才用；用户以「赶时间」为由要求跳步不构成豁免
python scripts/run_pipeline.py --book "<书.epub>" \
    --presentation inline [--lexicon "<自己的词表.csv>"] \
    [--grades 四级,雅思] \
    [--density chapter|book|segment|all] [--segment-words 1200] \
    [--max-note-chars 12] [--no-stoplist] [--glossary] \
    [--check-policy compatible|strict] [--no-epubcheck] \
    [--out <输出目录>] [--report <报告.json>] \
    [--html [<目录或 .html>]] [--html-chapters 1-2] [--html-force]
```

`--out` 省略时默认写到 **`<书目录>/<书名>_伴读/`**，报告同目录 `vocab_task_report.json`；
任务文件不写进技能安装目录。`--html` 省略值即与 EPUB 同名同目录，
章节范围按原书目录的逻辑章节（`--html-chapters`），不是段落数。

⚠️ **不要只给 `--book` 就期待出书**。默认的 `--presentation inline-notes`
（正文语境短注 + 段后完整词条）必须由宿主按上方「编排契约」完成识别与独立审查；
没有 decisions 时会产出 **0 处标注**。程序按实际产出判定：删除空书、返回 exit 6、
在 stderr 给出二选一指引，**绝不把空书报成「已完成」**（§23 实测修正）。
可立即出书的三条直出路径：`--presentation inline`、`--presentation footnote`、
`--undecided lexicon`——**这三条都属降级交付**（只有词表义项、无语境短义、无独立审查，
不写正文括注与段后词条），只有用户明确接受降级产物时才可用；正式伴读版只能走完整四步
`--apply-annotations` 链路。**不要把直出路径当作跳过识别与审查的捷径。**

### 兼容：legacy v2 消歧（仅 `--engine legacy`，不是默认链路）

> 默认链路是上文 v6（冻结版本 + 独立审查 + 严格门槛）。本节保留旧 `--emit-requests /
> --apply-decisions` 的兼容能力，便于处理历史调用；它没有内容版本哈希，
> 不能用于需要「审查绑定」的交付。

```bash
# ① 出请求：只导出「待判定」的候选，每个文件自描述、可直接读
python scripts/run_pipeline.py --book "<书>" [ --lexicon "<自己的词表>" ] \
    --emit-requests out/requests [--include-multi-sense] [--batch-size 50]

# ② 你（harness）读 out/requests/part-*.json，写 out/decisions/part-*.json
#    结构见 schema.md「消歧决策」，文件名与请求分片一一对应

# ③ 应用并生成 EPUB（自动校验结论合法性）
python scripts/run_pipeline.py --book "<书>" [ --lexicon "<自己的词表>" ] \
    --apply-decisions out/decisions [--undecided skip|all-senses]
```

**harness 的判定规则（v2 协议，硬约束）**

1. `action` 三选一：`annotate` / `skip` / `uncertain`。判断不了用 `uncertain`，不要猜。
2. `short_gloss_zh` 只给**当前语境的一个**意思（≤8 字为宜），不要堆词典义项；
   能在 `options[*].gloss` / `senses` 里找到即视为"来自词表"，否则记为 `cleaned`
   （允许对词表噪声做忠实整理，但段后词条块会同时显示词表原始释义，可审计）。
3. `selected_entry_ids` 必须取自该 candidate 的 options，且决定词性/等级/例句/完整释义。
4. `sense_id` 建议直接用 `short_gloss_zh`；同章同词同义会复用编号。
5. **专有名词组合**（如 `Gulf Stream`）不适合逐词学习：对涉及的每个词返回
   `action=skip` + `reason_code=proper_noun`，不要拆成"海湾""小河"。
6. 词表里没有对应义项时（如 `at their orders` 的"吩咐"）返回 `action=skip` +
   `no_matching_sense`；**不得造一个词表里没有的意思**冒充已消歧。
7. 只依据 candidate 给出的 `context` 判断；一次处理一个 part 文件，分片间不要互相推断。
8. 决策会绑定书 SHA-256、词表 SHA-256、扫描配置指纹与协议版本，任一不符拒绝应用。

`inline-notes` 下**全量候选送审**（不再只挑多义项）：单条词条、单一义项也可能不合语境
（`fish` 名词/动词、`sail` 帆/航行）。量级参考：Animal Farm 雅思词表约 2,300 条 /
47 个分片；《老人与海》380 词样例 40 条。

兼容模式的 `--include-multi-sense` 仅影响旧 `footnote` / `inline` 引擎。

## 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 成功（完整交付 / 请求或审查包已产出 / 完整审查后 no_annotations） | 汇报输出路径与 `release` 状态 |
| 2 | 输入错误（格式不支持 / 结构损坏 / 词表不可用 / 版本绑定或分片错配 / `strict` 与 `--no-epubcheck` 冲突） | 原样转述，不重试 |
| 3 | 输入已含本技能标注 | 提示用户改用原始文件 |
| 4 | 输出结构校验未通过，或兼容判定阻断（新增规范问题 / 工具出错 / strict 非零 ERROR；临时文件与 pending 映射已删除） | 看 `validation.errors` 与 `release.compatibility.issues`，视为未交付 |
| 5 | 未预期内部错误 | 附 traceback，别隐藏 |
| 6 | 发布门槛未通过（缺审 / 待复审 / 范围缺失 / 硬校验失败），或**直出链路 0 处标注**（空书已删除） | 不生成成品；看 `release.pending_review` / `missing_scope`，或按 stderr 的二选一指引改走词表直注（**= 降级交付，须先向用户说明差异，不得当成跳审捷径**）/ 宿主编排 |
| 7 | 仅部分交付（显式 `--allow-partial`） | 文件名带 `_partial`，`publishable=false` |

## 安装与发布包

- 新环境先跑 `python -m scripts.doctor --smoke`；最后一行「结论：就绪」才继续，
  出现 `✗` 按提示处理（缺依赖 / 词库不完整 / 冒烟失败）。
- 面向人的安装与启用步骤见 `INSTALL.md`（Windows + WorkBuddy 实测；其他宿主未实测）。
- 发布包按显式清单生成：`python -m scripts.pack_release`。包内含运行脚本、随包词库、
  `smoke/book.epub` 冒烟素材与人读文档；排除缓存、样例书、历史输出与私有绝对路径。
  `--check` 只校验清单与路径泄露，不打包。

## 工作流内部实现（给维护者）

实现细节、模块分工与数据流见 `references/maintenance.md`。

## 已知限制

- **词表直注不是「跳过审查」的替代品**：它是降级交付（只有词表义项、无语境短义、
  无独立审查，不写正文括注与段后词条）。用户以「赶时间 / 别审查 / 直接出书」为由要求
  跳步**不构成豁免**，见「编排契约」的对应红线；降级产物只能称"浅标版"。
- 跨 HTML 节点的短语（`give <em>up</em>`）不强行拼接，计入 `skipped_cross_node`。
- 不做消歧时，多义/多词条命中会列出词表全部相关释义。
- 现有 `<a>` 链接内文字、原有脚注内文字不标注。
- 消歧结论只覆盖"被标注的那一处"（默认每章首次），同一词的其它出现不重复判定。
- **官方规范状态与可交付结论分开**（§22）：EPUBCheck 5.4.0 已实跑，产物**不再引入**
  新的规范问题（标注 DOM 已改为 XHTML 1.1 合规，见下文「EPUB 2 DOM 合规与兼容交付」）。
  但两本样例原书自带瑕疵（小王子 3 ERROR；老人与海 14 ERROR + 5 WARNING）依旧存在，
  因此 `release.official_check.status` 如实为 `failed`，默认 `compatible` 策略下
  `publishable=true`。**这不等于官方校验通过**，需要官方零错误时必须用
  `--check-policy strict`（两本原书会因此被阻断），或显式做确定性兼容修复。
  详见 `ACCEPTANCE.md` §18 / §19。
- 真实阅读器人工检查**尚未验收**（无 Calibre/Thorium/Apple Books）；
  Firefox / Safari 未测——见 `ACCEPTANCE.md` §19。
- 三档中 `detailed` 的**增量效果未验收**：已审查集合是上限，须以
  `--emit-annotations --reading-level detailed` 重新识别并重新审查才能证明增量。
- 局部更新当前是整本重渲染（编号与回链正确），未做只重建受影响章节的增量优化；
  纠错反馈不自动复核、不自动改正文；长书小样预览仍是 `--paragraph-start` /
  `--max-paragraphs` 命令，没有专门交互页。
- 浏览器「下载写入磁盘」未验证（headless 下改为拦截 Blob 文本，内容与下载一致）。
- §23 起完整词库已随包（`assets/lexicon.vault`），但**只防顺手查看**：
  源码可派生密钥、产物词条块保留完整释义，都不构成保密（见下节）。
  容器由第三方整理词表生成时，**分发许可须先确认**。

## EPUB 2 DOM 合规与兼容交付（§22）

产物只保留 XHTML 1.1 合法写法，交互字段存在包外映射
`<产物>.vocab-map.json`（schema `vocab-reader-map/v1`，按稳定 id 绑定并记 SHA-256）。
官方校验默认 `--check-policy compatible`：新增问题为 0 即可交付，原书遗留瑕疵如实披露；
`strict` 要求 ERROR/FATAL 归零。完整判定表、问题五分类与兼容修复规则见
`references/maintenance.md`。

## 随包词库与轻量封装（§23，省心化）

词库随包 `assets/lexicon.vault`（6861 条，四级/六级/雅思），`--lexicon` 可省略；
容器只防顺手查看，**不得对外宣称保密**。来源、内容哈希与核验结论见
`assets/lexicon.source.json`，实现与重建方式见 `references/maintenance.md`。

## 阅读档位、译文通道与 11-11 裁定（§24）

**两档（出书前宿主必问，见编排契约 ⓪）**

| 档位 | 中文译文 | 其余规格 |
|---|---|---|
| `basic` 初级 | 每个自然段后直接显示「参考译文：…」（不逐段加免责声明） | 与高级完全一致 |
| `advanced` 高级 | 完全不带 | 正文短注 + 段后词条（章末索引隐藏） |

**书名 / 章节名 / 标题不翻译**（11-11 拍板）：`reader_mode.paragraph_records()` 导出译文请求时
就把标题类段落排除（`h1`–`h6`、`epub:type` 含 title、class 含 title、章首段与章节名同文），
渲染端 `_append_translation()` 再挡一层，宿主即使多给译文也不会印进书里。

译文**不是**程序翻译的：程序只导出 `vocab-para-translation-request/v1`（`pid` + 原文 +
`text_sha256`），宿主逐段写回 `vocab-para-translations/v1`。程序按 `pid + 原文哈希` 双重定位，
串段即整段省略；初级覆盖率低于 95% 或完全没译文时**不出成品**。
EPUB 不支持点击，所以译文直接平铺在段后，不做折叠、不做开关；英文原文逐字不改。

宿主链路带 `--require-reader-mode`，未确认档位直接 exit 6。报告新增
`reader_mode.{mode,confirmed,has_chinese_translation,translation_coverage}`；
⑤ 的完成判据必须包含 `reader_mode.confirmed=true`。档位进 `config_hash`，
**中途改档位会让已完成的识别与审查全部失效**，必须从 ② 重新开始。

**词库层 P1-b（0.10.0）**：随包词库升级为 15 列增强词表（层 A）+ 独立词形表
`assets/lexicon.forms.vault`（层 B，11,825 条）。音标只做 L1 字符归一，L2 不做；
例句 3,624 条原样，词条数 6,861 不变，层 C 扩词头与层 D 全量都不做。
词形表只作为 `lemma_candidates()` 的**候选来源**，§21 token 定位与词形硬校验照常执行。

**读者 11-11 明确裁定（不要后续擅自改回）**

- 同句 `strange` **不补标**：按现有识别结果交付，不为了「看起来该标」追标。
- 6 处专名清理时带走的角标**不恢复**：保持现状，不回补编号。
- 许可统一按 **MIT 体系**设计：`LICENSE`（本项目）+ `THIRD_PARTY_NOTICES.md`（分层口径）
  + `references/ECDICT_LICENSE.txt`（ECDICT MIT 原文）随包。
  原始中文词表由**本项目维护者本人整理并按 MIT 授权**（11-12 确认，D-05 已结案）；
  封装只是轻量门槛、不是授权手段。登记见 `assets/lexicon.source.json` 的 `license` 段。

**已固化（11-11 补修，0.10.1）**：段后词条块不再只是 `外机测试v1/02_tools/reflow_notes.py`
的外置版式，已并入 `scripts/render_units.py` 默认渲染链：按自然段分组插入 `vocab-para-notes`、
正文 `[n]` 改指就近 `vpara-*`、章末块降级为隐藏索引；EPUB 与离线阅读页共用同一份内容。
初级译文与词条块都在段后，顺序固定「正文 → 译文 → 词条」。
`阅读体验试点v1` 的专名译名 `Name(译名)` 与「默认通行译本可换」**仍只是外置试点**，
未并入本体，别当成已支持。

## 测试

- 用例清单、分布与全量回归命令：`references/testing.md`
- **覆盖度矩阵（H1）**：`references/h1-coverage.md` —— 六个维度（链路阶段 / 请求类型 /
  信息完备度 / 输入性质 / 数字来源 / 多轮状态）逐格标明"哪条规则兜住、状态是 ✅/🟡/❌"。
  改代码后回填对应格：新增一条拒绝分支 = 某格转 ✅；删一条校验 = 该格退回 ❌。
  状态为 🟡 的项，**对外文案不许说成"已支持"**。

## 阶段路线图

阶段路线图与未完成项见 `references/testing.md`。


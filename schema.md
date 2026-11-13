# Schema 规范（当前实现：v6 内容版本 + §20 A/B 批 + §22 兼容交付 + §23 省心化）

## 1. 词表 CSV

> §23 起用户不必提供 CSV：`assets/lexicon.vault` 随包，`load_lexicon()` 自动解封；
> 自备 CSV 仍按下列规则解析。容器格式见 §14。

必需列（**按列名映射，不按列序号**）：

| 规范名 | 可接受别名 | 说明 |
|---|---|---|
| `word` | 单词 / 词头 / word / headword / term | 词头，支持单词与短语 |
| `gloss` | 中文释义 / 释义 / gloss / definition / meaning | 必填，作为释义来源 |
| `grade` | 单词等级 / 等级 / level / tag | 四级 / 六级 / 雅思 / 托福 / GRE / 牛津3000 / AWL 及其组合，可为空 |
| `pos` | 词性 / pos / part of speech | 原样保留 |
| `example` | 例句 / example / sentence | 仅提示用，不写入正文 |
| `extra` | 扩展说明 / note / notes | 仅提示用，不写入正文 |

- 编码：UTF-8 / UTF-8 BOM / UTF-16 / GB18030 自动识别；分隔符自动探测（`,` `\t` `;` `|`）。
- 归一化：去首尾空格 → NFC → 折叠连续空白 → casefold；**原始写法保留用于显示**。
- 标签：按 `+ ＋ / ／ 、 , ， ; ； |` 拆分，`四级+雅思` 同时属于两个标签；
  `--grades` 按交集筛选（只影响**匹配与展示**），未命中时错误消息列出可用标签。
- **随包词库实际只含四级 / 六级 / 雅思三个标签**（6861 条）；托福 / GRE /
  牛津3000 / AWL 是解析层支持的标签，只有在用户自备词表时才可能出现。
  来源、内容哈希与核验结论见 `assets/lexicon.source.json`。
- **完整词库与筛选视图分开保存**：`all_entries` / `index_all` 保留全部词条（来源判定用），
  `entries` / `index` 只保留筛选后的词条（匹配用）。这样才能区分
  「真正未收录」与「被等级筛掉」，后者不得被包装成词表外词。
- 词头内并列写法按 `/` 拆（`percent/per cent` → 两个都能命中），主键取第一个写法。
- 完全重复词条合并计数；同词头不同词性/释义各自保留。
- 空词头/空释义/短行缺列记入 `issues` 后跳过，不中断整批。

## 2. v5 语义单位协议（默认引擎 `--engine units`）

### 2.1 切词表（位置由程序生成）

```json
{
  "pid": "OPS/book.xhtml#1",
  "text": "He was taking a fish from the sea.",
  "tokens": [
    {"id": "tk-a16a24012c", "i": 0, "t": "He", "start": 0, "end": 2, "slot": 0},
    {"id": "tk-8b1c9d2e4f", "i": 2, "t": "taking", "start": 7, "end": 13, "slot": 0}
  ],
  "sentences": [
    {"sid": "sn-...", "start": 0, "end": 33,
     "token_ids": ["tk-a16a24012c", "tk-...", "tk-8b1c9d2e4f"]}
  ]
}
```

- 切词规则固定（`TOKENIZER_VERSION`）：Unicode 字母/数字为主，内部允许直/弯撇号
  （`don't`、`boy’s`）；连字符是分隔符（`well-known` → `well` + `known`）；
  缩写整体成词（`U.S.`、`a.m.`）；其余标点空白都是分隔符。
- token ID = `blake2s(切词器版本|书 SHA-256|doc|段号|start|end|原文)`，
  换书、换段、换切词配置都会失效；同段重复词也不会撞 ID。
- `slot` 是 DOM 文本节点序号；一个 token 跨槽位时带 `"cross_slot": true`
  （如 `tak<em>ing</em>`），上层明确跳过，不输出残缺词。
- 偏移口径统一为 Python 字符（Unicode 码点）。

### 2.2 段落窗口请求 `windows-*.json`

```json
{
  "schema": "vocab-annotation-request/v5",
  "protocol": "v5",
  "tokenizer": "v1",
  "instruction": "……只能选 token ID，不能自己数偏移……",
  "response_schema": {"annotations": [{"annotation_id": "a-1", "pid": "...",
      "token_ids": ["tk-..."], "unit_type": "word",
      "source_type": "exact_entry", "whole_entry_ids": ["fish|四级+雅思+牛津3000|n./v."],
      "short_gloss_zh": "捕鱼", "usage_category": "", "action": "annotate"}]},
  "book": {"title": "…", "sha256": "…"},
  "lexicon": {"sha256": "…"},
  "scan": {"grades": ["雅思"], "label_order": [], "hash": "…"},
  "windows": [
    {"window_id": "w-0001", "chapter": 1, "chapter_label": "One",
     "paragraphs": [{"pid": "...", "text": "...", "tokens": [...], "sentences": [...]}],
     "program_hints": [{"pid": "...", "surface": "taking", "token_ids": ["tk-..."],
       "lemma": "take", "entry_ids": ["take|四级+牛津3000|vt."],
       "labels": ["四级"], "label_short": "4", "gloss": "拿；取；花费"}]}
  ]
}
```

- `program_hints` 只是词库预匹配参考（已剔除 the/he/in 等虚词），**不构成标注边界**；
- 提示里直接带 `token_ids`，模型不用自己拼字符；
- 窗口按连续段落切（默认 4 段），句子表随窗口给出。

### 2.3 识别结果 `ann-*.json`

```json
{
  "schema": "vocab-annotation-decisions/v5",
  "protocol": "v5",
  "tokenizer": "v1",
  "book_sha256": "…", "lexicon_sha256": "…", "scan": {"hash": "…"},
  "annotations": [
    {"annotation_id": "a-1", "pid": "OPS/book.xhtml#1",
     "token_ids": ["tk-taking", "tk-a", "tk-fish"],
     "unit_type": "phrase", "source_type": "component_entries",
     "whole_entry_ids": [],
     "components": [
       {"token_ids": ["tk-taking"], "entry_ids": ["take|四级+牛津3000|vt."]},
       {"token_ids": ["tk-fish"], "entry_ids": ["fish|四级+雅思+牛津3000|n./v."]}
     ],
     "short_gloss_zh": "捕到鱼", "usage_category": "", "action": "annotate"}
  ],
  "suppressed": [{"pid": "...", "token_ids": ["tk-..."], "reason": "inside_phrase"}]
}
```

字段要求：

| 字段 | 取值 | 说明 |
|---|---|---|
| `token_ids` | 字符串数组 | **必填**；同段、连续；surface/span 由程序回填 |
| `surface` | 可选 | 仅一致性检查；与 token 原文不符 → `surface_mismatch` |
| `unit_type` | `word` / `phrase` / `proper_name` | 意义单位类型 |
| `source_type` | `exact_entry` / `component_entries` / `out_of_lexicon` | 来源声明 |
| `whole_entry_ids` | 词条 ID 列表 | 仅整体真实命中时填写 |
| `components[].token_ids` | 字符串数组 | 必须落在父区间内、互不重叠 |
| `usage_category` | `专`/`书`/`俚`/`方`/`名` | 仅未收录且确需分类时填 |

**模型不提供 span/offset**；旧版只有 span/surface 的记录判 `missing_token_ids` 并拒收。
即使模型给出 `surface`，也只能与程序回填值比对，不能覆盖。

### 2.4 审查结果 `review-*.json`（可多轮）

```json
{
  "schema": "vocab-annotation-review/v5", "protocol": "v5", "stage": "review",
  "book_sha256": "…",
  "verdicts": [
    {"annotation_id": "a-1", "review_status": "modify",
     "issues": ["组合短义没有覆盖整个区间"], "revised": {"short_gloss_zh": "捕到鱼"}},
    {"annotation_id": "a-2", "review_status": "pass", "issues": []}
  ]
}
```

`modify` 必须带 `revised`，下一轮对修订版再给 `pass` 才写回；
`pending`/`reject` 一律不写。**审查 pass 不能覆盖程序硬校验失败**。

### 2.5 程序侧硬校验（不通过即 invalid）

| 检查 | 错误码 |
|---|---|
| 没有 token ID（旧 span/surface 记录） | `missing_token_ids` |
| token ID 不存在 / 属于别的段落 / 序号不连续 | `unknown_token` / `cross_paragraph` / `non_contiguous` |
| 模型附带 surface 与回填值不一致 | `surface_mismatch` |
| 成员越出父区间 / 成员互相重叠 | `member_outside_parent` / `member_overlap` |
| 所选词条与原文词形无可靠关系 | `entry_form_mismatch` |
| 词在完整词库但被等级筛掉 | `filtered_out` |
| 与已采纳标注重叠 / 无短义 / ID 重复 | `overlap` / `missing_gloss` / `duplicate_annotation_id` |
| 跨内联标签的完整词 | `skipped_unsupported`（跳过，不算 invalid） |
| 书/词库/扫描配置/协议/切词器版本不匹配 | 整体拒绝（退出码 2） |

**发布策略**：`invalid == 0` 才出成品；否则退出码 2 并列出错误码。
`--allow-partial` 才允许丢弃 invalid、重新编号后出书，报告保留 `invalid_codes`。
invalid 记录不会在后续轮次被重新关联（§19.5）。

### 2.6 词形关联判定（`related_form`）

1. 完全相同，或命中词条的并列写法（`percent` / `per cent`）；
2. 所有格去尾（`boy’s` → `boy`）；
3. 规则/不规则还原命中词头或变体（`taking` → `take`、`caught` → `catch`、`orders` → `order`）；
4. 短语按词逐个校验（`gave up` → `give up`）；
5. 后缀候选只用于查找，最终必须等于词库真实词头/变体；不相关就拒绝，
   不因为"某个候选存在"而强行关联。

## 3. `inline-notes` 的 DOM 规范（渲染器内存 DOM；EPUB 2 落盘会净化）

> **§22 重要**：下面示例是渲染器在内存 DOM 中写入的形态。最终 **EPUB 2** 的
> XHTML 里不会保留 `data-*`，章末 `<section>` 也会换成 `<div>`；这些字段按
> §13 的 `vocab-reader-map/v1` 写到包外 sidecar，阅读页 / 校验器在内存里注入。
> 稳定 `id`（`vref-*` / `vnote-*`）、`class`、`href` 与全部可见文本仍在包内。

正文短注：

```xml
<span class="vocab" id="vref-3-1-1" data-vocab-id="fish|四级+雅思|n./v."
      data-entry-id="fish|四级+雅思|n./v." data-note="1" data-chapter="3">fished</span><span class="vocab-note">(钓鱼,4,<a class="vocab-link" href="#vnote-3-1">[1]</a>)</span>
```

- `data-note`：章内编号；`data-chapter`：逻辑章节号；`id="vref-{章}-{号}-{第几处}"`：回链锚点；
- `data-source`：`exact_entry` / `component_entries` / `out_of_lexicon`；
- `data-head`：词库真实词头（仅整体命中/成员行），供渲染后校验词形关系；
- `data-label-scope`：`whole`（整体命中）/ `components`（仅成员命中）/ `none`（未收录）；
- 短注文本 `(语境义,标签,[编号])` 全部是真实文本，不依赖 CSS 生成；
- 标签取值：`4`/`6`/`ielts`/`toefl`/`GRE`/`牛津`/`AWL`，或用法类别 `专`/`书`/`俚`/`方`/`名`；
  **标签位可以为空**（`skiff(小渔舟,[3])`），不写"补充"占位；
- 组合只命中成员时，正文**不写整体标签**（不继承成员等级）。

词条块分两层（0.10.0 段后规格）：

- **可见层 `vocab-para-notes`**：插在自然段组最后一段之后（初级档在该段译文之后）。
  英文词数 ≥ 60 的长段自己一组；短段最多连续 4 段 / 110 词合并一组。
  内部是 `div.vocab-para-item`，id 形如 `vpara-{章}-{编号}-{文档}-g{组号}`；
  正文 `[n]` 的 `a.vocab-link` 指向这个 id。复现词条只留词形与本句意思，加「· 再遇」。
- **隐藏索引 `vocab-chapter-notes vocab-archive`**：仍插在该**逻辑章节**真正结束处，
  CSS `display:none`；供结构校验、包外映射与阅读页词表名单使用，读者不可见。
  结构与字段如下（`<li>` 的 `data-*` 落盘前会剥到包外映射）：

```xml
<div class="vocab-para-notes vocab-notes">
  <p class="vocab-para-title">词汇注释 · 2 条</p>
  <div class="vocab-para-item" id="vpara-3-1-bookxhtml-g1">…完整词条字段…</div>
</div>
```

隐藏索引结构：

```xml
<section class="vocab-chapter-notes" id="vnotes-3">
  <h2 class="vocab-notes-title">本章词汇（Three）</h2>
  <ol class="vocab-list">
    <li class="vocab-item" id="vnote-3-1">
      <p class="vocab-head"><span class="vocab-num">[1]</span><span class="vocab-word"> fished</span><span class="vocab-arrow"> → </span><span class="vocab-lemma">fish</span><span class="vocab-grade"> | 四级、雅思</span></p>
      <p class="vocab-sense">本句意思：钓鱼（fished 为过去式）</p>
      <p class="vocab-pos">词表词性：n./v.</p>
      <p class="vocab-gloss">词表完整释义：鱼/钓鱼</p>
      <p class="vocab-example">词表例句：We caught three little fish</p>
      <p class="vocab-extra">扩展说明：音标：fiʃ</p>
      <p class="vocab-back"><a href="#vref-3-1-1">返回正文</a></p>
    </li>
  </ol>
</section>
```

只命中成员的组合，同一个 `<li>` 内按「成员头行 + 原义/例句/扩展」展开（整体不给等级）：

```xml
<li class="vocab-item" id="vnote-1-5" data-source="component_entries"
    data-label-scope="components">
  <p class="vocab-head"><span class="vocab-num">[5]</span><span class="vocab-word"> taking a fish</span></p>
  <p class="vocab-sense">整体理解：捕到鱼</p>
  <p class="vocab-member" data-head="take"><span class="vocab-member-word">taking</span><span class="vocab-arrow"> → </span><span class="vocab-member-head">take</span><span class="vocab-member-label">｜四级、牛津3000</span></p>
  <p class="vocab-member-gloss">词表原义：拿；取；花费；乘坐；认为</p>
  <p class="vocab-extra">扩展说明（take）：音标：teɪk</p>
  <p class="vocab-member" data-head="fish"><span class="vocab-member-word">fish</span><span class="vocab-member-label">｜四级、雅思、牛津3000</span></p>
  <p class="vocab-member-gloss">词表原义：鱼/钓鱼</p>
  <p class="vocab-example">词表例句（fish）：We caught three little fish</p>
  <p class="vocab-back"><a href="#vref-1-5-1">返回正文</a></p>
</li>
```

- 成员词形来自程序回填的 token 原文；词头来自词库真实 `entry.head`；
  两者不同才显示 `→`（`taking → take`、`caught → catch`）。
- 词表原义**不做定长截断**；例句/扩展说明的来源名用词头（`（take）`），
  不能写成 `（taking）`，避免让读者误以为 take 的音标属于 taking。
- 面向实现者的说明（词库关联策略、为何不显示整体等级等）**不写进读者词注**，
  只进审计报告；校验器 `note_no_impl_notes` 会拦。
- 编号在章节内从 1 连续递增，按被采用标注的首次出现顺序；同「词条+义项」复用编号；
  组合按「整段表面+义项」去重，不复用单个成员的编号。
- 一条被多处引用时，`vocab-back` 里有多个「返回正文(n)」链接；
- 词表没有的字段（例句/扩展说明/词性）整行省略，不凭空补。
- 整体命中时 `vocab-head` 显示 `词形 → 词头 | 完整标签`；
  未收录时显示 `| 用法类别`（若已审查确定），不再附实现说明。

## 4. 标注 DOM 规范（兼容模式 footnote / inline）

命中词：

```xml
<a class="vocab"
   data-vocab-id="vocab-fn-2-study_a1b2c3"
   data-entry-id="study|四级+雅思|n./v."
   data-confidence="0.92"            <!-- 仅在应用了 harness 消歧时存在 -->
   href="#vocab-fn-2-study_a1b2c3"
   id="vocab-ref-2-study_a1b2c3">studies</a>
```

- `data-vocab-id`：本章脚注 id，同时是 href 目标；
- `data-entry-id`：词表词条 ID（`key|等级|词性`），**必填**，用于追溯；
- `data-confidence`：harness 给出的置信度（0–1），仅消歧时写入；
- `id`：回链锚点，每个脚注组在每章只给首次出现加 id。

ID 规范：

| 对象 | 格式 | 示例 |
|---|---|---|
| 章节脚注 | `vocab-fn-{spine序号}-{slug}[_{hash}]` | `vocab-fn-2-study` |
| 正文回链锚点 | `vocab-ref-{spine序号}-{slug}[_{hash}]` | `vocab-ref-2-study` |
| 章节词注容器 | `vocab-notes-{spine序号}` | `vocab-notes-2` |
| slug | 归一化后仅保留 `[a-z0-9]`，其余转 `_` | `look_forward_to` |
| `_{hash}` | 消歧后同一词头出现多个义项时追加（blake2s 前 6 位） | `study_a1b2c3` |

与原书已有 `id` 冲突时自动追加 `-1`、`-2`。

章节词注块（EPUB 3）：

```xml
<aside class="vocab-notes" id="vocab-notes-2" epub:type="footnote">
  <p class="vocab-notes-title">本章词汇</p>
  <ol>
    <li id="vocab-fn-2-work_855789">
      <span class="vocab-word">work</span>
      <span class="vocab-grade"> [四级+雅思]</span>
      <span class="vocab-count"> 本章出现 3 次</span>
      <a class="vocab-back" href="#vocab-ref-2-work_855789" title="返回正文">↩</a>
      <span class="vocab-sense"><span class="vocab-pos">v. </span><span class="vocab-gloss">起作用</span></span>
      <span class="vocab-conf"> 按语境选择 · 置信 0.92</span>
    </li>
  </ol>
</aside>
```

- 未消歧时列出词表全部相关释义（每个词条一个 `span.vocab-sense`）；
- `vocab-count`：密度 `chapter`/`book` 下写"本章出现 N 次"，`all` 且同章多次时写 `×N`；
- EPUB 2 容器为 `<div class="vocab-notes">`，无 `epub:type`。

## 4. 消歧请求 v2（`inline-notes` 默认协议）

```json
{
  "schema": "vocab-disambiguation-request/v2",
  "protocol": "v2",
  "instruction": "……判定规则（见 SKILL.md）……",
  "response_schema": {"decisions": [{"id": "c-…", "action": "annotate",
      "selected_entry_ids": ["fish|四级+雅思|n./v."], "sense_id": "钓鱼",
      "short_gloss_zh": "钓鱼", "context_pos": "v.",
      "usage_note": "fished 为过去式", "confidence": 0.92,
      "reason_code": "context_match"}]},
  "book": {"title": "…", "sha256": "…"},
  "lexicon": {"sha256": "…"},
  "scan": {"grades": ["雅思"], "density": "chapter", "presentation": "inline-notes",
           "segment_words": 1200, "reduce_forms": true, "stoplist": true,
           "min_confidence": 0.55, "hash": "52c913e064a03420"},
  "candidates": [
    {"id": "c-f28d23aaeb", "chapter": "1", "chapter_index": 1, "surface": "fished",
     "head": "fish", "occurrence": 0, "reason": "context_check",
     "context": "He was an old man who fished alone in a skiff in the Gulf Stream",
     "options": [{"entry_id": "fish|四级+雅思|n./v.", "pos": "n./v.",
                  "grade": "四级+雅思", "gloss": "鱼/钓鱼", "senses": ["鱼", "钓鱼"]}]}
  ]
}
```

- **全量送审**：单条词条、单一义项也会进入请求（语境可能不合）；
- `id` = `c-` + `blake2s(v2|doc|chapter|key|occurrence)`，确定性；
- `scan.hash` 是扫描配置指纹，决策必须带回，不符即拒绝应用。

## 5. 消歧决策 v2（harness 写，Skill 校验）

```json
{
  "schema": "vocab-disambiguation-decisions/v2",
  "book_sha256": "…", "lexicon_sha256": "…", "scan": {"hash": "52c913e064a03420"},
  "decisions": [
    {"id": "c-…", "action": "annotate", "selected_entry_ids": ["fish|四级+雅思|n./v."],
     "sense_id": "钓鱼", "short_gloss_zh": "钓鱼", "context_pos": "v.",
     "usage_note": "fished 为过去式", "confidence": 0.92, "reason_code": "context_match"}
  ]
}
```

| 条件 | 结果 |
|---|---|
| `action=skip` | 该处不标注（`skipped_by_model`） |
| `action=uncertain` | 不标注（`uncertain`） |
| `confidence < min_confidence` | 不标注（`low_confidence`） |
| `selected_entry_ids` 不在 options 内 | 非法（`unknown_entry_id`） |
| 没有决策 | 按 `--undecided`：`skip`（默认）/ `lexicon`（未消歧版本） |
| 书/词表/配置/协议任一不匹配 | 整体拒绝（退出码 2） |

`short_gloss_zh` 能在选项释义里找到 → 记为 `lexicon`；找不到但给了 entry_id →
记为 `cleaned`（允许对词表噪声作忠实整理），段后词条块仍显示词表原始释义，报告计数。

## 6. 消歧请求 v1（兼容模式 footnote / inline）

```json
{
  "schema": "vocab-disambiguation-request/v1",
  "part": 1,
  "parts": 46,
  "instruction": "……给 harness 的自然语言说明……",
  "response_schema": {"decisions": [{"id": "c-3f2a91b0c4", "is_target_sense": true,
      "selected_entry_ids": ["work|四级+雅思|v."], "short_gloss_zh": "起作用",
      "confidence": 0.9, "reason_code": "context_match"}]},
  "book": {"title": "Animal Farm", "sha256": "2f7e…"},
  "lexicon": {"sha256": "f914…"},
  "min_confidence": 0.55,
  "max_context_chars": 300,
  "candidates": [
    {
      "id": "c-9a62805b35",
      "chapter": "index_split_004.html",
      "chapter_index": 4,
      "surface": "work",
      "head": "work",
      "occurrence": 0,
      "reason": "multi_entry",
      "context": "Read more at … before the work begins.",
      "options": [
        {"entry_id": "work|四级+雅思|v.", "pos": "v.", "grade": "四级+雅思",
         "gloss": "工作；起作用", "senses": ["工作", "起作用"]},
        {"entry_id": "work|四级|n.", "pos": "n.", "grade": "四级",
         "gloss": "作品；著作", "senses": ["作品", "著作"]}
      ]
    }
  ]
}
```

- `id` = `c-` + `blake2s(chapter|entry_key|occurrence)` 前 10 位，**确定性**：
  生成请求与应用决策两次运行算出同一个 id，无需中间状态。
- `context` 只到句子级，长度 ≤ `max_context_chars`；绝不导出整章/整书/文件路径。
- `reason`：`multi_entry`（同头多词条）或 `multi_sense`（单条释义多义项，需 `--include-multi-sense`）。
- 同目录 `index.json` 记录分片清单与总候选数，不含正文。

## 7. 消歧决策 v1（兼容模式）

```json
{
  "schema": "vocab-disambiguation-decisions/v1",
  "book_sha256": "2f7e…",
  "lexicon_sha256": "f914…",
  "decisions": [
    {
      "id": "c-9a62805b35",
      "is_target_sense": true,
      "selected_entry_ids": ["work|四级+雅思|v."],
      "short_gloss_zh": "起作用",
      "confidence": 0.92,
      "reason_code": "context_match"
    }
  ]
}
```

校验规则（不满足即按 `invalid` / 回退处理，不会误用）：

| 条件 | 结果 |
|---|---|
| `id` 在本次运行中不存在 | 忽略（计入 issues） |
| `confidence < --min-confidence` | 回退：`low_confidence` |
| `is_target_sense = false` | 该处不标注：`not_target` |
| `selected_entry_ids` 含 options 之外的 id | 回退：`unknown_entry_id`（invalid） |
| `short_gloss_zh` 不是任何选项释义/义项的子串 | 回退：`unfaithful_gloss`（invalid） |
| `short_gloss_zh` 为空 | 回退：`empty_gloss`（invalid） |
| 没有对应决策 | 按 `--undecided`：`skip`（默认，不标）或 `all-senses`（列出全部释义） |
| `book_sha256` / `lexicon_sha256` 不匹配 | 整体拒绝（退出码 2） |

回退时该处**不写入猜测结果**：默认直接不标注并计数，绝不假装消歧过。

## 8. 任务报告 JSON（`run_pipeline` 真实输出，节选）

```json
{
  "skill": "vocab-reading-skill",
  "stage": 2,
  "matching_mode": "no-model+harness-disambiguation",
  "timestamp": "2026-11-04T18:02:11+0800",
  "grades_requested": ["雅思"],
  "grades_applied": "6861 -> 3873 词条",
  "lexicon": {"rows_total": 6861, "entries": 3873, "heads": 3873, "phrases": 41,
              "grades": ["六级", "四级", "雅思"], "issues": 0, "encoding": "utf-8-sig"},
  "lexicon_sha256": "f914…",
  "precheck": {"version": "2.0", "chapters_total": 20, "tokens_total": 35980,
               "candidates_estimate_total": 7588, "already_annotated_files": 0},
  "annotation": {
    "density": "segment",
    "segment_words": 1200,
    "chapters": [{"index": 4, "tokens": 1800, "matches": 12, "words": 12,
                  "phrases": 0, "footnotes": 12, "skipped_repeat": 25,
                  "skipped_undecided": 0, "skipped_inside_link": 3,
                  "skipped_cross_node": 0, "skipped_hidden": 0,
                  "candidates": 2, "per_entry": {"work": 3}, "modified": true}],
    "per_entry": {"work": {"display": "work", "gloss": "工作；起作用", "count": 3}},
    "modified_entries": ["OPS/chapter1.xhtml", "OPS/content.opf", "OPS/vocab.css"],
    "total_matches": 3305,
    "total_footnotes": 3305,
    "disambiguation": {
      "mode": "harness",
      "density": "chapter",
      "include_multi_sense": false,
      "min_confidence": 0.55,
      "undecided_policy": "skip",
      "candidates": 1,
      "decisions_loaded": 1,
      "decisions_applied": 1,
      "missing_decision": 0,
      "low_confidence": 0,
      "invalid": 0,
      "not_target": 0,
      "no_decisions_available": 0
    },
    "warnings": []
  },
  "validation": {
    "ok": true,
    "checks": {"mimetype": true, "container": true, "manifest": true, "spine": true,
               "xhtml": true, "links": true, "vocab_refs": true, "sense_trace": true,
               "nav": true, "text_integrity": true, "fidelity": true, "epubcheck": null},
    "errors": [],
    "warnings": ["环境中无 EPUBCheck（Java 工具），使用自研检查替代"],
    "fidelity": {"checked": 11, "identical": 11, "changed_unexpected": 0, "diffs": []},
    "epubcheck": {"ran": false, "reason": "未找到 EPUBCheck 可执行文件或 jar"}
  },
  "output": {"path": "…", "sha256": "…", "glossary": "", "css": "OPS/vocab.css"},
  "input_sha256_before": "…",
  "input_sha256_after": "…",
  "elapsed_sec": 1.9
}
```

字段约定：

- `checks` 值：`true` 通过 / `false` 失败 / `null` 未运行（仅 `epubcheck`）。
- `sense_trace`：每个标注都有 `data-entry-id`；有 `data-confidence` 时必须在 0–1 内。
- `fidelity`：源文件中未声明改动的资源逐字节比对，`changed_unexpected > 0` 即失败。
- `text_integrity`：去掉词注块后，输出正文与源正文逐字一致（防丢字/重复/错位）。
- `annotation.presentation`：`inline`（正文括注，无脚注）或 `footnote`（章末词注块）；
- `annotation.density`：本次使用的密度（`all` / `chapter` / `book` / `segment`）；
- `disambiguation.stoplist_skipped`：因虚词停用表跳过的命中数（仅统计，不标注）；
- `disambiguation.max_note_chars`：inline 括注限长（仅 inline 模式有值）；
  `disambiguation.segment_words` 只在 `segment` 时有值；`skipped_repeat` 是因密度策略未标注的重复出现数。
- 完整真实样例见仓库根目录 `report_example.json`，由下面命令生成（非手写）：

```bash
python scripts/run_pipeline.py --book tests/fixtures/epub3_complex.epub \
  --lexicon tests/fixtures/lexicon_sample.csv --glossary \
  --out examples --report report_example.json --no-epubcheck
```

## 9. 阶段边界

- 本阶段**不调用任何模型**：`disambiguation.py` 不含 SDK、不含网络请求，只做协议与校验。
- 判断责任在 harness；Skill 不接受"没有结论却当成已消歧"的输入。

## 10. v6 内容版本协议（审计整改后默认）

v6 把「定位」与「内容版本」分开：`annotation_id` 稳定指向一处，`annotation_revision_hash`
证明审查针对哪份内容。v5 及更早文件缺少绑定，程序直接拒绝并提示重新生成。

### 10.1 任务清单 `scope.json`

```json
{
  "schema": "vocab-annotation-scope/v6",
  "protocol": "v6",
  "tokenizer": "v1",
  "book_sha256": "…", "lexicon_sha256": "…", "config_hash": "…",
  "identify_prompt": "id-v6.1", "review_prompt": "rv-v6.1",
  "context_pack_hash": "cp-…",
  "max_paragraphs": 24, "window_paragraphs": 4, "paragraph_start": 0,
  "paragraph_count": 24,
  "windows": [
    {"window_id": "w-0001", "chapter": 1, "chapter_label": "Chapter I",
     "pids": ["book.xhtml#0", "…"], "hash": "wh-…"}
  ],
  "pids": ["book.xhtml#0", "…"],
  "task_id": "task-…"
}
```

`task_id` 是清单自身哈希；被改动即拒绝。`paragraph_start`/`max_paragraphs`
声明本次只处理哪一段范围，不能据此宣称全书完成。

### 10.2 识别 `ann-*.json`（`vocab-annotation-decisions/v6`）

顶层除 `book_sha256/lexicon_sha256/scan.hash` 外，必须有
`protocol`、`tokenizer`、`identify_prompt`、`review_prompt`、`context_pack_hash`、`task_id`，
以及 `windows` 覆盖记录：

```json
{"window_id": "w-0001", "status": "identified", "candidates": 4,
 "annotations": ["a-…"], "reason": ""}
```

每条标注新增：

| 字段 | 说明 |
|---|---|
| `window_id` | 所属窗口；与 pid 不一致记 `window_mismatch` |
| `revision_hash` | 可选声明值；程序重算，不一致记 `revision_hash_mismatch` |
| `reader_usage_note` | 读者可见的学习说明（词性/变形/搭配/指代/语境用法） |
| `audit_note` | 只进审计报告，不渲染进 EPUB |

### 10.3 审查包 `review-request-*.json`

由 `--emit-review` 生成，含 `task_id`、`review_round`、`binding`、`instruction`、
`response_schema`、`windows[]`。每个窗口提供：

- `paragraphs`：完整段落；
- `unselected_paragraphs`：本窗口完全没有拟标注的段落（查漏标）；
- `proposed[]`：`annotation_id`、`revision_hash`、`surface`、`span`、
  `proposed_output`（短义、reader、词库标签/原义/例句/成员）、
  `context`（原句、段落、未选中片段）、`audit_note`。

### 10.4 审查 `review-*.json`（`vocab-annotation-review/v6`）

```json
{
  "schema": "vocab-annotation-review/v6", "protocol": "v6",
  "book_sha256": "…", "lexicon_sha256": "…", "config_hash": "…",
  "identify_prompt": "id-v6.1", "review_prompt": "rv-v6.1",
  "context_pack_hash": "cp-…", "task_id": "task-…", "review_round": 2,
  "reviewer": {"kind": "model", "name": "…", "method": "…"},
  "window_verdicts": [
    {"window_id": "w-0001", "reviewed_window_hash": "wh-…",
     "coverage_status": "pass|missing|over_merged", "issues": [], "missed": []}
  ],
  "verdicts": [
    {"annotation_id": "a-…", "reviewed_revision_hash": "rh-…",
     "review_status": "pass|modify|pending|reject", "issues": [],
     "revised": {"short_gloss_zh": "…"}}
  ]
}
```

规则：

- 每个分片都核对绑定，不只看第一片；
- `pass` 只对哈希完全相同的版本生效；
- `modify` 在 `revised` 里给修改字段，程序生成新版本与新哈希，下一轮必须再审；
- 同一哈希出现多个冲突判定 → `conflict`，不采用；
- `reject` 是审查确认的合理跳过，不计入「待审」，但进入报告 `rejected_items`。

### 10.5 报告新增 `release`

```json
"release": {
  "structural_valid": true,
  "scope_complete": true,
  "review_complete": true,
  "delivery_status": "complete",
  "publishable": true,
  "missing_scope": [],
  "pending_review": [],
  "rejected_items": [],
  "invalid_items": [],
  "omitted_items": [],
  "validation": {"invalid": 0, "invalid_codes": {}, "accepted": 71,
                 "skipped_unsupported": 0},
  "counts": {"scope_windows": 6, "identified_windows": 6,
             "reviewed_windows": 6, "annotate_items": 71}
}
```

`delivery_status`：`blocked` / `complete` / `partial` / `no_annotations`。
`publishable` 只在 `complete` 且结构校验通过时为 true。

## 11. 阅读提示偏好（§20 A 批）

三档提示量与本地熟词名单**只决定显示哪些条目**，不改变标注内容、审查结论和词库；
它们也**不进入 `config_hash` / `task_id`**，所以换档位不会让已完成识别与审查失效。

### 11.1 命令行

```bash
--reading-level light|standard|detailed   # 默认 standard
--word-list words.csv                     # 或 words.json
```

### 11.2 判定类别（不使用词库等级）

| 类别 | 条件 | light | standard | detailed |
|---|---|---|---|---|
| `core` | `reading_need=required`，或 `sense_protected=true`，或结构性难点 | 保留 | 保留 | 保留 |
| `useful` | `reading_need=useful`，或 `unknown`/缺字段的单字（保守） | 保留 | 保留 | 保留 |
| `transparent` | `reading_need=optional`（识别给出、审查核对） | **剔除** | 保留 | 保留 |

**必要性来自 `reading_need`，不由 `reader_usage_note` 是否为空推断**（§21.2）。
`unknown`/缺字段一律保守保留，不得默认当作 `optional`。
档位与密度独立：`detailed` **不再**自动把 `chapter` 改成 `all`（§21.4），
需要重复提示时显式传 `--density all`。

### 11.3 名单格式

```csv
word,status
take,mastered
apparition,focus
"taking my engine apart",focus
```

网页导出的键是**已核验原形**：`swallowing → swallow`、`caught → catch`；
词组按整体保存，不拆成员；没有可信原形的未收录词保留原文词形。

JSON 等价写法：`{"mastered": ["take"], "focus": ["apparition"]}`。
`status` 也接受「已掌握 / 重点学习」。匹配复用 `normalize_span` + `lemma_candidates`。

- `mastered` 只剔 `transparent`；组合、未收录、语境义不受影响；
- `focus` 优先于 `mastered`，并把 `transparent` 提升为保留；
- 同一词同时出现在两名单时以 `focus` 为准。

### 11.4 报告字段

```json
"annotation": {"disambiguation": {
  "rendered_items": 58, "rendered_marks": 58,
  "reading": {
    "level": "light", "level_label": "轻提示", "density": "chapter",
    "importance": {"core": 44, "useful": 14, "transparent": 33},
    "filtered_counts": {"level": 33},
    "filtered": [{"annotation_id": "a-…", "surface": "hat",
                  "reason": "level", "importance": "transparent"}],
    "filtered_total": 33,
    "focus_hits": 0,
    "word_list": {"path": "…", "mastered": 3, "focus": 2, "issues": []}
  }
}}
```

`reason` 取 `level`（档位主动筛除）/ `mastered`（名单主动筛除）；
**缺审、无效不计入这里**，仍走 `release.pending_review` / `invalid_items`，两者不能混。

### 11.5 正文与阅读页

- `span.vocab` 增加 `data-importance="core|useful|transparent"`（前端不需要重算规则）
  与 `data-key`（名单键：可信原形或整段 surface），`data-head` 记录原形以便审计；
  EPUB 2 产物中这些属性存于 §13 的 sidecar，由阅读页 / 校验器注入内存 DOM；
- 同章同义去重仍由渲染器 `_dedup_key` + `state.seen` 负责，组合按整段表面作键，
  不会因为成员词已在名单里而被删；
- `scripts/build_reader_html.py` 从产物生成离线阅读页，交互（开关/详解/位置/名单）见 `SKILL.md`。

### 11.6 识别新增字段（§21.2）

| 字段 | 取值 | 说明 |
|---|---|---|
| `reading_need` | `required` / `useful` / `optional` / `unknown` | 提示必要性。缺字段或非法值按 `unknown` 处理；`unknown` 保守保留，不得默认当 `optional` |
| `sense_protected` | 布尔 | 新义或特殊用法保护。为真时任何档位、任何名单都不省略 |

两者都写进 `annotation_fingerprint`，因此改动它们会产生新的 `revision_hash`，
**旧 `pass` 不能沿用**，必须重新审查；只切换档位（判断不变）不需要重跑模型。

审查包 `proposed[]` 会带 `proposed_output.reading_need` / `sense_protected` / `head`，
审查者据此核对判断是否与语境一致，并可在 `revised` 中修改这两项。

## 12. §20 B 批新增数据格式

### 12.1 网页纠错反馈 `vocab-reader-feedback/v1`

阅读页导出的 JSON：单条对象、`{"items": [...]}` 或顶层数组均可；
`--feedback` 接受单个文件或目录（读取目录下全部 `*.json`）。

| 字段 | 说明 |
|---|---|
| `annotation_id` | 必填；缺失的条目忽略并记入 `issues` |
| `kind` | `sense`（意思不对）/ `span`（组合范围不对）/ `too_much`（解释太多）；未知值按 `sense` 处理并记入 `issues` |
| `surface` / `lemma` / `gloss` / `sentence` / `chapter` / `book` | 取自产物 `data-*` 属性，供离线复核，不由模型改写 |
| `version` | 产物版本哈希，用于确认反馈针对哪一版 |
| `note` | 选填说明 |

`--feedback-review` 输出 `vocab-feedback-review/v1`：`{schema, note, items, issues}`，
内容只是**待复核清单**。反馈只让旧审查失效、让发布门槛拦截，不自动改写正文；
修改仍须回到「改识别 → 独立审查 → 硬校验」原链路。

### 12.2 报告新增字段

- `release.progress`：`{chapters, windows, identified, reviewed, items, passed, pending, complete}`；
  `chapters[]` 每行为 `{chapter, label, windows, identified, reviewed, items, passed, pending}`；
  `complete` 要求每个窗口都有同哈希的审查结论，且 `pending` 全为 0。产物存在不等于通过。
- `release.feedback`：`{summary, items, status}`，`status` 为 `none` / `pending_review`。

> §11.4 的 `importance` / `filtered` 示例数字来自 A 批初版（`light=58`）；
> 第 21 节修复后同一小样为 `light=71 / standard=91 / detailed=91`，字段结构不变。
> 早期四份 `examples/preview_two_chapters/*/report_*.json` 生成于这两个字段加入之前，
> 曾不含 `release.progress` / `release.feedback`；**§22 第 4 步已用当前代码重跑刷新，
> 现有四份正式报告均含这两个字段**（没有手工补写 JSON）。

### 12.3 `validation.epubcheck`（EPUBCheck 官方报告摘要）

由 `validate_epub.py::_run_epubcheck` 解析官方 `--json` 报告得到；未安装时为 `null`，
不可用时不写 `ok=true`，绝不把自研检查冒充官方校验。

```json
"epubcheck": {
  "ran": true,
  "ok": false,
  "status": "failed",
  "version": "EPUBCheck v5.4.0",
  "returncode": 1,
  "counts": {"nError": 3, "nWarning": 0, "nFatal": 0, "nUsage": 0},
  "messages": [
    {"severity": "ERROR", "id": "RSC-005", "location": "10258.xhtml",
     "message": "Error while parsing file: element \"img\" missing required attribute \"alt\""}
  ],
  "output_tail": "..."
}
```

- `ok` 只由 EPUBCheck 的返回码决定；`counts` 取官方 `checker` 字段；
- `status`：`passed` / `failed` / `not_run` / `tool_error`，**工具已安装但调用出错**
  必须写 `tool_error`（阻断），工具缺失写 `not_run`（不冒充通过）；
- `messages` 保留前 10 条，字段名兼容官方大写的 `ID`（旧版可能是小写 `id`）；
  完整官方 JSON 另存 `<产物>.epub.epubcheck.json`：顶层 `messages` / `checker` /
  `publication` / `items`，每条 `messages[]` 内含 `ID` / `severity` / `locations[]` /
  `additionalLocations`。报告中的摘要**不能**替代留档；
- CLI 用 `--json <file>`，不是 `--format json`；非 `.epub` 后缀（如 `*.epub.pending`）
  先复制成临时 `candidate.epub` 再跑；
- §22 起 `checks["epubcheck"]=false` 只表示官方状态失败，**不再**令
  `validation.ok=false`；发布结论由 §13 的 `release.compatibility` 单独给出。

## 13. §22 新增数据格式（EPUB 2 合规 + 兼容交付）

### 13.1 `vocab-reader-map/v1`（包外 sidecar）

文件名 `<产物>.epub.vocab-map.json`；校验失败时的临时文件为
`<产物>.epub.vocab-map.json.pending`，成功后随 EPUB 一起原子提交。

```json
{
  "schema": "vocab-reader-map/v1",
  "epub_name": "The little prince_vocab_2.epub",
  "epub_sha256": "b61c76539bd3…",
  "attrs": {
    "vref-1-1-1": {
      "_doc": "7358.xhtml", "_tag": "span", "_class": "vocab", "_text": "fished",
      "data-vocab-id": "a-…", "data-entry-id": "fish|四级+雅思|n./v.",
      "data-note": "1", "data-chapter": "1", "data-importance": "core"
    },
    "vb-73bbfcfc-1": {
      "_doc": "7521.xhtml", "_tag": "span", "_class": "vocab-note",
      "_text": "(撒哈拉沙漠,", "data-gloss": "撒哈拉沙漠"
    }
  }
}
```

- `attrs` 的键是**最终 EPUB 节点上的 `id`**：渲染器已给的 `vref-*` / `vnote-*`
  原样使用；没有 id 的节点补 `vb-{文档哈希}-{序号}`（合法 XHTML ID）。
- `VOCAB_ATTRS` 白名单之外的属性一律不剥离，原书自带 `data-*` 保留；
- `_doc` / `_tag` / `_class` / `_text` 用于 `audit_binding()` 按最终 DOM 核对，
  防止只改旁边 JSON 就通过；`epub_sha256` 绑定具体产物，换产物即失效；
- EPUB 本体只留合法 `id` / `class` / `href`；章末 `<section>` → `<div>`；
  被改节点追加 `vocab-skill` class 作为「已标注」检测标记。

### 13.2 `vocab-epubcheck-baseline/v1`（源书基线缓存）

`_epubcheck_baseline/<源书SHA前16位>-<工具版本>.baseline.json` 存摘要与
`run` / `official` 引用；同目录 `.baseline.official.json` 存**完整**官方 JSON
（每条 `messages[]` 的 `locations[]` / `additionalLocations` 都保留）。字段：
`schema`、`source_path`、`source_sha256`、`tool_version`、`cached`、`run`、`official`。

差异归因 key = `(severity, ID, 归一化消息, 元素/属性目标, 资源路径集合)`，
不含行列号；`additionalLocations` 被归并时路径集合记为 `("(merged)",)`
并在 `diagnostic_note` 说明证据不足。问题五分类：
`acceptable_legacy` / `fixed` / `new` / `blocking` / `unresolved`；
**不按错误码整体放行**（`ANNOTATION_DOM` 专门拦同码的新增非法标注节点）。

### 13.3 `vocab-compat-fixes/v1`（修复清单）

`python -m scripts.compat_fixes --out <工作副本> --report <清单>` 产出：
`schema`、`source_path`、`source_sha256`、`version`、`applicable[]`
（每项含 `rule` / `path` / `before` / `after` / `reason`）、`recorded_only[]`、
`blocked[]`、`verification`。

- 确定性修复：`date_format`、`uri_spaces`、`missing_ncx`、`spine_toc_attr`；
- 只记录不修：`missing_alt`、`legacy_presentation_attr`、`filename_spaces`、
  `invalid_id`；`duplicate_id` 直接阻断创建工作副本；
- 工作副本会改变书籍哈希，**必须重新 emit / identify / review 重建 v6 绑定**；
  默认流水线不调用此工具。

### 13.4 `validation` 新增字段

```json
"validation": {
  "ok": true,
  "checks": {"…": true, "binding": true, "epub2_dom": true, "epubcheck": false},
  "binding": {
    "present": true, "schema": "vocab-reader-map/v1",
    "path": "…/<产物>.epub.vocab-map.json",
    "records": 421, "matched": 421, "missing": 0,
    "injected_attrs": 1197, "audit_errors": 0
  },
  "compatibility": {
    "policy": "compatible",
    "official": {"status": "failed", "ran": true, "version": "5.4.0",
                 "nError": 3, "nWarning": 0, "nFatal": 0},
    "…": "…"
  },
  "epubcheck": { "…": "见 §12.3" }
}
```

- `checks.binding`：从最终 EPUB 实际节点核对 sidecar（ID / 标签 / class / 文本 /
  `data-gloss` / `data-note` / `data-head` / SHA-256）；
- `checks.epub2_dom`：EPUB 2 产物不得残留 `VOCAB_ATTRS` 或章末 `<section>`；
- `validation.ok` **只表示内部硬校验**，不含官方结论；
  `checks.epubcheck` 允许为 `false` 而 `validation.ok=true`。

### 13.5 `release` 新增字段

```json
"release": {
  "check_policy": "compatible",
  "official_check": {
    "status": "failed", "ran": true, "version": "5.4.0",
    "nError": 3, "nWarning": 0, "nFatal": 0,
    "source_report": "…/_epubcheck_baseline/0077…-5.4.0.baseline.json",
    "output_report": "…/<产物>.epub.epubcheck.json"
  },
  "compatibility": {
    "delivery_kind": "compatible_delivery",
    "publishable": true,
    "external_validation_incomplete": false,
    "issues": {"acceptable_legacy": [ … ], "fixed": [], "new": [],
               "blocking": [], "unresolved": []},
    "work_copy": {"created": false, "reason": "…"},
    "summary": "已生成产物；原书有 3 类格式瑕疵…未发现本次新增问题。",
    "diagnostic_note": "部分官方诊断位置被归并截断…"
  },
  "publishable": true
}
```

| 字段 | 含义 |
|---|---|
| `check_policy` | `compatible`（默认）/ `strict` |
| `official_check.status` | `passed` / `failed` / `not_run` / `tool_error`；`tool_error` 一律阻断 |
| `compatibility.delivery_kind` | `spec_passed` / `compatible_delivery` / `compatible_unverified` / `blocked` |
| `compatibility.issues` | 五分类；`new` / `blocking` / `unresolved` 非空即不可交付 |
| `compatibility.work_copy` | 是否创建兼容修复工作副本及原因 |
| `release.publishable` | 最终可交付结论；与 `validation.ok`、官方状态三者**不能混用** |

CLI：`--check-policy compatible|strict`（默认 compatible）；
`strict` 要求 EPUBCheck 实际运行且 ERROR/FATAL 为 0，
与 `--no-epubcheck` 同时给出时为参数冲突（exit 2）；
兼容判定阻断时删除 pending EPUB 与 pending 映射并返回 exit 4。

## 14. §23 随包词库与省心化

### 14.1 `vocab-lexicon-vault/v1` 容器

`assets/lexicon.vault` 是**纯文本**容器，线格式：

```
VOCABLEX1
<base64 载荷，按 96 字符分行>
```

载荷字节 = `nonce(16)` + `cipher` + `hmac_sha256(key, MAGIC || nonce || cipher)(32)`，
其中 `cipher = CSV_bytes XOR SHA256-keystream(key, nonce)`，`key` 由源码中
分散片段经 `_key()` 派生。`pack_bytes()` 每次用随机 nonce，因此同一 CSV
两次打包结果不同；`unpack_bytes()` 先校验 HMAC 再解封，**失败抛 `VaultError`，
绝不返回部分内容**。`load_lexicon()` 用 `is_vault()` 嗅探文件头：是容器则解封，
否则按明文 CSV 读取；解封失败包装成 `LexiconError` → exit 2。

**能力边界（不得对外宣称更强）**：这是防顺手查看的**门槛**，不是密码学保密。
密钥可从源码推导，能读源码的人可还原；不防内存截获，也不防从产物章末词条
反推词库——后者按用户口径有意保留完整释义 / 例句 / 音标。

开发期重建：`python -m scripts.pack_lexicon --csv <原始.csv> --out assets/lexicon.vault`
（会做往返校验；原始明文 CSV 不进分发包）。再分发许可已明确（整套 MIT、D-05 已结案）：
分发时随包保留 `LICENSE`、`THIRD_PARTY_NOTICES.md` 与 `references/ECDICT_LICENSE.txt`，
详见 `THIRD_PARTY_NOTICES.md` §3。

### 14.2 CLI 与报告变化

| 项 | §23 起 |
|---|---|
| `--lexicon` | **可选**，默认 `assets/lexicon.vault`；显式传入自备 CSV 时优先 |
| `report.lexicon.path` | 本次实际词库来源路径（随包容器或用户 CSV） |
| `report.lexicon.rows_total` / `entries` / `heads` / `phrases` | 不变 |
| `report.lexicon_sha256` / 请求里的 `lexicon.sha256` | **词库内容哈希**：`lexicon_content_sha256()` 按解封后的 CSV 字节计算；容器随机 nonce 不影响，自备明文 CSV 与随包容器等价 |
| 词库缺失 | `LexiconError` → exit 2，提示确认随包容器或指定 `--lexicon` |
| 容器损坏 / 篡改 / 截断 | `VaultError` → `LexiconError` → exit 2 |
| 直出链路 `total_matches == 0` | 删除产物与 pending sidecar → **exit 6**；不写成功报告 |
| 直出档 `report.release` | 精简摘要：`mode="direct"` / `publishable` / `check_policy` / `official_check` / `compatibility`。词表直注原走 legacy 分支、报告没有 `release`，§23 补齐后宿主编排契约在两条路径都能读 `release.publishable` |

exit 6 现在同时表示「发布门槛未通过」与「直出 0 标注不交付空书」，
两种情形都不产生成品；stderr 会给出二选一指引（宿主编排 / 词表直注）。

### 14.3 宿主编排契约

用户自然语言 → 宿主自行抽取书 / 档位 / 输出位置 → 自动依次跑完
`--emit-annotations` → 写 `ann-*.json` → `--emit-review` → 另起一轮写
`review-*.json` → 最终 `--apply-annotations --out` → 读 `release.publishable`
后只回一句人话。详见 `SKILL.md`「编排契约」；面向人见 `README.md`。

终端摘要 `可交付` / `格式检查` 是展示层文案，`nError`、`delivery_kind`
等工程字段只存在于 JSON 报告；未交付时仍如实显示官方错误。
`external_validation_incomplete=true`（例如显式 `--no-epubcheck`）时，
文案改为「内部校验通过；官方格式检查未运行」，**不声称**「未引入新的格式问题」。

---

## 15. 0.10.0 新增：增强词表、词形表、阅读档位与译文协议

### 15.1 层 A 增强词表（`assets/lexicon.vault` 解封后）

6,861 行 × 15 列，UTF-8 BOM，表头按列名映射（不按序号）：

| 规范列名 | 表头 | 说明 |
|---|---|---|
| `word` / `grade` / `pos` / `gloss` / `example` / `extra` | 单词 / 单词等级 / 词性 / 中文释义 / 例句 / 扩展说明 | 旧 6 列，前 5 列逐行等于原词表；`扩展说明` 只剥掉 `音标：xxx` |
| `phonetic` | 音标 | L1 归一后的 `/…/`；读入时再走一次幂等校验，脏值整条省略并记 issue |
| `root` / `form_info` | 词根 / 词形信息 | 中文可读描述，不暴露 `0:fine/1:r` 类型码 |
| `bnc` / `frq` / `collins` / `oxford` | 词频BNC / 词频当代 / 柯林斯 / 牛津3000 | 数值列，非数值内容记 issue 并置空 |
| `en_definition` / `source` | 英义 / 数据来源 | 原样字符串 |

`Entry` 增 9 个可选字段；`Lexicon.summary().enhanced` 给 `with_phonetic` /
`with_example` / `with_form_info` 计数。缺任一新列的旧 6 列 CSV 仍可正常加载。

### 15.2 层 B 词形表（`assets/lexicon.forms.vault` 解封后）

11,825 行 × 4 列：`词形, 词根, 词形音标, 词形信息`。
`FormInfo(surface, root, phonetic, form_info)`，键为 `normalize_key(词形)`。
只保留「唯一词根 + 词根在现有词库 + 词形自身不在词库」的映射；多词根歧义保留首次并记 issue。

`Lexicon.form_of(surface)` / `phonetic_for(surface, entry)` / `form_info_for(surface, entry)`：
词形 == 词头时取层 A 词头字段；词形 != 词头时**只取层 B 该词形自己的字段**，
没有就返回空串（渲染层整行省略），**不回退词头、不编造**。
`matching.lemma_candidates(token, form_map)` 把层 B 词根作为首选候选，
后续仍逐个 `lexicon.lookup()` 验证，§21 token 定位与词形硬校验不放宽。

### 15.3 词条块新增 DOM（EPUB 2 落盘仍按 §13 净化）

```html
<p class="vocab-phonetic">音标：/fiʃt/</p>
<p class="vocab-form">词形信息：词根 fish · 本词是词根的过去式/过去分词</p>
```

两行都是**有值才出现**，无值整行省略。正文括注恒为 `(语境义,[n])`，
不再拼等级或用法类别；等级只在段后词条块（及其隐藏索引）的 `vocab-grade` 出现一次。
新增的是 `class`，不新增 `data-*`，`binding.VOCAB_ATTRS` 无需扩白名单。

### 15.4 阅读档位

`--reader-mode basic|advanced`（也接受中文「初级 / 高级」），`unspecified` 为未确认。
`_config()` 含 `reader_mode`，**进 `config_hash`**；换档位等于换任务。
`--require-reader-mode` 是宿主链路硬门槛：未确认档位 → exit 6、不生成成品。
不带该开关的调试/兼容路径行为不变，但报告 `reader_mode.confirmed=false`。

### 15.5 译文协议

`vocab-para-translation-request/v1`（程序 → 宿主）：

```json
{
  "schema": "vocab-para-translation-request/v1",
  "book_sha256": "<源书 SHA-256>",
  "reader_mode": "basic",
  "paragraph_count": 11,
  "paragraphs": [
    {"pid": "OEBPS/ch1.xhtml#3", "text": "He was an old man ...",
     "text_sha256": "<sha256(NFC+折叠空白后的段落原文)>"}
  ]
}
```

`vocab-para-translations/v1`（宿主 → 程序）：

```json
{
  "schema": "vocab-para-translations/v1",
  "book_sha256": "<源书 SHA-256>",
  "reader_mode": "basic",
  "paragraphs": [
    {"pid": "OEBPS/ch1.xhtml#3", "text_sha256": "<同上>", "translation": "他是个独自……的老人"}
  ]
}
```

- `pid` = `<doc>#<段落序号>`，与 `units.Paragraph.pid` 一致；前置页 / 目录（`chapter<=0`）不译。
- 只收 `chapter>0` 的自然段；`text` 长度 < 2 的段落跳过。
- **书名 / 章节名 / 标题不导出也不渲染**：`h1`–`h6`、`epub:type` 含 title/titlepage、
  class 含 title、章首段与章节名同文，命中即排除（11-11 拍板）。
- 加载时校验 `schema` / `book_sha256` / 哈希长度 64 / 译文非空；重复 pid 译文冲突保留首次并记 issue。
- 渲染按 `pid + text_sha256` 双重定位，任一不符整段省略，绝不拿相邻段译文顶替。
- 初级覆盖率 < 95% 或无任何译文 → exit 6，不交付「名为初级、实则无译文」的成品。
- 译文节点：`<p class="vocab-translation">参考译文：…</p>`，用 `addnext()` 插在自然段后；
  **不逐段重复「仅供参考」**，免责说明只写报告 `reader_mode.translation_disclaimer`；
  EPUB 不支持点击，不做折叠/开关。

### 15.6 报告新增字段

```json
"reader_mode": {
  "mode": "basic",
  "label": "初级（带中文译文）",
  "confirmed": true,
  "has_chinese_translation": true,
  "translation_prefix": "参考译文：",
  "translation_disclaimer": "段落译文由宿主模型生成，仅供参考；正文只在每段前加「参考译文：」，不逐段重复免责声明，免责说明记录在本报告里。",
  "required_by_host": true,
  "translation_coverage": {"total": 11, "covered": 11, "missing": 0, "ratio": 1.0},
  "translations": {"provided": 11, "files": ["..."], "issues": []}
}
```

### 15.7 许可文件（随发布包）

`LICENSE`（本项目 MIT）、`THIRD_PARTY_NOTICES.md`（分层口径、D-05 授权已结案）、
`references/ECDICT_LICENSE.txt`（ECDICT MIT 原文）、
`assets/lexicon.source.json` 的 `license` 字段。详见 `THIRD_PARTY_NOTICES.md`。

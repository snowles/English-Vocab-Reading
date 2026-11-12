# 实现与维护参考

本文件承接 `SKILL.md` 中被移出的实现细节，按需读取；主流程与红线仍在 `SKILL.md`。

## 工作流内部实现（给维护者）

1. **预检** `inspect_book.py`：ZIP / container.xml / OPF / manifest / spine / NCX / nav
   只读解析；拒绝损坏包、DRM、非字体类加密资源；统计章节/词数/候选预估；
   发现 `vocab-skill` / `data-vocab-id` 即拒绝重复处理（兼容新旧产物）。可单跑：
   `python scripts/inspect_book.py --book <书> --lexicon <词表>`。
2. **词表** `load_lexicon.py`：编码（UTF-8/BOM/UTF-16/GB18030）与分隔符自动识别；
   按列名映射；NFC + casefold；合并完全重复词条、保留同头不同义；等级按 `+` `/` 拆分后取交集。
3. **匹配** `matching.py`：Unicode 词边界（`art` 不命中 `article`/`part`）；
   撇号/连字符整体；短语最长优先；词形还原只用不规则表 + 规则后缀，
   **还原结果必须落在词表内才接受**；URL 内 token 跳过。
4. **章节识别** `chapters.py`：按 nav/NCX 目标切逻辑章节，支持同文件多章、跨文件一章；
   第一个章节起点之前的前置页不标注；无导航时整本一章。
   **段落容器** `units.py`：`--engine units` 取「最内层且有文本的块容器」，
   标准 EPUB 的 `<p>` 与 Calibre 转出的 `<div class="bs">` 都支持；
   空 `<p class="calibre1"/>`（纯段间距）不参与编号，外层 `<div>` 不会与内层 `<p>` 重复计数。
5. **扫描与渲染** `inline_notes.py`：按章节做匹配 → 密度过滤（单位是「词条+义项」）
   → 校验 harness 决策 → 分配章内编号 → DOM 写短注与词条块。
   默认 `--engine units` 走 `render_units.py`：词条块按自然段分组插到段后
   （长段 ≥60 词单独、短段 4 段 / 110 词合并），章末块降级为 `vocab-archive` 隐藏索引；
   `--engine legacy` 仍是章末词条。
   跳过 script/style/head/nav/code/pre/svg/math/上下标/隐藏节点/`<a>` 内文字/原有脚注；
   未命中章节**不重写**。
6. **写回与净化** `annotate_epub.py::_write_epub()` + `binding.py`：
   `mimetype` 首个且不压缩；只有实际改动的 XHTML/OPF/新资源变化；
   落盘前剥离 `data-*`、章末 `<section>` → `<div>`、写包外 sidecar 映射；
   临时文件 + `os.replace` 原子改名；失败清理（含 pending 映射），不覆盖源文件。
7. **校验** `validate_epub.py`：独立重开输出，检查 mimetype、container、manifest、
   spine 顺序、XHTML 可解析、内部锚点、标注↔脚注往返、**词义可追溯（data-entry-id /
   data-confidence 合法性）**、nav/NCX 结构、未改动资源字节一致、**正文文本逐字一致**；
   另按最终 DOM 核对 sidecar 映射、扫描 EPUB 2 残留 `data-*` / `<section>`，
   再把映射注入内存 DOM 供上述检查使用。EPUBCheck 结果按
   `--check-policy compatible|strict` 单独判定，找不到工具写 `not_run`、
   调用出错写 `tool_error`，都不冒充官方通过。

## EPUB 2 DOM 合规与兼容交付（§22）

### 包内合法 DOM + 包外 sidecar 映射（`vocab-reader-map/v1`）

EPUB 2 要求 XHTML 1.1，HTML5 的 `data-*` 与 `<section>` 不合法。渲染器在**内存 DOM**
里照旧写完整标注；`annotate_epub.py::_write_epub()` 落盘前由 `scripts/binding.py` 统一净化：

- 只剥离本 Skill 写入的白名单属性（`data-vocab-id` / `data-entry-id` / `data-gloss` /
  `data-head` / `data-importance` / …），**原书自带的其它 `data-*` 不动**；
- 章末 `<section class="vocab-chapter-notes">` 降级为 `<div>`；
- 用稳定 `id`（`vref-*` / `vnote-*`）、`class`、`href` 维持定位、编号、词注与回链；
- 给每个被改节点追加合法 class `vocab-skill`，兼作「已标注」检测标记
  （替代旧的 `data-vocab-id` 扫描）；
- 被剥离的字段写到 `<产物>.epub.vocab-map.json`，按稳定 `id`（原有
  `vref-*` / `vnote-*`；无 id 时补合法 `vb-*`）+ 节点特征绑定产物，
  含 `_doc` / `_tag` / `_class` / `_text` 与 EPUB SHA-256。

阅读页 `build_reader_html.py`、校验器 `validate_epub.py`、双格式比对 `_verify/compare.py`
都在**内存 DOM** 里从 sidecar 注入这些字段，不写回 EPUB 包。校验器用
`audit_binding()` 从最终 EPUB 的**实际节点**核对映射，篡改 / 缺记录 / SHA 变化会被拦下。
EPUB 脱离 sidecar 仍是完整可读的电子书，只是没有网页交互字段。

### 源书基线与差异归因（`vocab-epubcheck-baseline/v1`）

`compatibility.py` 按源书 SHA-256 + 工具版本缓存 EPUBCheck 基线，避免重复检查；
比较产物与基线时按 `(级别, 错误码, 归一化消息, 元素/属性目标, 资源路径集合)`
做多重集归因，**行列号变化不算新增**，`additionalLocations` 被归并时在
`diagnostic_note` 显式写明证据不足，不声称逐一完成定位。问题分成五类：
`acceptable_legacy` / `fixed` / `new` / `blocking` / `unresolved`。
不能按 `RSC-005` 之类错误码整体放行：同错误码的非法标注节点仍算 `new`。

### 两种校验策略

```bash
# 默认：兼容交付（内部硬校验 + 官方校验都跑，允许无新增问题的原书遗留瑕疵）
python scripts/run_pipeline.py ... --check-policy compatible

# 严格：要求 EPUBCheck 实际运行且 ERROR/FATAL 为 0；两本样例原书会在此被阻断
python scripts/run_pipeline.py ... --check-policy strict

# 仅开发期临时跳过（跳过 ≠ 合规；strict 与此参数互斥，会报参数冲突 exit 2）
python scripts/run_pipeline.py ... --no-epubcheck

# 单本直接走官方 CLI（--json <file>，不是 --format json）
java -jar "$EPUBCHECK_JAR" <book.epub> --json report.json -q
```

| `delivery_kind` | 含义 |
|---|---|
| `spec_passed` | 官方校验实际通过且零 ERROR/FATAL |
| `compatible_delivery` | 官方状态如实失败，但无新增 / 阻断 / 未解决问题，可交付并披露遗留瑕疵 |
| `compatible_unverified` | 工具缺失且内部校验通过：外部校验未完成，**不是官方通过** |
| `blocked` | 新增问题、工具调用出错、strict 非零 ERROR 或内部校验失败 |

报告里三层含义严格分开：`validation.ok` = 内部硬校验；
`release.official_check.status` = 官方校验（`passed` / `failed` / `not_run` / `tool_error`）；
`release.publishable` = 是否可交付。工具**缺失**（`not_run`）与工具**调用出错**
（`tool_error`）分开，后者一律阻断，不允许假装未运行。

### 有限确定性兼容修复（默认不启用）

`scripts/compat_fixes.py` 是独立的显式工具，**不在主流水线自动调用**——
工作副本会改变书籍哈希，必须重新 emit / identify / review，不能复用旧 v6 绑定。

```bash
python -m scripts.compat_fixes --book <书> --analyze-only           # 只看清单
python -m scripts.compat_fixes --book <书> --out <工作副本> --report <清单.json>
```

确定性修复：日期格式、URI 空格、缺 NCX 补最小导航、spine 的 `toc` 属性；
只记录不修：图片 alt、旧式外观属性、含空格文件名、非法但唯一的 ID；
重复 ID 直接拒绝创建工作副本。**两本真实样例默认不创建工作副本**：
遗留瑕疵均可接受，保持原输入哈希与 v6 内容绑定，能力用独立 fixture 验证。

### 官方报告留档

每次运行都把 EPUBCheck 完整官方 JSON 写到 `<产物>.epub.epubcheck.json`：
顶层 `messages` / `checker` / `publication` / `items`，每条 `messages[]` 内含
`ID` / `severity` / `locations[]` / `additionalLocations`，基线存
`_epubcheck_baseline/*.baseline.official.json`；报告里的 `messages` 只是摘要，
留档文件才是完整证据。本机已装 Temurin JRE 21.0.13（便携展开）+
EPUBCheck 5.4.0，路径见 `requirements.txt`。`find_epubcheck()` 自动探测
`~/tools/epubcheck*/epubcheck.jar` 与 `~/tools/java/**/bin/java.exe`，
**无需设置环境变量**；也可由 `EPUBCHECK_JAR` / `JAVA_HOME` 显式指定。

> 复跑注意：本 Skill 不覆盖既有输出。同一 `--out` 目录里已有 `<书名>_vocab.epub` 时，
> 再次运行会生成 `<书名>_vocab_2.epub`，报告 `output.path`、`validation.binding.path`、
> `release.official_check.output_report` 都指向新文件。阅读页配置 `_verify/compare.py`
> 必须同步指向新产物，否则会把旧 EPUB 与当前报告混为一谈（§22 审计中已修过一次）。

## 随包词库与轻量封装（§23，省心化）

用户口径（2026-11-06）：**只防君子不防小人**；词表随包、零配置；
稍微设个门槛即可；本意是让大家更好地学习。按此落地：

- `assets/lexicon.vault` 随包分发，`--lexicon` 变成**可选参数**；
  `load_lexicon()` 嗅探文件头自动解封，用户不需要密码 / 联网 / 激活 / 环境变量，
  也不新增第三方依赖（只用标准库）。
- 容器格式 `vocab-lexicon-vault/v1`：**纯文本**（`VOCABLEX1` 头 + 分行 base64），
  载荷 = 随机 nonce + 流加密后的 CSV + HMAC-SHA256 完整性标签。
  双击或文本搜索看不到词条；篡改 / 截断会明确报 `VaultError` → `LexiconError`，
  绝不静默产出坏词库。
- **如实边界**：密钥由源码中的分散片段派生，能读源码的人仍可还原；
  这是**门槛**，不是密码学保密，不防有动机的攻击者，也不防内存截获。
  对外不得宣称"拆包也看不到"。
- **产物完整词条保留**：章末仍给出完整释义、例句、音标（用户口径：学习优先）。
  这意味着词库可经多次使用的产物被逐步反推——这是有意接受的产品取舍，
  不属于实现缺陷，也不做「收紧产物暴露档位」。
- 用户自备 CSV 仍按明文读取：`--lexicon <自己的.csv>` 优先级高于随包词库；
  报告 `lexicon.path` 记录本次实际来源。
- `lexicon_sha256` 按**词库内容**（容器解封后的 CSV 字节）计算，不按容器文件字节：
  容器每次打包用随机 nonce，若按文件哈希绑定，同一份词表重新封装就会让已有
  scope / ann / review 全部失效；按内容哈希后自备明文 CSV 与随包容器等价。
- 开发期重建容器（原始明文 CSV **不要放进分发包**）：
  ```bash
  python -m scripts.pack_lexicon --csv <开发目录/原始.csv> --out assets/lexicon.vault
  ```
  `pack_lexicon.py` 会做往返校验并打印源 CSV 的 SHA-256 前 16 位。
- ⚠️ **分发前须确认词表再分发许可**。当前容器由用户个人目录的
  `四级六级雅思词汇表.csv`（6861 条 / 702 KB）生成；若该表来自第三方整理，
  封装不解决版权问题。

### 不交付空书（§23 修正）

实测发现：`--book` + `--lexicon` 直接运行默认档会产出 **0 处标注**却打印
「已完成」（`--engine legacy` 同样如此），用户会拿着空书走。
现按**实际产出**判定而非参数猜测：`total_matches == 0` 时删除刚写出的 EPUB
与 pending sidecar、返回 exit 6、在 stderr 给出二选一指引。
`footnote` / `inline` / `--undecided lexicon` / 完整四步链路不受影响
（实测词表直注 3933 处）。§3 允诺的「no-model 降级」由此以
`--undecided lexicon` 形式真正可用。

### 终端摘要人话化

`可交付` / `格式检查` 面向人：可交付且无新增问题时，官方遗留报错显示为
「原书遗留 N 处格式瑕疵，不影响阅读（已保留，详见报告）」。
`nError`、`compatible_delivery` 等工程字段只进 JSON 报告；
未交付时仍如实显示官方错误，不淡化。

## 0.10.0 词库重建与许可（P1-b）

随包数据由两个独立容器组成，都走同一套 `vocab-lexicon-vault/v1` 封装：

| 容器 | 解封后 | 用途 |
|---|---|---|
| `assets/lexicon.vault` | 层 A 增强词表，6,861 行 × 15 列 | 词条本体，`load_lexicon()` 自动解封 |
| `assets/lexicon.forms.vault` | 层 B 词形映射，11,825 行 × 4 列 | `load_form_map()` 自动解封，只作候选来源 |

重建顺序（开发机）：

1. `python -m scripts.pack_lexicon --csv <层A增强词表.csv> --out assets/lexicon.vault`
2. `python -m scripts.pack_lexicon --csv <层B精简词形表.csv> --out assets/lexicon.forms.vault`
3. 更新 `assets/lexicon.source.json`：`bundled.content_sha256`、`source.sha256`、
   `enhanced.layer_b.content_sha256`、容器/解封字节数、行数统计。
4. 回跑 `tests/test_ecdict_merge.py` 的 `test_provenance_record_matches_bundled`
   （哈希不符会直接失败），再跑全量 343 项。

音标 L1 的**唯一实现**是 `scripts/phonetic_l1.py`；`词库合并试点v1/phonetic_l1.py`
只是 shim，不要再在那里改规则。`load_lexicon.py` 读入时对 `/…/` 先剥包裹再归一（幂等），
含义不明的脏音标整条省略并记 `LexiconIssue`。

**内容哈希一变，所有 decisions / scope / review 全部失效**：外机 36 窗口的旧结论不能沿用，
整书 840 窗口须重新识别与独立审查。`--reader-mode` 也进 `config_hash`，中途改档位同样重来。

许可文件 `LICENSE`、`THIRD_PARTY_NOTICES.md`、`references/ECDICT_LICENSE.txt`
必须随包（已列入 `pack_release.py:FILES`）。ECDICT 部分是 MIT；原始中文词表由本项目
维护者本人整理并按 MIT 授权（11-12 确认，外机验收 D-05 已结案），登记见
`assets/lexicon.source.json` 的 `license` 段，详见 `THIRD_PARTY_NOTICES.md` §3。


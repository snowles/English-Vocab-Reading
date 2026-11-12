# 测试与路线图

本文件承接 `SKILL.md` 的测试清单与阶段路线图。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
# 当前 387 项（20 个文件），全部通过（0.10.6 补 H2 模板契约后全量重跑）
#
# 覆盖度矩阵（H1）见 references/h1-coverage.md：这里答"跑了哪些用例"，
# 那里答"每类输入有没有规则兜住"。两边要同步更新。
```

- `test_lexicon.py`：编码/分隔符/列名映射/等级/坏行/引号内逗号。
- `test_matching.py`：`art/article/part` 陷阱、大小写、撇号、连字符、Unicode 标点、
  规则与不规则词形、短语最长优先、跨节点短语、URL/隐藏/代码/脚注区跳过。
- `test_pipeline.py`：三本 fixture 全流程、结构保真、字节一致、标注往返、正文逐字一致、
  重复处理拒绝、SHA-256 不变、输出不覆盖、稳定性、退出码、报告字段；
  另含 **EPUBCheck CLI 参数 `--json`（不是 `--format json`）**、官方 JSON 摘要解析、
  `JAVA_HOME` 回退查找、报告标签计数（用 mock，不依赖本机 Java）。
- `test_disambiguation.py`：v1 协议、legacy footnote/inline 引擎、四种密度、停用词、
  截断、SHA 不匹配拒绝。
- `test_inline_notes.py`：`inline-notes` 全链路——短注格式 `(释义,等级,[n])`、
  章内编号从 1 重启、同文件多章各自成块、同义复用、缺失决策跳过、配置指纹不匹配拒绝、
  正文剥离后逐字一致、编号/链接/一致性/字段四项校验、重复处理拒绝、
  **释义含逗号/括号时短注↔章末仍一致（`data-gloss`）**。
- `test_units.py` / `test_tokens.py`：§19 token 定位与词形硬校验；
  另覆盖 **Calibre `<div>` 正文、空 `<p>` 间距、外层 div 不重复、章节 fragment 落在 div 上**。
- `test_release_gate.py`：§19 发布门槛——缺审/待复审/范围缺失/严格不出书/`--allow-partial`/原子提交。
- `test_reading_prefs.py`：§20 A 批——三档筛选、`mastered` 不误删组合、`focus` 优先、
  词形还原匹配、CSV/JSON 往返、`detailed` 与密度相互独立（§21.4）、同章同义与变义去重。
- `test_feedback.py`：§20 B 批——`vocab-reader-feedback/v1` 单文件/目录解析、去重、
  缺 ID 与未知类型处理、坏 JSON 记录、待复核清单生成。
- `test_lexicon_vault.py`（§23，24 项）：容器往返 / 纯文本无明文词头 / 篡改与截断检测、
  **重新封装不改变内容哈希**、随包词库可加载且 >6000 条、自备 CSV 仍可用、
  坏容器报 `LexiconError`、**零配置默认档拒绝空书**、零配置导出窗口与任务清单、
  零配置直注出书且来源为随包词库、**直注档报告也写 `release.publishable`**、
  自备 CSV 覆盖默认值、章末完整释义保留、终端摘要人话化、
  未交付时如实报错、官方未运行时不声称「未引入新问题」、
  **来源记录与随包内容一致**、随包只有三级标签不超范围宣称、
  **P0-2 编码契约**（不设 UTF-8 环境变量 / 强制 cp936 都能被 UTF-8 读取、终端不改写）。
- `test_compatibility.py`（§22，32 项）：sidecar 净化 / 注入 / 审计 / SHA 绑定、
  重复标注检测、行列变化不误判、归并位置标注、同错误码新增非法节点拦截、
  修一旧错引一新错检出、样例遗留问题分类、compatible / strict 判定、
  工具出错与工具缺失分离、strict 未运行阻断、URL 空格不双重编码、
  工作副本修复 NCX / 日期 / URI 且保真、重复 ID 拒绝修复、
  Java 可用时干净 EPUB 2 加词注官方零 ERROR；
  **P0-1 跨文档同名 ID 放行、同文档重复拦截、按 `_doc` 定位不错绑、缺 `_doc` 报歧义**。
- `test_delivery.py`（§P1-2/P1-1，11 项）：按原书目录取逻辑章节、`1-2` 只选两章、
  越界拒绝、单章渲染、不覆盖既有阅读页（`--force` 才覆盖）、
  `--html` 记入 `deliverables`、HTML 失败时 exit 7 且如实说明 EPUB 已交付；
  发布清单含运行脚本与 `smoke/book.epub`、无私有绝对路径与缓存、
  不打进 `examples/` 与 `tests/`。
- `test_contract_guard.py`（11 项，0.10.5）：T8 契约条款守护——「赶时间不构成降级授权」
  红线存在且含降级四要素、点名必须拒绝的措辞、「1 章小样」最快路径、只能称「浅标版」、
  旧宽松句不得回归、直出路径标降级、退出码 6 提示、已知限制条目、
  「精细 / 快标」二选一须说明差异、H1 已记录本次修复。
- `test_resume.py`（3 项，0.10.6）：断点续作——删一个窗口请求后 `--resume` 补齐且
  `task_id` 不变；改变段落范围后 `task_id` 变、窗口按新范围重建且不残留越界分片；
  续作不删宿主写好的 `ann-*.json`。
- `test_lexicon_template.py`（5 项，H2 契约）：`assets/lexicon-template.csv` 随包存在、
  每个表头列名都能被 `COLUMN_ALIASES` 识别（用户照抄不会被判缺列）、必需列齐全、
  模板本身能被完整加载（2 行示例的词头/释义/例句/音标/等级都解析出来）、
  带 UTF-8 BOM（Windows Excel 打开不乱码）。

用例分布（合计 387 / 20 文件）：`test_disambiguation` 44、`test_lexicon` 38、
`test_pipeline` 36、`test_compatibility` 32、`test_matching` 31、`test_units` 27、
`test_reading_prefs` 26、`test_lexicon_vault` 24、`test_reader_mode` 19、
`test_ecdict_merge` 18、`test_release_gate` 15、`test_tokens` 13、
`test_inline_notes` 12、`test_contract_guard` 11、`test_delivery` 11、
`test_frontmatter` 10、`test_feedback` 6、`test_para_notes` 6、
`test_lexicon_template` 5、`test_resume` 3。

上架字段新增（11-11，0.10.3）：

- `test_frontmatter.py`（10 项）：SKILL.md 必填字段齐全非空、`name`/`version` 与
  `pack_release` 同源不分叉、版本是 `x.y.z`、author 非占位值、`allowed-tools` 放行 Bash、
  含冒号的值必须加引号（防平台 Go-YAML 的 `mapping values` 报错）。
- `test_pipeline` 增 4 项：`_clear_emit_dir` 只清本程序产物、目录里有外来文件时拒绝清理
  且不误删、目录不存在时自动创建（安全红线：禁止对用户路径无条件递归删除）；
  导不出窗口时返回 exit 6 而不是 0（不留空目录让宿主往下走）。

译文文案 / 标题不译新增（11-11，0.10.2）：

- `test_reader_mode` 增 4 项：书名 / 章节名 / 标题段（`h2`、class 含 `title`）
  即使有译文也不渲染；普通正文段（`p.calibre1`）不会被章名误判成标题；
  书名页整页（书名 + 作者名）被识别为扉页；带句末标点的短文 / 段落过多 / 长段
  都不会被误判成扉页。

段后词条补修新增（11-11）：

- `test_para_notes.py`（6 项）：长段单独成组、短段 4 段封顶、累计词数上限、
  章界不跨、非章节段落跳过、词数只数拉丁词。
- `test_units.py` 增 1 项：端到端断言词条块紧跟自然段（或该段译文）、
  章末块带 `vocab-archive`、全部 `[n]` 链接指向真实 `vpara-*` 锚点、
  无重复 id / 无残留 `data-*`。

0.10.0 / P1-b 新增：

- `test_ecdict_merge.py`（18 项）：层 A 15 列解析、L1 幂等、脏音标整条省略、
  词形表加载与坏行、按被标注词形自身取音标（`fished → /fiʃt/`、`Lived → /livd/`）、
  无词形音标不回退词头、中文可读词形信息、词形表只作候选来源且硬校验不放宽、
  例句与词条数不变、许可文件随包、内容哈希与来源登记一致。
- `test_reader_mode.py`（15 项）：档位别名与非法档位、`para_hash`、译文请求导出、
  译文分片严格校验、串段拒绝、覆盖率、`_append_translation` DOM 插入与「仅供参考」注明、
  宿主硬门槛 exit 6、高级记录、初级缺译文拦截、直注路径不交付初级。
- `test_release_gate.py` 增 1 项：真实 EPUB 的初级完整链路（识别 → 独立审查 → 出书），
  断言成品含译文段落、注明、正文括注不带等级、英文原文逐字不改、`publishable=true`。

fixture 重建：`python tests/fixtures/build_fixtures.py`（确定性、离线）。

关3 边界压测（0.10.6，上架三关之一）：

```bash
python tests/stress_gate3.py --json 关3边界压测结果.json
```

18 个畸形 / 越界场景，逐项核对四件事：退出码是否符合预期、stderr 有无 traceback、
失败场景是否留下 `*_vocab.epub`（不产假货）、失败时 stderr 是否有人话指引。
场景含：路径不存在 / 非 EPUB / 截断 ZIP / 空 ZIP / DRM 加密 / 已标注书 / 只有图片页 /
约 3080 词超长单段 / 窗口与范围参数越界 / 非法 density 与 presentation / 空 CSV /
缺列 CSV / 目录混入外来文件 / 同目录重复出书 / 两条成功基线。
这是验证工具，不与 `test_*.py` 一起被 discover；结果文件作为上架证据留存。

## 阶段路线图

1. ✅ 阶段 1–2：EPUB 无模型标注原型 + **harness 语境消歧**（请求/决策协议、最小上下文、
   置信度与回退策略），密度默认每章首次。
2. ✅ 阶段 3–6（§16–§19）：正文短注 + 章末词条、语义单位与独立审查、完整词库与组合解释、
   程序 token 定位 + 词形硬校验 + v6 内容版本绑定与严格发布门槛。
3. ✅ 阶段 7–8（§20–§21）：三档提示与熟词名单、离线阅读页、生词导出、断点续作、
   局部失效与纠错反馈；第 21 节四项修复与 B 批核心链路。
4. ✅ 阶段 9（§22）：EPUB 2 标注 DOM 合规 + sidecar 映射、源书基线与差异归因、
   `compatible` / `strict` 发布策略、有限确定性兼容修复、官方报告完整留档。
5. ✅ 阶段 10（§23）：词库随包 + 零配置轻量封装、宿主自然语言编排契约、
   不交付空书、终端摘要人话化、面向人的 `README.md`；**产物完整词条按用户口径保留**。
6. ✅ 阶段 11（§24，0.10.0 / P1-b）：层 A 增强词表 + 层 B 词形表随包、L1 音标按词形、
   正文括注去等级、词条块新增音标与词形信息、初级/高级出书前必问、段落译文通道、
   统一 MIT 许可分层、343 项回归、EPUBCheck 差异归因 0 新增、正式包与测试包重打。
7. ⛔ 不排期 / 已取消：Markdown / TXT 导入属历史设想；PDF / OCR 导入**不实现**
   （`convert_source.py` 仅保留历史兼容入口，不纳入验收范围）。见设计文档 §12。
8. ⬜ 待验收：至少两个不同实现的 EPUB 阅读器（Calibre / Thorium / Apple Books）、
   Firefox / Safari、`detailed` 档增量识别、局部增量重建、纠错自动复核、
   小样预览交互化。EPUBCheck 已实跑，不再是环境缺口；§22 明确不授权扩大
   B 批范围、不做全能 EPUB 修复器、不新增其它格式支持。
   ✅ **段后词条块已于 11-11 补修固化进默认渲染链**（不再是外置试点、不再等 P1-c），
   见 `SKILL.md` §呈现方式；试跑见 `重跑第一章/第一章试跑报告.md`。
   ✅ **关3 边界压测已于 11-12 完成**（18 场景 0 偏差，见 `关3边界压测结果.json`）；
   ✅ **H3 A/B 增益 36.0%（0.10.5 修复 T8 后）达标**，见 `H3_AB对照/实跑结论报告.md`；
   ✅ **D-05 已于 11-12 结案**：原始中文词表由维护者本人整理并按 MIT 授权。
   仍待办：0.10.0 词库内容哈希已变，外机 36 窗口旧 decisions 全部失效，
   整书 840 窗口须重新识别与独立审查。


"""英语词汇伴读 Skill —— 阶段 1（EPUB 无模型标注原型）。

模块职责：
    load_lexicon   词表 CSV 读取、校验、归一化
    matching       词边界、短语最长优先、词形还原匹配
    inspect_book   EPUB 只读预检
    annotate_epub  DOM 文本节点标注与最小包写回
    validate_epub  结构化校验与保真比对
    run_pipeline   命令行入口
    convert_source 阶段 3/4 输入转换（未实现，调用即报错）
"""

__version__ = "1.0.0-stage1"

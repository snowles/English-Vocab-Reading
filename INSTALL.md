# 安装与启用（首版：Windows + WorkBuddy）

本版只在 **Windows + WorkBuddy** 上实测；Codex、豆包工作版等宿主理论可用，但**未实测**，
遇到问题请按本文末尾的排查步骤反馈。不需要装 Java，也不需要准备词表。

## 1. 准备 Python

需要 **Python 3.10 或更高**。打开 PowerShell 或 CMD：

```bat
python -V
```

显示 `Python 3.10.x` 或更高即可。若提示找不到命令，去 python.org 安装并勾选
「Add python.exe to PATH」，然后重开一个终端。

## 2. 安装依赖

在**解压后的技能目录**里执行（只有 `lxml` 一个依赖）：

```bat
python -m pip install -r requirements.txt
```

公司网络装不上时，可换国内镜像：

```bat
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 3. 放置技能

把整个文件夹复制到下面任一位置，**文件夹名保持 `vocab-reading`**（与 `SKILL.md` 里的
`name` 一致，否则部分宿主不识别）：

| 范围 | 路径 |
|---|---|
| 当前用户（推荐） | `%USERPROFILE%\.workbuddy\skills\vocab-reading` |
| 单个项目 | `<项目目录>\.workbuddy\skills\vocab-reading` |

不要放进 `C:\Program Files` 等需要管理员权限的位置，否则运行时会写不了任务目录。

## 4. 在宿主里启用

1. 打开 WorkBuddy，进入技能 / 技能管理页面，点刷新；
2. 列表里出现 `vocab-reading`（英语词汇伴读）后启用它；
3. 若没出现，重启一次 WorkBuddy 再看。

## 5. 确认装好了

在技能目录执行自检，它会把环境、词库和一次真实出书都跑一遍：

```bat
python -m scripts.doctor --smoke
```

看到最后一行 **「结论：就绪」** 即安装完成。中间若出现 `✗`，按它给出的提示处理：

| 提示 | 处理 |
|---|---|
| lxml 不可用 | 回到第 2 步装依赖 |
| 随包词库缺失 | 解压不完整，重新复制整个文件夹 |
| 冒烟未通过 | 把整段输出贴给助手，不要只贴最后一行 |

## 6. 第一次使用

把英文 EPUB 放到任意目录，然后对助手说一句话即可，例如：

> 把 `D:\Books\The little prince.epub` 做成伴读版

助手会自己完成识别、审查、出书和校验。默认产物有两份：

- 新的 `.epub`（原书不动）
- 离线阅读页 `.html`（双击可看）

开始前助手会问一句「初级（每段带中文译文）还是高级（不带）」，照你自己的阅读习惯选就行，
之后不用再管。许可与第三方声明见 `LICENSE` 和 `THIRD_PARTY_NOTICES.md`。

**默认输出位置**：与书同目录的 `<书名>_伴读\`，任务文件都在这个目录里，
不会写进技能安装目录。

## 常见安装问题

**要不要装 Java / EPUBCheck？**
不用。它是可选的官方规范校验工具，装了就自动跑并留档，没装也不影响出书（会如实写明未运行）。

**能不能直接双击运行？**
不能，也不需要。这个技能由 AI 助手调用，你只需要用自然语言提需求。

**换电脑怎么办？**
把整个 `vocab-reading` 文件夹拷过去，重复第 1、2、5 步即可；词库已经随包。

**卸载**：删掉 `vocab-reading` 文件夹即可，不会残留系统级配置。

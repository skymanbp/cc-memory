<!-- i18n-source: README.md | sha256: e44d79672225eabb | version: 2.11.4 | translated: 2026-08-17 -->
> [English](README.md) · **简体中文**

<div align="center">

# cc-memory

**给 Claude Code 的持久化记忆。**
项目里的决策、结果、缺陷与计划，能挺过上下文压缩、会话边界和关掉的终端——
并且下一个会话在动手之前会被**强制**先读它们。

[![version](https://img.shields.io/badge/version-2.11.4-blue.svg)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#运行要求)
[![release gates](https://img.shields.io/badge/release%20gates-11-orange.svg)](#发布闸门)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#运行要求)

</div>

---

## 目录

- [要解决的问题](#要解决的问题)
- [60 秒看懂它怎么工作](#60-秒看懂它怎么工作)
- [快速开始](#快速开始)
- [功能总览](#功能总览)
  - [1 · 记忆采集](#1--记忆采集)
  - [2 · 记忆质量——反补丁契约](#2--记忆质量反补丁契约)
  - [3 · 会话交接——PROGRESS.md](#3--会话交接progressmd)
  - [4 · 计划与意图——PLAN.md 与指令账本](#4--计划与意图planmd-与指令账本)
  - [5 · 检索](#5--检索)
  - [6 · 各类接口](#6--各类接口)
  - [7 · 隐私与安全](#7--隐私与安全)
  - [8 · 可靠性工程](#8--可靠性工程)
- [参考手册](#参考手册)
  - [`/cc-mem`——32 个子命令](#cc-mem32-个子命令)
  - [`cc-memory-plan`——计划队列](#cc-memory-plan计划队列)
  - [MCP 工具](#mcp-工具)
  - [配置](#配置)
  - [每个项目的文件](#每个项目的文件)
  - [数据库表](#数据库表)
  - [钩子一览](#钩子一览)
- [架构](#架构)
- [开发](#开发)
- [故障排查](#故障排查)
- [v2.11.3 有什么新东西](#v2113-有什么新东西)
- [文档地图](#文档地图)
- [许可](#许可)

---

## 要解决的问题

上下文窗口填满时，Claude Code 会**压缩**（compact）对话。被丢掉的那些轮次里的
东西就没了：三小时前做的决策、量出来的基准数字、已经修过一次的 bug、你说过
还得再说一遍的约束。正常结束的会话——关掉终端、合上笔记本——同样会悄无声息地
丢掉这些。

常见的几种应付办法，在长周期项目里都撑不住：

| 应付办法 | 为什么不行 |
|---|---|
| 手写一份 `NOTES.md` | 赶进度时没人更新；它会过期，然后开始说假话 |
| 每个会话把上下文再粘回去 | 手工、有损，而且烧掉的正是你想省下的 token |
| 更大的上下文窗口 | 只是把压缩推迟，并没有消除它 |
| 只追加的记忆文件 | 同一个事实每个会话重述一遍、越堆越高，文件最后变成噪音 |

cc-memory 四个一起解决。它在每个对话边界采集结构化记忆，把每条新事实与已存
内容**归并**而不是追加，并在会话开始时发出一段 `<system-reminder>`，要求下一个
Claude 在回应**之前**先读交接文档。

---

## 60 秒看懂它怎么工作

```
┌──────────────────────── 你的 Claude Code 会话 ────────────────────────────┐
│                                                                          │
│  UserPromptSubmit ──▶ 首次接触时创建 memory/、计轮次、                      │
│                       在第 1 轮播种"用户要什么"                            │
│                                                                          │
│  PostToolUse     ──▶ 实时计划锚点（ExitPlanMode → 捕获计划、                │
│                       TodoWrite → 步骤同步、编辑 → 漂移计数器）             │
│                       + 每次被观察的工具调用写一行 observation             │
│                                                                          │
│  Stop            ──▶ Haiku 读本轮 observation 写记忆 ·                     │
│                       增量更新 PROGRESS.md · 强制执行计划                   │
│                                                                          │
│  PreCompact      ──▶ 同步腿：从有界的 transcript 窗口抽取                   │
│                       → 归并 → 全量重写 PROGRESS.md → 归档                 │
│                       异步腿：LLM 整理，不在阻塞路径上                       │
│                                                                          │
│  SessionStart    ──▶ 注入主题 + 关键记忆 + 时间线，然后                     │
│                       强制："回应之前先读 memory/PROGRESS.md"               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                     <project>/memory/memory.db   (SQLite + FTS5)
                     <project>/memory/PROGRESS.md (交接，全量重写)
                     <project>/memory/PLAN.md     (实时计划锚点)
                     <project>/memory/MEMORY.md   (可浏览索引)
```

一切都是**项目本地**的。`memory/` 就在你的仓库里，由 cc-memory 自己写的
`.gitignore` 忽略掉，除了发往 Anthropic 的抽取调用之外不出本机——而那次调用
你可以用 `<private>` 标签划定范围，或者按项目整个关掉。

---

## 快速开始

### 安装——marketplace（推荐）

```bash
claude /plugin marketplace add skymanbp/cc-memory
claude /plugin install cc-memory
```

### 安装——从本地 checkout

```bash
git clone https://github.com/skymanbp/cc-memory.git
claude /plugin marketplace add ./cc-memory
claude /plugin install cc-memory
```

### 安装——独立安装器，不走 marketplace

```bash
python cc-memory/cc_memory/ui/installer.py          # 图形界面
python cc-memory/cc_memory/ui/installer.py --cli    # 无界面
```

Windows 上也可以从 [Releases](https://github.com/skymanbp/cc-memory/releases)
下载 `cc-memory-installer.exe`。

### 然后

什么都不用做。每个项目的初始化是自动的：你在一个项目里发出的第一条消息就会创建
`<project>/memory/` 和它的数据库。用这个确认：

```
/cc-mem status
```

要让某个目录完全不参与，把它列进 `excluded_projects`——见[配置](#配置)。

---

## 功能总览

### 1 · 记忆采集

| 功能 | 做什么 |
|---|---|
| **AI 判定的抽取** | 由 Haiku 读对话并返回结构化的 `{category, content, importance}` 记录——不是关键词刮取 |
| **正则兜底** | 没有可用凭据时启用一层与项目无关的模式匹配，所以采集不依赖网络 |
| **可选的本地兜底** | Ollama 后端，通过 `ccl.enabled` **显式开启**（默认 `false`） |
| **两个采集点** | `Stop` 按轮从该轮的工具 observation 采集；`PreCompact` 在上下文被销毁前从 transcript 采集 |
| **有界的 transcript 读取** | 读头尾窗口（40 条记录 + 32 MiB）而不是整个文件——一个 2.11 GiB 的 transcript 从 88 秒降到 1.66 秒，钩子因此不会被写到一半杀掉 |
| **从最新往回摘要** | 抽取预算从最近的记录往回填；从最旧往前填会把每次抽取都钉死在会话最开头的几小时 |
| **七个类别** | `decision` · `result` · `config` · `bug` · `task` · `arch` · `note` |
| **五档重要性** | `1` 噪音 → `5` 关键/永不遗忘 |
| **三种项目模式** | `code` · `research` · `writing`——各有自己的被观察工具集与注入优先级（`/cc-mem mode`） |
| **内容与语言无关** | 抽取与恢复信号检测按设计同时识别英文**和**中文；存下来的记忆可以是任何语言 |

### 2 · 记忆质量——反补丁契约

这是差异点所在。多数记忆工具只做追加；而追加正是把记忆库变成噪音的原因。

| 功能 | 做什么 |
|---|---|
| **写入即归并** | 每条保存路径都走同一个 writer，由它决定 **MERGE**（就地覆盖近似行）、**SUPERSEDE**（归档旧行，用 `supersedes_id` 链接新行）还是 **INSERT** |
| **相似度基座** | trigram-Jaccard，且**在 CJK 连续段内改用字符 bigram**——纯 trigram 在中文上会塌陷，一个十字中文事实的一字修正只得 0.4545 分，于是每次修正都被存成一条新的、互相矛盾的事实 |
| **LLM 判定的语义去重** | 同一事实每个会话换个说法，trigram 分数很低；第二遍用词级 Jaccard 提名候选组，再让 Haiku 确认是不是同一事实后才合并 |
| **过时检测** | 给出 `{过时, 当前}` 配对，并带时间守卫，避免一个历史动作把仍然成立的事实判为过时 |
| **可回溯的历史** | `supersedes_id` 构成 DAG（拒绝成环，用 `COALESCE` 保住最早的血缘事实）；`/cc-mem supersedes <id>` 可以走这条链 |
| **从不真正删除** | 归档是 `is_active=0`，永远可恢复。`/cc-mem archive` 是退役一条**被发现是错的**事实的正式出口 |
| **保住来源信息** | 标签与存活行的标签取并集，不是替换，并设上限 |

### 3 · 会话交接——PROGRESS.md

| 功能 | 做什么 |
|---|---|
| **唯一真相来源** | `memory/PROGRESS.md` 永远由一行 SQL **全量重写**，从不追加——它不可能过期，也不可能自相矛盾 |
| **强制阅读** | `SessionStart` 发出 `<system-reminder>`，要求下一个 Claude 在回应*之前*先读它 |
| **11 个面向用户的字段** | 当前请求 · 已完成 · 进行中 · 受阻 · 待办 · 计划 · 关键上下文 · 触碰过的文件 · transcript 指针 · 更新时间 · 触发类型 |
| **四个写入者，一份契约** | `PreCompact` 全量覆盖；`Stop` 每轮增量更新触碰文件；`UserPromptSubmit` 在第 1 轮播种请求；`SessionStart` **只填仍为空**的字段 |
| **按会话标注** | 该行记录当前是哪个会话在说话、何时开始 |
| **被杀死的运行可见** | 起始标记能挺过超时击杀，因此一次写到一半死掉的压缩是可证明的，而不是无痕的 |
| **注入可观测** | `.last_inject.json` 精确记录注入了什么；`/cc-mem inject-show` 打印实况，`/cc-mem inject-usage` 报告 Claude 到底读没读 |

### 4 · 计划与意图——PLAN.md 与指令账本

两件不同的事，刻意分开：**计划步骤**是执行单元，计划被替换时它就死了；**指令**
是用户意图单元，它的寿命长于任何一份计划。

| 功能 | 做什么 |
|---|---|
| **实时计划锚点** | `memory/PLAN.md` 由 `plan_active` 行全量重写；`ExitPlanMode` 的输出会被自动捕获 |
| **随插件发布的两个子代理** | `plan-refiner` 把原始计划规范成结构化 JSON；`plan-guardian` 做只读的 ≤150 词漂移检查 |
| **机械式步骤同步** | `TodoWrite` 事件按标题相似度同步步骤状态——不用 LLM，也就不会漂 |
| **漂移计数器** | 编辑会加计数；一次敏感 Bash 调用（`git push`、`rm -rf`、部署）一次加 20 |
| **强制的结转闸门** | 替换计划时，每条未完成步骤都必须被自动结转、或被显式给出理由地处置。**按设计没有 force 开关** |
| **成功判据结转提示** | 替换中消失的判据会被点名——一个只覆盖 `steps` 的闸门，对其余部分什么都没说 |
| **只追加的计划历史** | 每份被换下的计划都归档进 `memory/.plan_history/` |
| **指令账本** | `/cc-mem directive-add` 记录**用户**提了什么要求。重复提同一个 slug 会让 `times_stated` 在**同一行**上累加——重复次数是计划表达不了的重要性信号 |
| **闭环必须有证据** | `/cc-mem directive-close` **没有 `--evidence` 就拒绝**：一个 commit、一个 `file:line`、或一个闸门名。凭断言关掉一条指令，正是这个账本要防的失败 |
| **Stop 强制执行** | 当计划未被 refine、活跃计划未做漂移检查、或某条活跃指令闲置过久时，`Stop` 钩子可以拒绝结束本轮——带有保证释放的逃生预算，以及关闭开关 `CC_MEMORY_PLAN_ENFORCE=0` |

### 5 · 检索

| 功能 | 做什么 |
|---|---|
| **FTS5 全文检索** | `/cc-mem search "auth flow"`，带 `LIKE ? ESCAPE` 兜底与上下夹紧的 limit |
| **主题摘要** | 记忆汇总成主题，由整理流程刷新 |
| **关键词词汇表** | 按词频统计的项目词汇，跨会话生长 |
| **分层注入** | `SessionStart` 注入主题 + 关键记忆 + 近期时间线 + PROGRESS 预览，各层都有预算 |
| **索引自动新鲜** | 每次批量写入后重新生成 `memory/MEMORY.md` |
| **编码损坏扫描** | `/cc-mem encoding-check` 在文本表里找 U+FFFD 损坏；`--apply` 可恢复地隔离它们 |

### 6 · 各类接口

| 接口 | 入口 | 你得到什么 |
|---|---|---|
| **斜杠命令** | `/cc-mem <sub>` | 32 个子命令，与路径无关，自动替你解析 `--project .` |
| **Shell CLI** | `cc-memory` / `cli/mem.py` | 同样的 32 个子命令，在 Claude Code 之外用 |
| **计划队列 CLI** | `cc-memory-plan` / `cli/plan.py` | 12 个子命令，`draft → ready → executing → done` 的任务队列 |
| **MCP 服务器** | `cc_memory/mcp/server.py` | 8 个工具，JSON-RPC 2.0 over stdio，在插件清单里内联注册 |
| **桌面看板** | `/cc-mem dashboard` | Tkinter 图形界面，7 个页签：Memories · Plans · Sessions · Keywords · SQL Console · Stats · Progress/Plan |
| **网页查看器** | `/cc-mem serve` | 仅回环的浏览器界面：浏览、检索、添加记忆 |
| **技能** | `/ccm-load`、`/save-memories` | 一次性激活与引导；经反补丁 writer 的手动保存 |
| **子代理** | `plan-refiner`、`plan-guardian` | 随插件发布，两种安装布局下都能被发现 |

### 7 · 隐私与安全

| 功能 | 做什么 |
|---|---|
| **按项目退出** | `excluded_projects`——被列出的目录*及其下所有内容*没有 `memory/`、没有数据库、没有 observation、没有抽取、没有注入。每个钩子和 MCP 服务器都会执行，判断用的是**原始** cwd，且在项目根锚定**之前** |
| **失败即关闭** | 一个存在但无法使用的 `config.json` 会排除**每一个**项目并记录原因，而不是猜"没被排除"然后不可逆地存下数据 |
| **`<private>` 区段** | `<private>` 标签之间的文本在发往 Anthropic 和写入数据库之前就被剥离——线性时间、无上限，且一个未闭合的起始标签会丢弃剩余部分而不是泄漏它 |
| **权威标记中和** | 存储内容在写入路径和每一条渲染路径上都被**转义，绝不原样插入**——一条记忆无法把 `<system-reminder>` 伪造进你的下一个会话 |
| **只读 SQL** | `/cc-mem sql` 拒绝一切写语句，包括 `PRAGMA name(value)` 这种 setter 形式 |
| **仅回环的网页查看器** | 不发 CORS 头，`Origin` 与 `Host` 双重校验（防 DNS 重绑定），POST 要求 JSON content-type，请求头**与**请求体两个阶段都有时限，并发有上限 |
| **MCP 模式校验** | `tools/call` 的参数按声明的 `inputSchema` 校验，不合法就用 `-32602` 拒绝，而不是强行转换 |
| **无遥测** | 除了发往 Anthropic（或你自己的 Ollama）的抽取/整理调用之外什么都不外发，用的是你已有的 Claude Code 凭据 |

### 8 · 可靠性工程

| 功能 | 做什么 |
|---|---|
| **零运行时依赖** | 纯 Python 标准库。PyInstaller 只在构建期用到 |
| **原子化产物写入** | 只有一个 writer——临时文件 + `os.replace`，带按墙钟计的重试预算。契约是：*要么完整替换，要么抛异常；绝不截断* |
| **项目根锚定** | `cwd` 会跟着代理自己的 `cd` 走；解析器沿祖先链行走（数据库 → `CLAUDE_PROJECT_DIR` → 项目标记），所以不会在子目录里生出游离数据库。项目容器目录和依赖树永远不是候选 |
| **共用的钩子入口阶梯** | stdin 解析 → 退出检查 → 根锚定，只实现一次；每个钩子各自的策略仍留在各自那里 |
| **有界的 LLM 墙钟** | 每个调用 LLM 的钩子都传绝对截止时刻，而不只是单腿超时，因此不可能超出宿主的硬性钩子超时 |
| **移出阻塞路径** | 整理作为 `PreCompact` 的 `async` 腿在预算闸门下运行，因此永远不会表现为 `Hook cancelled` |
| **按项目限定作用域** | 每个会碰表的命令都用 `project_id` 限定——一个数据库文件里合法地存着多个项目 |
| **11 道发布闸门** | 四道文档闸门、四个测试套件、`compileall`、一次 `pyproject` 解析，以及版本站点一致性。见[发布闸门](#发布闸门) |
| **一份可证伪登记册** | 每条已登记的修复都会在临时副本上被撤销，以证明它的闸门确实会**变红**。一个不可能失败的闸门，就是一个在说谎的闸门 |

---

## 参考手册

### `/cc-mem`——32 个子命令

在 Claude Code 内（与路径无关——包装脚本会解析插件根）：

```
# ── 状态与健康 ─────────────────────────────────────────────────────────────
/cc-mem status                      完整健康检查（钩子、DB、API key、PROGRESS）
/cc-mem stats                       记忆计数 + supersede 链计数
/cc-mem schema                      实时 SQLite schema（表、索引、迁移）
/cc-mem mode [code|research|writing] 查看或设置项目模式
/cc-mem summary                     最近一次会话摘要
/cc-mem sessions                    压缩历史与归档路径
/cc-mem observations                尚待抽取的原始 PostToolUse 行

# ── 读取记忆 ───────────────────────────────────────────────────────────────
/cc-mem search "<query>"            FTS5 检索
/cc-mem list [category]             最近的记忆，可按类别过滤
/cc-mem topics                      主题摘要
/cc-mem keywords                    按词频的项目词汇
/cc-mem supersedes <id>             走一条记忆的 supersede 链
/cc-mem sql "<SELECT ...>"          只读查询（写语句被拒）

# ── 写入记忆 ───────────────────────────────────────────────────────────────
/cc-mem add <category> "<text>" [--importance N]   反补丁式 upsert
/cc-mem archive <id>... [--supersedes ID]          退役一条**错的**事实（可恢复）
/cc-mem consolidate                 完整的 LLM 整理流程
/cc-mem cleanup                     轻量、不用 LLM 的清理 + 重建 MEMORY.md
/cc-mem encoding-check [--apply]    U+FFFD 损坏扫描

# ── 交接 ───────────────────────────────────────────────────────────────────
/cc-mem progress                    重建 memory/PROGRESS.md 并打印
/cc-mem inject-show                 上次 SessionStart 注入了什么
/cc-mem inject-usage                Claude 到底有没有读 PROGRESS.md / MEMORY.md

# ── 实时计划锚点 ───────────────────────────────────────────────────────────
/cc-mem plan-status                 计数器 + 新鲜度摘要
/cc-mem plan-show                   重建并打印 memory/PLAN.md
/cc-mem plan-set --raw "<text>"     捕获原始计划，标记 needs_refine
/cc-mem plan-set --raw-file FILE    同上，从文件读
/cc-mem plan-set --from-refiner     从 stdin 存入结构化 JSON
/cc-mem plan-check                  重置漂移计数器 + 给出 guardian 提示
/cc-mem plan-replan                 对已存的原始计划重新置位 needs_refine
/cc-mem plan-clear --reason "<why>" 丢弃活跃计划（有未完成步骤时必须给理由）

# ── 指令账本 ───────────────────────────────────────────────────────────────
/cc-mem directive-list [--status active|done|superseded|dropped|all]
/cc-mem directive-add <slug> --demand "..." [--quote "..."] [--kind ...] [--times N]
/cc-mem directive-close <slug> --evidence "<commit|file:line|闸门名>"

# ── 接口 ───────────────────────────────────────────────────────────────────
/cc-mem dashboard                   启动 Tkinter 图形界面
/cc-mem serve [--port N]            启动仅回环的网页查看器
```

每个子命令的完整语义见 [commands/cc-mem.md](commands/cc-mem.md)。

在 Claude Code 之外：

```bash
# 注意：用 $HOME，不要用 ~。bash 在参数展开之前就处理波浪号，且不会重新扫描
# 结果，所以存进变量里的 ~ 会保持为一个字面字符。
M="python $HOME/.claude/hooks/cc-memory/cli/mem.py --project ."   # 扁平安装
$M status
$M search "auth flow"
```

### `cc-memory-plan`——计划队列

同一个数据库里的任务队列，与实时计划**锚点**是两回事。

```bash
P="cc-memory-plan --project ."       # 或 python .../cli/plan.py --project .

$P add "任务 A" "任务 B" "任务 C"     # 追加草稿
$P list                              # 查看队列
$P reorder <id> <position>           # 调整顺序
$P evaluate                          # draft → evaluating
$P set-eval <id> "<结论>"            # 记录可行性结论
$P approve --all                     # evaluating → ready
$P exec --next                       # ready → executing，并打印计划正文
$P done <id> "<结果>"                # → done
$P fail <id> "<原因>"                # → failed
$P skip <id> "<原因>"                # → skipped
$P status                            # 队列摘要
$P clear                             # 清掉 done/failed/skipped
```

状态流：`draft → evaluating → ready → executing → done | failed | skipped`。
`exec` **不会**启动任何东西——它只是翻转状态，打印计划正文以及事后该跑的 `done`
命令。每个带 id 的子命令都先在 `--project` 内解析该 id，遇到未知或属于别的项目
的 id 就以 1 退出。

### MCP 工具

8 个工具，JSON-RPC 2.0 over stdio。服务器自己在 stdin 和 stdout 上强制 UTF-8 +
LF 换行——**不需要任何 `PYTHONUTF8` / `PYTHONIOENCODING` 环境变量**。

| 工具 | 用途 |
|---|---|
| `memory_search` | FTS5 检索，返回精简结果 |
| `memory_get_details` | 按 id 批量取完整详情 |
| `memory_add` | 经反补丁 writer 添加 |
| `memory_stats` | 项目统计 |
| `memory_topics` | 主题摘要（有上限） |
| `memory_recent` | 带过滤的最近记忆 |
| `progress_get` | 以结构化字段读取 PROGRESS 状态 |
| `progress_regenerate` | 从 SQL 强制重写 `memory/PROGRESS.md` |

**marketplace / dev checkout——什么都不用做。** `.claude-plugin/plugin.json`
里已内联注册。

**独立安装——需要手工注册。** 注意扁平布局里**没有** `cc_memory/` 这一段路径：

```jsonc
// <project>/.mcp.json，或用户级的等价文件
{
  "mcpServers": {
    "cc-memory": {
      "command": "python3",
      "args": ["<HOME>/.claude/hooks/cc-memory/mcp/server.py"]
    }
  }
}
```

### 配置

`config.json` 在你的安装根目录下——扁平安装：
`~/.claude/hooks/cc-memory/config.json`；marketplace / dev checkout：
`<plugin-root>/cc_memory/config.json`。

**这个文件里的每个键都有代码在读。** 没有读者的可调项是被删掉而不是留着的，
因为改一个什么都不做的键，看上去像是做了什么。

| 键 | 默认值 | 含义 |
|---|---|---|
| `version` | `2.11.4` | 给早于 `core/version.py` 的扁平安装的最后兜底；`core/version.py` 才是权威 |
| `consolidation.auto_interval_sessions` | `5` | 两次异步整理之间相隔的会话数 |
| `ccl.enabled` | `false` | 本地 Ollama 兜底——**需显式开启** |
| `ccl.ollama_url` | `http://localhost:11434` | Ollama 端点 |
| `ccl.local_model` | `ccl-9b` | 本地模型名 |
| `excluded_projects` | `[]` | 完全退出的绝对路径。这是唯一的退出方式，没有按项目的覆盖文件 |
| `notes` | — | 文件内文档，包括每个值由哪个模块读取 |

其余全是模块常量，记录在 `notes.removed_keys` 里：writer 阈值在
`llm/memory_writer.py`，注入预算在 `hooks/session_start.py`，空闲整理间隔在
`core/idle.py`，各模式的 observation 跳过名单在 `core/modes.py`，网页查看器的
默认端口在 `ui/web_viewer.py`。MCP 注册是 `.claude-plugin/plugin.json` 里的
`mcpServers` 块，不是配置键。

**环境变量**

| 变量 | 作用 |
|---|---|
| `ANTHROPIC_API_KEY` | 优先使用的凭据；缺失或失效时回落到 Claude Code 的 OAuth token |
| `CLAUDE_PROJECT_DIR` | 当它指向祖先链中的某个目录时，项目根解析器会采信它 |
| `CC_MEMORY_PLAN_ENFORCE=0` | Stop 钩子计划强制执行的关闭开关 |

### 每个项目的文件

```
<project>/memory/
├── memory.db                   SQLite（WAL）——真相来源
├── MEMORY.md                   可浏览索引，每次写入后刷新
├── PROGRESS.md                 交接；由 `progress` 行全量重写
├── PLAN.md                     实时计划锚点；由 `plan_active` 行生成
├── .gitignore                  由 cc-memory 写入；对既有安装会迁移
├── .last_save.json             上次 PreCompact 的状态与触发方式
├── .last_inject.json           SessionStart 注入了什么（可观测性）
├── .last_consolidation.json    异步腿的间隔标记
├── .consolidation.lock         防止异步 worker 重叠
├── .pre_compact_attempt.json   起始标记；它还在 ⇒ 上次运行被杀了
├── .plan_raw.md                最近一次 ExitPlanMode 的原始捕获
├── .plan_history/              被替换/清除计划的只追加归档
├── sessions/YYYY/MM/           每个会话的归档
└── topics/                     预留给按主题导出
```

### 数据库表

一个项目本地 SQLite 文件里的 12 张表：

| 表 | 存什么 |
|---|---|
| `projects` | 每个项目根一行 |
| `sessions` | 压缩/会话历史与归档路径 |
| `memories` | 事实本身，带 `supersedes_id`、`content_hash`、`is_active` |
| `topics` | 汇总出的主题摘要 |
| `keywords` | 按词频的项目词汇 |
| `observations` | PostToolUse 事件，抽取后清理 |
| `session_summaries` | 每个会话一份结构化摘要 |
| `progress` | **每项目一行**——PROGRESS.md 的真相来源 |
| `plan_active` | **每项目一行**——PLAN.md 的真相来源 |
| `plans` | 计划**队列**（`cc-memory-plan`） |
| `directives` | 用户意图账本（`/cc-mem directive-*`） |
| `_migrations` | 已应用的 schema 迁移 |

### 钩子一览

六个钩子命令 <!--ce:hooks-->，横跨五个 Claude Code 事件，声明在
[hooks/hooks.json](hooks/hooks.json)：

| 事件 | 脚本 | 超时 | 职责 |
|---|---|---|---|
| `UserPromptSubmit` | `hooks/user_prompt.py` | 8 秒 | 自动初始化 `memory/`、计轮次、第 1 轮播种请求 |
| `PostToolUse` | `hooks/post_tool_use.py` | 8 秒 | **在每种模式下**维护实时计划锚点，然后为被观察的工具各写一行 observation |
| `Stop` | `hooks/stop.py` | 22 秒 | Haiku 观察者、按轮增量更新 PROGRESS、每 5 轮空闲整理、计划强制执行 |
| `PreCompact`（同步） | `hooks/pre_compact.py` | 120 秒 | 抽取 → 归并 → 全量重写 PROGRESS.md → 归档 |
| `PreCompact`（异步） | `hooks/consolidate_async.py` | 300 秒 | 预算闸门下的整理，不在阻塞路径上 |
| `SessionStart` | `hooks/session_start.py` | 15 秒 | 注入分层上下文 + 强制的 `<system-reminder>` |

钩子契约，绝不违反：钩子从不写 stderr（Claude Code 会把 stderr 渲染成报错界面）、
从不抛异常、永远以 0 退出。

---

## 架构

完整细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
（[English](docs/ARCHITECTURE.md)）。三条硬契约的规范在
[docs/CONTRACTS.md](docs/CONTRACTS.md)：

- [反补丁契约](docs/CONTRACTS.md#anti-patch-contract)——一次写入如何归并
- [交接契约](docs/CONTRACTS.md#handoff-contract)——PROGRESS.md 规范
- [计划契约](docs/CONTRACTS.md#plan-contract)——PLAN.md、结转闸门、子代理

**两种安装布局，都受支持。** marketplace / dev checkout 保持嵌套的
`<plugin-root>/cc_memory/…` 形状。独立安装器把包铺成**扁平**的
`~/.claude/hooks/cc-memory/`——`core/`、`hooks/`、`llm/`、`cli/`、`mcp/`、`ui/`
直接在它下面，**没有 `cc_memory/` 这一段路径**。任何探测安装的代码或文档都必须
同时接受两者。

---

## 开发

### 仓库结构

```
cc-memory/
├── .claude-plugin/          plugin.json（含内联 mcpServers）· marketplace.json
├── .github/                 跑全部发布闸门的 CI · issue 与 PR 模板
├── agents/                  plan-refiner.md · plan-guardian.md
├── commands/                cc-mem.md —— /cc-mem 斜杠命令
├── hooks/hooks.json         钩子声明（6 条命令 / 5 个事件）
├── skills/                  ccm-load/ · save-memories/
├── cc_memory/               Python 包
│   ├── core/                db · extractor · consolidate · plan · progress · privacy
│   │                        modes · roots · atomic · markers · textsim · auth · …
│   ├── hooks/               _entry（共用阶梯）+ 六个钩子入口
│   ├── llm/                 ccl_backend · memory_writer · parse
│   ├── cli/                 mem.py · plan.py
│   ├── mcp/                 server.py
│   └── ui/                  installer · dashboard · web_viewer
├── docs/                    ARCHITECTURE.md · CONTRACTS.md（各带 .zh.md 兄弟文件）
├── scripts/                 build_exe.py —— PyInstaller 构建
├── tests/                   4 个套件 + run_gates.py（一条命令跑完所有闸门）
├── tools/                   citation_check · doc_claims · contracts · falsify_fixes · i18n_check
├── CLAUDE.md                给 Claude Code 的项目说明
├── CHANGELOG.md · README.md · README.zh.md · LICENSE · pyproject.toml
```

### 发布闸门

十一道闸门，全部纯标准库——没有 pytest，没有 pip 依赖。一条命令跑完：

```bash
python tests/run_gates.py           # 跑全部 10 道，打印表格，任一变红即非零退出
python tests/run_gates.py --list    # 看每道闸门查什么
```

或者单独跑：

```bash
python -m compileall -q cc_memory tests tools
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))"
python tests/smoke_test.py                    # 端到端 + 它承载的文档闸门 + 版本一致性
python tests/test_plan_carryover.py           # 结转闸门
python tests/test_surfaces.py                 # 安装器 · MCP · 查看器 · 退出开关 · 锚定
python tests/test_directive_enforcement.py    # 指令账本 + Stop 强制执行
python tools/i18n_check.py                    # 翻译漂移
python tools/citation_check.py                # 被跟踪文档里的每条 file.py:LINE 引用
python tools/doc_claims.py                    # 文字里的计数 vs 从代码树算出的集合
python tools/doc_coverage.py                  # 每个公开面都被拥有它的文档提到
```

还有两个脚本**不是**闸门，而是当你怀疑某道闸门时该跑的东西：

```bash
python tools/contracts.py       # 打印代码当前认为每个集合包含什么
python tools/falsify_fixes.py   # 在副本上撤销每条已登记的修复，断言其闸门变红
```

测试只能用 `tempfile` 目录，且必须清理干净：四个套件都在 import 这个包**之前**
把 `HOME`/`USERPROFILE` **和** `TMPDIR`/`TEMP`/`TMP` 重定向进沙箱，断言
`Path.home()` 确实移动了，并在 `finally` 里拆掉沙箱。清不掉的泄漏算测试失败。

### 构建可执行文件

```bash
pip install pyinstaller
python scripts/build_exe.py
# → dist/cc-memory-installer.exe
#   dist/cc-memory-dashboard.exe
```

### 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题报告见 [SECURITY.md](SECURITY.md)。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| Windows 上钩子从不触发 | `hooks/hooks.json` 调用的是 `python3`，而 python.org 的安装包默认不提供 `python3.exe`。勾选 "Add Python to PATH" + "py launcher"，或在 PATH 上把 `python3` 指向 `python` |
| `/cc-mem` 说找不到插件 | 两种布局都必须探测。跑 `/cc-mem status`——它会检查布局并报告缺了哪些文件 |
| 什么都没被抽取 | 没有凭据。`/cc-mem status` 会检查。登录 Claude Code，或设置 `ANTHROPIC_API_KEY` |
| 子目录里冒出一个 `memory/` | 根锚定之前留下的游离数据库。`/cc-mem status` 会列出项目根之下每一个独立数据库及其记忆数。游离库只被**报告，绝不合并或删除**——真正的嵌套子项目请用 `.ccm-root` 文件钉住 |
| 插件彻底不出声了 | 存在但无法解析的 `config.json` 会**失败即关闭**并排除所有项目。`SessionStart` 会打印一行说明；把 JSON 修好即可 |
| Claude 结束不了一轮 | 计划强制执行正在拦截。读那段拒绝文本——它会指明是哪个条件以及怎么修。逃生预算耗尽后它一定会退化成建议；`CC_MEMORY_PLAN_ENFORCE=0` 可以整个关掉 |
| 压缩时出现 `Hook cancelled` | v2.3.2 已通过把整理移到 `async` 腿修掉。若仍出现，请带上 `memory/.last_save.json` 提 issue |
| 某条记忆就是错的 | `/cc-mem archive <id>`——归并处理的是*重述*，`archive` 处理的是*推翻* |

---

## v2.11.3 有什么新东西

**文档追上了代码。** v2.11.2 改掉了指令闲置度的量法——那是一次带着承重规则的
schema 变更——而十道闸门照样全绿，因为它们查的是引用行号、绑定计数和翻译哈希。
**没有任何一道会问"这个新设计有没有被写下来"。** 规范文档里当时是零处提及。

- `docs/CONTRACTS.md` 的计划契约新增第四条承重性质：闲置度是
  `turns_total - turns_at_touch`，且**绝不可**改回用会被每次 guardian 检查
  重置的 `turns_since_last_guardian` 来量。
- `docs/ARCHITECTURE.md` 的数据库表用其他行已有的"自迁移 X 起带有 Y"格式
  记录了两个 v9 新列。
- `commands/cc-mem.md` 说清了对用户而言"闲置"到底怎么算：从**那一条指令**上次
  被写入起的轮次——重述或关闭它会重置这个时钟，跑 `/cc-mem plan-check` 不会。
- 两个中文兄弟文件同步更新。

另外更正：本 README 曾声称"按构造跨平台"，那不是证据。现在全部十道闸门在 CI 上
同时跑 Windows **与** Linux（3.11、3.13）；macOS 仍未被测量，文中已如实说明。

## v2.11.2 有什么新东西

**v2.11.1 记录为"仍未闭合"的三项，全部闭合——包括它自己糊过去的那一项。**

- **指令闲置度终于有了真实基准。** v2.11.1 是拿 `turns_since_last_guardian`
  来量的，而**那个计数器会被重置**——`/cc-mem plan-check` 和每次计划替换都会
  把它清零。于是一条真正三十轮没人碰的指令，只要有人跑一次 guardian 检查就
  显得刚被照料过：这个账本恰好赦免了它存在意义所在的那种疏忽。schema **v9**
  新增一个任何东西都不会重置的单调计数器 `turns_total`，每条指令记录自己上次
  被触碰时的轮次。闲置度变成两个只增不减的数字相减，任何重置都扭曲不了它。
- **Linux 现在跑全部十道闸门。** 它此前只跑平台无关子集，注释里断言另外两个
  是 Windows 专属——那是假设，不是测量，而它留下了本项目最大的未知：
  cc-memory 在 Linux 上到底能不能用。现在全量套件在 Linux 的 3.11 与 3.13
  上运行。
- **一个游离的 `.pytest_cache/`** ——在一个明确声明"不用 pytest"的项目里——已清除。

v2.11.1 自身闭合了六个位于"可以拒绝你这一轮"那条代码路径上的缺陷：永不释放的
逃生预算、以活权威标记抵达 Claude 的指令文本、不是 JSON 的拒绝输出、被清空后
仍永久强制执行的计划，等等。全部细节在 **[CHANGELOG.md](CHANGELOG.md)**。

更早的每个版本都在 **[CHANGELOG.md](CHANGELOG.md)** 里，那是本项目唯一的历史；
这份 README 记录的是这个软件**是什么**，而不是它曾经是什么。

---

## 运行要求

- **Python 3.8+**——仅标准库，零运行时依赖
- 支持钩子的 **Claude Code**
- **Tkinter** 只有桌面看板需要（CLI、MCP 服务器和网页查看器都不需要）
- **PyInstaller** 只在构建可执行文件时需要
- **Windows**：`python3` 必须能解析到一个 Python 3 解释器（见[故障排查](#故障排查)）

以 Windows 为首要平台开发。**全部发布闸门在 CI 上同时跑 Windows 与 Linux
（Python 3.11 与 3.13）**——此前这句话建立在"写的时候就考虑了可移植"之上，而那
不是证据；直到 v2.11.3 之前 Linux 只跑子集，而"另外两个套件是 Windows 专属"这个
假设后来被证明是错的。macOS 未被 CI 覆盖：预期可用（与 Linux 演练的是同一套 POSIX
路径），但**没有被测量过**，本文档不会说它被验证过。

---

## 文档地图

| 文档 | 用来做什么 |
|---|---|
| [README.md](README.md) · [README.zh.md](README.zh.md) | 这是什么、能做什么、怎么用 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [中文](docs/ARCHITECTURE.zh.md) | 模块地图、数据流、安装布局、i18n 约定 |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) · [中文](docs/CONTRACTS.zh.md) | 三条硬契约的规范形式 |
| [commands/cc-mem.md](commands/cc-mem.md) | 每个 `/cc-mem` 子命令及其语义 |
| [CLAUDE.md](CLAUDE.md) | 给在这个仓库**上**工作的 Claude Code 的说明 |
| [CHANGELOG.md](CHANGELOG.md) | 完整版本历史 |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | 怎么参与贡献；怎么报告漏洞 |

英文是规范骨架；每个 `*.zh.md` 都是被漂移跟踪的兄弟文件，绑定在其英文源的
归一化哈希上，一旦漂移 `tools/i18n_check.py` 就会让测试变红。记忆**内容**与语言
无关——只有项目自身的文档遵循这个约定。

---

## 许可

[MIT](LICENSE) © skymanbp

---

<sub>**关键词** — Claude Code 插件 · Claude Code 记忆 · LLM 智能体持久化记忆 ·
智能体长期记忆 · 上下文窗口管理 · 对话压缩恢复 · 会话交接 · AI 编程助手记忆 ·
Anthropic Claude · MCP 服务器 · Model Context Protocol · SQLite FTS5 记忆库 ·
编程智能体检索 · 提示注入加固 · 纯标准库 Python 插件。</sub>

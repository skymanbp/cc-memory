<!-- i18n-source: ARCHITECTURE.md | sha256: bcef4cdac790617b | version: 2.5.1 | translated: 2026-08-05 -->
> [English](ARCHITECTURE.md) · **简体中文**

# cc-memory — 架构（v2.5.1）

cc-memory 是一个 Claude Code 插件，为 Claude 提供**跨压缩、跨会话的持久化结构化
记忆**。本文档是总览：这个插件用来做什么、仓库如何布局、哪些钩子在何时触发、数据库
里存了什么、数据如何流动、LLM 调用如何认证、插件往项目里写了什么、安装后的代码物理上
位于何处，以及那套让文档保持翻译同步、不出现无声漂移的约定。

三条硬契约——反补丁写入、强制交接、实时计划锚点——规定在
[docs/CONTRACTS.md](CONTRACTS.md) 中。本文件描述机器；那份文件描述机器必须遵守的
规则。

**关于 `file:line` 引用。** 没有任何东西强制它们，而且它们在每一次重构后都会腐化。
v2.5.1 的文档整理重新推导了 §4 中 `core/db.py` 的引用，并对着代码核查了全部散文式
断言；但指向 `cc_memory/hooks/*`、`cli/mem.py` 和 `ui/installer.py` 的引用**刻意
没有**重新推导——那几个文件正在同一轮里被重写。请把行号当作线索，把**符号名**当作
事实。

## 目录

- [1. 总览 / 它解决什么问题](#1-总览--它解决什么问题)
- [2. 仓库布局](#2-仓库布局)
- [3. 钩子（Hooks）](#3-钩子hooks)
- [4. 数据库 schema](#4-数据库-schema)
- [5. 数据流](#5-数据流)
- [6. LLM 后端与认证](#6-llm-后端与认证)
- [7. 按项目的状态（memory/）](#7-按项目的状态memory)
- [8. 安装布局](#8-安装布局)
- [9. 文档语言约定（i18n）](#9-文档语言约定i18n)

---

## 1. 总览 / 它解决什么问题

三条设计约束驱动了其余的一切：

1. **反补丁写入。** 每一次记忆保存都经由同一个入口
   （`llm.memory_writer.upsert_smart`），它要么把新内容**归并**进一条已存在的相似
   记忆，要么**取代**一个旧版本（并保留取代链），要么作为一条新事实**插入**——由
   相似度决定，而不是由调用方决定。不存在“先追加、以后再去重”的路径。

2. **强制交接。** 在每一次 `SessionStart`，插件都会发出一个 `<system-reminder>`
   块，指示下一个 Claude 在回应之前先 `Read memory/PROGRESS.md`。PROGRESS.md 是
   单一真相来源，始终从 `progress` SQLite 表整篇重写——绝不追加。旧的 v2.0
   `SESSION_HANDOFF.md`（它已经漂移成补丁式的污染）会被迁移到一边。

3. **单一真相来源，不做堆叠。** 技能、命令、配置、文档各自只存在于**一个**地方。
   不会既有 `.claude/skills/` 又有 `skills/`。不会有三份 `save-memories`。不会有 6
   个文件各自声称不同的版本号。

由于这些代码运行在 Claude Code 自己的钩子预算内，还有两条运行层面的约束：

4. **钩子绝不能阻塞，也绝不能抛异常。** 每一个钩子入口都以 `sys.exit(0)` 结束，通过
   `core.logger` 把诊断信息写到 `~/.claude/hooks/cc-memory/logs/`，并且绝不写
   stderr（Claude Code 会把 stderr 渲染成错误）。延迟无上界的工作被彻底移出阻塞
   路径——这正是整理（consolidation）在 v2.3.2 中变成第二个 `async` PreCompact
   钩子的原因（见 [§3](#3-钩子hooks)）。

5. **运行时纯 stdlib。** `sqlite3`、`json`、`pathlib`、`urllib`、`datetime`、
   `subprocess`、`tkinter`、`time`、`hashlib`、`re`、`http.server`。无 pip 依赖。
   PyInstaller 仅在构建期使用。

### 设计上就是双语的——记忆内容与语言无关

记忆**内容**刻意保持语言中立。类别检测器（`core/extractor.py` 的 `_PATTERNS`，位于
`extractor.py:35-69`；`_IMPORTANCE_BOOST`，位于 `extractor.py:73-77`）与恢复信号
集合（`hooks/user_prompt.py:127-130`、`hooks/session_start.py:269-273` 的 RESUME
PROTOCOL）都是**有意**同时匹配中文和英文的，存储的记忆也可以是任意语言。这是文档
语言模型中的 **Tier 3**——与英文骨架的*文档*约定（Tier 1）是分开的。那些检测器带有
行内的 `i18n Tier 3` 注释，**不得**被削减为只识别英文。完整的三层模型见
[§9.1](#91-三层语言模型)。

---

## 2. 仓库布局

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← 插件清单（v2.5.1）
│   └── marketplace.json         ← /plugin marketplace add 条目
├── hooks/hooks.json             ← 钩子声明（6 条命令 / 5 个事件）
├── skills/                      ← 技能的唯一规范位置
│   ├── ccm-load/SKILL.md        （一次性激活 + 初始化 + 状态）
│   └── save-memories/SKILL.md   （经由 memory_writer 路由）
├── agents/                      ← 插件自带子代理（v2.2+）
│   ├── plan-refiner.md          （原始计划 → 结构化 JSON）
│   └── plan-guardian.md         （只读漂移检查，≤150 词）
├── commands/
│   └── cc-mem.md                ← /cc-mem 斜杠命令
├── docs/
│   ├── ARCHITECTURE.md          ← 本文件的英文源（总览 + i18n 约定）
│   ├── ARCHITECTURE.zh.md       ← 受漂移跟踪的翻译，即本文件（见 §9）
│   ├── CONTRACTS.md             ← 反补丁 + 强制交接 + 实时计划
│   └── CONTRACTS.zh.md          ← 受漂移跟踪的翻译（见 §9）
├── cc_memory/                   ← Python 包（已拆分子包）
│   ├── __init__.py              (转出口 core/version.py 的 __version__)
│   ├── config.json
│   ├── core/                    ← 领域层：db, extractor, consolidate, idle,
│   │                              progress, plan, privacy, modes, auth,
│   │                              logger, encoding_setup, version
│   ├── hooks/                   ← 钩子入口（6 个模块）
│   ├── llm/                     ← ccl_backend（Haiku/Ollama）+ memory_writer
│   ├── cli/                     ← mem.py, plan.py
│   ├── mcp/                     ← server.py（MCP stdio）
│   └── ui/                      ← installer, dashboard, web_viewer
├── tests/                       ← smoke_test.py（规范的端到端检查）+
│                                  test_plan_carryover.py + test_surfaces.py
├── tools/i18n_check.py          ← 文档翻译漂移检查器（仅 dev/CI）
├── build_exe.py                 ← PyInstaller 构建
├── pyproject.toml
├── README.md
├── README.zh.md                 ← 受漂移跟踪的翻译（见 §9）
├── CLAUDE.md                    ← 给 Claude Code 的项目指令
├── CHANGELOG.md
└── LICENSE
```

`agents/`、`tests/` 和 `tools/` 是承重结构，不是附带品：
`agents/plan-refiner.md` 与 `agents/plan-guardian.md` 都由
`cc_memory/hooks/stop.py` 提示触发；`tools/i18n_check.py` 正是
[§9](#9-文档语言约定i18n) 所规定的对象，也是
`tests/smoke_test.py:878-895` 作为漂移门禁导入的模块。

### 只有一个版本字符串（v2.5）

`cc_memory/core/version.py` 持有 `__version__`，是运行时**唯一**的权威来源。它放在
`core/` 而不是 `cc_memory/__init__.py` 里，原因是每个入口都通过把*包目录*插进
`sys.path` 再扁平导入（`from core.db import MemoryDB`）来自举——独立安装器铺出来的
是**扁平**目录树，在那种布局下 `import cc_memory` 会抛 `ModuleNotFoundError`，因此
任何必须在两种布局下都能跑的模块都不能用 `from cc_memory import __version__`。
`from core.version import __version__` 在两种布局下都成立，而
`cc_memory/__init__.py:64` 把它转出口，使得可从 wheel 导入的写法继续可用。

有四份**不可导入**的清单读不到它，必须一同 bump：`.claude-plugin/plugin.json`、
`.claude-plugin/marketplace.json`、`cc_memory/config.json`（其中的 `version` 是给
某个不知何故缺少 `core/version.py` 的扁平安装用的最后兜底）以及 `pyproject.toml`。
`tests/smoke_test.py` 会断言它们全部一致。

在 v2.5 之前，这个字符串还被重新手打进 CLI 横幅（`cli/mem.py`）、MCP 服务器横幅
（`mcp/server.py`）、安装器横幅与 GUI 标题（`ui/installer.py`）以及 `build_exe.py`；
其中两处字面量在 v2.4.3 时已经陈旧。它们现在全部在运行时解析该值，并带一条有文档的
兜底链（`core.version` → 包 `__init__` → 文本扫描 → `config.json` → `"unknown"`），
好让一次不完整的安装退化而不是崩溃。（在 v2.4.2 之前，这些副本曾漂移成三个不同的
值——2.4.1 / 2.3.4 / 2.3.3；见 `CHANGELOG.md` 的 2.4.2 → Changed。）

### 合并前的那几份文档去哪了

v2.4.3 把原本 5 份的 `docs/` 目录合并为 2 份。全部 79 处仓库内引用在同一次改动中
一并改指，因此树内已经没有任何链接会解析到被删掉的文件名。下表是重定向表，供那些
从已发布的 CHANGELOG 条目、GitHub 永久链接或以往会话笔记里追过来的读者使用：

| 合并前的文档 | 现在的位置 |
|---|---|
| `docs/MEMORY_RULES.md` | [CONTRACTS.md § Anti-patch contract](CONTRACTS.md#anti-patch-contract) |
| `docs/HANDOFF_PROTOCOL.md` | [CONTRACTS.md § Handoff contract](CONTRACTS.md#handoff-contract) |
| `docs/PLAN_PROTOCOL.md` | [CONTRACTS.md § Plan contract](CONTRACTS.md#plan-contract) |
| `docs/I18N.md` | [本文件 §9](#9-文档语言约定i18n) |
| `docs/ARCHITECTURE.md §3`（schema） | [本文件 §4](#4-数据库-schema)——合并前的文件没有编号章节，所以文件名没变但编号变了 |

`CHANGELOG.md` 刻意在历史条目里保留旧文件名：那些条目描述的是当时的目录状态，
改写它们等于伪造记录。

---

## 3. 钩子（Hooks）

`hooks/hooks.json` 声明了**横跨 5 个 Claude Code 事件的 6 条钩子命令**——
`PreCompact` 声明了两条：一条阻塞式的同步支路（`hooks.json:9`，120 秒）和一条后台
`async` 支路（`hooks.json:14`，300 秒，`"async": true`）：

| 钩子 | 入口 | 超时 | 职责 |
|------|-------|---------|-----|
| `PreCompact`（同步） | [`cc_memory/hooks/pre_compact.py`](../cc_memory/hooks/pre_compact.py) | 120s | 读取**有界**的 head+tail transcript 窗口（`extractor.load_transcript_window`）；经 Haiku 用 LLM 抽取记忆；经 `memory_writer.upsert_batch` 路由；**整篇重写** `memory/PROGRESS.md`；归档会话。写入一个起始标记，使被杀死的运行可被检测。 |
| `PreCompact`（异步） | [`cc_memory/hooks/consolidate_async.py`](../cc_memory/hooks/consolidate_async.py) | 300s，`async: true` | 每 N 次会话一次的 LLM 整理，在 v2.3.2 中被移出阻塞式压缩路径（间隔标记 + 锁，受预算门约束）。 |
| `SessionStart` | [`cc_memory/hooks/session_start.py`](../cc_memory/hooks/session_start.py) | 15s | 注入分层上下文（主题 / 关键项 / 时间线 / PROGRESS 预览 / 页脚）；发出强制的 `<system-reminder>`，要求 Read `PROGRESS.md` + `MEMORY.md`；追溯保存未保存的 JSONL。 |
| `Stop` | [`cc_memory/hooks/stop.py`](../cc_memory/hooks/stop.py) | 22s | 观察者：经 Haiku 从上一回合的 observations 抽取；每回合 `patch_progress(files_touched, ...)`；每 5 个回合运行 `idle.maybe_run_idle`（清理 + 重新生成 MEMORY.md）；当有活动计划时，累加其回合计数器并发出**一行**建议——若计划仍未精炼则是 plan-refiner 提示，否则在漂移阈值触发后给出 guardian 检查提示。 |
| `PostToolUse` | [`cc_memory/hooks/post_tool_use.py`](../cc_memory/hooks/post_tool_use.py) | 8s | **先**做实时计划集成，且所有模式一视同仁：`ExitPlanMode` → `plan_active.raw`，`TodoWrite` → 机械式步骤同步，`Edit`/`Write`/`MultiEdit`/`NotebookEdit` → 漂移计数器 +1，敏感 Bash 调用 → +20。**然后**才为被观测的工具调用向 `observations` 插入一行（模式白名单 / 跳过列表——`core.modes.should_observe`）。不调用 LLM。端到端实测约 180-290 ms，其中约 75-120 ms 是解释器启动。 |
| `UserPromptSubmit` | [`cc_memory/hooks/user_prompt.py`](../cc_memory/hooks/user_prompt.py) | 8s | 首次接触时自动初始化 `memory/`；跟踪回合数；为 Stop 观察者保存提示；在第 1 回合给会话打标签并为 `progress.current_request` 播种（依据双语恢复信号白名单，把触发类型判定为 `resume_request` 还是 `user_prompt`）。 |

### 钩子 stdout 契约

每个钩子的 stdout 都有特定角色，违反它就是一个用户可见的 bug：

- `SessionStart` 的 stdout → 注入的上下文（由 Claude 读取）。
- `Stop` 的 stdout → 状态行：每回合一行 `[cc-memory] …`
  （`stop.py:260-265`），外加至多一行 `[cc-memory.plan] …` 建议行。
- `PreCompact`（同步）的 stdout → **一行**状态行（会出现在下一次会话的压缩后
  上下文中）。
- `PreCompact`（异步）/ `PostToolUse` / `UserPromptSubmit` 的 stdout → 空。异步
  支路的 stdout 根本不会内联显示（`consolidate_async.py:31`）。

### PreCompact：为什么是两条支路

v2.3.1 把同步超时从 45 秒提高到 120 秒，但每 N 次会话一次的 LLM 整理在大型数据库上
仍可能超时，所以 `Compacted PreCompact … failed: Hook cancelled` 依旧会冒出来。
v2.3.2 把这个事件拆开了：

- **同步支路**只保留快速、对交接至关重要的工作（抽取 + PROGRESS.md，约 1-5 秒；
  `pre_compact.py:5-20`）。
- **异步支路**在一个 `BudgetGate` 之下运行 `core.consolidate.run_consolidation`，
  其中 `_BUDGET_TOTAL_S = 240.0`、`_BUDGET_SAFETY_S = 8.0`
  （`consolidate_async.py:59-60`），因此它启动的最后一次 LLM 调用会在
  `total_s - safety_s` = 232 秒之前完成，小于钩子自身的 300 秒超时——工作者绝不会
  在写入中途被杀。
- 节奏由**间隔标记 + 锁**决定，而不是脆弱的 `session_count % N` 检查：
  `memory/.last_consolidation.json` 记录上一次成功运行时的会话计数，
  `memory/.consolidation.lock` 防止工作者重叠（比 `_STALE_LOCK_S = 360.0`
  更旧的锁会被回收，见 `consolidate_async.py:64`）。这对并发的同步支路是
  竞态免疫的——计数上 ±1 的漂移既不会导致重复运行，也不会导致漏跑
  （`consolidate_async.py:19-28`）。

### 超时被声明了两次，必须保持完全同步

`hooks/hooks.json` 是市场 / 开发用的声明，也是唯一真相来源。
`cc_memory/ui/installer.py` 的 `HOOK_SCRIPTS` / `ASYNC_HOOK`
（`installer.py:92-102`）是独立安装的声明。自 v2.5 起，这些条目直接携带**最终上线
值**——PreCompact 120（同步）/ 300（异步）、SessionStart 15、Stop 22、
PostToolUse 8、UserPromptSubmit 8——并且只要 `hooks/hooks.json` 可用（开发检出，
或冻结构建内的 `cc_memory_meta/hooks.json`），`installer.py` 的
`_declared_hook_timeouts()` 就会去**读**它；只有在那个文件缺席的扁平 / 冻结安装
下，才回退到那张字面量表。

那个基于 `platform`、把上述值表达成“基础超时”的 **× 1.5 Windows 乘数已被删除**。
它曾让独立安装在五个事件中的三个上与市场安装不一致（Stop 33 对 22、
PostToolUse 12 对 8、UserPromptSubmit 12 对 8）。现在提高一个超时，意味着必须同时
改 `hooks/hooks.json` **和**那张兜底表；`tests/test_surfaces.py` 会断言两者在数值
上一致。

### observation 闸门不再遮蔽计划分支（v2.5 已修）

一直到 v2.4.3 为止，当 `core.modes.should_observe(mode, tool_name)` 为假时
`post_tool_use.py` 会提前返回，而实时计划集成块坐在那道闸门**之后**。
`should_observe` 先做跳过列表检查，再对 `observe_tools` 做白名单检查，而在全部
三种出厂模式中：

- `TodoWrite` 位于每种模式的 `skip_tools` 里 → `should_observe` 为 False；
- `ExitPlanMode` 不在任何模式的 `observe_tools` 里 → `should_observe` 为 False。

于是整个 v2.2 实时计划锚点**经由它自己的钩子是死的**：`PostToolUse` 从未写过
`plan_active`，从计划模式退出也从未生成 `memory/.plan_raw.md` 与 `memory/PLAN.md`，
而漂移计数还会随模式静默变化（编辑加分在 `code` 与 `writing` 下会触发，`research`
下不会；敏感 Bash 加分在 `code` 与 `research` 下会触发，`writing` 下不会）。实时
计划只能通过 `/cc-mem plan-set` 触达，而 `tests/smoke_test.py` 是直接调用
`core.plan.capture_exit_plan_mode` / `apply_todowrite_sync` 而不是经由钩子，所以
测试套件从来抓不到它。

`_apply_plan_integration`（`post_tool_use.py:77`）现在跑在闸门**之上**
（在 `post_tool_use.py:163` 调用），`should_observe` 只包住 `insert_observation`
那一块。按模式实测（code / research / writing）：`ExitPlanMode` → `plan_active`
行数 `0/0/0` → `1/1/1`；`Edit` → `edits_since_last_guardian` `1/0/1` → `1/1/1`；
Bash `git push`（1 次编辑 + 20）`21/20/1` → `21/21/21`。

**这条不变量在此写明，以防再次被破坏：** 模式决定的是什么值得**记住**；它绝不该
决定计划锚点是否跟得上现实。计划控制不是观测。不要把该块移回闸门之下，也不要靠
把 `TodoWrite` / `ExitPlanMode` 加进模式白名单来「修」它——`core/modes.py` 的
`should_observe` docstring 记录了这两条禁令。

另外注意，`config.json` 已经不再带 `observation.skip_tools` 键。它属于那约 34 个
没有任何读取者的叶子键之一，已在 v2.5 全部删除；真正生效的过滤器一直都是
`core/modes.py` 中按模式的 `skip_tools` + `observe_tools`。

---

## 4. 数据库 schema

SQLite 表（定义在 [`cc_memory/core/db.py`](../cc_memory/core/db.py)），按项目位于
`<project>/memory/memory.db`，WAL 模式：

| 表 | 用途 |
|-------|---------|
| `projects` | 每个项目路径一行（`db.py:36`）；自迁移 `v2_project_mode` 起带有 `mode`（`db.py:158`） |
| `sessions` | 每次压缩事件一行（`db.py:44`） |
| `memories` | 抽取出的事实（category、importance、topic、content_hash、**supersedes_id**、last_referenced_at）（`db.py:55`） |
| `topics` | 按主题名的整理摘要（带版本）（`db.py:69`） |
| `keywords` | 自动检测的项目词汇（`db.py:79`） |
| `plans` | 计划队列（draft → ready → done）（`db.py:88`） |
| `observations` | 原始 PostToolUse 事件，抽取后清理（`db.py:129`） |
| `session_summaries` | 每会话 6 字段结构化摘要（request / investigated / learned / completed / next_steps / notes）+ files_read/files_modified（`db.py:143`） |
| **`progress`** | v2.1 新增——每项目一行。`memory/PROGRESS.md` 的唯一真相来源（`db.py:188`）。 |
| **`plan_active`** | v2.2 新增——每项目一行。`memory/PLAN.md` 的唯一真相来源（`db.py:210`）。 |
| `_migrations` | 记录已应用的迁移（`db.py:289`） |

共十一张表，与 `CLAUDE.md` 的 §“Database schema (11 tables)” 一致。

此外还有 `memories_fts`——一个建立在 `memories` 之上的 FTS5 虚拟表
（`core/db.py:328`），由三个触发器保持同步（`core/db.py:332-350`，迁移 `v2_fts5` 在
`db.py:161`）。它只在本地 SQLite 构建带 FTS5 时才会创建；否则
`db.search_fts`（`core/db.py:1203`）回退到 `LIKE ? ESCAPE '\'`
（`core/db.py:1216-1226`）。FTS5 在 `.claude-plugin/plugin.json:4` 与 `:12` 中被
宣传，`/cc-mem status` 会报告当前实际走哪条路径（`cli/mem.py` 的 `cmd_status`）。

`memories` 上的 `supersedes_id` 列（迁移 `v3_supersedes`，`db.py:180`）把反补丁的
取代链显式化：当 `upsert_smart` 判定一条新记忆取代了一条旧记忆时，新行会回链到旧行
的 ID（旧行被归档）。通过 `db.get_supersede_chain(memory_id)`（`db.py:524`）走一遍
链条，就能看到完整的更新历史。`content_hash`（迁移 `v2_content_hash`，`db.py:124`）
是归一化内容的 `sha256[:16]`，用于廉价的精确重复检查（`db.compute_content_hash` 在
`db.py:749`，`db.find_by_hash` 在 `db.py:762`）。

迁移按 `_MIGRATIONS` 列表（`db.py:119`）的顺序应用，并记录在 `_migrations` 中。目前
已交付的层级：**v1**（topic 列 + 索引）、**v2**（content_hash、observations、
session_summaries、项目模式、FTS5、哈希回填）、**v3**（反补丁 + 强制交接：
`supersedes_id`、`progress`）、**v4**（`plan_active`）、**v5**（会话标注：
`progress.current_session_id`、`progress.session_started_at`——这样多会话工作流就能
从 PROGRESS.md 判断自己读到的是不是自己写的内容，`db.py:230-233`）、**v6**
（引用感知的老化：`memories.last_referenced_at`，在注入时设置，因此有效年龄是
`now - COALESCE(last_referenced_at, created_at)`，被引用过的事实保持“年轻”，
`db.py:244-248`）。

`progress` 行面向用户的字段是 `current_request`、`status_done`、
`status_in_flight`、`status_blocked`、`open_todos`、`plan`、`critical_context`、
`files_touched`、`transcript_ptr`、`updated_at`、`trigger_type`（共 11 个 ——
`db.py:188-201` —— 外加 v5 的两个会话标注列，合计 13 个非主键列）。`plan_active`
行持有 `raw`、`structured`、`active_step`、`edits_since_last_guardian`、
`turns_since_last_guardian`、`last_guardian_at`、`last_refined_at`、
`needs_refine`、`created_at`、`updated_at`（`db.py:210-222`）。

所有查询都使用参数化语句；禁止用字符串格式化拼 SQL。

---

## 5. 数据流

### 记忆写入流程（反补丁）

```
调用方（PreCompact / Stop 观察者 / SessionStart 追溯保存 /
        /save-memories 技能 / MCP add / mem.py add /
        dashboard Add-Memory + Save-Session / web_viewer add）
  │
  ▼
llm.memory_writer.upsert_smart(db, project_id, session_id, category, content,
                               importance, tags, topic)
  │
  ├─ 0. clean_for_storage + 拒绝短于 10 字符的内容（"too_short"）；把未知类别
  │      强制为 "note"；把 importance 钳制到 1..5
  │
  ├─ 1. compute_content_hash → find_by_hash → 精确命中则 SKIP
  │
  ├─ 2. 找出最相似的 ACTIVE 记忆（字符三元组上的 Jaccard）。
  │      范围：当设置了 topic 且该扫描能给出候选时，取同一 topic 内的记忆；
  │      否则按类别扫描最近更新的 50 条（memory_writer._find_similar,
  │      memory_writer.py:63-92）
  │      │
  │      ├─ sim >= 0.80 → MERGE_IN_PLACE (db.update_memory)
  │      │                  不新增行、不堆叠；importance = max(new, old)；
  │      │                  tags 增加 "merged"
  │      │
  │      ├─ sim >= 0.50 → SUPERSEDE (db.supersede_memory)
  │      │                  归档旧行，插入带 supersedes_id 链接的新行；
  │      │                  importance = max(new, old)；tags 增加 "supersedes"
  │      │
  │      └─ sim <  0.50 → 落到插入
  │
  └─ 3. INSERT NEW（独立事实）

regenerate_memory_index(db, project_id, memory_dir)   ← MEMORY.md 刷新
```

`upsert_smart` 本身**不会**重新生成 `MEMORY.md`。刷新是调用方的责任，且只有两种
形态：

- `upsert_batch`（`memory_writer.py:161-196`）逐条循环调用 `upsert_smart`，并在最后
  重新生成**一次**，但仅当传入了 `memory_dir` 时才会（`memory_writer.py:190-194`）。
  所有钩子调用方都会传（`pre_compact.py:435`、`stop.py:166`、
  `session_start.py:722`）；同步 PreCompact 支路还会在其余状态变更之后再刷一次
  （`pre_compact.py:509`）。
- 单发调用方显式调用 `regenerate_memory_index`：`cli/mem.py:524` 与 `:584`、
  `mcp/server.py:192`、`ui/dashboard.py:956`、`ui/web_viewer.py:325`，外加
  `skills/ccm-load` 的内联脚本（`SKILL.md:127, 137`）。`core/idle.py:94` 与
  `hooks/consolidate_async.py:188` 也会在维护之后刷新它。

（合并前的示意图把重新生成画成 `upsert_smart` 的无条件步骤，并省略了 `db` 参数；
上面已依据 `memory_writer.py:95, 190, 199` 对两者做了修正。调用方清单同样是在
`cc_memory/` 内 grep `upsert_smart|upsert_batch` 得到的完整集合。）

阈值只存在于一个地方——`memory_writer.HIGH_SIM = 0.80`、`MID_SIM = 0.50`、
`MIN_CONTENT_LEN = 10`、`MAX_CANDIDATES_TO_SCAN = 50`（`memory_writer.py:44-47`）。
`config.json` 里那个只作信息展示的 `writer` 块没有任何读取者，已在 v2.5 删除——
一个惰性的可调项比没有可调项更糟。完整契约见
[docs/CONTRACTS.md](CONTRACTS.md#anti-patch-contract)。

### 隐私过滤器（第 0 步）——线性、无上限、失败闭合（v2.5）

`clean_for_storage`（`core/privacy.py`）同时守卫面向 LLM 的路径
（`core/extractor.py`）与记忆写入路径，所以它的失效模式是**双向同时泄漏**。一直到
v2.4.3 为止，它是 `re.sub(r"<private>.*?</private>", "", text)` 外加一层
`text.count("<private>") > 100` 的 ReDoS 守卫——而那道守卫是**原样返回文本**，也就是
说它恰好在载荷看起来有敌意时**失败开放**：100 个标签剥除正确，101 个就同时泄漏进
Anthropic 请求**和** `memories` 表。

这个上限也校准在错误的信号上。格式良好的标签对正则引擎来说是廉价的；**未闭合**的
`<private>` 才是二次方那一档，因为每一个开标签位置都要把其后全部内容重新扫一遍去找
一个并不存在的闭标签（实测，CPython 3.13）：

| 输入 | `re.sub` | 线性扫描 |
|---|---|---|
| 20000 个格式良好标签（996.1 KiB） | 6.0 ms | 5.1 ms |
| 16000 个未闭合标签（140.6 KiB） | **9517.4 ms**，尾部泄漏 | 0.0 ms，尾部丢弃 |

`_strip_tagged_spans` 是一次从左到右、无回溯的 `str.find` 扫描，因此根本不需要任何
上限——而且悬空的开标签现在**失败闭合**：从它到文本末尾的一切都会被丢弃而不是发出
去。配对语义未变（每个开标签绑定其后第一个闭标签），并在 20000 个随机标签汤输入上
验证：全部 13328 个格式良好输入零行为差异。

同一类缺陷也存在于 `hooks/post_tool_use.py`，它在 `_truncate_output` **之后**才计算
`is_private`——而那个辅助函数会把 `Read` 的正文替换成字面量 `"(file content)"`。于是
对一个被用户标记为 private 的文件所做的 `Read` 会以 `is_private=0` 存下来；而
`is_private` 是 `db.get_recent_observations` / `get_observations_since` 里唯一的
过滤器，所以那一行的路径抵达了 Stop 观察者、PreCompact 抽取提示词以及
`progress.files_touched`。现在分类改在原始输入/响应上运行，早于任何一个有损截断辅助
函数。

### 交接流程（强制）

```
PreCompact（同步）：
  _write_attempt(memory_dir, trigger, claude_sid, transcript_bytes)
    ↓                              ← 在加载 transcript 之前写起始标记
  load_transcript_window(transcript_path)   ← 有界的 head+tail 读取
    ↓
  collect_progress_state(db, project_id, memory_dir, ...)
    ↓
  db.upsert_progress(...)                   ← 整行覆盖 progress 行
    ↓
  write_progress_md(db, project_id, memory_dir)   ← 整篇重写 memory/PROGRESS.md
    ↓
  .last_save.json（含 trigger: auto|manual）+ _clear_attempt(memory_dir)

Stop（每回合）：
  db.tag_progress_session(project_id, session_id)   ← v5，在打补丁之前
    ↓
  db.patch_progress(files_touched=..., trigger_type="stop")
    ↓
  write_progress_md(db, project_id, memory_dir)   ← 再次整篇重写（幂等）

UserPromptSubmit（仅第 1 回合）：
  db.tag_progress_session(project_id, session_id)
    ↓
  db.patch_progress(current_request=<user msg>,
                    trigger_type="resume_request" | "user_prompt")
    ↓
  write_progress_md(db, project_id, memory_dir)

SessionStart：
  注入上下文块（主题 30% + 关键项 15% + 时间线 20% +
                 PROGRESS 预览 25% + 页脚 10%，总预算约 16000 字符
                 —— session_start.py:48-56）
  页脚可能携带：PreCompact 被杀警告（残留的 .pre_compact_attempt.json，
                 需超过 10 分钟宽限窗口）、OAuth/api-key 警告、各类计数
  发出：<system-reminder>
          You MUST Read memory/PROGRESS.md and memory/MEMORY.md before
          responding to any user request. Explicitly state in your reply:
          "Read PROGRESS.md — prior progress: <summary>."
          …… 外加 RESUME PROTOCOL（双语 token 白名单 → 自动执行
          open_todos[0]）。
        </system-reminder>
```

上面的调用签名都是真实的：`write_progress_md(db, project_id, memory_dir)`
（`core/progress.py:239`；调用点 `pre_compact.py:501`、`stop.py:213`、
`user_prompt.py:133`、`session_start.py:680`、`mcp/server.py:243`、
`cli/mem.py:648`）。PROGRESS.md 的结构规格见
[docs/CONTRACTS.md](CONTRACTS.md#handoff-contract)。

### 被杀运行检测（v2.4.2）

被宿主超时杀死的 `PreCompact` 死于 `TerminateProcess`：不走 `except`，也不走
`finally`，所以 `.last_save.json` 仍然描述着*上一次*成功的运行，失败因此不可见。
为此，同步支路会在加载 transcript **之前**写入
`memory/.pre_compact_attempt.json`（`pre_compact.py:359-368`），并且只在运行完整
结束时才移除它（`pre_compact.py:536`）——包括在它自己的错误路径上
（`pre_compact.py:571`），这样一次*报错*的运行绝不会被报告成一次*被杀*的运行。
`SessionStart` 会报告残留的标记，但只在它至少已存在 10 分钟之后才报，因此一次仍在
进行中的运行绝不会被误标（`session_start.py:187-206`）。

### transcript 归属：永不做模糊匹配（v2.5）

有两条 SessionStart 路径会读取 PreCompact 从未处理过的 `.jsonl` transcript ——
`retroactive_save`（对未保存的既往会话做 LLM 抽取）与第 3 级的
`_refresh_progress_row` 挖掘（`open_todos`、`files_touched`、`transcript_ptr`）。
两者都要在 `~/.claude/projects/` 下解析本项目的目录，而一直到 v2.4.3 为止，两者都
可能解析到**另一个项目的**目录。

slug 约定是：把 `[A-Za-z0-9]` 之外的**每一个**字符替换成 `-`。cc-memory 只替换了其中
三个（`:` `\` `/`），于是任何路径含 `_` 或 `.` 的项目都会算出一个并不存在的 slug ——
而这次未命中会继续掉进对本机全部 slug 目录的模糊子串搜索。参考机器上的爆炸半径
（179 个 slug 目录）：子串 `core` 命中其中 131 个，`app` 与 `data` 各 141 个，
`proj` 33 个。于是一个项目可能摄取外来 transcript、把它送去 Haiku，再把抽取出的
事实当作自己的记忆存下来。

三处改动关闭了它：

1. `core.extractor.mangle_project_path`（`extractor.py:390`）成为该约定的唯一真源，
   由 `find_latest_transcript`、`hooks/session_start.py` 和 `ui/dashboard.py` 共用
   —— 后者此前逐字复制了旧解析器，连模糊分支一起。
2. 模糊兜底被**删除**。未命中返回 `None`。调用方必须把它当作「没有 transcript」，
   绝不能当成可以猜的许可。
3. 归属改为正向校验。`_transcript_belongs_to`（`session_start.py:478`）读取
   transcript 自身记录携带的 `cwd`，并且是**失败闭合**的 —— 没有 `cwd` 就不摄取 ——
   它在有界窗口加载之后为 `retroactive_save` 把关。第 3 级挖掘则使用故意更弱的
   `_transcript_is_foreign`（`session_start.py:498`）：缺失 `cwd` 放行，`cwd`
   **不同**才拒绝。两者的差异是刻意的 —— 追溯保存会把 LLM 抽取的记忆永久落库，
   理应要求证明；而第 3 级还必须对 `tests/smoke_test.py:266-278` 构造的那种没有
   `cwd` 的 transcript 形态继续可用。

实测：用两份植入的 transcript（其中一份外来），追溯保存从 2 条 LLM 腿摄取
`['aaaa-foreign', 'bbbb-mine']` 变为 1 条腿摄取 `['bbbb-mine']`；完全没有 `cwd` 的
transcript 得到 0 条腿、0 条记忆。第 3 级从
`open_todos=[{'content': 'FOREIGN TODO leak'}]` + `files=['FOREIGN_SECRET_FILE.py']`
+ 一个外来 `transcript_ptr` 变为全空，并记录一条拒绝日志。窗口在写入
`transcript_ptr` **之前**加载，因为一个指向别的项目 transcript 的指针本身就是污染。

### 实时计划流程（v2.2）

`ExitPlanMode` 的输出（或用户提供的 `/cc-mem plan-set` 文本）落入
`plan_active.raw` 并置 `needs_refine = 1`；`plan-refiner` 子代理把它规范化为 JSON，
经 `/cc-mem plan-set --from-refiner` 写回；`TodoWrite` 事件按三元组 Jaccard 匹配
机械地同步步骤状态（不调用 LLM）；`Edit`/`Write`/`MultiEdit`/`NotebookEdit` 会累加
`edits_since_last_guardian`，而敏感的 Bash 调用（`git push`、`rm -rf`、
`DROP TABLE`、`npm publish`、`kubectl apply`、`terraform apply`……见
`core.plan.is_sensitive_tool_call`，`plan.py:729`）一次加 20。一旦
`turns_since_last_guardian >= 8` 或 `edits_since_last_guardian >= 12`，Stop 钩子
就发出 guardian 建议（`core.plan.should_nudge_guardian`，`plan.py:702`），并把
refiner 提示限速为每会话每 5 个回合至多一次。钩子自己绝不派生子代理——它们只提示。
完整规格见 [docs/CONTRACTS.md](CONTRACTS.md#plan-contract)。**自 v2.5 起，上面的
每一条分支在每种模式下都会运行**——此前遮蔽它们的是什么，见
[observation 闸门](#observation-闸门不再遮蔽计划分支v25-已修)。

尚未精炼的原始计划也不再是隐形的：`core.plan.raw_pending_refinement`
（`plan.py:262`）是共享判据，`write_plan_md` 与 `/cc-mem plan-status` 都会以一条
PENDING REFINEMENT 横幅加逐字原文开头，并把更旧的结构化计划明确标注为已被取代。
那段逐字块的围栏宽度会超过原始文本里最长的一串反引号，因为计划模式的输出里经常
带有代码围栏。

---

## 6. LLM 后端与认证

`llm.ccl_backend.call_llm` 调用 Anthropic Haiku（模型
`claude-haiku-4-5-20251001`，`ccl_backend.py:27`）。调用方先用
`core.auth.get_api_key()` 解析出一份凭据并传进来；`call_llm` 会**先**尝试这一份，
当某一支失败时再**逐级回退**到 `core.auth.get_api_candidates()` 的其余条目——总共
限制为 2 条 Anthropic 支路（`ccl_backend.py:149`），这样最坏情况的墙钟时间对整理的
BudgetGate 来说仍是已知量。候选顺序与传输格式（`core/auth.py:20-57`、`_wire_for`
位于 `core/auth.py:8-17`、`_call_haiku` 的请求头位于 `ccl_backend.py:62-72`）：

1. `ANTHROPIC_API_KEY` 环境变量 → `x-api-key` 请求头
2. `~/.claude/.credentials.json` 中的 Claude Code OAuth 令牌（自动检测，按
   `expiresAt` 校验，毫秒时间戳）→ `Authorization: Bearer` +
   `anthropic-beta: oauth-2025-04-20`

传输格式的区分并非装饰：2026-07-14 实测验证，一个 `sk-ant-oat…` 令牌经
`x-api-key` 发送会得到 HTTP 401 "invalid x-api-key"，而同一个令牌经 Bearer + beta
发送则得到 HTTP 200（`core/auth.py:14-15`）。

`get_api_key()` 是同一份候选列表的单凭据向后兼容视图（它不重试，
`core/auth.py:60-93`）；它同时承载 `oauth_expired` 信号，支撑 SessionStart 的
“[WARNING: OAuth expired — LLM extraction disabled]” 页脚
（`session_start.py:214-219`）。钩子调用方用它来*提供*传给 `call_llm` 的凭据：
`pre_compact.py:146 → :166`、`stop.py:93`、`session_start.py:490`、
`core/consolidate.py:355, 549, 724`。

逐级回退是 v2.3.4 为一个具体故障加入的：一个失效的环境变量密钥（例如额度为零 →
HTTP 400）过去会把排在它后面的健康订阅令牌黑洞掉，从而无声地把每一次 LLM 调用推给
Ollama，每一批整理都要冷加载一个 5.9 GB 的本地模型（`core/auth.py:30-33`、
`ccl_backend.py:10-12`）。

本地 Ollama 兜底是**按需开启、默认关闭**的（`cc_memory/config.json:63` 的
`ccl.enabled: false`；`ccl_backend.py:33` 的 `_DEFAULT_OLLAMA_ENABLED = False`，
因此缺少该键也读作 False），与之并列的还有 `ccl.ollama_url` / `ccl.local_model`。
当它被禁用时，这条支路被跳过，只记录一个原因字符串
`"ollama: disabled (config ccl.enabled=false)"`（`ccl_backend.py:169-170`），因此
默认安装根本没有本地兜底。

### 给墙钟设界：`fallback_timeout` 与 `deadline`

`call_llm`（`ccl_backend.py:123`）提供两条互相独立的界限。

`fallback_timeout` 为 Ollama 支路设定上界。当它为 `None` 时默认取
`min(timeout*3, 120)`。于是一次调用的最坏包络是

```
2 * timeout  +  (启用 ccl 时的 fallback_timeout，否则 0)
```

因为 Anthropic 候选被限制在 2 个（`ccl_backend.py:149`）。正是这套算术让整理的
`BudgetGate` 能保证按时完成——见 `core.consolidate._worst_call_cost`。

**`deadline` 是更强的那条界限，也是钩子必须使用的那条。** 它是一个绝对的
`time.monotonic()` 时刻，调用必须在此之前**结束**：每条腿的有效超时都会被夹到实际
剩余的时间，剩余不足最小值的腿直接跳过。因此总墙钟时间由该期限兜底，与存在多少个
凭据候选无关——这让调用方可以为常见的单候选路径保留一个宽裕的单腿 `timeout`，而不必
为了扛住病态情形把它压小。

一直到 v2.4.3 为止，`core/consolidate.py` 是**唯一**遵守这套规则的模块，而三个带硬性
宿主超时的钩子都违反了它——其中一个在出厂默认配置下就违反：

| 调用点 | 宿主预算 | v2.5 之前的包络，`ccl` 关 | 开 |
|---|---|---|---|
| `hooks/stop.py` | 22 s | 16 s | 40 s ✗ |
| `hooks/pre_compact.py` | 120 s | 50 s + 约 26 s transcript 工作 | 125 s ✗ |
| `hooks/session_start.py` | 15 s | **40 s ✗** | 100 s ✗ |

`session_start` 在 v2.5 之前的 `_RETRO_DEADLINE_S` 只决定要不要再开一个*文件*；它
无法中断一条已经在飞的腿，所以一个卡住的 socket 就是 40 s 撞 15 s 的强杀。三个钩子
现在都在其包导入**之前**捕获 `_HOOK_T0 = time.monotonic()`（这样导入开销也计入预算），
并把 `deadline=_HOOK_T0 + <预算>` 传进 `call_llm`：`stop.py` 的
`_LLM_DEADLINE_S = 14.0`、`pre_compact.py` 的 `75.0`、`session_start.py` 的
`_RETRO_DEADLINE_S = 13.0`。用刻意拖住的腿实测：Stop `25.45 s → 15.99 s`（预算 22 s）；
PreCompact `约 144 s → 74.39 s`（预算 120 s）。常规路径延迟不变（0.29 s → 0.30 s），
因为预算充裕时每条腿仍拿到完整的单腿 `timeout`。

诚实的界限是 `deadline + (k-1) * timeout`，其中 `k` 是 socket 超时被实测到的超出
比率（期限阻止腿*开始*，最后一条腿仍可能在飞）。按实测的 `k ≈ 1.48`，即 Stop 22 s
中的 17.4 s、PreCompact 120 s 中的 87 s。

如果所有启用的支路都失败，`call_llm` 抛出携带逐支路聚合原因的 `RuntimeError`
（`ccl_backend.py:172-174`），钩子则优雅降级——抽取被跳过，但归档 / 交接 /
observations 仍会保存。钩子**绝不会**把异常抛进 Claude Code。（最后这句话直到
v2.4.2 才成立：`_extract_via_llm` 的 `except` 元组此前不包含 `RuntimeError`，因此
一次彻底的 LLM 故障会逃逸到钩子的外层处理器，连同抽取一起跳过 `PROGRESS.md` 重写
——见 `CHANGELOG.md` 的 2.4.2。）

---

## 7. 按项目的状态（memory/）

按项目的状态位于 `<project>/memory/`：

```
<project>/memory/
├── memory.db                    SQLite（WAL 模式，所有表）
├── MEMORY.md                    自动生成，每次批量写入后刷新
├── PROGRESS.md                  每次 Stop+PreCompact 从 `progress` 行整篇重写
├── PLAN.md                      从 `plan_active` 行整篇重写（v2.2）
├── .last_save.json              上一次 PreCompact 的状态（含 auto/manual 触发方式）
├── .last_inject.json            SessionStart 实际注入了什么（v2.3）
├── .last_consolidation.json     上一次整理时的会话计数（v2.3.2）
├── .consolidation.lock          防止异步工作者重叠（v2.3.2）
├── .pre_compact_attempt.json    起始标记；残留 ⇒ 上一次运行被杀（v2.4.2）
├── .plan_raw.md                 最近一次 ExitPlanMode 原始捕获（v2.2）
├── .plan_history/               被替换/清除计划的只追加归档（v2.4.0）
├── .gitignore                   忽略 memory.db（+ -wal/-shm）、sessions/、以及上面
│                                每一个点前缀的运行时产物，外加 *.tmp——三个生成的
│                                .md 文件是刻意不被忽略的
├── sessions/YYYY/MM/            按会话归档的摘要
└── topics/                      预留给未来的按主题 md 导出
```

写入方，便于溯源：`MEMORY.md` ← `memory_writer.regenerate_memory_index`
（`memory_writer.py:199`）；`PROGRESS.md` ← `core.progress.write_progress_md`
（`progress.py:239, 366`）；`PLAN.md` ← `core.plan.write_plan_md`
（`plan.py:310`）；`.plan_history/` ← `plan.py:437`；`.last_save.json` ←
`pre_compact.py:526, 556`；`.last_inject.json` ← `session_start.py:291-309`
（临时文件 + `os.replace`，是真正原子的，不同于 `.last_save.json` 用的普通写）；
`.last_consolidation.json` / `.consolidation.lock` ←
`consolidate_async.py:155-156`；`.pre_compact_attempt.json` ←
`pre_compact.py:284-311`。`sessions/` 与 `topics/` 由最先接触该项目的那条路径创建
——自动初始化时是 `user_prompt.py:44-45`，否则是 `pre_compact.py:342-343`。

`memory/PROGRESS.md`、`memory/MEMORY.md` 和 `memory/PLAN.md` 都是**生成产物**。请改
SQL 真相来源（PROGRESS.md 对应 `progress`，PLAN.md 对应 `plan_active`，MEMORY.md
对应 `memories`/`topics`/`keywords`）。

### .gitignore 会迁移，而不只是创建（v2.4.2）

`core.progress.MEMORY_GITIGNORE_LINES`（`progress.py:42-56`）是规范的忽略集合，
`ensure_memory_gitignore`（`progress.py:59-80`）**只追加缺失的行**，保留用户自己
添加的任何内容。此前每一版生成器都被 `if not gi.exists()` 守卫着，因此每当插件开始
写一种新产物，已有安装就会永远保留过期的忽略列表，并开始无声地泄漏它。这些产物中
有几种会逐字嵌入对话或计划原文，所以那是隐私问题，而不只是噪声。`pre_compact.py:353`
在**每一次**压缩时都运行它（而不只是在项目创建时），正是为了让老安装完成迁移。这份
列表另有两份独立副本，因为它们无法导入本模块，必须手工保持同步：
`cc_memory/ui/installer.py`（仅 stdlib 的引导程序）与 `skills/ccm-load/SKILL.md`
（内联脚本）。

旧的 v2.0 `SESSION_HANDOFF.md` 文件会在 v2.1 下的首次 PreCompact 时被重命名为
`SESSION_HANDOFF.md.v2.bak`（一次性迁移 `core.progress.migrate_legacy_handoff`，
`progress.py:383`）。

---

## 8. 安装布局

`cli/mem.py` 的 `_detect_install_layouts`（`cc_memory/cli/mem.py:103-188`）识别三种
布局。一台机器上可以同时存在多种（例如一个开发检出加上一条过期的市场缓存条目），
因此 `/cc-mem status` 会逐一报告：

- **marketplace-directory**——`extraKnownMarketplaces["cc-memory"].source.path`
  指向一个检出目录（`mem.py:134-142`）。此时 `hooks/hooks.json` 里的
  `${CLAUDE_PLUGIN_ROOT}` 会解析到工作树本身，因此编辑 `cc_memory/**.py` 就会更新
  实时钩子，无需任何复制步骤。这是本仓库使用的开发布局，也是 `CLAUDE.md` 的
  §“Sync protocol” 说代码改动无需复制到 `~/.claude/hooks/` 的原因。
- **marketplace-cache**——来自 `~/.claude/plugins/installed_plugins.json` 的
  `installPath`（`mem.py:144-174`）。一个已记录但已不存在的 `installPath` 会被
  报告为损坏布局，而不是被跳过（`mem.py:158-170`）。
- **legacy / 独立安装**——`~/.claude/hooks/cc-memory/`（`mem.py:176-187`），由
  PyInstaller 安装器写入（`ui/installer.py:33` 的 `TARGET_DIR`）。这里的钩子由
  `_merge_into_settings`（`installer.py:127+`）直接注册进
  `~/.claude/settings.json`，而不是通过插件清单。

在市场类布局下，`~/.claude/hooks/cc-memory/` 只保留 `logs/`（`core.logger` 的输出
目标）。`logs/` 在每种布局下都存在，因为 logger 的路径是绝对的，与代码位于何处无关。

### 嵌套 vs 扁平：独立安装器写出的是**扁平**目录树

这个区别对任何要探测文件系统的人都很重要，因为两种形态并不共享 `cc_memory/` 这一段
路径。

**市场 / 开发检出（嵌套）**——包位于插件根目录下的 `cc_memory/` 目录里，钩子命令是
`${CLAUDE_PLUGIN_ROOT}/cc_memory/hooks/<name>.py`（`hooks/hooks.json:9, 14, 27,
39, 51, 63`）：

```
<plugin root>/                       ← 例如 D:\Projects\cc-memory
├── hooks/hooks.json
└── cc_memory/
    ├── __init__.py, config.json
    ├── core/  hooks/  llm/  cli/  mcp/  ui/
```

**独立安装器（扁平）**——`_copy_subpackages(TARGET_DIR)` 把每一个 `SUBPACKAGE_FILES`
键（`installer.py:61`）直接写到 `TARGET_DIR`（`installer.py:56`）之下，
**没有 `cc_memory/` 这一段**，并且 `_make_hooks_config` 把命令构造成
`python "<TARGET_DIR>/hooks/<name>.py"`：

```
~/.claude/hooks/cc-memory/           ← ui/installer.py:56 TARGET_DIR
├── __init__.py
├── config.json
├── installed_surfaces.json  ← 写进了 ~/.claude 的东西（v2.5）
├── core/    auth.py consolidate.py db.py encoding_setup.py extractor.py
│            idle.py logger.py modes.py plan.py privacy.py progress.py
│            version.py
├── hooks/   consolidate_async.py post_tool_use.py pre_compact.py
│            session_start.py stop.py user_prompt.py
├── llm/     ccl_backend.py memory_writer.py
├── cli/     mem.py plan.py
├── mcp/     server.py
├── ui/      dashboard.py installer.py web_viewer.py
└── logs/    ← core.logger 的输出目标
```

注意扁平树里*缺少*什么：没有 `hooks/hooks.json`（`SUBPACKAGE_FILES` 不包含它——
注册改为写入 `~/.claude/settings.json`），没有 `.claude-plugin/`，也没有 `docs/`、
`tests/` 或 `tools/`。尤其是 `tools/i18n_check.py`，它被刻意排除在
`SUBPACKAGE_FILES` 和 `build_exe.py` 之外；打包后的插件不受它影响
（见 [§9.5](#95-检查器)）。由此有两个后果：扁平布局永远读不到 `plugin.json`，
所以它的 **MCP 注册必须手工写**，指向 `<TARGET_DIR>/mcp/server.py`；以及
`core/version.py` 必须被加进 `SUBPACKAGE_FILES["core"]`，否则每一个执行
`from core.version import __version__` 的模块都会在扁平安装下退化。

### 五个用户界面是单独安装的（v2.5）

`skills/`、`agents/` 和 `commands/` **不是**包文件，而一直到 v2.4.3 为止，独立安装器
根本不发布它们：安装完成后 `~/.claude` 里只有 `hooks/` 和 `settings.json`，别无他物
—— 没有 `/cc-mem` 命令、没有 `plan-refiner` / `plan-guardian` 子代理、没有技能。
用户真正会去交互的东西全都缺失。

`SURFACE_FILES`（`installer.py:79`）恰好点名五条路径 —— `commands/cc-mem.md`、
`agents/plan-refiner.md`、`agents/plan-guardian.md`、`skills/ccm-load/SKILL.md`、
`skills/save-memories/SKILL.md` —— 而 `_copy_surfaces` 在安装的第 [2/3] 步把它们写进
`~/.claude/`，并把写了什么记录进 `installed_surfaces.json`（`SURFACE_MANIFEST`，
`installer.py:58`）。

卸载是**按名字**进行的，绝不 `rmtree`：`~/.claude/{commands,agents,skills}` 里放着
用户自己的文件。`_remove_surfaces` 只删除被记录的那些路径，会
移除被清空的 `skills/<name>/` 但绝不移除 `commands/` 或 `agents/` 本身，并且区分
「没有清单」（回退到本次构建的 `SURFACE_FILES`）与「清单记录了零个文件」（什么都不
删，并明说）。在预先放入用户自己的 `commands/my-own.md` 和 `agents/my-agent.md` 后
做一次安装-卸载往返，剩下的恰好就是那两个文件。

### settings.json 在任何复制之前就被校验（v2.5）

`_read_settings`（`installer.py:508`）返回 `(dict, None)` 或 `(None, error)`，绝不
抛异常；`cli_install` 在第 **[0/3]** 步调用它，解析失败时以 1 退出并打印
`Nothing has been installed.`。一直到 v2.4.3 为止，解析发生在复制**之后**，所以一份
安装器读不懂的 `settings.json` 会留下 32 个文件在盘上、**零个钩子被注册** —— 卸载器
也以同样方式死在半途。19 种设置形状 ×{预检、安装、二次安装、卸载、二次卸载}：
**18 次崩溃 → 0**。

其中值得知道的几个判断：空文件或只含空白的文件按 `{}` 处理（没有用户数据会丢，而
拒绝会让一台全新机器卡住）；UTF-8 BOM 被容忍，因为 PowerShell 的 `>` 就会写一个；
三种确实编码了我们无法解析之意图的形状（JSONC 注释、尾逗号、顶层数组）以 rc=1 拒绝
且不复制任何东西；畸形的钩子组被**逐字保留**而不是搅碎；一条仅仅提到 "cc-memory"
却并未运行本次构建那六个钩子脚本之一的钩子命令，会被**保留并给出提示**而不是删除。

### 布局检测与检查现在一致了（v2.5 已修）

检测同时接受两种形态：`mem.py:181` 测试
`(legacy / "cc_memory").exists() or (legacy / "core" / "db.py").exists()`。检查此前
与它自相矛盾：`_inspect_layout`（`mem.py:251`）把 `_REQUIRED_PLUGIN_FILES`
（`mem.py:133`）中每一条带 `cc_memory/…` 前缀的条目都以布局**根目录**为基准解析，
于是一个健康的扁平安装被报成 22 个文件全缺、打印 `[FAIL]`——而且因为
`/cc-mem status` 只对「完全可用」的布局跑 API key 检查，那项检查被整个跳过。

它现在只解析一次 `pkg_dir`（`mem.py:274`：若 `root/"cc_memory"` 目录存在则取它，
否则取 `root`），据此剥去前缀，并且只对 plugin-manifest 安装要求
`hooks/hooks.json` —— 独立安装器从不复制它，而当钩子来自 `settings.json` 时它也毫无
意义。报告会打印 `(flat)` / `(nested)` 让形态可见，`cmd_status` 也改为把返回的
`pkg_dir` 插进 `sys.path`，而不是硬编码的 `root/"cc_memory"`。

安装器自己的安装后说明此前打印 `TARGET_DIR/cc_memory/cli/mem.py`——一条它从不创建的
路径；现在打印的是确实存在的 `TARGET_DIR/cli/mem.py`（`installer.py:1087`）。

### 解释器要求

`hooks/hooks.json` 调用的是 `python3`。在 Linux/macOS 上这就是标准的 Python 3
可执行文件。在 Windows 上，python.org 的安装器会提供 `python.exe` 加上 `py.exe`
启动器，但默认**不**提供 `python3.exe`——请在安装插件之前勾选“Add Python to
PATH” + “py launcher”，或者把 `python3` 别名到 `python`。否则钩子会无声失败
（会记录到 `~/.claude/hooks/cc-memory/logs/`，但 Claude Code 对命令缺失不显示任何
错误 UI）。

独立安装器绕开这一点的方式是**运行**每一个候选，而不是探测它是否存在：
`_detect_python_cmd`（`cc_memory/ui/installer.py`）以 15 秒超时执行
`<cand> -c "import sys;print(sys.version_info[0])"`，取第一个回答 `3` 的候选。
`shutil.which("python3")` 不够用 —— 在没有安装 Store Python 的 Windows 上，它会解析
到一个 0 字节的应用执行别名（App Execution Alias），由此生成的钩子命令会无声失败。
（残留的代价：在那种机器上*执行*该别名可能弹出 Microsoft Store；那个超时给它设了
上界。）

---

## 9. 文档语言约定（i18n）

本章定义 cc-memory 如何**在不产生无声漂移的前提下**用不止一种语言维护面向人类的
文档。英文是规范骨架；其他语言是受漂移跟踪的兄弟文件，通过其英文源的哈希绑定。

它就是这套约定的英文真相来源。执行它的检查器是 `tools/i18n_check.py`（纯 stdlib，
仅 dev/CI——不随插件分发）。

> 合并说明：本章在 v2.4.2 之前是 `docs/I18N.md`，在 v2.4.3 被并入这里。所有代码内
> 指针在同一次改动中已一并改指——`core/extractor.py`（`:32`、`:71`）、
> `hooks/session_start.py`、`hooks/user_prompt.py` 里的 Tier-3 守卫注释，以及
> `cc_memory/__init__.py` 的模块 docstring，全都引用
> `docs/ARCHITECTURE.md#9-documentation-language-convention-i18n §1`，而且
> `grep -rn I18N cc_memory/` 没有任何输出。（本条此前带的那三个钩子 / 包内行号已被
> 无关改动挪动；`grep -rn "i18n Tier 3" cc_memory/` 才是稳定的定位方式。）

### 9.1 三层语言模型

整个体系建立在把人们说“语言”时所指的三件不同的事分开之上，每一层有各自的规则：

| 层 | 是什么 | 规则 | 位于何处 |
|------|------|------|----------------|
| 1 — 骨架 | 英文规范文档 + 所有面向 LLM 的字符串 | 英文具有权威性；每一份翻译都需要一个英文源 | `README.md`、`docs/*.md`；钩子 / CLI 指令字符串 |
| 2 — 翻译 | 供人阅读的其他语言文档 | `NAME.<lang>.md` 兄弟文件，受漂移跟踪，按需产出 | `README.zh.md`、`docs/ARCHITECTURE.zh.md`、`docs/CONTRACTS.zh.md`（自 v2.5 起，三份被跟踪的英文文档全都有译文） |
| 3 — 内容 | 用户存储的记忆内容 | 任意语言；双语检测是有意为之 | `extractor.py`、`user_prompt.py`、`session_start.py` |

- **Tier 1 刻意保持英文。** 钩子 stdout 和 `Claude:` 开头的 CLI 指令输出是给模型读
  的，不是给终端用户读的——它们是为指令遵循而调过的，翻译它们会降低行为质量。
  “英文规范”意味着这些内容永不翻译。
- **Tier 3 在设计上与语言无关。** 抽取器模式与恢复信号集合同时匹配英文和中文，存储
  的记忆也可以是任意语言。见
  [§1 “设计上就是双语的”](#设计上就是双语的记忆内容与语言无关)。
  **不要**把那些检测器削减为只识别英文——那会破坏设计的第 3 条
  （“内容可以是任意语言”）。具体的受守卫位置是 `core/extractor.py:35-69`
  （`_PATTERNS`）、`core/extractor.py:73-77`（`_IMPORTANCE_BOOST`）、
  `hooks/session_start.py` 里 RESUME PROTOCOL 的 token 行，以及
  `hooks/user_prompt.py` 里的 `resume_signals`；后两者必须彼此保持同步，因为
  强制提醒承诺的正是 `user_prompt` 判定为 `resume_request` 的那个行为。这四处都带
  `i18n Tier 3` 注释——`grep -rn "i18n Tier 3" cc_memory/` 可以在不依赖会移动的行号
  的前提下定位它们。

只有 **Tier 2**——面向人类的文档——才是这套约定所做版本控制的对象。

### 9.2 文件命名

- `NAME.md` —— 规范的**英文**源（骨架）。
- `NAME.<lang>.md` —— 翻译兄弟文件。目前 `<lang>` 是 `zh`（简体中文）；现存的翻译
  是 `README.zh.md`、`docs/ARCHITECTURE.zh.md`（即本文件）和 `docs/CONTRACTS.zh.md`。
- 每一份翻译**必须**有对应的英文源。有 `NAME.zh.md` 而没有 `NAME.md` 就是
  **ORPHAN**（检查器失败）。不存在只有翻译的文档。

被跟踪集合（检查器看什么）：仓库根目录的 `README.md` 加上 `docs/*.md`，排除
`*.zh.md`（`tools/i18n_check.py:146-157`）。翻译是 `README.zh.md` 和
`docs/*.zh.md`，非递归（`tools/i18n_check.py:160-166`）。在 v2.4.3 的文档合并之后，
被跟踪的英文集合恰好是三个文件——`README.md`、`docs/ARCHITECTURE.md`、
`docs/CONTRACTS.md`——而不是合并前的五个。自 v2.5 起**这三份全都有译文**，因此一次
健康的运行报告 `3 in-sync`，也不再有任何 MISSING-TRANSLATION：对任何一份英文文档
的编辑，只要没有跟上 [§9.7](#97-英文源变更之后的更新) 的第 2-4 步，就会同时让这个
检查器和 `tests/smoke_test.py` 变红。

### 9.3 语言切换器

每份文档都带一行语言切换器，形式是 **H1 之上的引用块（blockquote）**。引用块自成一
个 markdown 块，因此绝不会与 `#` 标题冲突，并且会渲染成一行普通的链接行：

- 英文文档，第 1 行（在翻译中则位于标记之后——见 §9.4）：
  `> **English** · [简体中文](NAME.zh.md)`
- 翻译，第 2 行（第 1 行是标记）：
  `> [English](NAME.md) · **简体中文**`

当前语言以**粗体**显示；其余的是链接。

切换器只有在其目标会随同一次发布一起存在时才可以添加。`docs/I18N.md` 曾交付了一个
指向 `docs/I18N.zh.md` 的切换器，而后者从未被写出来——在 GitHub 上就是一条死链，
并且按设计 CI 也抓不到，因为 MISSING-TRANSLATION 不在 `FAIL_STATES` 里
（`tools/i18n_check.py:68`）。添加切换器是 §9.6 中 5 步流程的第 2 步；第 3-5 步必须
在同一次改动中跟上。

**本文件带切换器**（第 2 行，第 1 行是漂移标记），因为
`docs/ARCHITECTURE.zh.md` 就是与添加该切换器同一次发布（v2.4.3）写出来的 —— §9.6
的第 2-5 步是一起完成的，这正是上面 `I18N.md` 的前车之鉴所要求的做法。
`docs/CONTRACTS.md` 的切换器是在 v2.5 加上的，与 `docs/CONTRACTS.zh.md` 同一次改动；
在那之前它正确地没有切换器，因为没有目标的切换器就是这套约定要防的那条死链。现在三份
被跟踪的英文文档全都带切换器，也全都有译文。

### 9.4 漂移标记

每一份翻译的第 1 行都是一个机器可读的标记——一段 HTML 注释，因此渲染时不可见，且对
Claude Code 的 plugin/skill/agent 加载器是惰性的（它**绝不是** YAML front-matter，
那是加载器的地盘）：

```
<!-- i18n-source: README.md | sha256: dc06fb064d615ae5 | version: 2.3.2 | translated: 2026-07-11 -->
```

那一行展示的是标记的**格式**；它并不是关于任何当前文件摘要值的断言。真实标记要用
`--emit-marker` 生成（§9.6、§9.7）。语法见 `tools/i18n_check.py:50-60`
（`MARKER_FMT` / `MARKER_RE`，要求恰好 16 位小写十六进制数字和一个 ISO 日期）。

字段：

| 字段 | 含义 | 用于判定漂移？ |
|-------|---------|-----------------|
| `i18n-source` | 英文源的兄弟文件名 | 否（用于定位源） |
| `sha256` | **归一化后**英文源 sha256 的 16 位十六进制前缀 | **是——唯一的漂移信号** |
| `version` | 翻译时的 `cc_memory.__version__` | 否（仅供参考） |
| `translated` | 翻译刷新的日期 `YYYY-MM-DD` | 否（仅供参考） |

漂移**仅仅**由 `sha256` 决定。`version` 与 `translated` 只是信息性的，因此将来的版本
号提升绝不会把所有翻译一次性标成过期——只有英文*内容*的真实变化才会。

标记解析是**失败即关闭（fail-closed）**的（`tools/i18n_check.py:107-124`）：它容忍
BOM，但任何读取/解码错误，或者第一行不匹配语法，都会得到 `None`，调用方随即报告
NO-MARKER（一个 FAIL 状态），而不是无声地把该翻译当作有效。

#### 哈希归一化（跨平台的关键部分）

摘要是对英文源的**归一化**形式计算的，并且生成标记时与检查时跑的是完全相同的
归一化器。这正是让该哈希跨 Windows/Unix 稳定的原因：CRLF 与 LF、UTF-8 BOM、或者
行尾空白的抖动，都无法改变摘要。

配方（`tools/i18n_check.py:85-97` 中的 `normalize_markdown`）：

```
剥离 UTF-8 BOM  →  按 utf-8 解码  →  CRLF/CR → LF  →  每行 rstrip
                →  恰好一个结尾 "\n"  →  sha256(...).hexdigest()[:16]
```

标记对**整个英文文件（包括它的切换器行）**求哈希。所以当你添加或修改切换器时，摘要
也会变——这就是为什么切换器必须在生成标记*之前*定稿（见 §9.6）。

### 9.5 检查器

`tools/i18n_check.py` 是纯 stdlib 的，并且刻意位于 `cc_memory` 包之外——它是一个
dev/CI 工具，被有意排除在 `ui/installer.py` 的 `SUBPACKAGE_FILES`
（`installer.py:37-48`）、`build_exe.py` 以及 `cli/mem.py` 的
`_REQUIRED_PLUGIN_FILES`（`mem.py:77-100`）之外，因此打包后的插件不受它影响。

```bash
python tools/i18n_check.py            # 检查每一份被跟踪的文档
python tools/i18n_check.py --list     # 每个 英文/翻译 配对 + 记录哈希 vs 当前哈希
python tools/i18n_check.py --verbose  # 为每份文档打印一行详情，而不只是失败项
python tools/i18n_check.py --emit-marker README.md          # 打印一行新的标记
python tools/i18n_check.py --root /path/to/repo             # 覆盖仓库根目录
python tools/i18n_check.py --emit-marker README.md --version-label 2.4.2  # 覆盖标记的 version 字段
python tools/i18n_check.py --emit-marker README.md --date 2026-08-04      # 覆盖标记的 translated: 日期
```

`--root` 默认取包含该脚本的仓库，而不是当前工作目录
（`tools/i18n_check.py:305-307`），因此从任何目录运行检查器都会得到相同答案。

状态、标签与退出码：

| 状态 | 标签 | 含义 | 退出码 |
|-------|-------|---------|------|
| IN-SYNC | `[OK]` | 翻译的标记哈希 == 当前英文哈希 | 0 |
| MISSING-TRANSLATION | `[WARN]` | 英文文档还没有 `.zh.md` 兄弟文件 | 0 |
| STALE | `[STALE]` | 标记哈希 != 当前英文哈希（源已变更） | 非零 |
| ORPHAN | `[FAIL]` | 其英文源已消失/被改名的翻译 | 非零 |
| NO-MARKER | `[FAIL]` | 首行没有合法标记的翻译 | 非零 |

只要存在**任何** STALE / ORPHAN / NO-MARKER，检查器就以非零退出
（`FAIL_STATES`，`tools/i18n_check.py:68`；`main` 在失败时返回 `1`，`:351-353`）。
MISSING-TRANSLATION 是软警告——只是还没产出翻译而已——绝不会让构建失败。
`tests/smoke_test.py:878-895` 导入该检查器，断言被跟踪文档中没有
STALE/ORPHAN/NO-MARKER，并另外断言 `README.zh.md` 的标记摘要等于实时的
`hash_source(README.md)`，因此一份过期的翻译会让冒烟测试变红。`--emit-marker` 是
一个独立模式：它打印一行标记并以 0 退出，或者在指定的英文源不存在时以 **2** 退出
（`tools/i18n_check.py:339-341`）。

### 9.6 新增一份翻译

顺序很重要：标记对整个英文文件求哈希，所以英文切换器必须在生成标记*之前*就位。

1. 定稿英文文档的**内容**。
2. 在英文文档的第 1 行前置英文切换器：
   `> **English** · [简体中文](NAME.zh.md)`。
3. 生成标记：`python tools/i18n_check.py --emit-marker docs/NAME.md`，复制打印出的
   那一行。
4. 创建 `docs/NAME.zh.md`：
   - 第 1 行 = 生成出的标记，
   - 第 2 行 = 翻译切换器 `> [English](NAME.md) · **简体中文**`，
   - 空行，然后是完整译文。
5. 验证：`python tools/i18n_check.py` 对该配对显示 `[OK] IN-SYNC` 并以 0 退出。

不要在第 2 步之后就停下。只有切换器而没有第 3-5 步，就是一条检查器不会标出的死链
（§9.3）。

### 9.7 英文源变更之后的更新

当你编辑一份已经有翻译的英文文档时，检查器会一直报 `[STALE]`，直到你刷新译文：

1. 编辑英文文档。
2. 更新译文正文以匹配新的英文内容。
3. 重新生成标记：`python tools/i18n_check.py --emit-marker docs/NAME.md`。
4. 用新生成的标记替换 `docs/NAME.zh.md` 的**第 1 行**。
5. 验证：`python tools/i18n_check.py` → `[OK]`，退出码 0。

如果你是删除或重命名了一份英文文档，那就同时删除或重命名它的译文，否则检查器会报
`[FAIL] ORPHAN`。

### 9.8 范围与排除项

**在范围内（按需翻译）：** `README.md` 与 `docs/*.md`。

**明确排除在翻译之外：**

- `CLAUDE.md`、`commands/`、`skills/`、`agents/` —— 面向 Claude，且它们的 YAML
  front-matter 归加载器所有；添加未知键有被加载器拒绝的风险。
- `CHANGELOG.md` —— 只追加的发布流水；不是一份你会从头读到尾的文档。
- `memory/**` —— 生成产物。
- 运行时 UI 字符串（CLI / dashboard）—— 面向 LLM（Tier 1），且没有集中的输出接缝；
  刻意推迟，不属于本约定。

### 9.9 验证清单

- `python tools/i18n_check.py` → 被跟踪的英文文档为 `[OK]` 或 `[WARN]`，退出码 0。
- `python tools/i18n_check.py --list` → 每个配对的记录哈希 `==` 当前哈希。
- `python tests/smoke_test.py` → 打印 `[OK] i18n: ...` 行，并以
  `===== ALL SMOKE TESTS PASSED =====` 结尾。
- 跨平台哈希稳定性：`normalize_markdown` 在 LF 与 CRLF 下产出相同摘要（往返转换行
  结束符再重跑 `--list`）。
- 反例测试：对一份有翻译的英文文档做一次**真实的内容变更**——例如在行中间插入一个
  词——→ 检查器报告 `[STALE]` 且冒烟测试失败；刷新标记 → 变绿。本条在合并前的措辞
  （“在任意一行末尾追加一个空格”）是错的，在此纠正：`normalize_markdown` 会对每一行
  做 rstrip（`tools/i18n_check.py:96`），所以行尾空白会被归一化掉，无法改变摘要。
- 加载器安全性：每份文档的首字节都是引用块（`>`）、HTML 注释（`<!--`）或 `#`——
  绝不是 `---`。任何面向 Claude 的文件都不会被修改。

---

## 参见

- [docs/CONTRACTS.md](CONTRACTS.md) —— 三条硬契约：反补丁写入、强制交接、实时计划
  锚点
- [CHANGELOG.md](../CHANGELOG.md) —— 版本历史
- [CLAUDE.md](../CLAUDE.md) —— 给 Claude Code 的项目指令
- `tests/smoke_test.py` —— 规范的端到端检查；在改动 `memory_writer`、`progress`、
  `plan` 或 `session_start._refresh_progress_row` 之后都要运行它

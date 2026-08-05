<!-- i18n-source: ARCHITECTURE.md | sha256: 0e58c232efadf803 | version: 2.4.3 | translated: 2026-08-04 -->
> [English](ARCHITECTURE.md) · **简体中文**

# cc-memory — 架构（v2.4.3）

cc-memory 是一个 Claude Code 插件，为 Claude 提供**跨压缩、跨会话的持久化结构化
记忆**。本文档是总览：这个插件用来做什么、仓库如何布局、哪些钩子在何时触发、数据库
里存了什么、数据如何流动、LLM 调用如何认证、插件往项目里写了什么、安装后的代码物理上
位于何处，以及那套让文档保持翻译同步、不出现无声漂移的约定。

三条硬契约——反补丁写入、强制交接、实时计划锚点——规定在
[docs/CONTRACTS.md](CONTRACTS.md) 中。本文件描述机器；那份文件描述机器必须遵守的
规则。

## 目录

- [1. 总览 / 它解决什么问题](#1-overview--what-it-solves)
- [2. 仓库布局](#2-repository-layout)
- [3. 钩子（Hooks）](#3-hooks)
- [4. 数据库 schema](#4-database-schema)
- [5. 数据流](#5-data-flow)
- [6. LLM 后端与认证](#6-llm-backends-and-auth)
- [7. 按项目的状态（memory/）](#7-per-project-state-memory)
- [8. 安装布局](#8-install-layouts)
- [9. 文档语言约定（i18n）](#9-documentation-language-convention-i18n)

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
   钩子的原因（见 [§3](#3-hooks)）。

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
[§9.1](#91-the-three-tier-language-model)。

---

## 2. 仓库布局

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← 插件清单（v2.4.3）
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
│   ├── ARCHITECTURE.md          ← 本文件（总览 + i18n 约定）
│   └── CONTRACTS.md             ← 反补丁 + 强制交接 + 实时计划
├── cc_memory/                   ← Python 包（已拆分子包）
│   ├── __init__.py              (__version__ = "2.4.3", cc_memory/__init__.py:64)
│   ├── config.json
│   ├── core/                    ← 领域层：db, extractor, consolidate, idle,
│   │                              progress, plan, privacy, modes, auth,
│   │                              logger, encoding_setup
│   ├── hooks/                   ← 钩子入口（6 个模块）
│   ├── llm/                     ← ccl_backend（Haiku/Ollama）+ memory_writer
│   ├── cli/                     ← mem.py, plan.py
│   ├── mcp/                     ← server.py（MCP stdio）
│   └── ui/                      ← installer, dashboard, web_viewer
├── tests/                       ← smoke_test.py（规范的端到端检查）+
│                                  test_plan_carryover.py
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
`agents/plan-refiner.md` 由 `cc_memory/hooks/stop.py:279-284` 提示触发，
`agents/plan-guardian.md` 由 `stop.py:286-296` 触发；`tools/i18n_check.py` 正是
[§9](#9-documentation-language-convention-i18n) 所规定的对象，也是
`tests/smoke_test.py:878-895` 作为漂移门禁导入的模块。

版本字符串在六个规范位置声明，且必须保持完全同步：
`.claude-plugin/plugin.json:3`、`pyproject.toml:3`、`cc_memory/__init__.py:64`、
`cc_memory/config.json:2`，外加 CLI 横幅（`cli/mem.py:276, 984`）与 MCP 服务器横幅
（`mcp/server.py:274, 316`）。（在 v2.4.2 之前，它们曾漂移成三个不同的值——
2.4.1 / 2.3.4 / 2.3.3；见 `CHANGELOG.md` 的 2.4.2 → Changed。）

### 合并前的那几份文档去哪了

v2.4.3 把原本 5 份的 `docs/` 目录合并为 2 份。全部 79 处仓库内引用在同一次改动中
一并改指，因此树内已经没有任何链接会解析到被删掉的文件名。下表是重定向表，供那些
从已发布的 CHANGELOG 条目、GitHub 永久链接或以往会话笔记里追过来的读者使用：

| 合并前的文档 | 现在的位置 |
|---|---|
| `docs/MEMORY_RULES.md` | [CONTRACTS.md § Anti-patch contract](CONTRACTS.md#anti-patch-contract) |
| `docs/HANDOFF_PROTOCOL.md` | [CONTRACTS.md § Handoff contract](CONTRACTS.md#handoff-contract) |
| `docs/PLAN_PROTOCOL.md` | [CONTRACTS.md § Plan contract](CONTRACTS.md#plan-contract) |
| `docs/I18N.md` | [本文件 §9](#9-documentation-language-convention-i18n) |
| `docs/ARCHITECTURE.md §3`（schema） | [本文件 §4](#4-database-schema)——合并前的文件没有编号章节，所以文件名没变但编号变了 |

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
| `PostToolUse` | [`cc_memory/hooks/post_tool_use.py`](../cc_memory/hooks/post_tool_use.py) | 8s | 为每一次被观察的工具调用向 `observations` 插入一行（模式白名单 / 跳过列表——`core.modes.should_observe`）；外加实时计划捕获：`ExitPlanMode` → `plan_active.raw`，`TodoWrite` → 机械式步骤同步，`Edit`/`Write`/`MultiEdit`/`NotebookEdit` → 漂移计数器 +1，敏感 Bash 调用 → +20。不调用 LLM。 |
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

`hooks/hooks.json` 是市场 / 开发用的声明。`cc_memory/ui/installer.py` 的
`HOOK_SCRIPTS`（`installer.py:55-61`）是独立安装的声明，它以一个**基础**超时表达，
在 Windows 上乘以 1.5（`installer.py:101`）：PreCompact `80 × 1.5 = 120`、
SessionStart `10 × 1.5 = 15`、Stop 22、PostToolUse 8、UserPromptSubmit 8。异步支路
是单独追加的，采用**固定** 300 秒（`apply_mult=False`，`installer.py:122-123`），
因为它是一个后台截止期限，而不是阻塞 UI 的预算。

### 已知缺口：observation 闸门遮蔽了计划分支

当 `core.modes.should_observe(mode, tool_name)` 为假时，`post_tool_use.py:86-87`
会提前返回，而实时计划集成块位于该闸门**之后**的
`post_tool_use.py:113-141`。`should_observe`（`core/modes.py:65-71`）先做跳过列表
检查，再对 `observe_tools` 做白名单检查，而在全部三种出厂模式中
（`modes.py:9-56`）：

- `TodoWrite` 位于每种模式的 `skip_tools` 里（`modes.py:18, 30, 45`）→
  `should_observe` 为 False；
- `ExitPlanMode` 不在任何模式的 `observe_tools` 里 → `should_observe` 为 False。

已通过对三种模式直接运行 `core.modes.should_observe` 验证：两个工具在每种模式下都
返回 `False`。因此按当前写法，`ExitPlanMode` 捕获（`post_tool_use.py:117-122`）与
`TodoWrite` 步骤同步（`post_tool_use.py:124-129`）经由该钩子是不可达的；如今实时
计划是由 `cli/mem.py:772`（`/cc-mem plan-set`）喂入的，而
`tests/smoke_test.py:419, 466` 是直接调用 `core.plan.capture_exit_plan_mode` /
`apply_todowrite_sync`，而不是经由钩子，所以测试套件抓不到这个问题。编辑计数器
（`post_tool_use.py:131-133`）在 `code` 与 `writing` 模式下**确实**会触发（这两种
模式会观察 `Edit`/`Write`/`MultiEdit`），但在 `research` 模式下不会，因为那里这些
工具被跳过（`modes.py:31`）；敏感调用的加分（`post_tool_use.py:139-141`）需要
`Bash`，它在 `code` 和 `research` 中被观察，但在 `writing` 中被跳过
（`modes.py:46`）。这是一处记录在此的代码缺陷，而不是文档错误——它不在审计的行动
清单里，作为新发现在此报告。

另外注意，`config.json` 的 `observation.skip_tools` 键
（`cc_memory/config.json:35-40`）是**死键**：没有任何 Python 读取它（`skip_tools`
仅出现在 `core/modes.py:17, 29, 44, 67`）。真正生效的过滤器是 `core/modes.py` 中
按模式的 `skip_tools` + `observe_tools`。

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
| **`progress`** | v2.1 新增——每项目一行。`memory/PROGRESS.md` 的唯一真相来源（`db.py:177`）。 |
| **`plan_active`** | v2.2 新增——每项目一行。`memory/PLAN.md` 的唯一真相来源（`db.py:199`）。 |
| `_migrations` | 记录已应用的迁移（`db.py:278`） |

共十一张表，与 `CLAUDE.md` 的 §“Database schema (11 tables)” 一致。

此外还有 `memories_fts`——一个建立在 `memories` 之上的 FTS5 虚拟表，由三个触发器保持
同步（`core/db.py:317-341`，迁移 `v2_fts5`）。它只在本地 SQLite 构建带 FTS5 时才会
创建；否则 `db.search_fts` 回退到 `LIKE`（`core/db.py:306-313`、`:1106`）。FTS5 在
`.claude-plugin/plugin.json:4` 与 `:12` 中被宣传，`/cc-mem status` 会报告当前实际走
哪条路径（`cli/mem.py:307-308`）。

`memories` 上的 `supersedes_id` 列（迁移 `v3_supersedes`，`db.py:169`）把反补丁的
取代链显式化：当 `upsert_smart` 判定一条新记忆取代了一条旧记忆时，新行会回链到旧行
的 ID（旧行被归档）。通过 `db.get_supersede_chain(memory_id)`（`db.py:513`）走一遍
链条，就能看到完整的更新历史。`content_hash`（迁移 `v2_content_hash`，`db.py:124`）
是归一化内容的 `sha256[:16]`，用于廉价的精确重复检查（`db.compute_content_hash` 在
`db.py:722`，`db.find_by_hash` 在 `db.py:735`）。

迁移按 `_MIGRATIONS` 列表（`db.py:119`）的顺序应用，并记录在 `_migrations` 中。目前
已交付的层级：**v1**（topic 列 + 索引）、**v2**（content_hash、observations、
session_summaries、项目模式、FTS5、哈希回填）、**v3**（反补丁 + 强制交接：
`supersedes_id`、`progress`）、**v4**（`plan_active`）、**v5**（会话标注：
`progress.current_session_id`、`progress.session_started_at`——这样多会话工作流就能
从 PROGRESS.md 判断自己读到的是不是自己写的内容，`db.py:219-222`）、**v6**
（引用感知的老化：`memories.last_referenced_at`，在注入时设置，因此有效年龄是
`now - COALESCE(last_referenced_at, created_at)`，被引用过的事实保持“年轻”，
`db.py:226-237`）。

`progress` 行面向用户的字段是 `current_request`、`status_done`、
`status_in_flight`、`status_blocked`、`open_todos`、`plan`、`critical_context`、
`files_touched`、`transcript_ptr`、`updated_at`、`trigger_type`（共 11 个，外加 v5
的两个会话标注列）。`plan_active` 行持有 `raw`、`structured`、`active_step`、
`edits_since_last_guardian`、`turns_since_last_guardian`、`last_guardian_at`、
`last_refined_at`、`needs_refine`、`created_at`、`updated_at`（`db.py:199-211`）。

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
`MIN_CONTENT_LEN = 10`、`MAX_CANDIDATES_TO_SCAN = 50`（`memory_writer.py:44-47`），
并以信息性方式镜像在 `config.json` 的 `writer` 块中。完整契约见
[docs/CONTRACTS.md](CONTRACTS.md#anti-patch-contract)。

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

### 实时计划流程（v2.2）

`ExitPlanMode` 的输出（或用户提供的 `/cc-mem plan-set` 文本）落入
`plan_active.raw` 并置 `needs_refine = 1`；`plan-refiner` 子代理把它规范化为 JSON，
经 `/cc-mem plan-set --from-refiner` 写回；`TodoWrite` 事件按三元组 Jaccard 匹配
机械地同步步骤状态（不调用 LLM）；`Edit`/`Write`/`MultiEdit`/`NotebookEdit` 会累加
`edits_since_last_guardian`，而敏感的 Bash 调用（`git push`、`rm -rf`、
`DROP TABLE`、`npm publish`、`kubectl apply`、`terraform apply`……见
`core/plan.py:596-613`）一次加 20。一旦 `turns_since_last_guardian >= 8` 或
`edits_since_last_guardian >= 12`，Stop 钩子就发出 guardian 建议
（`core/plan.py:569-585`）。钩子自己绝不派生子代理——它们只提示。完整规格见
[docs/CONTRACTS.md](CONTRACTS.md#plan-contract)。这些分支中哪些当前被 observation
闸门遮蔽，见上文的
[已知缺口](#known-gap-the-observation-gate-shadows-the-plan-branches)。

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

`call_llm` 的 `fallback_timeout` 为 Ollama 支路设定上界。当它为 `None` 时默认取
`min(timeout*3, 120)`；受时间预算约束的调用方**必须**传入一个显式值，这样最坏情况
的在途墙钟时间才是已知的：每个 Anthropic 候选至多 `timeout`（上限 2 个）+ 启用
Ollama 时的 `fallback_timeout`。正是这个上界让整理的 `BudgetGate` 能够保证在截止
期限之前完成——见 `core.consolidate._worst_call_cost`（`ccl_backend.py:127-134`）。

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

**独立安装器（扁平）**——`_copy_subpackages(TARGET_DIR)`（`installer.py:74-92`）把
每一个 `SUBPACKAGE_FILES` 键（`installer.py:37-48`）直接写到 `TARGET_DIR` 下，
**没有 `cc_memory/` 这一段**，并且 `_make_hooks_config` 把命令构造成
`python "<TARGET_DIR>/hooks/<name>.py"`（`installer.py:104-115`）：

```
~/.claude/hooks/cc-memory/           ← ui/installer.py:33 TARGET_DIR
├── __init__.py
├── config.json
├── core/    auth.py consolidate.py db.py encoding_setup.py extractor.py
│            idle.py logger.py modes.py plan.py privacy.py progress.py
├── hooks/   consolidate_async.py post_tool_use.py pre_compact.py
│            session_start.py stop.py user_prompt.py
├── llm/     ccl_backend.py memory_writer.py
├── cli/     mem.py plan.py
├── mcp/     server.py
├── ui/      dashboard.py installer.py web_viewer.py
└── logs/    ← core.logger 的输出目标
```

注意扁平树里*缺少*什么：没有 `hooks/hooks.json`（`SUBPACKAGE_FILES` 不包含它——
注册改为写入 `~/.claude/settings.json`），也没有 `skills/`、`agents/`、
`commands/`、`docs/`、`tests/` 或 `tools/`。尤其是 `tools/i18n_check.py`，它被刻意
排除在 `SUBPACKAGE_FILES` 和 `build_exe.py` 之外；打包后的插件不受它影响
（见 [§9.5](#95-the-checker)）。

检测同时接受两种形态：`mem.py:181` 测试
`(legacy / "cc_memory").exists() or (legacy / "core" / "db.py").exists()`，注释里
记录了原因——只探测 `cc_memory/` 会让这个安装器产出的每一个安装对
`/cc-mem status` 都不可见（`mem.py:176-179`）。

### 两处已核实的扁平布局不一致

之所以在这里说明，是因为任何探测独立布局的使用者都会撞上它们：

1. **`/cc-mem status` 会把一个健康的扁平安装标成损坏。**
   `_inspect_layout` 以布局根目录为基准解析 `_REQUIRED_PLUGIN_FILES`
   （`mem.py:195`），而该列表中的每一项都带 `cc_memory/…` 前缀
   （`mem.py:77-100`），外加 `hooks/hooks.json`。扁平安装没有这些路径中的任何一条，
   于是全部 22 项都报缺失，即便钩子工作正常，该布局也会打印 `[FAIL]`。检测（接受
   扁平）与检查（假定嵌套）互相矛盾。
2. **安装器自己的安装后说明打印了一条它从不创建的路径。**
   `installer.py:479` 与 `:481` 打印
   `TARGET_DIR / 'cc_memory' / 'cli' / 'mem.py'` 和
   `TARGET_DIR / 'cc_memory' / 'ui' / 'dashboard.py'`；而文件实际落在
   `TARGET_DIR/cli/mem.py` 与 `TARGET_DIR/ui/dashboard.py`。同一文件里的 GUI 路径是
   对的（`installer.py:396` 的 `TARGET_DIR / "ui" / "dashboard.py"`），已安装探测也
   是对的（`installer.py:311` 的 `TARGET_DIR / "core" / "db.py"`）。模块文档字符串
   中“hooks/settings 路径指向 `cc_memory/hooks/<name>.py`（不是扁平）”的说法
   （`installer.py:13`）相对于 `_make_hooks_config` 同样已经过期。

这两处都不是被纠正的文档错误——它们都是在核实本章时发现的代码缺陷，记录在此以免
文档重复它们。

### 解释器要求

`hooks/hooks.json` 调用的是 `python3`。在 Linux/macOS 上这就是标准的 Python 3
可执行文件。在 Windows 上，python.org 的安装器会提供 `python.exe` 加上 `py.exe`
启动器，但默认**不**提供 `python3.exe`——请在安装插件之前勾选“Add Python to
PATH” + “py launcher”，或者把 `python3` 别名到 `python`。否则钩子会无声失败
（会记录到 `~/.claude/hooks/cc-memory/logs/`，但 Claude Code 对命令缺失不显示任何
错误 UI）。独立安装器通过探测规避了这一点：`_detect_python_cmd` 只有在
`shutil.which("python3")` 找得到时才用 `python3`，否则用 `python`
（`installer.py:95-96`）。

---

## 9. 文档语言约定（i18n）

本章定义 cc-memory 如何**在不产生无声漂移的前提下**用不止一种语言维护面向人类的
文档。英文是规范骨架；其他语言是受漂移跟踪的兄弟文件，通过其英文源的哈希绑定。

它就是这套约定的英文真相来源。执行它的检查器是 `tools/i18n_check.py`（纯 stdlib，
仅 dev/CI——不随插件分发）。

> 合并说明：本章在 v2.4.1 之前是 `docs/I18N.md`。`core/extractor.py:34, 72`、
> `hooks/session_start.py:271`、`hooks/user_prompt.py:125` 中的 Tier-3 守卫注释以及
> `cc_memory/__init__.py:37` 中的指针仍引用 `docs/I18N.md §1`；在那些字符串被刷新
> 之前，请把它读作下面的 §9.1。

### 9.1 三层语言模型

整个体系建立在把人们说“语言”时所指的三件不同的事分开之上，每一层有各自的规则：

| 层 | 是什么 | 规则 | 位于何处 |
|------|------|------|----------------|
| 1 — 骨架 | 英文规范文档 + 所有面向 LLM 的字符串 | 英文具有权威性；每一份翻译都需要一个英文源 | `README.md`、`docs/*.md`；钩子 / CLI 指令字符串 |
| 2 — 翻译 | 供人阅读的其他语言文档 | `NAME.<lang>.md` 兄弟文件，受漂移跟踪，按需产出 | `README.zh.md`（目前唯一存在的翻译；`docs/*.zh.md` 按需） |
| 3 — 内容 | 用户存储的记忆内容 | 任意语言；双语检测是有意为之 | `extractor.py`、`user_prompt.py`、`session_start.py` |

- **Tier 1 刻意保持英文。** 钩子 stdout 和 `Claude:` 开头的 CLI 指令输出是给模型读
  的，不是给终端用户读的——它们是为指令遵循而调过的，翻译它们会降低行为质量。
  “英文规范”意味着这些内容永不翻译。
- **Tier 3 在设计上与语言无关。** 抽取器模式与恢复信号集合同时匹配英文和中文，存储
  的记忆也可以是任意语言。见
  [§1 “设计上就是双语的”](#bilingual-by-design--memory-content-is-language-agnostic)。
  **不要**把那些检测器削减为只识别英文——那会破坏设计的第 3 条
  （“内容可以是任意语言”）。具体的受守卫位置是 `core/extractor.py:35-69`
  （`_PATTERNS`）、`core/extractor.py:73-77`（`_IMPORTANCE_BOOST`）、
  `hooks/session_start.py:269-273`（RESUME PROTOCOL 的 token）和
  `hooks/user_prompt.py:127-130`（`resume_signals`）；后两者必须彼此保持同步，因为
  强制提醒承诺的正是 `user_prompt` 判定为 `resume_request` 的那个行为。

只有 **Tier 2**——面向人类的文档——才是这套约定所做版本控制的对象。

### 9.2 文件命名

- `NAME.md` —— 规范的**英文**源（骨架）。
- `NAME.<lang>.md` —— 翻译兄弟文件。目前 `<lang>` 是 `zh`（简体中文）；当前唯一
  存在的翻译是 `README.zh.md`。
- 每一份翻译**必须**有对应的英文源。有 `NAME.zh.md` 而没有 `NAME.md` 就是
  **ORPHAN**（检查器失败）。不存在只有翻译的文档。

被跟踪集合（检查器看什么）：仓库根目录的 `README.md` 加上 `docs/*.md`，排除
`*.zh.md`（`tools/i18n_check.py:146-157`）。翻译是 `README.zh.md` 和
`docs/*.zh.md`，非递归（`tools/i18n_check.py:160-166`）。在 v2.4.2 的文档合并之后，
被跟踪的英文集合恰好是三个文件——`README.md`、`docs/ARCHITECTURE.md`、
`docs/CONTRACTS.md`——而不是合并前的五个。`docs/ARCHITECTURE.md` 与
`docs/CONTRACTS.md` 目前都没有 `.zh.md` 兄弟文件，即两者都是 MISSING-TRANSLATION，
这是一个永远不会让构建失败的软警告。

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
在同一次改动中跟上。**因此本文件在交付时不带切换器**：目前尚不存在
`docs/ARCHITECTURE.zh.md`，所以在这里的第 1 行放一个切换器只会复现这次合并正要修掉
的那条死链。等翻译真正写出来时，把它作为 §9.6 的第 2 步加上。

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

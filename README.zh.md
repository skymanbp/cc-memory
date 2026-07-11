<!-- i18n-source: README.md | sha256: 3478fc4f1e83526b | version: 2.3.3 | translated: 2026-07-11 -->
> [English](README.md) · **简体中文**

# cc-memory

**Claude Code 持久化记忆插件（v2.3.3）**——反补丁式的写入即归并（reconcile-on-write）、
LLM 判定的语义去重、强制 PROGRESS.md 交接、带 plan-refiner / plan-guardian 子代理的
实时 PLAN.md 锚点、注入可观测性、FTS5 搜索，以及以 Haiku + 本地 Ollama 兜底的
AI 判定式抽取。

## 它解决什么问题

当上下文窗口写满时，Claude Code 会压缩（compact）对话，从而丢失信息：决策、结果、
待办、项目知识都会消失。正常结束的对话（关闭终端）同样会丢失上下文。

cc-memory 在每一个对话边界捕获结构化记忆，并且**强制下一次会话在开始工作之前先阅读
一份交接文档**。

## v2.3.3 有什么新变化

- **文档多语言版本控制。** 英文是规范骨架；中文文档是受漂移跟踪的 `*.zh.md` 兄弟文件
  （从 [README.zh.md](README.zh.md) 开始），每一份都通过首行标记绑定到其英文源的
  归一化 sha256。一个纯标准库的检查器（[tools/i18n_check.py](tools/i18n_check.py)）
  加上 [tests/smoke_test.py](tests/smoke_test.py) 门禁，会在英文文档改动而对应译文未
  刷新的那一刻立即变红。记忆*内容*保持与语言无关——只有文档被跟踪。参见
  [docs/I18N.md](docs/I18N.md)。

这是一次文档 + 版本元数据的发布——运行时行为没有任何改变。

## v2.3 有什么新变化

- **LLM 判定的语义去重。** 反补丁写入器基于字符三元组（char-trigram）的相似度只能
  捕获近乎逐字的复述，因此同一条事实每次会话换一种措辞就会不断堆叠（数据库无限膨胀）。
  `consolidate.semantic_dedup` 会按词级 Jaccard 提名同类别的小候选组，由 Haiku 确认
  是否为同一事实，然后把幸存者刷新为归并后的规范条目，其余条目被归档（`is_active=0`）
  并通过 `supersedes_id` 建立前向链接。
- **过时检测 + 引用感知的陈旧兜底网。** `detect_obsolete_llm` 以时间守卫（取代者必须
  更新）加“反事件”提示，指名 `{陈旧, 现行}` 配对；`decay_and_archive` 只归档那些同时
  满足“非常旧、低重要度、且从未被注入”的条目。所有归档都可恢复（`is_active=0`，绝不
  `DELETE`）。
- **注入可观测性。** SessionStart 会写入 `memory/.last_inject.json`，精确记录哪些记忆/
  主题被注入，并打印一行回执；`/cc-mem inject-show` 输出实况真相，`/cc-mem inject-usage`
  报告 Claude 是否真的读了 PROGRESS.md / MEMORY.md。
- **`/cc-mem encoding-check [--apply]`**——只读扫描文本表中的 U+FFFD 乱码（保留有效
  的中日韩字符）。

### v2.3.1 / v2.3.2——彻底修复 “Hook cancelled”

偶发的 `Compacted PreCompact [...] failed: Hook cancelled` 已经消失。v2.3.1 把
PreCompact 的超时从 45 秒提高到 120 秒，但在大型数据库上这只是把球门往后挪了。
**v2.3.2 移除了这一失败模式**：`PreCompact` 现在声明两个命令钩子——一个快速的
**同步**支路（`hooks/pre_compact.py`，抽取 + PROGRESS.md，约 1-5 秒），以及一个后台
的 **`async`** 支路（`hooks/consolidate_async.py`，超时 300 秒），后者把每 N 次会话
一次的整理搬离阻塞式的压缩路径。带诚实最坏情况成本模型的预算门（budget gate）保证
异步工作者会在其超时之前完成，因此绝不会被中途杀掉。参见 [CHANGELOG.md](CHANGELOG.md)。

## v2.2 有什么新变化

- **实时计划锚点（`memory/PLAN.md`）。** 把 `ExitPlanMode` 的输出（或用户提供的
  原始计划）捕获为一份结构化、按步骤跟踪、可跨会话存续的文档。`TodoWrite` 会机械地
  同步各步骤状态；敏感的 Bash 调用（`git push`、部署等）会标记漂移。参见
  [docs/PLAN_PROTOCOL.md](docs/PLAN_PROTOCOL.md)。
- **插件自带子代理。** `plan-refiner` 把原始计划规范化为 JSON；`plan-guardian` 在
  漂移计数触发时检查一致性。定义位于 `agents/`，安装后自动被发现。
- **`/cc-mem dashboard`** 子命令：无需知道插件安装路径即可启动 Tkinter GUI。

## v2.1 有什么新变化

- **反补丁写入。** 每一次保存都经由 `llm.memory_writer.upsert_smart`，它会按三元组
  Jaccard 相似度选择 MERGE（就地覆盖相似记忆）、SUPERSEDE（归档旧记忆并用
  `supersedes_id` 链接新记忆）或 INSERT。不再有堆叠的重复项。参见
  [docs/MEMORY_RULES.md](docs/MEMORY_RULES.md)。
- **经由 PROGRESS.md 的强制交接。** `memory/PROGRESS.md` 是会话交接的唯一真相来源，
  始终从一条 SQL 记录整篇重写，绝不追加。SessionStart 会发出一个 `<system-reminder>`
  块，要求下一个 Claude 在回应之前先读它。参见
  [docs/HANDOFF_PROTOCOL.md](docs/HANDOFF_PROTOCOL.md)。
- **自动保鲜的 MEMORY.md。** 每次写入后重新生成——不再有陈旧 50 天的索引文件。
- **空闲整理。** Stop 钩子每 5 个回合运行一次轻量清理（不调用 LLM）。
- **清爽的子包布局。** `cc_memory/{core,hooks,llm,cli,mcp,ui}/`。
- **一个安装器、一处技能位置、一个版本号。** 移除了 `.claude/skills/` 副本、移除了
  `save-memories` 的第三份拷贝、移除了双安装器。

## 安装

### 通过市场安装（发布后推荐）

```bash
claude /plugin marketplace add skymanbp/cc-memory
claude /plugin install cc-memory
```

### 从本仓库作为本地市场安装

```bash
claude /plugin marketplace add /path/to/cc-memory
claude /plugin install cc-memory
```

### 独立可执行文件（Windows）

1. 从 [Releases](https://github.com/skymanbp/cc-memory/releases) 下载
   `cc-memory-installer.exe`
2. 双击 → Install Plugin → Configure Hooks → 完成。

### 从源码安装

```bash
git clone https://github.com/skymanbp/cc-memory.git
python cc-memory/cc_memory/ui/installer.py        # GUI
# 或
python cc-memory/cc_memory/ui/installer.py --cli  # 命令行
```

安装器会：
1. 把子包目录树复制到 `~/.claude/hooks/cc-memory/`。
2. 向 `~/.claude/settings.json` 添加钩子条目（横跨 5 个事件的 6 条命令——
   `PreCompact` 声明一个同步支路 + 一个 `async` 支路）。
3. 自动检测并升级任何 v2.0 扁平布局的旧安装。

按项目的初始化是**自动**的——第一条用户消息会创建 `<project>/memory/` 和 SQLite 数据库。

## 架构速览

```
钩子（注册在 ~/.claude/settings.json）：

  UserPromptSubmit ─► 回合计数 + 用首条提示为 PROGRESS.md 播种
                      首次接触时自动初始化 memory/

  PostToolUse     ─► 每次工具调用插入一条 observation 行（不调用 LLM）

  Stop            ─► Haiku 观察者从本回合抽取记忆
                     patch_progress(files_touched=...)
                     每 5 个回合做一次空闲整理

  PreCompact      ─► 触发两个钩子：
                     • 同步 (pre_compact.py, 120s)：Haiku 从完整 transcript 抽取记忆
                       → memory_writer.upsert_smart → 整篇重写 memory/PROGRESS.md
                       → 归档 → 重新生成 MEMORY.md
                     • 异步 (consolidate_async.py, 300s，脱离阻塞路径)：
                       每 N 次会话一次、受时间预算约束的 LLM 整理

  SessionStart    ─► 注入上下文（主题 + 关键项 + 时间线 + PROGRESS 预览）
                     记录 memory/.last_inject.json
                     发出强制的 <system-reminder>：“先读 PROGRESS.md”
                     追溯保存此前未保存的 JSONL
```

按项目的状态位于 `<project>/memory/`：

```
memory/
├── memory.db                SQLite WAL，schema 见 core/db.py
├── MEMORY.md                自动生成的索引，每次写入后刷新
├── PROGRESS.md              每次 Stop+PreCompact 从 `progress` 行整篇重写
├── PLAN.md                  从 `plan_active` 行整篇重写（实时计划锚点）
├── .last_save.json          上一次 PreCompact 的状态
├── .last_inject.json        SessionStart 注入了什么（可观测性）
├── .last_consolidation.json 异步整理支路的间隔标记
├── .gitignore               排除 DB + 会话
├── sessions/YYYY/MM/        按会话归档
└── topics/                  预留给未来的按主题导出
```

## 记忆模型

| 类别 | 抽取什么 | 默认重要度 |
|----------|--------------------|--------------------|
| `decision` | 明确的选择、设计变更 | 3 |
| `result`   | 可测量的结果（数字 + 单位） | 3 |
| `config`   | 超参数、环境变量、常量 | 2 |
| `bug`      | 已定位并修复的问题、“绝不要做 X” | 4 |
| `task`     | 待办/被阻塞的工作项 | 2 |
| `arch`     | 模块/管线结构、数据流 | 3 |
| `note`     | 噪声之上的其他一切 | 1 |

重要度等级：`1`=噪声，`2`=低，`3`=普通，`4`=重要，`5`=关键（绝不遗忘）。

记忆**内容**是语言无关的——抽取器和恢复信号检测器在设计上同时识别英文和中文，
存储的记忆可以是任意语言。只有项目自身的文档遵循“英文骨架 + 翻译”的约定。参见
[docs/I18N.md](docs/I18N.md)。

## 命令行（CLI）

**在 Claude Code 内部**（推荐，与路径无关）：

```
/cc-mem status                                    # 完整健康检查
/cc-mem stats                                     # 记忆 + 取代链计数
/cc-mem list decisions                            # 按类别列出近期记忆
/cc-mem search "auth flow"                        # FTS5 搜索
/cc-mem topics                                    # 主题摘要
/cc-mem progress                                  # 重新生成 memory/PROGRESS.md 并打印
/cc-mem supersedes 42                             # 走一遍记忆 #42 的取代链
/cc-mem consolidate                               # 完整的 LLM 支撑整理
/cc-mem cleanup                                   # 轻量、不调用 LLM 的清理
/cc-mem add decision "Chose X" --importance 4     # 反补丁式 upsert
/cc-mem inject-show                               # 上一次 SessionStart 注入了什么（实况真相）
/cc-mem inject-usage                              # Claude 是否读了 PROGRESS.md / MEMORY.md
/cc-mem encoding-check                            # 扫描文本表中的 U+FFFD 乱码
/cc-mem dashboard                                 # 启动 Tkinter GUI
/cc-mem serve                                     # 启动基于浏览器的 web 查看器

# 实时计划锚点（v2.2）：
/cc-mem plan-status                               # 计数器 + 新鲜度摘要
/cc-mem plan-show                                 # 重新生成并打印 memory/PLAN.md
/cc-mem plan-set --raw "Build feature X by ..."   # 捕获原始计划，标记 needs_refine
/cc-mem plan-set --from-refiner                   # 存储结构化 JSON（stdin）
/cc-mem plan-check                                # 重置计数器 + 发出 guardian 提示
/cc-mem plan-replan                               # 在已存原始计划上重新点亮 needs_refine
/cc-mem plan-clear                                # 丢弃当前活动计划
```

**在 Claude Code 外部**（shell，下面展示的是独立安装路径——市场安装请自行调整）：

```bash
M="python ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project ."
$M status
$M search "auth flow"
# ... 子命令与上面相同
```

## MCP 工具

经由 `cc_memory/mcp/server.py` 暴露 8 个工具：

| 工具 | 用途 |
|------|---------|
| `memory_search` | FTS5 搜索（精简结果） |
| `memory_get_details` | 按 ID 批量取回完整详情 |
| `memory_add` | 经反补丁 upsert 添加 |
| `memory_stats` | 项目统计 |
| `memory_topics` | 列出主题摘要 |
| `memory_recent` | 带过滤的近期记忆 |
| `progress_get` | 读取 PROGRESS.md 状态（结构化字段） |
| `progress_regenerate` | 从 SQL 状态强制重写 memory/PROGRESS.md |

在 `~/.claude/mcp.json` 中启用（在 `cc_memory/config.json` 中设置
`cc_memory.mcp.auto_register=true` 后重新安装）。

## 可视化仪表盘

```bash
# 市场安装或独立安装——会自动解析插件路径：
/cc-mem dashboard

# 或直接调用 CLI（把 <plugin-root> 替换为你的安装路径）：
python <plugin-root>/cc_memory/cli/mem.py --project . dashboard

# 或独立可执行文件（Windows）：
cc-memory-dashboard.exe
```

6 个标签页：Memories · Plans · Sessions · Keywords · SQL Console · Stats。

## Web 查看器

```bash
/cc-mem serve
# 在浏览器中打开 http://127.0.0.1:9377
```

## 计划队列（Plan Queue）

使用同一 SQLite 数据库的任务规划系统：

```bash
P="python ~/.claude/hooks/cc-memory/cc_memory/cli/plan.py --project ."

$P add "Task A" "Task B" "Task C"
$P list
$P evaluate           # 标记 draft → evaluating；Claude 评估可行性
$P approve --all      # evaluating → ready
$P exec --next        # ready → executing（启动 Claude Code CLI）
$P done 1 "Result"    # 标记完成
$P status             # 队列摘要
$P clear              # 丢弃 done/failed/skipped
```

状态流转：`draft` → `evaluating` → `ready` → `executing` → `done`/`failed`/`skipped`。

## 配置

编辑 `~/.claude/hooks/cc-memory/cc_memory/config.json`：

- `extraction.*` — 抽取上限（句子数、指标数、待办数、文件变更数）
- `writer.*` — 反补丁阈值（`high_similarity_threshold`、
  `mid_similarity_threshold`）
- `injection.*` — SessionStart 的 token 预算与逐层占比
- `observation.*` — PostToolUse 截断上限、跳过列表
- `idle_reorg.interval_turns` — 空闲整理之间相隔的回合数 N（默认 5）
- `consolidation.*` — 完整 LLM 整理的排程（含异步支路的
  `auto_interval_sessions`）
- `ccl.*` — Ollama 兜底 URL + 模型
- `modes.default` — 默认项目模式（code/research/writing）

## API 密钥

cc-memory 会从 `~/.claude/.credentials.json` 自动检测你的 Claude OAuth 令牌。
只要你已登录 Claude Code，就无需手动设置 API 密钥。

解析顺序：`ANTHROPIC_API_KEY` 环境变量 → Claude OAuth 令牌。

## 测试

`tests/smoke_test.py` 是一个端到端的纯 stdlib 脚本（无需 pytest），验证反补丁写入器
的决策、PROGRESS.md 整篇重写、只填空的刷新契约、last-wins 的 TodoWrite 抽取、
tier-3 transcript 兜底、旧版 `SESSION_HANDOFF.md` 迁移、布局检查器、两支路的
PreCompact 形态，以及 i18n 漂移门。

```bash
python tests/smoke_test.py
# 期望：一连串 [OK] 行，以 "===== ALL SMOKE TESTS PASSED =====" 结尾
```

文档翻译单独做漂移检查：

```bash
python tools/i18n_check.py          # 逐文档给出 [OK]/[STALE]/[FAIL]；有漂移则非零退出
python tools/i18n_check.py --list   # 显示每个 英文/翻译 配对 + 记录哈希 vs 当前哈希
```

## 构建可执行文件

```bash
pip install pyinstaller
python build_exe.py
# 产出：
#   dist/cc-memory-installer.exe
#   dist/cc-memory-dashboard.exe
```

## 依赖要求

- Python 3.8+（仅 stdlib——运行时无 pip 依赖）
- 支持钩子的 Claude Code
- PyInstaller（仅用于构建 exe，运行时不需要）
- Windows 上：确保 `python3` 能解析到一个 Python 3 解释器，因为
  `hooks/hooks.json` 调用的是 `python3`，而 python.org 安装器默认不提供
  `python3.exe`。最简单的修复是在 PATH 上把 `python3` 软链接或 shim 到 `python`。

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 完整架构概览
- [docs/MEMORY_RULES.md](docs/MEMORY_RULES.md) — 反补丁写入契约
- [docs/HANDOFF_PROTOCOL.md](docs/HANDOFF_PROTOCOL.md) — PROGRESS.md 规格
- [docs/PLAN_PROTOCOL.md](docs/PLAN_PROTOCOL.md) — PLAN.md + 子代理规格
- [docs/I18N.md](docs/I18N.md) — 文档多语言（英文 / 中文）版本控制
- [CHANGELOG.md](CHANGELOG.md) — 版本历史

## 许可证

MIT

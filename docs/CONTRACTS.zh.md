<!-- i18n-source: CONTRACTS.md | sha256: 93f6432f0dcb0d06 | version: 2.14.0 | translated: 2026-09-01 | translation: 31def881733339dd -->
> [English](CONTRACTS.md) · **简体中文**

# cc-memory — 契约（Contracts）

本插件**在代码里**强制执行三条不变量，而不是靠约定。每一条都有唯一的收口函数、唯一
的生成产物，以及自动化断言。违反其中任何一条都是 bug，不是风格选择：这些机制之所以
存在，是因为对应的失效模式在生产中被真实观察到过（v2.0 的堆叠重复记忆、v2.0 无人
阅读的交接、v2.4.0 之前被无声沉没的计划步骤）。

断言分布在哪里：`tests/smoke_test.py` 覆盖反补丁决策、PROGRESS.md 的整篇重写与
「只填空字段」刷新，以及计划生命周期（v4 迁移 → 捕获 → 精炼 → TodoWrite 同步 →
PLAN.md）。**R610 结转门禁**由它自己的套件 `tests/test_plan_carryover.py`（20 项
检查）覆盖 —— `grep -n "carryover\|dispositions" tests/smoke_test.py` 仍然没有任何
输出，所以两个都要跑。`tests/test_surfaces.py`（v2.5）覆盖这些契约被触达时所经过的
发布表面。

本文件取代 2.4.3 之前的三件套 `docs/MEMORY_RULES.md`、`docs/HANDOFF_PROTOCOL.md` 和
`docs/PLAN_PROTOCOL.md`；版本字符串的权威来源是 `cc_memory/core/version.py`。

**关于 `file:line` 引用。** 它们现在有强制手段了：`tools/citation_check.py`
（v2.5.2），并在 `tests/smoke_test.py` 内部运行。对每一条引用，它用 `ast` 解析出
上下文散文里点到的符号，然后断言被引用的行号区间覆盖了该符号的定义，或者至少提到了
它。第一次运行就查出 **594 条引用里有 163 条已经失效**，并已机械修复。如果一条引用
所在的句子里没有任何可以唯一解析的函数、类或 ALL_CAPS 常量，它会被判为 SKIP，
**不做检查**——所以请把这里的行号当作线索，把**符号名**当作事实：
`grep -n "def <symbol>" <file>` 才是权威，修行号请用
`python tools/citation_check.py --fix`。

## 目录

1. [反补丁写入契约](#反补丁写入契约anti-patch-contract) —— 每一次记忆写入都会与已经
   存在的内容做调和（`llm/memory_writer.py:upsert_smart`）。
2. [强制交接契约](#强制交接契约handoff-contract) —— `.ccm/PROGRESS.md` 是单条 SQL
   行的整篇重写投影，而且下一次会话会被*强制*读取它（`core/progress.py`、
   `hooks/session_start.py`）。
3. [实时计划契约](#实时计划契约plan-contract) —— `.ccm/PLAN.md` 是实时任务锚点，
   替换或清除它都不可能无声地丢掉未完成的步骤（`core/plan.py`、`cli/mem.py`）。

架构总览、模块布局和 i18n 约定见 [ARCHITECTURE.zh.md](ARCHITECTURE.zh.md)。

---

## 反补丁写入契约（Anti-patch contract）

### 规则

> **记忆更新必须是源码式的，而不是补丁式的。**
>
> 当一条新记忆 M 描述的事实与一条已存在的记忆 E 相同时，写入器会**就地修改 E**
> （或以一个链接取代它），而不是追加一条独立的行。绝不存在两条活动行描述同一个
> 事实的情况。

这就是 `llm.memory_writer.upsert_smart` 实现所强制执行的规格。每一条保存路径都必须
经由那一个函数路由。技能、CLI、MCP、钩子、GUI、web viewer——无一例外。完整的调用方
清单见[保存路径表](#各条保存路径如何遵守它)；如果你新增一条保存路径，
它就要进那张表。

### 为什么

v2.0 有四条互相独立的保存路径（`pre_compact`、`stop` 观察者、`/save-memories` 技能、
`mcp_server.handle_memory_add`），每一条都有自己的去重逻辑。它们产出了**堆叠记忆**
——语义上完全相同的事实以措辞略有差异的形式被保存了 3-5 次——以及一个被污染的
`SESSION_HANDOFF.md`，其内容本身就证明了补丁式反模式（同一节里混着用户提示、工具
输出和决策）。

修复是结构性的，而不是化妆式的：写入路径收敛到一个**先看再写**并选择正确动作的
函数上。

### 决策树（即契约本身）

输入：`content`、`topic`、`category`、`importance`、`tags`、`session_id`
（`llm/memory_writer.py:95-158`）。

```
0. content = clean_for_storage(content.strip()).           (memory_writer.py:66)
   若 len < MIN_CONTENT_LEN (10) 则 SKIP（reason: too_short）。   (:110-111)
   把 {decision,result,config,bug,task,arch,note} 之外的 category
     强制为 "note"。                                             (:113-114)
   把 importance 钳制到 1-5；tags 默认为 []。                     (:115-116)

0b. 从这里到第 5 步的全部逻辑运行在同一个 `BEGIN IMMEDIATE` 事务里 ——
   `db.reconcile_upsert`（core/db.py:1424-1552）。`upsert_smart` 以参数形式提供
   策略：阈值、最佳候选函数（`_make_pick`）与标签并集规则
   （`_merge_fields`）；原子性归数据库所有。在这个事务出现之前，两个并发
   保存同一句话的写入方会同时看到空表并各自插入（实测：
   actions=['inserted','inserted']，同一哈希两条活跃行）。

1. 计算 content_hash = sha256(content.strip().lower())[:16]。
                                                     (db.compute_content_hash)
   精确哈希命中由事务**内部**的 SQL 检查：
       → SKIP。Action: "skipped"。Reason: hash_match。
       理由：文本完全重复。
   （`db.find_by_hash` 只作为 IntegrityError 的恢复路径存在——竞态的
   落败方重读胜者，而不是抛异常。）

2. 在作用域内查找最相似的 ACTIVE 记忆（`_make_pick`）：
       主作用域：  topic == new_topic
       兜底作用域：category == new_category, is_active = 1,
                   ORDER BY updated_at DESC LIMIT max_candidates
                   —— 在 topic 为空，或按 topic 的查询没有返回任何行时触发
   相似度是基于 `core.textsim.shingle_set` 的 Jaccard —— 非 CJK 文本用
   字符三元组，CJK 连续段用字符**二元组**（十个汉字里改一个字在三元组下
   只得 0.4545，低于 MID_SIM，永远无法 merge 或 supersede）。至多对
   MAX_CANDIDATES_TO_SCAN (500) 个候选打分。令 sim = 最大相似度。

3. 若 sim >= HIGH_SIM (0.80)：
       → MERGE_IN_PLACE，由同一事务内的 SQL 完成。
       content=new_content, importance=max(new_imp, existing_imp),
       topic=new_topic or existing_topic,
       tags=_merged_tags(existing_tags, new_tags, ["merged"])
       # 与**存活行**的 tags 做并集，绝不整体替换 —— 来源标签
       # （["observer","realtime"]、["mcp"] 等）得以继承 —— 并以
       # MAX_TAGS (32) 封顶，因为 memory_add 是模型可调用的。
       Action: "merged"。不新增行。content_hash 会被重算。
       理由：“本质上是同一句话”—— 保留最新的措辞。

4. 否则若 sim >= MID_SIM (0.50)：
       → SUPERSEDE：在**同一个事务**里插入带 supersedes_id=existing.id
       的新行**并**归档旧行（拆成两个事务时，中途被杀会留下两条活跃行）。
       tags=_merged_tags(existing_tags, new_tags, ["supersedes"])，同样封顶。
       Action: "superseded"。
       理由：同一事实的精炼 / 整合版本；
       通过取代链保留历史，这样才能审计究竟改了什么。

5. 否则：
       → INSERT NEW（仍在事务内）。
       Action: "inserted"。
       理由：独立事实。

6. 循环结束后，`upsert_batch` 会调用
   regenerate_memory_index(db, project_id, memory_dir) 让 .ccm/MEMORY.md 保持
   同步 —— 无条件执行（即便每一条都被跳过），但仅当传入了 `memory_dir` 时才会
   (memory_writer.py:106-169)。单独调用 `upsert_smart` 绝不会重新生成。
   绝不允许 MEMORY.md 漂移。
```

`upsert_smart` 返回
`{"action": "skipped"|"merged"|"superseded"|"inserted", "id": ..., "similarity": ..., "old_id": ...}`
（`memory_writer.py:200-235`）；跳过路径会额外带上 `"reason"`，取值为 `too_short` 或
`hash_match`。`upsert_batch` 把这些聚合成按动作分类的计数，外加一个 `results` 列表
（`memory_writer.py:200-235`）。

### 阈值与常量

`HIGH_SIM` / `MID_SIM` 是 `cc_memory/llm/memory_writer.py:64` 中的模块常量
（0.80 / 0.50），与之并列的还有 `MIN_CONTENT_LEN`（10）、
`MAX_CANDIDATES_TO_SCAN`（500）和 `MAX_TAGS`（32）。`cc_memory/config.json`
里**没有** `writer.*` 键——它们与其他 34 个惰性键一起在 v2.5.0 被删除
（`config.json` 的 `removed_keys` 注记有案），原因正是从来没人读它们。
**要改就改常量，不要改配置。** 默认值（0.80 / 0.50）是凭经验选定的：0.80 要求新内容
本质上就是同一句话（措辞不同，事实相同）；0.50 能抓住“精炼过”的版本，同时仍让真正
相关但彼此不同的事实通过。

### 它排除了什么

以下反模式在机制上被封死了，因为要复现它们就必须绕过 `upsert_smart`：

1. **堆叠重复。** v2.0 曾有一个 `cc-memory` 主题下挂着 10 条记录，它们合起来只是用
   措辞略有差异的方式反复复述同一段插件描述。有了 `upsert_smart`，第 2 次尝试就会被
   归并进第 1 条。

2. **没有历史的补丁式更新。** 如果一个事实真的发生了变化（“我们把 lr=3e-4 换成了
   lr=1e-4，因为……”），取代路径会把旧事实以 `is_active=0` 保留下来，并通过
   `supersedes_id` 链接。`db.get_supersede_chain(id)`（`core/db.py:1667-1682`）可以走一遍
   历史。不需要什么“记忆版 git blame”的黑魔法。

3. **MEMORY.md 过期。** 每次批量写入之后自动重新生成，避免了 v2.0 中观察到的“过期
   50 天”失效模式（当时 PreCompact 会写 MEMORY.md，而 Stop / 技能 / MCP / CLI 不
   会）。保存路径之外还有若干刷新点让它保持诚实：PreCompact 的尾部
   （`hooks/pre_compact.py:509`）、Stop 钩子的空闲整理（`core/idle.py:96`）、异步
   整理支路（`hooks/consolidate_async.py:187-188`）、`/cc-mem cleanup`
   （`cli/mem.py:1100`）以及 `ccm-load` 技能（`skills/ccm-load/SKILL.md:318`）。

4. **只做哈希去重从而掩盖语义重复。** 哈希去重是第 1 步，但第 2-5 步才抓得住
   “fix bug” 与 “fix bug.”（同一事实，标点不同）这种 v2.0 会漏掉的情况。

### 各条保存路径如何遵守它

下面是树内 `llm/memory_writer` 的每一个调用方，以及它使用的确切入口。
`core/db.py` 与写入器之外没有任何直接调用 `db.insert_memory` 的地方——
而且自 v2.8.0 起这是一条**计算出来**的契约，不再是散文：
`tools/contracts.py` 从代码树推导 `insert_memory_callers`，`smoke_test.py`
断言它为空，`tools/falsify_fixes.py --case r8antipatch` 证明出现绕行调用方
时该断言会变红。

| 保存路径 | 入口函数 |
|-----------|---------------|
| `PreCompact` 钩子 | `upsert_batch(db, pid, sid, extracted_list, memory_dir)`（`hooks/pre_compact.py:673`） |
| `Stop` 观察者 | `upsert_batch(db, pid, None, observer_list, memory_dir)`（`hooks/stop.py:384`） |
| `SessionStart` 追溯保存 | `upsert_batch(db, pid, sid, memories, memory_dir=memory_dir)` —— 处理此前未保存的会话（`hooks/session_start.py:1138`） |
| `/save-memories` 技能 | `upsert_batch(db, pid, None, memories, memory_dir=mem_dir)` —— `mem_dir` 是 `core.layout.memory_dir(project)`，绝不是手写的路径拼接（`skills/save-memories/SKILL.md:180`） |
| `mem.py add` CLI | `upsert_smart(...)` + `regenerate_memory_index(...)`（`cli/mem.py:1171,524`） |
| `mcp/server.py handle_memory_add` | `upsert_smart(...)` + `regenerate_memory_index(...)`（`mcp/server.py:629-656,192`） |
| Dashboard UI 的 “Add Memory” | `upsert_smart(...)` + `regenerate_memory_index(...)` —— 自 v2.2 起改为路由（`ui/dashboard.py:1688,956`）。`ui/dashboard.py` 中没有任何 `db.insert_memory` 调用。 |
| Dashboard UI 的 “Save Session” | `upsert_batch(...)`（`ui/dashboard.py:2270`） |
| Dashboard UI 的 “Init Project” 扫描 | `upsert_batch(db, pid, None, batch, memory_dir=memory_dir)`（`ui/dashboard.py:2270`） |
| web_viewer 的 POST `/api/memory` | `upsert_smart(...)` + `regenerate_memory_index(...)`（`ui/web_viewer.py:66`） |

### 整理兜底的例外（Consolidation backstop，v2.3）

**整理**流水线（`core/consolidate.py`）是“每一次写入都经由 `memory_writer` 路由”这条
规则的成文例外。它是清理兜底，不是保存路径，而且它操作的是**已经存在**的记忆：

- `semantic_dedup`（LLM 判定的同事实归并，`consolidate.py:405-492`）与
  `detect_obsolete_llm`（新事实与旧事实矛盾，`:817-897`）直接调用 `db.update_memory`
  + `db.archive_obsolete`。它们绝不从零创造面向用户的内容——幸存行本来就已经存在；
  落败者被归档（`is_active=0`），并带一个向前的 `supersedes_id` 链接
  （`db.archive_obsolete`，`core/db.py:2062-2191`），因此血缘依然可追溯、可恢复。
  自 v2.9.0 起这个链接用 `COALESCE` 写入，绝不覆盖已有的：由更早一次 SUPERSEDE
  产生的落败行，本身已经指向它所替代的那一行，覆盖会让那个更旧的版本从任何链
  游走中都不可达（实测：链 `[2,1]` 变成 `[2,3]`）。该槽位记录它学到的**第一条**
  血缘事实；槽位已被占用时，替代关系改为写进日志。
  `semantic_dedup` 在写入之前会并上幸存行原有的 tags（`consolidate.py:479-487`）；
  自 v2.8.0 起 `upsert_smart` 也这样做（`llm/memory_writer.py:_merged_tags`），所以
  这已经不再是两者的差别——归并分支此前写的是 `set(incoming + ["merged"])`，把幸存
  行自己的来源 tags 整个销毁了。
- `decay_and_archive`（引用感知的陈旧度安全网，`consolidate.py:876-920`）**只**归档
  非常老 + 低重要度 + 从未被注入过的行——一张零误归档的安全网。有效年龄是
  `now - COALESCE(last_referenced_at, created_at)`（`core/db.py:224-234`；
  `consolidate.effective_age_days`，`:56`）。
- **每一个整理阶段都是可逆的**（`is_active=0`，绝不 `DELETE`），自 v2.8.0 起
  `cleanup_garbage` 也包含在内。它曾经是唯一的例外，而且例外得很不是地方：它由 Stop
  钩子每五轮无人值守地跑，并按自己私有的 20 字符下限硬删除——而写入器的下限是 10，
  于是它销毁的正是四个入口刚刚接受下来的内容。实测：`/cc-mem add note "lr=3e-4 wins"`
  报告 `[inserted]`，五轮之后表里一行不剩。它现在从 `llm.memory_writer` 导入那唯一的
  下限，并经由 `db.archive_if_unchanged`（`core/db.py:1826-1861`）归档，与另外两个
  「快照判决」阶段一致。用这个变体而不是 `bulk_archive` 的原因是：本阶段的判决来自
  一次**独立事务**里的快照读，而 PreCompact 写入器是并发跑的，所以一行在这个窗口里
  被修好之后仍然会被归档——实测，刚刚归并进去的好内容被置为 `is_active=0`。以判决
  当时那个 `content_hash` 作为条件，就把陈旧判决变成了空操作。
  `db.delete_memories` 只保留给用户主动清除，代码树里没有调用方——这是 `core/db.py`
  给自己写下的契约。`/cc-mem archive <id>...` 是 v2.8.0 新增的**用户侧**退役入口，
  它同样只归档：`sql` 是只读的，`add` 只在相似度够高时才归并，所以一条被发现是**错**
  的记忆此前根本没有受支持的出口。
  `merge_near_duplicates`（`:243-296`）同样经由 `archive_if_unchanged` 归档，可逆，
  但**没有** `supersedes_id` 链接。

这是有意为之且边界清晰的；它并不放松对**保存**路径的规则。

#### 整理到底何时运行（v2.12.0——背压）

写入路径逐行做调和；上面的兜底是**批量**工作，而直到 v2.11.4 为止，它唯一的
自动触发器是异步 PreCompact 支路上"距上次运行 ≥ N 次会话"这道门。该判据的两半
都假设压缩会发生：一个只在短会话里工作的项目从不压缩，于是整理被饿死——在本仓库
上实测：**一个月写入 349 条记忆，而整理标记已经 17 天没动**，SessionStart 注入的
主题摘要落后了三个小版本。现在有三个触发器，背后是同一个判据、同一个标记：

- **压缩节奏**（v2.3.2，未变）：异步 PreCompact 支路在
  `sessions - last ≥ auto_interval_sessions` 时运行——**或**在下面的积压判据
  判定到期时运行。
- **背压**（`core.consolidate.consolidation_backlog`）：自标记的
  `last_memory_id` 行号水位线以来落了 `BACKLOG_ROWS`（50）条记忆，或
  `BACKLOG_DAYS`（7）天已过且新增至少 10 条时到期——一个闲置的项目绝不会
  仅仅因为日程而付出一次运行。Stop 钩子每回合探测一次
  （`hooks/stop.py:_maybe_kick_consolidation`——一条 COUNT 查询），并以
  **分离进程**方式拉起 `consolidate_async.py --cwd <root>`；工作者会在整理锁
  之下重新检查判据，因此竞态的拉起是空操作，而 `.consolidation.kick` 冷却
  （10 分钟）为反复失败的工作者的重启设了上界。
- **手动**（`/cc-mem consolidate`），自 v2.12.0 起它也经由那**唯一**的共享
  写入方（`core.consolidate.write_consolidation_marker`）盖章标记——此前从不
  盖，于是一次手动运行之后探针仍然读到"到期"，跟着又跑了一遍多余的后台整理。
  `--deep` 会先循环 `semantic_dedup` 直到某一轮确认再无新发现
  （`core.consolidate.deep_dedup`；被否决的组在本次运行内会被记住，绝不重复
  送审），一份积了几个月的账就是这样一次清完的——每次运行 12 组的上限是为
  受预算门约束的后台通道定的尺寸，不是为积压定的。

### 不应该做什么

- 不要在任何保存路径里直接调用 `db.insert_memory`。（它仍然暴露出来用于迁移 / 批量
  装载，但不用于日常写入——`core/db.py:1397-1412`。）
- 不要自己撸一套 `"SELECT content FROM memories ..."` 去重。那正是
  `db.find_by_hash`（`core/db.py:2235-2243`）和写入器的 `_find_similar`
  （`llm/memory_writer.py:179`）的职责。（并不存在 `db.find_similar`；匹配器就住在
  写入器里，按设计是私有的。）
- 不要手工“打补丁”改 MEMORY.md，也不要指望别的路径去刷新它。任何非平凡的状态变更
  之后都要调用 `regenerate_memory_index`。生成出的文件自带一条 DO-NOT-EDIT 横幅，
  列出了每一条会覆盖它的路径（`llm/memory_writer.py:238-333`）。

### 验证

在一个装了 cc-memory 的项目里：

```bash
# 显示取代链数量（证明反补丁正在生效）
/cc-mem stats

# 走一条具体的链
/cc-mem supersedes <memory_id>
```

`/cc-mem` 会自己解析 CLI 的位置（`commands/cc-mem.md:54-69`）：它先探测
`${CLAUDE_PLUGIN_ROOT}`，然后是 `$HOME/.claude/hooks/cc-memory`，并且在每一个根目录
下先尝试**嵌套**布局 `<root>/cc_memory/cli/mem.py`（市场 / 开发检出），再尝试**扁平**
布局 `<root>/cli/mem.py`。独立安装器把每一个子包直接拷进 `TARGET_DIR/<subdir>/`
（`cc_memory/ui/installer.py:77-89` 的 `TARGET_DIR`、`:37-48` 的 `SUBPACKAGE_FILES`、
`:74` 的 `_copy_subpackages`），因此独立安装**没有** `cc_memory/` 这一段路径——它的
CLI 是 `~/.claude/hooks/cc-memory/cli/mem.py`。在市场安装下，那棵树只保留 `logs/`
（已在本机核实），所以任何硬编码的 `python ~/.claude/hooks/cc-memory/.../mem.py`
调用在那里都会失败——本仓库就是一个市场 / 目录安装。

如果出现 `Supersede chains: N update events recorded`（`cli/mem.py:399-405`），说明
契约在生效。为零也没问题（还没有事实被精炼过），但一个稳步增长的数字意味着真实世界
的整理正在发生。

---

## 强制交接契约（Handoff contract）

### v2.1 解决的问题

v2.0 把 `memory/SESSION_HANDOFF.md` 写成一份*追加式*文档——每次 PreCompact 都添加
新的小节，久而久之这个文件里堆满了零散的 Bash 输出、对话片段，以及来自更早会话的
互相矛盾的状态。下一个 Claude 只被*温和地提醒*要“记得调用 /save-memories”，但没有
任何东西真正迫使它去读 SESSION_HANDOFF.md。结果：交接不可靠；新会话重复做已经做完
的工作。

v2.1 用 **PROGRESS.md**（始终从一条 SQL 行整篇重写）+ **SessionStart 处强制注入的
`<system-reminder>`** 修掉了这一点。旧的 `SESSION_HANDOFF.md` 会在 v2.1+ 下的首次
PreCompact 时被重命名为 `SESSION_HANDOFF.md.v2.bak`（一次性迁移
`migrate_legacy_handoff`，`core/progress.py:628-646`，从 `hooks/pre_compact.py:543`
调用）。

### PROGRESS.md 就是唯一真相来源（SOT）

`.ccm/PROGRESS.md` 由 `cc_memory/core/progress.py:write_progress_md` 从 `progress`
SQL 行生成。Schema 见 `cc_memory/core/db.py:_MIGRATIONS:v3_progress`（`db.py:176-190`），
外加 `db.py:2615-2669` 处的两个 v5 会话标注列。§0 还会经 `db.get_recent_sessions`
读取 `sessions` / `session_summaries` 表（`core/progress.py:324`；`core/db.py:2615-2669`）：

| 列 | 类型 | 主来源 · 兜底 |
|--------|------|---------------------------|
| `project_id` | INTEGER PK | `upsert_project` |
| `current_request` | TEXT | UserPromptSubmit 第 1 回合（`user_prompt.py:164`）→ PreCompact 的 `_first_user_request(window.head)`（`pre_compact.py:269-311`）—— 它会扫描至多 200 条记录，越过开头的 `queue-operation` / `attachment` 元数据行，并跳过内容为空的 user 行（`pre_compact.py:269-311`，v2.4.2）→ `session_summaries.request`（`progress.py:241`） |
| `status_done` | TEXT | `session_summaries.completed`（`progress.py:236`），PreCompact 用抽取结果里 `result` / `decision` 类的记忆填充（`pre_compact.py:666-700`），仅当抽取没给出任何结论时才退回到观察到的 Edit/Write 路径列表。v2.8.0 以前**永远**走那条路径列表，于是 §2 的 “Done” 渲染出来是一份文件清单，而不是“做完了什么”。若为空，SessionStart 会补上（`session_start.py:589-590`） |
| `status_in_flight` | TEXT | `session_summaries.learned`，由抽取结果里 `arch` / `config` / `bug` 类的记忆填充（`pre_compact.py:666-700`）。v2.8.0 以前 PreCompact 把它硬编码成 `""`，所以 §2 的 “In-flight” 无条件渲染成 `*(none active)*` —— 那是结构性的，不是因为真的没有在办事项 |
| `status_blocked` | TEXT | 显式的 `patch_progress(status_blocked=...)` —— 今天树内没有任何调用方这样做；它是留给外部工具的 API。全仓库 grep 只能找到 schema 默认值（`core/db.py:2548-2587,853`）、空播种（`core/progress.py:276`）和读取处（`core/progress.py:276`） |
| `open_todos` | JSON | PreCompact 经 `ext["latest_todos"]` 调用 `extract_latest_todo_state(window)`（`core/extractor.py:478-513,558`；`pre_compact.py:630,656`）→ SessionStart 第 3 级：挖掘上一次会话的 transcript（`session_start.py:874`）→ **最后手段**：把 `session_summary.next_steps` 按 `;` 切分（`session_start.py:874`）。只保留非 `completed` 的 todo（`progress.py:276`） |
| `plan` | TEXT | `session_summaries.next_steps` —— 若有最新 TodoWrite 的 pending 项则取自它，否则取自 LLM 抽取出的 `task` 类记忆（`pre_compact.py:462-468`）；在 `progress.py:255` 传播，在 `session_start.py:874` 按“空则填”补齐 |
| `critical_context` | JSON | importance ≥ 4 的前 10 条记忆，内容截断到 200 字符（`progress.py:107-113`；`session_start.py:875`） |
| `files_touched` | JSON | `observations` 表（`pre_compact.py:446-453` → `progress.py:128-134`；Stop 每回合打补丁 `stop.py:193-211`；SessionStart 第 2C 级 `session_start.py:875`）→ 第 3 级：对上一次会话 transcript 跑 `extract_file_changes`（`session_start.py:875`） |
| `transcript_ptr` | TEXT | PreCompact 解析为绝对路径的 `transcript_path`（`pre_compact.py:751`）→ 第 3 级 `find_latest_transcript(cwd, exclude_session_id=...)`（`session_start.py:946`） |
| `updated_at` | TEXT | ISO 时间戳，由 `upsert_progress` / `patch_progress` 打戳（`db.py:2389-2465`、`:937-943`） |
| `trigger_type` | TEXT | "auto" \| "manual"（PreCompact 把宿主自己的触发字符串原样透传 —— `pre_compact.py:71,492`；`"precompact"` 只是 `collect_progress_state` 在 `progress.py:200-260` 的默认关键字参数，且总会被覆盖）\| "stop"（`stop.py:561`）\| "user_prompt" \| "resume_request"（`user_prompt.py:213`）\| "session_start_refresh"（`session_start.py:890`） |
| `current_session_id` | TEXT | 只由 `db.tag_progress_session` 写入（`db.py:2589-2613`）—— 由 PreCompact（`pre_compact.py:750`）、Stop（`stop.py:561`）、SessionStart（`session_start.py:890`）、UserPromptSubmit（`user_prompt.py:213`）打标签 |
| `session_started_at` | TEXT | `db.tag_progress_session` —— 只在存储的 sid 发生变化时重置；`upsert_progress` 在整篇重写时会把这两个字段一并保留（`db.py:2589-2613`） |

渲染出的 Markdown（[`cc_memory/core/progress.py`](../cc_memory/core/progress.py)
中的第 0-7 节）就是从这一行生成的。手工编辑 PROGRESS.md 毫无意义：四条自动更新路径
（PreCompact / Stop / UserPromptSubmit / SessionStart 刷新）中的任何一条——加上两个
手动重新生成入口 `/cc-mem progress`（`cli/mem.py:1238`）和 MCP 的
`progress_regenerate` 工具（`mcp/server.py:745`）——都会覆盖它。全部六处
`write_progress_md` 调用点：`pre_compact.py:752`、`stop.py:500`、`user_prompt.py:205`、
`session_start.py:1013`、`cli/mem.py:1416`、`mcp/server.py:745`。

### 渲染布局（§0-§7）

`write_progress_md`（`core/progress.py:390-570`）按顺序发出：

| 区块 | 来源 | 空状态文本 |
|-------|--------|------------------|
| `# PROGRESS — <project name>` + `*Generated: <updated_at>* · via <trigger> · <project path>` | `progress.py:261-265` | — |
| 引用块："SINGLE SOURCE OF TRUTH for session handoff … **Never append. Never patch by hand.**" | `:267-268` | — |
| `## 0. Session` | `_render_session_section`，`:172-236`（标题在 `:188` 发出） | `⚪ **Current session**: *(no session tagged …)*`（`:206`）与 `*(no prior compacted sessions yet)*`（`:215`） |
| `## 1. Current Request` | `:279-281` | `*(no request recorded yet)*` |
| `## 2. Status` —— **Done** / **In-flight** / **Blocked** | `:285-293` | `*(none yet)*` / `*(none active)*` / `*(none)*` |
| `## 3. Open Todos` —— `- [ ] \`priority\` content`，非 pending 用 `[~]` | `:297-306` | `*(no open todos)*` |
| `## 4. Plan (sequenced next steps)` | `:310-312` | `*(no plan recorded)*` |
| `## 5. Critical Context (must-know memories)` —— 至多 10 条 `- #id \`category\` [topic] content` | `:316-327` | `*(no critical memories)*` |
| `## 6. Files Touched This Session` —— 按动作分组，每个动作至多 30 条路径 | `:331-344` | `*(no files touched)*` |
| `## 7. Pre-compact Transcript Pointer` | `:347-356` | `*(transcript pointer not yet recorded)*` |
| 页脚：`---` + "This file is the handoff contract for the next session. Read it FIRST." + 一行规格指针 | `:360-364` | — |

§0 是 v5 的会话标注，它被**放在最前面**是有意的：读者必须能立刻判断这一行是它自己
会话写的，还是另一个会话留下的过期写入。当前会话那一行是
`🟢 **Current session**: \`#<sid8>\` · started \`<ts>\` · last write \`<ts>\` · trigger \`<t>\``，
后面跟着一条明确警告：如果短 sid 不匹配，就把 §3/§6 当成另一个会话的工作
（`:191-204`）。此前会话的时间线列出 `db.get_recent_sessions` 中至多 5 行（排除当前
sid），每一行形如
`` - `#sid` · ended `<ts>` · <n> msgs · <summary> ``，其中摘要优先取
`session_summaries.completed`，回退到 `sessions.brief_summary`，空白被压平并在 100
字符处截断（`:210-234`）。

### PROGRESS.md 在什么时候被重写

1. **PreCompact**（整篇重写，所有字段全新）：
   - 触发：Claude Code 的自动压缩，或手动 `/compact`。
   - `collect_progress_state(...)` 从
     `extracted_memories + observations + session_summaries` 构建完整状态
     （`progress.py:355`）。
   - `db.tag_progress_session(...)` **先**运行，这样标签才能存活
     （`pre_compact.py:750`；保留逻辑见 `db.py:2589-2613`）。
   - `db.upsert_progress(**all_fields)` 覆盖整行（`pre_compact.py:751`）。
   - `write_progress_md(db, pid, memory_dir)` 重写文件（`:501`）。

2. **Stop**（部分更新，每回合）：
   - 先 `db.tag_progress_session(...)`，再
     `db.patch_progress(files_touched=<来自 observations>, trigger_type="stop")`
     （`stop.py:486`、`:211`）。
   - `write_progress_md(...)` 用打过补丁的状态重写文件（`:213`）。
   - 这让 “Files Touched This Session” 保持最新，无需等到下一次压缩。

3. **UserPromptSubmit**（仅第 1 回合）：
   - 先 `db.tag_progress_session(...)`（`user_prompt.py:213`），再
     `db.patch_progress(current_request=<prompt>, trigger_type="user_prompt" | "resume_request")`
     （`:132`）。
   - `write_progress_md(...)` 重写（`:133`）。
   - 立刻捕获这次会话的目标，而不是拖到 8 个回合之后。
   - 如果提示恰好是**恢复信号**之一（`""`、`"继续"`、`"接着"`、`"接着做"`、
     `"接着干"`、`"继续干"`、`"resume"`、`"continue"`、`"go on"`、`"keep going"`
     —— `user_prompt.py:127-131`），trigger_type 会被置为 `"resume_request"`，这样
     下游工具（以及强制提醒里的 RESUME PROTOCOL）就能据此行动。

4. **SessionStart 刷新**（每次会话启动，第 2/3 级兜底）：
   - `_refresh_progress_row(db, pid, memory_dir, current_session_id)`
     （`session_start.py:855-1015`）。
   - 空则填：绝不覆盖上游写入的非空字段（契约陈述见 `session_start.py:555-557`）。
   - 来源依次为：DB 的 critical_memories / session_summary / observations，然后
     （如果仍为空）去挖掘上一次会话的 `.jsonl` transcript，取 `open_todos`、
     `files_touched` 和 `transcript_ptr`。
   - 例外：`open_todos` **优先**从 transcript 填充——按 `next_steps` 切分的启发式
     只是最后手段（`session_start.py:582-585`、`:663-674`）。TodoWrite 的
     `tool_use` 块是结构化数据；而把散文式的 `next_steps` 字符串按 `;` 切开，会把
     一个长句坍缩成一条幻觉 todo。
   - 正是这一步保证了当 PreCompact 没有运行时（transcript 已被裁剪之后才手动
     `/compact`、上一次会话极短、PreCompact 崩溃，或项目的第一次会话），
     PROGRESS.md 不会渲染成一整面 `*(none)*` 占位符。

### 下一次会话是如何被强制读取它的

`cc_memory/hooks/session_start.py:_build_forced_reminder`（`:234-288`）在注入上下文
的末尾发出这一段：

```
<system-reminder>
CC-MEMORY HANDOFF — MANDATORY READ-FIRST PROTOCOL

Before responding to any user request in this session, you MUST:
  1. Use the Read tool on `.ccm/PROGRESS.md` (absolute: <path>).
  2. Use the Read tool on `.ccm/MEMORY.md` (absolute: <path>).

After reading, explicitly state in your first reply:
  "Read PROGRESS.md — prior progress: <one-sentence summary>."

RESUME PROTOCOL — if the user's first message is exactly one of:
    "" (empty)  ·  "继续"  ·  "接着"  ·  "接着做"  ·  "接着干"  ·
    "继续干"  ·  "resume"  ·  "continue"  ·  "go on"  ·  "keep going"
  then DO NOT ask for clarification. Instead:
    1. Read PROGRESS.md §3 (Open Todos) and §4 (Plan).
    2. If §3 has at least one open todo, announce
       "Resuming prior task: <todos[0].content>" and start executing it.
    3. If §3 is empty but §4 (Plan) is non-empty, follow the plan's first step.
    4. If both are empty, fall back to a one-sentence prior-progress
       summary plus "what would you like to do next?".

Why: this is the project's handoff contract (single source of truth).
Skipping it risks duplicating work or contradicting prior decisions.
Spec: `docs/CONTRACTS.md#handoff-contract`.
</system-reminder>
```

每一条带编号的 Read 行只在对应文件存在时才发出（`n` 会重新编号），并且当两者都不
存在时整块都被抑制（`session_start.py:243-246,255-262`）。

那份双语的恢复 token 列表是刻意为之的，必须与 `user_prompt.py` 的 `resume_signals`
保持同步——它带有一条 `# i18n Tier 3` 守卫注释（`session_start.py:270-271`；见
[ARCHITECTURE.md](ARCHITECTURE.md#9-documentation-language-convention-i18n)）。

Claude 会把 `<system-reminder>` 块当作权威，就像对待 cc-enforcer 的纪律规则一样。
措辞是刻意的：

- “You MUST”——不是“请考虑一下”。
- “Use the Read tool on `<absolute path>`”——对读哪个文件不留歧义。
- “Explicitly state in your first reply”——强制给出一个用户看得见的确认
  （下一次 PreCompact 也会抓到它）。
- 引用了规格，好让未来的 Claude 能推理出这么做的原因。

### 压缩前 transcript 指针（需要更深上下文时）

当 `current_request` 加上分层注入还不够用时，下一个 Claude 可以退回去直接读原始
transcript。PROGRESS.md 的第 7 节包含：

````
## 7. Pre-compact Transcript Pointer

If you need raw conversation history before compaction, read:

```
C:\Users\<user>\.claude\projects\<project-hash>\<session-uuid>.jsonl
```

This is a JSONL file: one message per line. Read with the Read tool.
````

这让那些没能装进抽取预算的信息可以被找回，而不必重跑一次压缩。**它是一条刻意设置
的最后手段路径**，不是主要的交接信号。

### 如果下一次会话不遵守这条提醒怎么办？

这条提醒是一份契约，不是一道硬闸门。Claude *应当*遵守它，但如果它没有，下一次
`PreCompact` 会用新会话产出的任何状态重写 PROGRESS.md——所以系统是自愈的。不存在
灾难性失效模式；只是有一次会话的交接被漏掉了。

如果你观察到 Claude 在系统性地无视这条提醒，可能的修法是：(a) 收紧
`_build_forced_reminder` 里的措辞，或者 (b) 加一个 `PreToolUse(Read)` 钩子，在第一次
Read 的目标不是 PROGRESS.md 时阻断它（类比 cc-enforcer 对规则 08 的强制执行）。
截至 v2.4.2 还不存在这样的钩子——`hooks/hooks.json` 声明了 5 个事件 / 6 条命令钩子
<!--ce:hooks-->（PreCompact 带两条支路：120 秒的同步支路和 300 秒的 `async`
整理支路；成员由 `python tools/contracts.py` 计算给出）——这条提醒依然只是建议性的。

### 验证

要审计一个项目里的交接健康度：

```bash
# 1. 显示 PROGRESS.md 当前的内容（这同时也会从 SQL 强制重新生成该文件 ——
#    cli/mem.py:1238 在每次调用时都会重写它）
/cc-mem progress

# 2. 显示 SQL 行里是不是当前数据
/cc-mem sql "SELECT current_request, trigger_type, updated_at FROM progress"
```

`/cc-mem` 对两种安装布局都能解析出 CLI ——完整的解析顺序见[反补丁一节的验证说明](#验证)
（`commands/cc-mem.md:54-69`；市场 / 开发检出用嵌套的 `<root>/cc_memory/cli/mem.py`，
独立安装器的产物用扁平的 `<root>/cli/mem.py`，见 `cc_memory/ui/installer.py` 的
`TARGET_DIR` / `SUBPACKAGE_FILES` / `_copy_subpackages`）。在市场安装下，硬编码的
`python ~/.claude/hooks/cc-memory/...` 调用是错的，因为那棵树只保留 `logs/`。

一份健康的 PROGRESS.md 应当具备：

- 非空的 `current_request`（在会话的第 1-2 回合内被设置）。
- 在任何编辑密集的回合之后，`files_touched` 非空。
- 较新的 `updated_at`（活跃工作期间不超过约 5 分钟）。
- 没有残留的 `.ccm/.pre_compact_attempt.json`（v2.4.2）。PreCompact 在入口写下这个
  标记，并且只在完成时才移除它（`pre_compact.py:281-320`；在 `:368` 写入，在
  `:378,536,571` 清除）；一个超过 10 分钟的标记意味着上一次压缩在保存之前就被**杀
  死**了，它的记忆已经丢失——SessionStart 会把它呈现为
  `[WARNING: PreCompact … DID NOT FINISH …]`（`session_start.py:187-206`，年龄闸门在
  `:199`）。`.last_save.json` 显示不出这一点：超时杀进程不会跑任何 `except` 块，也不
  会跑 `finally`，所以那个文件描述的仍然是**上一次**成功的运行。

---

## 实时计划契约（Plan contract）

cc-memory 的**实时计划锚点**：每个项目一份 `.ccm/PLAN.md`，它与真正在做的事情保持
同步，这样 AI 就不会随着上下文增长而忘记计划，也不会漂移到无关的工作上。它在 v2.2
引入；强制结转门禁在 v2.4.0 落地，并在 v2.4.1 被收紧。

### 为什么要和 PROGRESS.md 分成两个文件

`PROGRESS.md` 是**跨会话交接**文档——下一个 Claude 为了从上一个 Claude 停下的地方
接着做所需要知道的东西。它在每次 PreCompact 时被覆盖，在每次 Stop 时被打补丁。

`PLAN.md` 是**任务锚点**——我们*此刻*想要完成什么，并带有明确的步骤状态。它比单个
回合、单个会话活得更久。把两者混在一起会让 PROGRESS.md 过长，也会让 PLAN.md 不稳定。

两者共用同一个 SQLite 数据库（分别是 `plan_active` 和 `progress` 表），因此它们不
可能与自己的真相来源漂移开。`write_plan_md`（`core/plan.py:733-782`）是从行里做的
整篇重写，生成出的文件自带一条 DO-NOT-EDIT 横幅，写明那张 SQL 表和三个合法的编辑
入口（`core/plan.py:257-260`）。

### 生命周期

```
                      ┌───────────────────────┐
                      │   ExitPlanMode 调用   │
                      │   （或 `plan-set`）   │
                      └───────────┬───────────┘
                                  │
                                  ▼
              PostToolUse 钩子捕获 `plan` 字段
              → plan_active.raw = <markdown>
              → plan_active.needs_refine = 1
              → 写入 .ccm/.plan_raw.md
                                  │
                                  ▼  （下一个 Stop 钩子回合）
              [cc-memory.plan] NEW PLAN captured → invoke @plan-refiner
                                  │
                                  ▼
              主 Claude 派生 plan-refiner 子代理（Haiku）
              → 子代理读取 .plan_raw.md
              → 读取当前计划（plan-show / PLAN.md）
              → 输出 JSON {goal, success_criteria, steps[], dispositions?}
                                  │
                                  ▼
              `/cc-mem plan-set --from-refiner`（stdin = JSON）
              → R610 结转门禁：除非旧计划的每一个未完成步骤都被结转或被
                disposition 记录，否则拒绝（exit 1）
              → 旧计划归档到 .ccm/.plan_history/
              → plan_active.structured = JSON
              → plan_active.needs_refine = 0
              → 写入 .ccm/PLAN.md
                                  │
                                  ▼
              ┌─── 实时工作继续 ────────────────────┐
              │                                     │
              ▼                                     ▼
   PostToolUse: TodoWrite          PostToolUse: Edit/Write/...
   → sync_todos_to_steps()         → 累加 edits_since_last_guardian
   → 重写 PLAN.md                  （敏感工具一次加 20）
   （没有活动计划行时两者都是空操作：post_tool_use.py:126,132；
    todo 同步还额外要求一个 schema 合法的结构化计划：core/plan.py:551-556）
              │                                     │
              └─────────────────┬───────────────────┘
                                ▼
              Stop 钩子检查 should_nudge_guardian()
              若 turns≥8 或 edits≥12：
                [cc-memory.plan] guardian check recommended
                                ▼
              主 Claude 派生 plan-guardian 子代理（Haiku）
              → 读取 PLAN.md + PROGRESS.md + 近期 git 活动
              → 报告 ALIGNMENT + DRIFT + NEXT ACTION（≤150 词）
                                ▼
              `/cc-mem plan-check`（重置计数器）
                                ▼
              [继续；若漂移严重则 `/cc-mem plan-replan`]
```

钩子自己绝不派生子代理——它们只提示。Stop 钩子的回合计数器在每一个存在活动计划行的
回合上累加（`stop.py:274-277`），而两种提示是互斥的：`needs_refine` 优先于 guardian
提示（`stop.py:279-296`）。

### 数据模型：`plan_active`

每个项目一行。Schema（v4 迁移，`core/db.py:198-211`）：

| 列                              | 类型    | 用途 |
|---------------------------------|---------|---------|
| `project_id`                    | INTEGER | 主键，外键 → projects.id |
| `raw`                           | TEXT    | 计划模式输出的原文（或用户粘贴的文本） |
| `structured`                    | TEXT    | JSON {goal, success_criteria, steps[], context, dispositions?, ...} |
| `active_step`                   | INTEGER | 当前进行中步骤的 id |
| `edits_since_last_guardian`     | INTEGER | 漂移计数器（由 Edit/Write/MultiEdit/NotebookEdit 累加 —— `hooks/post_tool_use.py:131-133`） |
| `turns_since_last_guardian`     | INTEGER | 漂移计数器（由 Stop 累加） |
| `last_guardian_at`              | TEXT    | 上一次 guardian 检查的 ISO 时间戳 |
| `last_refined_at`               | TEXT    | 上一次精炼的 ISO 时间戳 |
| `needs_refine`                  | INTEGER | 1 = raw 是新的，但 structured 已过期 |
| `created_at`, `updated_at`      | TEXT    | 标准时间戳 |

### 结构化计划的 JSON schema

```json
{
  "version": 1,
  "goal": "Implement JWT-based auth for the dashboard",
  "success_criteria": [
    "All routes return 401 without a token",
    "Token refresh works without re-login",
    "Tests in tests/test_auth.py pass"
  ],
  "steps": [
    {"id": 1, "title": "Wire up token refresh",   "status": "done",        "notes": ""},
    {"id": 2, "title": "Add CSRF protection",     "status": "in_progress", "notes": "blocked on framework choice"},
    {"id": 3, "title": "Write integration tests", "status": "pending",     "notes": ""}
  ],
  "context": "Chose JWT over sessions for horizontal scaling.",
  "dispositions": [
    {"old_title": "Wire up token refresh",
     "action": "carried",
     "reason": "no evidence it shipped; re-listed as step 2"}
  ],
  "refined_at": "2026-05-25T14:30:00",
  "refined_by": "plan-refiner"
}
```

合法的 `status` 取值：`pending`、`in_progress`、`done`、`blocked`、`skipped`
（`core/plan.py:145-204`）。`normalize_structured`（`core/plan.py:89-134`）是防御性的：
它容忍常见的 LLM 状态别名（`todo`→`pending`，`wip`/`doing`→`in_progress`，
`complete`/`completed`→`done`），丢弃没有 title 的步骤条目，并按位置为缺失的 `id`
重新编号。`is_valid_structured`（`:70-86`）要求非空的 `goal` 和 ≥1 个格式良好的
步骤——达不到这一点的会被 `apply_refined_plan` 以
`"refined plan does not satisfy schema (needs goal + ≥1 step)"` 拒绝（`:496`）。

`dispositions` 是可选的，只有当这份计划**替换**另一份计划时才有意义。合法的
`action` 取值：`done`、`dropped`、`merged`、`carried`；`reason` 必须非空
（`core/plan.py:347,415-423`）。它会被保留在存储的计划里以供审计
（`core/plan.py:105-111`）。

### 同步算法（TodoWrite ↔ 步骤）

当观察到 `TodoWrite` 时，`core.plan.sync_todos_to_steps`
（`core/plan.py:235-280`，匹配器在 `:139-170`）会：

1. 对每一条 todo，基于 `core.textsim.shingle_set` 的 shingle（非 CJK 用
   三元组，CJK 连续段用二元组）计算它与每一个步骤 title 的 Jaccard 相似度。
2. 在相似度 ≥ `MATCH_THRESHOLD`（0.35，`core/plan.py:98`）时挑出最佳匹配的步骤。
3. 用 todo 的状态更新步骤状态，映射关系为
   （`_TODO_TO_STEP_STATUS`，`core/plan.py:244-251`）：
   - `completed` → `done`
   - `in_progress` → `in_progress`
   - `pending` → `pending`
   - `cancelled`/`canceled` → `skipped`
   - `blocked` → `blocked`
4. 已经是 `done` 的步骤绝不回退（一条走失的 `pending` todo 不会把它撤销）
   （`:212-213`）。
5. 未匹配上的 todo 会被计为漂移信号（todo 内容没有对应的计划步骤），并作为
   `n_unmatched` 返回。
6. 匹配按相似度从高到低应用，每个步骤只用一次，因此重复的 todo 不会争抢同一个步骤
   （`:202-207`）。第一个变成 `in_progress` 的步骤成为 `active_step`；如果没有，
   则取第一个 `pending` 步骤（`:215-223`）。

整条路径都是机械的——不调用 LLM。`apply_todowrite_sync`
（`core/plan.py:1253-1294`）会持久化更新后的计划并重写 PLAN.md，但如果没有那一行、
或存储的 `structured` 不符合 schema，它会原样返回 `{"skipped": "no_active_plan"}`
而不改动任何东西（`:551-554`）。

### 结转门禁（Carryover gate，R610，自 v2.4.0 起强制）

`plan_active` 是一个**单槽位**，因此替换计划正是已排布的工作可能无声消失的那个瞬间。
这是一次真实的、有记录的损失（SELF-ITER 的 S1-S3 沉没事件：已经被批准的后续阶段从未
重新进入任何计划，在下一轮计划覆盖该槽位的那一刻就消失了——`core/plan.py:443-458`）。
通往那个槽位的两道门都设了闸，而且刻意**没有强制标志（force flag）**
（`core/plan.py:455-456`，并在 `:647` 的错误文案里重申）：没有记录理由的丢弃，正是
这道门禁存在的目的所要杀死的失效模式。因此 `plan-set` 只接受
`--raw / --raw-file / --from-refiner`（`cli/mem.py` 的 `cmd_plan_set`）——根本没有
可以传进去绕过它的东西。

#### 入口 1 —— REPLACE（`/cc-mem plan-set --from-refiner` → `core.plan.apply_refined_plan`）

`check_carryover(old_structured, new_plan)`（`core/plan.py:754-871`）收集旧计划的
未完成步骤——状态属于 `pending | in_progress | blocked`（`_UNFINISHED_STATUSES`，
`:461`；选择器 `unfinished_steps` 在 `:465-472`）——并要求其中每一个要么

  (a) **被自动结转**：与新步骤的裸 `title`，或与其 `title + notes` 的
      shingle-Jaccard 相似度达到 `_carryover_bar` —— 非 CJK 标题为 0.5
      （`CARRYOVER_MATCH_THRESHOLD`），任一标题含 CJK 连续段时为 2/3
      （`CARRYOVER_MATCH_THRESHOLD_CJK`）：帮了合并侧写入器的 CJK 二元组
      底层会**放松**这道门，而门的误匹配意味着静默丢步骤（实测：325 个
      单字替换里 98 个从 FLAGGED 翻成自动结转，包括三十秒 vs 六十秒——
      相反的事实）（自 v2.4.1 起两者都是候选，`:492-506` —— 只与
      `title+notes` 比较，会让一段很长的 notes 把一个完全相同的 title
      稀释到阈值以下，这是在该门禁的第二次真实替换 R610 中发现的；
      `title+notes` 这个候选被保留下来，是为了让一个被折叠进另一步骤
      notes 里的步骤仍能被结转），要么

  (b) **被 disposition 记录**：存在一条顶层 `"dispositions"` 条目，其 `old_title`
      以 ≥ 0.5 的相似度匹配上，`action` 属于 `done | dropped | merged | carried`，
      且 `reason` **非空**——`detail` 被接受为 `reason` 的同义词
      （`:507-538`，同义词处理在 `:528-529`）。

dispositions 是从**原始的 refiner 字典**里读的，在归一化之前（`apply_refined_plan`
在 `:635-637` 传的是 `structured`，不是 `normalised`；理由见 `check_carryover` 的
docstring，`:485-487`）：schema 保持只增不减，因此更老的 refiner 的输出在没有未完成
步骤的计划上仍然可用。

任何违规都会抛出 `ValueError`（`core/plan.py:639-647`）。`plan-set --from-refiner`
会捕获它，打印 `[FAIL] refined plan rejected: …` 并以 1 退出
（`cli/mem.py` 的 `cmd_plan_set`）。什么都不会被写入——旧计划原封不动地留在那里。

##### 这道门**管不到**的部分——`success_criteria` 失配播报（v2.5.6）

门的宪章是「换计划不许丢步骤」：它只读 `steps`。`success_criteria` 在射程外，
2026-08-05 这件事应验了——一次真实替换顺利通过步骤门，而**十条判据里蒸发了两条**，
其中一条是已达成却从未记录的发布闸。`context` 同理。

`unmatched_criteria(old_structured, new_plan)`（**有意追加在 `core/plan.py` 末尾**——
放在 `check_carryover` 旁边会让本文档里约 60 条行号引用集体腐烂）返回每一条
「其对替换方 `success_criteria` **加上 `goal` 与 `context`** 的最佳
shingle-Jaccard 低于同一个 `_carryover_bar`（0.5，CJK 为 2/3）」的旧判据。
被并进新 context 的判据
算作已继承：有损的存活仍是存活，把它也报出来只会训练读者忽略这条播报。

这**刻意不是**第二道拒写门。判据会被改写、合并、翻译、因达成而退役；一份英文计划
被中文计划取代时自动继承率为零，硬门会让正常的计划演进无法进行。`cmd_plan_set`
在调用 `apply_refined_plan` **之前**快照旧计划（此后它只存在于
`.ccm/.plan_history/`），然后打印：

```
[!] carryover advisory — 2 of 10 previous success_criteria have no close match
    in the replacement.
    The R610 gate covers `steps`, so these did not block the write. Retiring a
    criterion is fine; losing one silently is not. Confirm each was deliberate:
      - no XXXXXX placeholder survives into a shipped string
      - all seven machine-breaking defects are fixed and re-verified
    `context` is free text and is NOT compared at all — re-read it yourself.
    The outgoing plan is archived under .ccm/.plan_history/.
```

播报在最后一行**点名自己的盲区**：`context` 是自由文本，从不比对。由
`tests/test_plan_carryover.py` §7 钉死（核心结果、context 并入的抑制、以及
CLI 确实把它打出来了——一个没人呈现的核心函数，等于换了个方式继续沉默）。

一次拒绝在用户看来是这样的：

```
[FAIL] refined plan rejected: carryover gate REFUSED — the outgoing plan still has
unfinished steps not accounted for in the replacement:
  - step #4 'Add CSRF protection' — not in the new plan and no disposition
  - step #6 'Write integration tests' — disposition has no reason (a drop without
    a recorded reason is the exact failure mode this gate kills)
Every unfinished step must either appear in the new plan's steps (auto-carry by
title similarity) or be listed in the new JSON's top-level "dispositions":
[{"old_title": ..., "action": "done|dropped|merged|carried", "reason": ...}].
There is no force flag by design.
```

三种违规形态，逐字取自 `core/plan.py:522-538`：

| 条件 | 消息 |
|-----------|---------|
| 没有相似的新步骤，也没有匹配的 disposition | `step #N '<title>' — not in the new plan and no disposition` |
| 有 disposition，但 `action` 不在枚举内 | `step #N '<title>' — disposition action '<x>' not in ('done', 'dropped', 'merged', 'carried')` |
| 有 disposition，但 `reason` 为空 | `step #N '<title>' — disposition has no reason (a drop without a recorded reason is the exact failure mode this gate kills)` |

**如何解决一次拒绝。** 不要试图绕过去；没有路可绕。针对每一个被点名的步骤，从下面
选一种：

- 这个步骤依然成立 → 把它加进新计划的 `steps`（任何超过结转阈值的标题都会
  自动结转；原样复用旧标题一定有效）。
- 这个步骤其实已经交付了 → 添加
  `{"old_title": "<旧计划中的确切标题>", "action": "done", "reason": "<证据 —— commit、file:line、测试>"}`。
  refiner 被明确要求：在原始文档或当前 PLAN.md 中没有证据时，绝不宣称 `done`
  （`agents/plan-refiner.md:79-82`）。
- 这个步骤被放弃了 → `"action": "dropped"`，并给出不再需要它的理由。
- 这个步骤被折叠进了另一个步骤 → `"action": "merged"`，并在理由中点名吸收它的那个
  步骤。
- 不确定 → `"action": "carried"` **并且**把它重新列进 `steps`。这是 refiner 被规定
  的默认动作（`agents/plan-refiner.md:79-82`）。

然后把 JSON 重新经 `/cc-mem plan-set --from-refiner` 管道输入。

#### 入口 2 —— CLEAR（`/cc-mem plan-clear`）

当 `unfinished_steps(row["structured"])` 非空且没有给出 `--reason` 时，
`cmd_plan_clear`（`cli/mem.py:1783-1813`）会拒绝并以 1 退出（`:788-798`）：

```
[FAIL] carryover gate: the active plan still has 2 unfinished step(s):
    - #4 Add CSRF protection
    - #6 Write integration tests
  Clearing would silently sink them. Re-run with --reason "<why these steps are
  being dropped>" -- the reason is recorded in .ccm/.plan_history/.
```

解决办法是带上 `--reason "<why>"` 重新运行。这个理由不是装饰品——它会被写进归档
载荷。只有在门禁通过之后，命令才会归档、执行 `db.clear_plan_active(pid)`，并删除
`.ccm/PLAN.md` + `.ccm/.plan_raw.md`（`cli/mem.py:1806`）。

#### 兜底 —— 只追加的计划历史

每一份被替换掉的计划——哪怕它的 disposition 记录得干干净净——都会由 `archive_plan`
（`core/plan.py:983-1049`）归档到

```
.ccm/.plan_history/plan_<YYYYmmddTHHMMSS>_<replace|clear>.json
```

其中包含 `archived_at`、`event`、`reason`、`structured` 形式、`raw` 文本和
`active_step`（`:556-563`）。调用点在 `core/plan.py:1025`（替换，无理由字符串）与
`cli/mem.py` 的 `cmd_plan_clear`（清除，带用户的 `--reason`）。既没有 `structured`
也没有非空白 `raw` 的行会被跳过（`:549-550`）。

归档写入失败是**非阻塞**的：它会向 stderr 打印
`[WARN] plan history archive failed (<err>) — proceeding; the carryover gate
already enforced accounting` 并返回 `None`（`core/plan.py:567-575`）。代码里把理由
说得很明白：门禁的 dispositions 才是首要的反丢失保证，因此让每一次计划操作都卡在
一次归档磁盘打嗝上，等于把兜底机制变成对规划本身的拒绝服务。

### 提示阈值

硬编码的默认值在 `core/plan.py:1299-1315`（`turn_threshold=8`、`edit_threshold=12`）；
Stop 钩子调用 `should_nudge_guardian(plan_row)` 时不传任何覆盖值
（`hooks/stop.py`）。这些**没有** `config.json` 键——要改就改函数签名的默认值，
或者显式传关键字参数。敏感调用的 `+20` 加分同样是硬编码的
（`hooks/post_tool_use.py`）：

| 触发                                      | 阈值           | 发出什么 |
|------------------------------------------|----------------|-------------------|
| `turns_since_last_guardian` 达到          | 8（默认）      | Stop **拒绝**（`plan-drift`） |
| `edits_since_last_guardian` 达到          | 12（默认）     | Stop **拒绝**（`plan-drift`） |
| 检测到敏感 bash 工具                       | 不适用（经 +20 加分立即触发） | 下一回合 Stop 拒绝 |
| `needs_refine = 1`                       | 不适用（立即） | Stop **拒绝**（`plan-unrefined`） |
| 某条活跃指令闲置超过阈值                    | 25 轮          | Stop **拒绝**（`directive-idle:<slug>`） |

在没有 schema 合法的计划时，`should_nudge_guardian` 返回
`(False, "no_active_plan")`；当一份原始计划正等待精炼时返回
`(False, "needs_refine_first")`，因此两种条件绝不会撞车（`core/plan.py:708-711`）。

#### Stop 钩子可以拒绝本轮（v2.11.0）

**在 v2.11.0 之前这一节写的是相反的意思，而那句话比它描述的行为多活了一个版本。**
曾经为真的部分：钩子只发一行软性建议状态行，且被限流成每五轮一次——而这恰恰就是
一份原始计划能无限期不被精炼、同时 `PLAN.md`、`plan-status` 和漂移守卫全都在按
*上一份*计划回答的原因。

现在为真的部分：`core.plan.blocking_reasons` 返回必须让本轮停下的条件，
`hooks/stop.py:_emit_block` 把 `{"decision": "block", "reason": …}` 写到 stdout
并以 0 退出。有六条性质是承重的，任何改动都不得破坏其中任何一条（这一句曾在
列表已有四条时还写着"三条"——现在计数与列表一同维护）：

1. **逃生预算一定会释放——而且按「一次事件」计数。** 对**同一条件集**连续拒绝
   `_BLOCK_MAX_CONSECUTIVE` 次之后，钩子退化成醒目的建议；而且 `_block_attempt`
   以条件键的摘要为计数键，所以修好一个问题绝不会花掉下一个问题的预算。如果这个
   尝试计数**根本无法落盘**，钩子就**改为建议而不是拦截**——一个逃不出去的 block
   比没有 block 更糟。一次被允许结束的 Stop 会终止这段连击（`_block_reset`，
   v2.14.0）：之后对任何条件集的下一次拒绝都从第 1 次重新计。在此之前，计数会活过
   那次解决问题的 Stop，于是一个会回来的条件——`plan-drift` 每 8 轮就回来一次——
   会从上次停下的地方接着数，三次已被解决的拒绝之后，整个会话就再也没有任何强制
   执行了。
2. **一次拒绝写到 stdout 的必须是一份 JSON 文档，不能有别的。** 每轮的状态行先
   构造好，只有在允许本轮结束的路径上才输出。前面挂着散文的 `{"decision": …}`
   不是 JSON，而一个解析不了它的宿主就看不到任何决策——那正好把这个版本要终结的
   "建议"给悄悄恢复了。
3. **只有 LIVE 的计划才会被强制执行。** `clear_plan_active` 会保留一行墓碑
   （这正是让 `revision` 跨越清除仍单调递增的机制），所以钩子检查的是 `raw` /
   `structured` 非空，而不是这一行是否存在。没有计划的项目永远不会被强制执行，
   这也正是"选择加入"才会开启强制执行的原因。
4. **指令闲置度用的是一个单调时钟。** `turns_idle` 等于
   `plan_active.turns_total - directives.turns_at_touch`（都是 v9 新增，都只增
   不减）。**绝不可**改回用 `turns_since_last_guardian` 来量：`/cc-mem plan-check`
   和每次计划替换都会把那个计数器清零，于是一条真正三十轮没人碰的指令，只要有人
   跑一次 guardian 检查就显得刚被照料过——这个账本恰好赦免了它存在意义所在的那种
   疏忽，而且是静默的，因为"没有指令闲置"和"账本在正常工作"长得一模一样。此前有
   两种写法都失败过，而且都看着像对的：v2.11.0 拿项目计数器给每条活跃指令打戳
   （于是几秒前记下的指令就会拦住这一轮），v2.11.1 的"自 guardian 窗口开启以来
   是否被触碰"守卫修好了那个，却从它仍在读的计数器那里继承了重置问题。可重置的
   计数器量不了"已流逝的疏忽"；正解是一个永不重置的时钟，而不是对着会重置的那个
   做更聪明的比较。

   这个戳由 `db.upsert_directive`、`db.edit_directive` 与
   `db.set_directive_status` **在它们各自的 `BEGIN IMMEDIATE` 内部**写入，从
   数据库读取而不是由调用方提供。不要把它外推给调用方：每个调用方都得知道
   "闲置以计划轮次计"，而忘掉的那一个会写出一条永远不可能被判为闲置的行。
   状态变更同样打戳——否则一条被重新打开的指令会立刻"闲置"了它关闭期间流逝的
   全部轮次。

5. **闲置强制执行跳过无法被"推进"的东西（v2.12.0）。** 两种形态，都来自
   Autoshop 的实地报告：当时让 block 闭嘴的唯一办法是重新陈述那条指令——而这
   会虚增 `times_stated`，账本仅有的那个重要性信号：

   - **`status = 'blocked'`** 把一条正在等**用户**的指令停靠起来（等材料、
     等决定）。闲置扫描只读 `status='active'` 的行，所以停靠的指令什么都不
     累积；`/cc-mem directive-edit <slug> --status active` 解除停靠。blocked
     不等于关闭：`directive-close` 及其证据门原封不动，而
     `directive-edit --status` 只接受 `active`/`blocked`，因此编辑这扇门
     绕不过那道证据门。
   - **`kind = 'constraint'`** 标记一条长期**禁令**——"绝不提交 token"——它在
     构造上就没有可记录的正向动作。它的成功就是什么都没发生；它靠被注入来
     生效，而不是靠被"推进"，所以 `core.plan.blocking_reasons` 整个跳过这个
     kind。这个跳过住在 `blocking_reasons`（策略点）里，而不在闲置扫描里——
     只有一处，否则两处会漂移。

     **"靠被注入来生效"在 v2.12.2 之前是一句没有机制的宣称。** CLI 会列出
     账本，Stop 钩子会数它的闲置轮数，但没有任何代码路径把一条指令放到模型
     面前：README《加上它之前与之后》种入了一条约束型指令，量到它到达会话的
     次数是零。现在它从两条路都能到达模型——SessionStart 注入的**第一层**
     （`session_start._build_directives_layer`：约束优先、其次按重复次数、每行
     一条且经过中和、超预算的行被跳过而不是整层丢弃）和 PLAN.md 的
     `## Standing directives` 段（`plan._render_directives_section`，没有计划
     时也渲染，因为账本比计划活得久，而守卫读的正是 PLAN.md）。注入清单记录
     `directive_slugs`，因此 `/cc-mem inject-show` 能说出哪些指令到达了模型。
     闸门：`tests/test_directive_enforcement.py` §7 与
     `falsify --case r12directiveinject` / `r12directiveplan`。

6. **一次编辑不是一次陈述。** `times_stated` 是重要性信号，`directive-list`
   按它排序，所以唯一可以累加它的路径是 `directive-add`（一次真正的重新
   陈述）。`db.edit_directive` 修正 `demand`/`quote`/`kind`/`status`，但不碰
   计数与 `last_seen_at`，并且**拒绝创建**（一扇会创建的编辑门就是第二个
   默认值不一致的 upsert）。实测的需要：九次经 `directive-add` 做的引用修复
   虚增了九个计数，被**编辑**最多的指令反而排到了被**要求**最多的指令前面。

关闭开关：`CC_MEMORY_PLAN_ENFORCE=0`（`core.plan.enforcement_enabled`）。
要显式请求一次 guardian 巡检，仍然用 `/cc-mem plan-check`；它会先刷新 PLAN.md
好让子代理读到当前状态，然后重置计数器（`cli/mem.py:834-836`）。

存储的指令文本在写入时（`db.upsert_directive` → `clean_for_storage`）**和**输出时
（`render_block_reason` → `neutralize_document`）都会被转义。block 的 `reason`
是作为决策回喂给 Claude 的，这使它比 PROGRESS.md 具有更高的权威等级——在这两半
都到位之前，一条 `demand` 能伪造 `<system-reminder>` 的指令会原样抵达模型。

#### 指令引用计划步骤要用标题，绝不用编号（v2.12.0）

指令的寿命长于任何一份计划；步骤 id 按位置分配，随分配它的那份计划一起死亡。
"先做步骤 12"这样的文本把一条长寿的行钉在了一个短命的坐标上，而 R610 门帮不上
忙：它保证的是替换时不丢任何**步骤**，对另一张表里指向某个步骤的文本只字未提。
在 Autoshop 项目实测（2026-08-25）：两次重排（23 → 12 → 14 步）留下了 **11 条
死引用**（编号已不存在）和 **4 条被无声重定向的引用**——它们仍能解析、读起来
没毛病、指向的却是错误的工作，比死引用严格更危险。

这条规则是词法层面的，所以配套机制是播报而非闸门：

- **写入时**——`directive-add` 与 `directive-edit` 在文本匹配序数步骤引用时
  给出警告（`core/plan.py:_STEP_REF_RE`——`步骤 N`、`step #N`、裸 `#N`），
  此刻作者还来得及换成标题。
- **替换时**——`/cc-mem plan-set --from-refiner` 对每一条 ACTIVE 指令运行
  `core.plan.stale_directive_step_refs`，对照出入两份步骤表，把每个发现点名
  为 `dead` 或 `retargeted`（标题是否被结转，按结转门自己的门槛 `_carried`
  判定）。刻意做成播报：腐烂发生在账本里，为它拒绝那份**计划**等于把修复
  扣为人质。它打印的修复路径是 `directive-edit`——不加计数的那扇门，于是
  九次修复不再重排账本。

### 子代理契约

- **`plan-refiner`**（`agents/plan-refiner.md`）：一次性的 raw→structured 转换。
  工具：Read、Grep、Bash；模型 `haiku`（`:4-5`）。输出：stdout 上的 JSON，别无其他
  ——它必须能被 `json.loads()` 直接解析，不要代码围栏，不要评注（`:25-26`、`:84`）。
  它的归一化规则：除非原文另有标记，否则状态默认为 `pending`；步骤是去掉前导编号的
  祈使短语；近似重复的步骤会被合并；计划模式的元闲聊会被丢弃；成功判据必须可测试；
  `context` 只承载持久的“这为什么重要”的信息；步骤数在 1 到 12 之间（`:45-60`）。
  自 v2.4.0 起，它**还**必须读取当前计划（`plan-show` / `PLAN.md`），并为它没有结转
  的任何未完成步骤发出 `dispositions`——否则存储层会拒绝这份 JSON
  （`agents/plan-refiner.md:61-82`）。
- **`plan-guardian`**（`agents/plan-guardian.md`）：漂移检查。
  工具：Read、Grep、Bash（仅限只读操作）；模型 `haiku`（`:4-5`）。输出：一个 ≤150 词
  的固定报告块——`ACTIVE STEP / ALIGNMENT / EVIDENCE / DRIFT / NEXT ACTION`
  （`:24-35`）。它先读 PLAN.md，再读 PROGRESS.md，可以用 Grep/Bash 对着工作树核实
  断言，并且会做校准：为目标服务的小绕路是 `on-track`，真正偏离计划的工作是
  `drifting`，已经不再匹配现实的计划是 `replan-needed`。它从不编辑、从不推送，并且
  在 PLAN.md 缺失或非法时报告 `replan-needed` 并停止（`:37-52`）。

两者都默认用 `haiku` 模型——它们是聚焦的、低上下文的任务。两者都随插件放在 `agents/`
目录里，因此在市场安装和独立安装下都能被解析到。

### CLI 界面

```bash
/cc-mem plan-status              # 计数器 + 新鲜度摘要（不调用 LLM）
/cc-mem plan-show                # 重新生成并打印 PLAN.md
/cc-mem plan-set --raw '<text>'  # 存储原始文本，标记 needs_refine
/cc-mem plan-set --raw-file FILE # 同上，但从文件读
/cc-mem plan-set --from-refiner  # 从 stdin 存储结构化 JSON
                                 # → R610 结转门禁；被拒时以 1 退出
/cc-mem plan-check               # 重置计数器 + 打印 guardian 调用提示
/cc-mem plan-replan              # 对已存储的 raw 重新置位 needs_refine
/cc-mem plan-clear               # 丢弃计划 + 删除 PLAN.md。
                                 # 会先归档到 .ccm/.plan_history/，并且在
                                 # 存在未完成步骤时拒绝（以 1 退出），
                                 # 除非给出 --reason "<why>"（v2.4.0）。
```

处理函数：`cli/mem.py:704-709`（show）、`:712-737`（status）、`:740-775`（set）、
`:778-807`（clear）、`:810-821`（replan）、`:824+`（check）；解析器接线在
`:1041-1059`，分发在 `:1081-1083`。`plan-status` 区分三种状态：完全没有行、有 raw
但未精炼的计划（打印 raw 的长度并告诉你去调用 `@plan-refiner`），以及已精炼的计划
（目标、N/M 已完成、活动步骤、上次精炼时间、上次 guardian 检查时间、两个计数器）。
如果没有存储任何 raw 文本，`plan-replan` 会以 1 退出并失败（`:815-817`）。

### 敏感工具清单

`core.plan.is_sensitive_tool_call`（`core/plan.py:1335-1358`）会标记以下 Bash 模式
——对 `command` 输入做大小写不敏感的子串匹配，且仅限 `Bash` 工具——从而立即触发一次
guardian 提示加分（+20 次编辑）：

- `git push`、`git push -f`、`git push --force`
- `rm -rf`、`drop table`、`drop database`
- `npm publish`、`cargo publish`、`pypi-upload`、`twine upload`
- `kubectl apply`、`terraform apply`、`ansible-playbook`

+20 的语义写在 `hooks/post_tool_use.py` 里：“这一个动作携带的漂移风险相当于
约 20 次普通编辑”，因此下一次 Stop 钩子会立刻浮出一条 guardian 建议。cc-memory
**不会**阻断这些调用；它只做标记（`core/plan.py:721-726`）。这个加分和普通的编辑
加分一样，在没有活动计划行时是空操作。

需要时请在 `cc_memory/core/plan.py:is_sensitive_tool_call` 里扩充这份清单。

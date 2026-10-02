<!-- i18n-source: CONTRACTS.md | sha256: 83b79843fc2169b7 | version: 2.16.0 | translated: 2026-10-02 | translation: 704b7427b4dc8e9d -->
> [English](CONTRACTS.md) · **简体中文**

# cc-memory — 契约（Contracts）

本插件**在代码里**强制执行三条不变量，而不是靠约定。每一条都有唯一的收口函数、唯一
的生成产物，以及自动化断言。违反其中任何一条都是 bug，不是风格选择：这些机制之所以
存在，是因为对应的失效模式在生产中被真实观察到过（v2.0 的堆叠重复记忆、v2.0 无人
阅读的交接、v2.4.0 之前被无声沉没的计划步骤）。

断言分布在哪里：`tests/smoke_test.py` 覆盖反补丁决策、PROGRESS.md 的整篇重写与
「只填空字段」刷新，以及计划生命周期（v4 迁移 → 捕获 → 精炼 → TodoWrite 同步 →
PLAN.md）。**R610 结转门禁**由它自己的套件 `tests/test_plan_carryover.py`（v2.16.0 时 28 项
检查）覆盖，所以两个都要跑。`tests/test_surfaces.py`（v2.5）覆盖这些契约被触达时
所经过的发布表面。

本文件取代 2.4.3 之前的三件套 `docs/MEMORY_RULES.md`、`docs/HANDOFF_PROTOCOL.md` 和
`docs/PLAN_PROTOCOL.md`；版本字符串的权威来源是 `cc_memory/core/version.py`。

**关于 `file:line` 引用。** 它们现在有强制手段了：`tools/citation_check.py`
（v2.5.2），它在 `tests/run_gates.py` 里是一道独立的发布门禁，同时也在
`tests/smoke_test.py` 内部运行。对每一条引用，它用 `ast` 解析出
上下文散文里点到的符号，然后断言被引用的行号区间覆盖了该符号的定义，或者至少提到了
它。第一次运行就查出 **594 条引用里有 163 条已经失效**，并已机械修复。如果一条引用
所在的句子里没有任何可以唯一解析的函数、类或 ALL_CAPS 常量，它只做边界检查——
在文件之内且非空行，汇总里写作 "NOT verified against a symbol"（v2.14.0）——
所以请把这里的行号当作线索，把**符号名**当作事实：
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
> 当一条新记忆 M 描述的事实与一条已存在的记忆 E 相同时，写入器会**替换 E**——
> 归档它，并写入经 `supersedes_id` 链接到它的 M——而不是在它旁边追加一条独立的行。
> 绝不存在两条活动行描述同一个事实的情况。

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
（`llm/memory_writer.py:247-332`，`upsert_smart`）。

```
0. content = clean_for_storage(content.strip())   (upsert_smart, memory_writer.py:265)
   若 len < MIN_CONTENT_LEN (10) 则 SKIP（reason: too_short）。   (:266-267)
   把 {decision,result,config,bug,task,arch,note} 之外的 category
     强制为 "note"。                                             (:269-270)
   把 importance 钳制到 1-5；tags 默认为 []。                     (:271-272)

0b. 从这里到第 5 步的每一个**决策**都在同一个 `BEGIN IMMEDIATE` 事务里做出并写入
   —— `db.reconcile_upsert`（core/db.py:1807-2000）；只有第 1 步的 reinforce 合入
   在它提交之后才运行。`upsert_smart` 以参数形式提供
   策略：阈值、最佳候选函数（`_make_pick`）与标签并集规则
   （`_merge_fields`）；原子性归数据库所有。在这个事务出现之前，两个并发
   保存同一句话的写入方会同时看到空表并各自插入（实测：
   actions=['inserted','inserted']，同一哈希两条活跃行）。

1. 计算 content_hash = sha256(content.strip().lower())[:16]。
                                                     (db.compute_content_hash)
   精确哈希命中由事务**内部**的 SQL 检查：
       → 不新增行、也不重写正文：文本确实重复。但 importance 与
       tags 并不参与哈希，所以它们仍然按第 3 步同样的方式合入
       命中行——由 `_fold_into_hash_match` 在 `reconcile_upsert` **提交之后**、
       在它自己的连接上完成（两个同时的重述可能漏掉一次提升，但绝不会丢失
       或复制该行；docstring 写明了这个窗口）：
       importance=max(new_imp, existing_imp)，
       tags=_merged_tags(existing_tags, new_tags) —— 不附加动作标记（既没有
       merge 也没有 supersede），topic 与正文也一律不动。
       Action：确实改变了该行时为 "reinforced"；重述未带来任何新信息
       时为 "skipped"（此时什么都不写，连 updated_at 也不动）。
       两种情况的 Reason 都是 hash_match。
       理由：文本完全重复 —— 但带着**更高** importance 的重述，是一条事实
       能得到的最强排序信号；而这条分支以前会直接丢掉它，反而是稍微
       改写过的重述（第 3/4 步）能把它保留下来。
   （`db.find_by_hash` 只作为 IntegrityError 的恢复路径存在——竞态的
   落败方重读胜者，而不是抛异常。）

2. 在作用域内查找最相似的 ACTIVE 记忆（`_make_pick`）：
       主作用域：  topic == new_topic
       兜底作用域：category == new_category, is_active = 1,
                   ORDER BY updated_at DESC LIMIT max_candidates
                   —— 在 topic 为空，或按 topic 的查询没有返回任何行时触发
       跨类别：    两个作用域都没有达到 HIGH_SIM 时，其他**所有**类别的
                   活跃行也会被打分，且只在 sim >= HIGH_SIM 时采用其一
                   （v2.16.0，C2：同一句话归到两个类别下仍是一个事实）；
                   MID 档永远不跨类别
   相似度是基于 `core.textsim.shingle_set` 的 Jaccard —— 非 CJK 文本用
   字符三元组，CJK 连续段用字符**二元组**（十个汉字里改一个字在三元组下
   只得 0.4545，低于 MID_SIM，永远无法 merge 或 supersede）。至多对
   MAX_CANDIDATES_TO_SCAN (500) 个候选打分。令 sim = 最大相似度。

3. 若 sim >= HIGH_SIM (0.80)：
       → MERGE，由同一事务内的 SQL 完成：归档 E，再插入带
       supersedes_id=E.id 的新行，并沿用 E 的 created_at（一次重述不会
       重置衰减时钟）。
       content=new_content, importance=max(new_imp, existing_imp),
       topic=new_topic or existing_topic,
       tags=_merged_tags(existing_tags, new_tags, ["merged"])
       # 与**被归档行**的 tags 做并集，绝不整体替换 —— 来源标签
       # （["observer","realtime"]、["mcp"] 等）得以继承 —— 并以
       # MAX_TAGS (32) 封顶，因为 memory_add 是模型可调用的。
       Action: "merged"；`id` 是新行，`old_id` 是被归档的那一行。
       理由：“本质上是同一句话”—— 保留最新的措辞。
       直到 v2.14.1，这条分支都是**就地**重写 E 的正文，不留下任何指向被替换
       文本的东西；shingle 相似度分不清 三十秒 与 六十秒，所以一个几乎相同的
       **错误**更正恰恰最可能走这条分支。自 v2.15.0 起可以经由取代链恢复
       （写入器的模块 docstring，llm/memory_writer.py:13-23）。

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
   同步 —— **仅当**传入了 `memory_dir` **且**确有写入落地时（有条目被
   inserted、merged、superseded 或 reinforced；v2.16.0，A7——一批纯跳过只会
   得到逐字节相同的文件）(memory_writer.py:335-383)。单独调用 `upsert_smart`
   绝不会重新生成。绝不允许 MEMORY.md 漂移。
```

`upsert_smart` 返回
`{"action": "skipped"|"reinforced"|"merged"|"superseded"|"inserted", "id": ..., "similarity": ..., "old_id": ...}`
（`memory_writer.py:247-332`）；跳过路径会额外带上 `"reason"`，取值为 `too_short` 或
`hash_match`，而 `"reinforced"` 的结果同样带着 `hash_match` —— 那是同一条分支在
声明它确实写入了。`upsert_batch` 把这些聚合成按动作分类的计数，外加一个
`results` 列表（`memory_writer.py:335-383`）；每个动作名都是预置的键，所以按名
读其中一个的调用方不会遇到缺失的键。

### 阈值与常量

`HIGH_SIM` / `MID_SIM`（0.80 / 0.50）只在 `cc_memory/core/textsim.py` 里写一次，
由 `cc_memory/llm/memory_writer.py` 以同名重新导出；自 v2.16.0 起 `HIGH_SIM` 同时是
**跨类别**的门槛——同一句话归到两个类别下仍是一个事实——写入器的扫描、
`core/consolidate.py` 的词法合并与语义提名都用它，而 MID 档永远不跨类别。与之并列的
还有写入器自己的 `MIN_CONTENT_LEN`（10）、`MAX_CANDIDATES_TO_SCAN`（500）和
`MAX_TAGS`（32）。`cc_memory/config.json`
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
   `supersedes_id` 链接。`db.get_supersede_chain(id)`（`core/db.py:2052-2067`）可以走一遍
   历史。不需要什么“记忆版 git blame”的黑魔法。

3. **MEMORY.md 过期。** 每次批量写入之后自动重新生成，避免了 v2.0 中观察到的“过期
   50 天”失效模式（当时 PreCompact 会写 MEMORY.md，而 Stop / 技能 / MCP / CLI 不
   会）。保存路径之外还有若干刷新点让它保持诚实：PreCompact 的尾部
   （`hooks/pre_compact.py:834`）、Stop 钩子的空闲整理（`core/idle.py:112`）、异步
   整理支路（`hooks/consolidate_async.py:286-287`）、`/cc-mem cleanup`
   （`cli/mem.py:1394`）以及 `ccm-load` 技能（`skills/ccm-load/SKILL.md:332`）。

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
| `PreCompact` 钩子 | `upsert_batch(db, pid, sid, extracted_list)`（`hooks/pre_compact.py:704`）; 不传 `memory_dir`：压缩只在末尾、写完关键词与会话摘要之后渲染一次 MEMORY.md（`regenerate_memory_index`，`hooks/pre_compact.py:834`） |
| `Stop` 观察者 | `upsert_batch(db, pid, session_row, observer_list, memory_dir)`（`hooks/stop.py:530`）; 所属会话的 `sessions` 行，PreCompact 尚未认领时由观察者先认领（v2.16.0） |
| `SessionStart` 追溯保存 | `upsert_batch(db, pid, sid, memories, memory_dir=memory_dir)`（`hooks/session_start.py:1488`）; 处理此前未保存的会话 |
| `/save-memories` 技能 | `upsert_batch(db, pid, None, memories, memory_dir=mem_dir)`（`skills/save-memories/SKILL.md:180`）; `mem_dir` 是 `core.layout.memory_dir(project)`，绝不是手写的路径拼接 |
| `mem.py add` CLI | `upsert_smart(...)`（`cli/mem.py:1250`）; 然后 `regenerate_memory_index(...)`（`cli/mem.py:1280`） |
| `mcp/server.py handle_memory_add` | `upsert_smart(...)`（`mcp/server.py:627`）; 然后 `regenerate_memory_index(...)`（`mcp/server.py:642`） |
| Dashboard UI 的 “Add Memory” | `upsert_smart(...)`（`ui/dashboard.py:1728`）; 然后 `regenerate_memory_index(...)`（`ui/dashboard.py:1735`）; 自 v2.2 起改为路由。`ui/dashboard.py` 中没有任何 `db.insert_memory` 调用。 |
| Dashboard UI 的 “Save Session” | `upsert_batch(...)`（`ui/dashboard.py:1952`） |
| Dashboard UI 的 “Init Project” 扫描 | `upsert_batch(db, pid, None, batch, memory_dir=memory_dir)`（`ui/dashboard.py:2128`） |
| web_viewer 的 POST `/api/memory` | `upsert_smart(...)`（`ui/web_viewer.py:1015`）; 然后 `regenerate_memory_index(...)`（`ui/web_viewer.py:1030`） |

### 整理兜底的例外（Consolidation backstop，v2.3）

**整理**流水线（`core/consolidate.py`）是“每一次写入都经由 `memory_writer` 路由”这条
规则的成文例外。它是清理兜底，不是保存路径，而且它操作的是**已经存在**的记忆：

- `semantic_dedup`（LLM 判定的同事实归并，`consolidate.py:454-593`）经由
  `db.apply_dedup_verdict` 写入（每组一个 `BEGIN IMMEDIATE`），`detect_obsolete_llm`
  （新事实与旧事实矛盾，`:1007-1100`）直接调用 `db.archive_obsolete`。它们绝不从零创造面向用户的内容——幸存行本来就已经存在；
  落败者被归档（`is_active=0`），并带一个向前的 `supersedes_id` 链接
  （`db.archive_obsolete`，`core/db.py:2463-2592`），因此血缘依然可追溯、可恢复。
  自 v2.9.0 起这个链接用 `COALESCE` 写入，绝不覆盖已有的：由更早一次 SUPERSEDE
  产生的落败行，本身已经指向它所替代的那一行，覆盖会让那个更旧的版本从任何链
  游走中都不可达（实测：链 `[2,1]` 变成 `[2,3]`）。该槽位记录它学到的**第一条**
  血缘事实；槽位已被占用时，替代关系改为写进日志。
  `semantic_dedup` 在写入之前会并上幸存行原有的 tags（`consolidate.py:567-582`）；
  自 v2.8.0 起 `upsert_smart` 也这样做（`llm/memory_writer.py:_merged_tags`），所以
  这已经不再是两者的差别——归并分支此前写的是 `set(incoming + ["merged"])`，把幸存
  行自己的来源 tags 整个销毁了。
- `decay_and_archive`（引用感知的陈旧度安全网，`consolidate.py:937-981`）**只**归档
  非常老 + 低重要度 + 从未被注入过的行——一张零误归档的安全网。有效年龄是
  `now - COALESCE(last_referenced_at, created_at)`（`core/db.py:248-259`；
  `consolidate.effective_age_days`，`:62-77`）。
- **每一个整理阶段都是可逆的**（`is_active=0`，绝不 `DELETE`），自 v2.8.0 起
  `cleanup_garbage` 也包含在内。它曾经是唯一的例外，而且例外得很不是地方：它由 Stop
  钩子每五轮无人值守地跑，并按自己私有的 20 字符下限硬删除——而写入器的下限是 10，
  于是它销毁的正是四个入口刚刚接受下来的内容。实测：`/cc-mem add note "lr=3e-4 wins"`
  报告 `[inserted]`，五轮之后表里一行不剩。它现在从 `llm.memory_writer` 导入那唯一的
  下限，并经由 `db.archive_if_unchanged`（`core/db.py:2205-2240`）归档，与另外两个
  「快照判决」阶段一致。用这个变体而不是简单批量归档的原因是：本阶段的判决来自
  一次**独立事务**里的快照读，而 PreCompact 写入器是并发跑的，所以一行在这个窗口里
  被修好之后仍然会被归档——实测，刚刚归并进去的好内容被置为 `is_active=0`。以判决
  当时那个 `content_hash` 作为条件，就把陈旧判决变成了空操作。
  `db.delete_memories`、`db.bulk_archive`、`db.archive_memory` 已在 v2.16.0 删除：
  代码树里没有任何调用方，而一条只存在于 schema 里的 DELETE 就是一条前面没有闸门的
  清除路径。`/cc-mem archive <id>...` 是 v2.8.0 新增的**用户侧**退役入口，
  它同样只归档：`sql` 是只读的，`add` 只在相似度够高时才归并，所以一条被发现是**错**
  的记忆此前根本没有受支持的出口。
  `merge_near_duplicates`（`consolidate.py:265-337`）同样经由 `archive_if_unchanged` 归档，可逆，
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
  （10 分钟）为反复失败的工作者的重启设了上界。探针施加的是工作者自己的陈旧锁
  时限（v2.14.0）：它从 `consolidate_async` 导入 `_STALE_LOCK_S` 并比较锁的年龄，
  于是年轻的锁推迟拉起，而被遗弃的锁——工作者中途被杀——不再永久否决它
  （实测：一把两小时前的锁让三次 Stop 的 kick 都停在 False）。`_acquire_lock`
  仍是唯一的策略点；探针自己不持有任何锁常量。
- **手动**（`/cc-mem consolidate`），自 v2.12.0 起它也经由那**唯一**的共享
  写入方（`core.consolidate.write_consolidation_marker`）盖章标记——此前从不
  盖，于是一次手动运行之后探针仍然读到"到期"，跟着又跑了一遍多余的后台整理。
  `--deep` 会先循环 `semantic_dedup` 直到某一轮确认再无新发现
  （`core.consolidate.deep_dedup`；被否决的组在本次运行内会被记住，绝不重复
  送审），一份积了几个月的账就是这样一次清完的——每次运行 12 组的上限是为
  受预算门约束的后台通道定的尺寸，不是为积压定的。

### 不应该做什么

- 不要在任何保存路径里直接调用 `db.insert_memory`。它的 docstring 把它留给测试
  （`core/db.py:1715-1732`）; `supersede_memory` 与 `reconcile_upsert` 都在各自的事务
  里自己写 `INSERT`，而 `tools/contracts.py` 计算出的调用方集合为空。
- 不要自己撸一套 `"SELECT content FROM memories ..."` 去重。决策归
  `db.reconcile_upsert`（`core/db.py:1807-2000`），喂给它的是写入器的纯函数最佳候选
  选择器 `_make_pick`（`llm/memory_writer.py:160-182`），它取代了 `_find_similar`；
  `db.find_by_hash`（`core/db.py:2636-2644`）只作为 IntegrityError 的恢复路径存在。
  （并不存在 `db.find_similar`；匹配器就住在写入器里，按设计是私有的。）
- 不要手工“打补丁”改 MEMORY.md，也不要指望别的路径去刷新它。任何非平凡的状态变更
  之后都要调用 `regenerate_memory_index`。生成出的文件自带一条 DO-NOT-EDIT 横幅
  （`MEMORY_MD_NOTICE`，`core/prompts.py:101-106`）。

### 验证

在一个装了 cc-memory 的项目里：

```bash
# 显示取代链数量（证明反补丁正在生效）
/cc-mem stats

# 走一条具体的链
/cc-mem supersedes <memory_id>
```

`/cc-mem` 会自己解析 CLI 的位置（`commands/cc-mem.md:100-111`）：它先探测
`${CLAUDE_PLUGIN_ROOT}`，然后是 `$HOME/.claude/hooks/cc-memory`，并且在每一个根目录
下先尝试**嵌套**布局 `<root>/cc_memory/cli/mem.py`（市场 / 开发检出），再尝试**扁平**
布局 `<root>/cli/mem.py`。独立安装器把每一个子包直接拷进 `TARGET_DIR/<subdir>/`
（`TARGET_DIR`，`cc_memory/ui/installer.py:72`; `SUBPACKAGE_FILES`，`:77-91`;
`_copy_subpackages`，`:389`），因此独立安装**没有** `cc_memory/` 这一段路径——它的
CLI 是 `~/.claude/hooks/cc-memory/cli/mem.py`。在市场安装下，那棵树只保留 `logs/`
（已在本机核实），所以任何硬编码的 `python ~/.claude/hooks/cc-memory/.../mem.py`
调用在那里都会失败——本仓库就是一个市场 / 目录安装。

如果出现 `Supersede chains: N update events recorded`（`cmd_stats`，`cli/mem.py:981`），说明
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
`migrate_legacy_handoff`，`core/progress.py:808-826`，从 `hooks/pre_compact.py:568`
调用）。

### PROGRESS.md 就是唯一真相来源（SOT）

`.ccm/PROGRESS.md` 由 `cc_memory/core/progress.py:write_progress_md` 从 `progress`
SQL 行生成。Schema 见 `cc_memory/core/db.py:_MIGRATIONS:v3_progress`（`db.py:198-212`），
外加 `_MIGRATIONS` 里 `db.py:241-244` 处的两个 v5 会话标注列; §0 还会经 `db.get_recent_sessions`
读取 `sessions` / `session_summaries` 表（`core/progress.py:580`；`core/db.py:3087-3141`），
§4 / §5 则在渲染时读取实时存储（`plan_active`、`memories`）：

| 列 | 类型 | 主来源 · 兜底 |
|--------|------|---------------------------|
| `project_id` | INTEGER PK | `upsert_project` |
| `current_request` | TEXT | UserPromptSubmit 首条非脚手架提示、每会话一次（`strip_scaffolding`，`user_prompt.py:390`）→ PreCompact 的 `_first_user_request(window.head)`（`pre_compact.py:315-372`，在 `:724` 调用）；它会越过开头的 `queue-operation` / `attachment` 元数据行，并跳过内容为空的 user 行（v2.4.2），它自己的 `max_scan` 是 200，但 `window.head` 至多只有 `_DEFAULT_HEAD_RECORDS`（40，`core/extractor.py:134`）条记录，所以实际上限是 40 → `session_summaries.request`（`collect_progress_state`，`progress.py:280`） |
| `status_done` | TEXT | `session_summaries.completed`（`collect_progress_state`，`progress.py:274`）；PreCompact 用抽取结果里 `result` / `decision` 类的记忆填充（PreCompact 的 `main` 里那次 `insert_session_summary` 调用，`pre_compact.py:749-758`），仅当抽取没给出任何结论时才退回到观察到的 Edit/Write 路径列表。v2.8.0 以前**永远**走那条路径列表，于是 §2 的 “Done” 渲染出来是一份文件清单，而不是“做完了什么”。若为空，SessionStart 会补上（`_refresh_progress_row`，`session_start.py:1133-1134`） |
| `status_in_flight` | TEXT | `session_summaries.learned`，由抽取结果里 `arch` / `config` / `bug` 类的记忆填充（PreCompact 的 `main` 里那次 `insert_session_summary` 调用，`pre_compact.py:751-756`）。v2.8.0 以前 PreCompact 把它硬编码成 `""`，所以 §2 的 “In-flight” 无条件渲染成 `*(none active)*` —— 那是结构性的，不是因为真的没有在办事项 |
| `status_blocked` | TEXT | 显式的 `patch_progress(status_blocked=...)` —— 今天树内没有任何调用方这样做；它是留给外部工具的 API; 全仓库 grep 只能找到 schema 默认值（`_MIGRATIONS`，`core/db.py:204`; `upsert_progress` 的默认值，`:2887`）; 空播种（`collect_progress_state`，`core/progress.py:283`）; 以及读取处（`_render_status_lines`，`core/progress.py:453`） |
| `open_todos` | JSON | PreCompact 的 `extract_latest_todo_state(window)`（`core/extractor.py:532`），经 `ext["latest_todos"]`（`build_extraction`，`core/extractor.py:813`; 在 `pre_compact.py:773-779` 读进 `collect_progress_state`）→ SessionStart 第 3 级：挖掘 transcript（`_refresh_progress_row`，`session_start.py:1195-1205`）—— 自 v2.16.0（D6）起这是它唯一的兜底：把 `session_summary.next_steps` 按 `;` 切分的最后手段已被移除。只保留非 `completed` 的 todo（`collect_progress_state`，`progress.py:253-262`） |
| `plan` | TEXT | §4 的**旧版**兜底：§4 先渲染实时的 `plan_active` 行（`_render_plan_section`，`progress.py:373-440`）。这一列存的是 `session_summaries.next_steps` —— 若有最新 TodoWrite 的 pending 项则取自它，否则取自 LLM 抽取出的 `task` 类记忆（那次 `insert_session_summary` 调用，`pre_compact.py:729-759`）; 由 `collect_progress_state` 传播（`progress.py:277,285`）; 由 `_refresh_progress_row` 按“空则填”补齐（`session_start.py:1137-1138`） |
| `critical_context` | JSON | 已退役（v2.16.0）：写入 `[]`，无读者——§5 在渲染时读 `db.get_critical_memories`（`progress.py:_render_critical_lines`），于是被归档或被取代的行在下一次渲染就从文件里消失，而不是在快照里活下来；仪表盘的 Progress/Plan 页仍显示原始列 |
| `files_touched` | JSON | `observations` 表（`files_from_observations`，`pre_compact.py:721`; `collect_progress_state`，`progress.py:264-271`）; Stop 每回合打补丁（`_patch_progress_from_recent_obs`，`stop.py:672`）; SessionStart 第 2C 级（`_refresh_progress_row`，`session_start.py:1141-1150`）→ 第 3 级：对 transcript 跑 `extract_file_changes`（`_refresh_progress_row`，`session_start.py:1206-1212`） |
| `transcript_ptr` | TEXT | PreCompact 解析为绝对路径的 `transcript_path`（`collect_progress_state(transcript_ptr=…)`，`pre_compact.py:773-782`）→ 第 3 级 `find_latest_transcript(cwd, exclude_session_id=...)`（`_refresh_progress_row`，`session_start.py:1168-1170`） |
| `updated_at` | TEXT | ISO 时间戳，由 `upsert_progress`（`db.py:2872-2948`）与 `patch_progress`（`:2968-3007`）打戳 |
| `trigger_type` | TEXT | "auto" \| "manual"（PreCompact 把宿主自己的触发字符串原样透传 —— `collect_progress_state(trigger_type=trigger)`，`pre_compact.py:773-783`; `"precompact"` 只是 `collect_progress_state` 在 `progress.py:234-241` 的默认关键字参数，且总会被覆盖）; "stop"（`patch_progress`，`stop.py:672`）; "user_prompt" \| "resume_request"（`patch_progress`，`user_prompt.py:457-459`）; "session_start_refresh"（`_refresh_progress_row`，`session_start.py:1227`） |
| `current_session_id` | TEXT | 只由 `db.tag_progress_session` 写入（`db.py:3061-3085`）—— `tag_progress_session` 的调用方：PreCompact（`pre_compact.py:790`）、Stop（`stop.py:769`）、SessionStart（`session_start.py:1113`）、UserPromptSubmit（`user_prompt.py:446`） |
| `session_started_at` | TEXT | `db.tag_progress_session` —— 只在存储的 sid 发生变化时重置；`upsert_progress` 在整篇重写时会把这两个字段一并保留（`db.py:2872-2948`） |

渲染出的 Markdown（[`cc_memory/core/progress.py`](../cc_memory/core/progress.py)
中的第 0-7 节）就是从这一行生成的。手工编辑 PROGRESS.md 毫无意义：四条自动更新路径
（PreCompact / Stop / UserPromptSubmit / SessionStart 刷新）中的任何一条——加上两个
手动重新生成入口 `/cc-mem progress`（`cli/mem.py:1484-1507`）和 MCP 的
`progress_regenerate` 工具（`mcp/server.py:719-728`）——都会覆盖它。全部六处
`write_progress_md` 调用点：`pre_compact.py:792`、`stop.py:674`、`user_prompt.py:460`、
`session_start.py:1230`、`cli/mem.py:1494`、`mcp/server.py:727`。

### 渲染布局（§0-§7）

`write_progress_md`（`core/progress.py:611-750`）按顺序发出：

| 区块 | 来源 | 空状态文本 |
|-------|--------|------------------|
| `# PROGRESS — <project name>` + `*Generated: <updated_at>* · via <trigger> · <project path>` | `write_progress_md` 开头，`progress.py:646-651` | — |
| 引用块："SINGLE SOURCE OF TRUTH for session handoff … **Never append. Never patch by hand.**" | `PROGRESS_MD_NOTICE`（`core/prompts.py:88-91`） | — |
| `## 0. Session` | `_render_session_section`，`:540-608`（标题在 `:558` 发出） | `⚪ **Current session**: *(no session tagged …)*`（`:576`）与 `*(no prior compacted sessions yet)*`（`:585`） |
| `## 1. Current Request` | `_render_request_lines`，`:443-446` | `*(no request recorded yet)*` |
| `## 2. Status` —— **Done** / **In-flight** / **Blocked** | `_render_status_lines` | `*(none yet)*` / `*(none active)*` / **Blocked** 行只在某次 patch 写入了 `status_blocked` 时才渲染（v2.16.0——没有写入方会填它） |
| `## 3. Open Todos` —— `- [ ] \`priority\` content`，非 pending 用 `[~]`，以 `_MAX_TODOS_RENDERED`（50）封顶并附说明 | `_render_todo_lines`，`:465-483` | `*(no open todos)*` |
| `## 4. Plan (sequenced next steps)` —— **实时**的 `plan_active` 行：`**Goal**`、`**Progress** — N/M steps done · active step #k`，然后至多 `_MAX_PLAN_STEPS_RENDERED`（8）个未完成步骤（封顶会自我声明）；有原始计划等待 `plan-refiner` 时，最前面加一条 `**PENDING REFINEMENT**` 横幅；旧版 `progress.plan` 文本保留在下方，没有结构化计划时则单独显示 | `_render_plan_section`，`:373-440` | `*(no plan recorded)*` |
| `## 5. Critical Context (must-know memories)` —— 至多 10 条 `- #id \`category\` [topic] content` | `_render_critical_lines`，`:486-511` | `*(no critical memories)*` |
| `## 6. Files Touched This Session` —— 按动作分组，每个动作至多 30 条路径 | `:687-704` | `*(no files touched)*` |
| `## 7. Pre-compact Transcript Pointer` | `:707-725` | `*(transcript pointer not yet recorded)*` |
| 页脚：`---` + "This file is the handoff contract for the next session. Read it FIRST." + 一行规格指针 | `PROGRESS_MD_FOOTER`（`core/prompts.py:92-97`） | — |

§0 是 v5 的会话标注，它被**放在最前面**是有意的：读者必须能立刻判断这一行是它自己
会话写的，还是另一个会话留下的过期写入。当前会话那一行是
`🟢 **Current session**: \`#<sid8>\` · started \`<ts>\` · last write \`<ts>\` · trigger \`<t>\``，
后面跟着一条明确警告：如果短 sid 不匹配，就把 §3/§6 当成另一个会话的工作
（`:561-574`）。此前会话的时间线列出 `db.get_recent_sessions` 中至多 5 行（排除当前
sid），每一行形如
`` - `#sid` · ended `<ts>` · <n> msgs · <summary> ``，其中摘要优先取
`session_summaries.completed`，回退到 `sessions.brief_summary`，空白被压平并在 100
字符处截断（`:578-607`）。

### PROGRESS.md 在什么时候被重写

1. **PreCompact**（整篇重写，所有字段全新）：
   - 触发：Claude Code 的自动压缩，或手动 `/compact`。
   - `collect_progress_state(...)` 从
     `extracted_memories + observations + session_summaries` 构建完整状态
     （`progress.py:234-290`; `collect_progress_state` 在 `pre_compact.py:773` 调用）。
   - 只有高于 `MemoryDB.observer_cursor` 的观察才会送进 LLM 提取（v2.16.0，A4）：
     游标及以下的行 Stop 观察者已经送过，无论提取是否运行，PreCompact 都会删掉它们。
   - `db.tag_progress_session(...)` **先**运行，这样标签才能存活
     （`pre_compact.py:790`；保留逻辑见 `db.py:3061-3085`）。
   - `db.upsert_progress(**all_fields)` 覆盖整行（`pre_compact.py:791`）。
   - `write_progress_md(db, pid, memory_dir)` 重写文件（`:792`）。

2. **Stop**（部分更新，每回合）：
   - 先 `db.tag_progress_session(...)`，再
     `db.patch_progress(files_touched=<来自 observations>, trigger_type="stop")`
     （`tag_progress_session` 在 `stop.py:769`，`patch_progress` 在 `:672`）。
   - `write_progress_md(...)` 用打过补丁的状态重写文件（`:674`）。
   - 这让 “Files Touched This Session” 保持最新，无需等到下一次压缩。

3. **UserPromptSubmit**（本会话首条**非脚手架**提示，仅一次）：
   - `strip_scaffolding`（`user_prompt.py`）是这个字段两条入口——本钩子与
     `pre_compact._first_user_request`——共用的**唯一**谓词，既覆盖转录里记录的
     `<command-name>` 包裹形态，也覆盖宿主交给 UserPromptSubmit 的裸 `/command`
     形态；仅仅以路径开头的请求保留它的斜杠。到 v2.13.2 为止，本钩子只是剥掉开头
     的 `/` 然后在第 1 回合播种剩下的东西，于是 `/ccm-load` 成了 §1（"ccm-load"），
     直到第一次压缩为止（v2.14.0）。
   - 播种每会话只发生**一次**，由 `cc_mem_seeded_` 临时标记记录（登记在
     `ui/installer.py` 的清扫表里）——而不是靠提示标记为空来判断：脚手架回合或
     整条私密的回合同样会把它留空，那样会把之后的某条提示重新播种成本会话的请求。
   - 先 `db.tag_progress_session(...)`（`user_prompt.py:446`），再
     `db.patch_progress(current_request=<prompt>, trigger_type="user_prompt" | "resume_request")`
     （`:459`）。
   - `write_progress_md(...)` 重写（`:460`）。
   - 立刻捕获这次会话的目标，而不是拖到 8 个回合之后。
   - 如果提示恰好是**恢复信号**之一（`""`、`"继续"`、`"接着"`、`"接着做"`、
     `"接着干"`、`"继续干"`、`"resume"`、`"continue"`、`"go on"`、`"keep going"`
     —— `core.prompts.RESUME_TRIGGERS`，唯一的拼写，在 `user_prompt.py:457-458`
     读取），trigger_type 会被置为 `"resume_request"`，这样
     下游工具（以及强制提醒里的 RESUME PROTOCOL）就能据此行动。

4. **SessionStart 刷新**（每次会话启动，第 2/3 级兜底）：
   - `_refresh_progress_row(db, pid, memory_dir, current_session_id)`
     （`session_start.py:1066-1233`）。
   - 「是否为空」的判定放在**写入事务内部**。`db.fill_empty_progress` 把每个字段写成
     `SET col = CASE WHEN COALESCE(col, '') IN ('', '[]') THEN ? ELSE col END`，
     在 `BEGIN IMMEDIATE` 下执行（`db.py:3009-3059`）；它上面那次读取只负责决定**提供**
     哪些值。旧写法是一条连接上的 `get_progress()` 读，加另一条连接上的无条件
     `patch_progress()` 写，中间还夹着第 3 级的 transcript 加载：在这个窗口里提交的
     PreCompact 整篇重写，会被那次过期读取所批准的启发式值覆盖 —— 实测
     `status_done`、`status_in_flight`、`plan`、`open_todos` 四项全部被替换。
   - **空**不等于**从未写过**。`[]` 是假值，所以 PreCompact 因为「没有待办」而写下的
     `open_todos` 被读成了「这个字段还没填」。当 `progress.trigger_type` 表明该行是被
     整篇重写敲定的（`progress_was_fully_written`，`session_start.py:1019-1045`），
     被挖掘出来的工作清单 —— `open_todos` 与 `files_touched` —— 就保持那次重写留下的
     样子。**局限**：该列记录的是**最后一个写入者**，而 Stop 钩子每回合都会把它盖成
     `"stop"`，所以这个事实只保护紧跟在一次重写之后的那次 compact/resume 启动。
   - 当 `source="compact"` / `"resume"` 时，第 3 级挖掘的是**当前** transcript
     （`tier3_exclusion`，`session_start.py:1048-1063`）：会话 id 没有变，而那个文件
     **就是**历史。把它排除掉，等于把挖掘对象交给磁盘上最新的**另一个**会话，那个会话
     待办中的 TodoWrite 条目于是作为本会话自己的条目进入 PROGRESS.md §3。
   - 空则填：绝不覆盖上游写入的非空字段（契约陈述见 `session_start.py:1093-1095`）。
   - 来源依次为：DB 的 session_summary（`status_done`、`status_in_flight`、`plan`）
     / observations（`files_touched`），然后（如果仍为空）去挖掘 `tier3_exclusion`
     选中的 transcript——上一次会话的 `.jsonl`，或在 compact/resume 时本会话自己的
     那份——取 `open_todos`、`files_touched` 和 `transcript_ptr`。（已退役的
     `critical_context` 列不再被填充。）
   - `open_todos` **只**从 transcript 填充（`session_start.py:1195-1205`）：按
     `next_steps` 切分的兜底已在 v2.16.0（D6）移除。TodoWrite 的 `tool_use` 块是
     结构化数据；而把散文式的 `next_steps` 字符串按 `;` 切开，会让计划冒充成 todo
     清单，而 RESUME PROTOCOL 会不加询问地执行 §3 的第一条 todo。
   - 正是这一步保证了当 PreCompact 没有运行时（transcript 已被裁剪之后才手动
     `/compact`、上一次会话极短、PreCompact 崩溃，或项目的第一次会话），
     PROGRESS.md 不会渲染成一整面 `*(none)*` 占位符。

### 下一次会话是如何被强制读取它的

`cc_memory/hooks/session_start.py:_build_forced_reminder`（`:543-600`）在注入上下文
的末尾发出这一段（每一句都是 `core/prompts.py` 里的常量）：

```
<system-reminder>
CC-MEMORY HANDOFF — MANDATORY READ-FIRST PROTOCOL

Before responding to any user request in this session, you MUST:
  1. Use the Read tool on `.ccm/PROGRESS.md` (absolute: `<path>`).

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

这一块只在 PROGRESS.md 存在时才发出，而且自 v2.16.0 起只要求这**一个**文件：
MEMORY.md 曾是第二个强制 Read——一份索引，它的事实注入的各层本来就带着——于是
每个会话都为交接根本不需要的东西付一次工具调用、外加一整份索引的上下文
（`session_start.py:_build_forced_reminder`；`/cc-mem inject-usage` 仍会统计对
MEMORY.md 的 Read）。没有 PROGRESS.md 就没有这一块——在一个从未压缩过的项目上，
一条只要求索引的提醒曾经就是整个握手。

### 哪种启动原因得到哪种注入（v2.16.0）

`session_start.build_context` 接收宿主给的 `source`，由
`session_start._injection_mode` 归成三种形态之一；注入清单记录 `source` 与
`ack_demanded`：

| `source` | 注入 | 提醒块 | 要求确认 | 清单 |
|---|---|---|---|---|
| `startup`、`clear`、未知 | 每一层 | 有 | 有 | 重写 |
| `compact` | 每一层——窗口刚被重建，先前的注入已经没了 | 有，但没有确认那一句 | 无：压缩后的启动没有"第一条回复"可供确认出现，`/cc-mem inject-usage` 把确认报为 `unmeasured` 而不是 `no` | 重写，`ack_demanded: false` |
| `resume`、`fork` | 头部、一行 `[cc-memory] session resumed …` 和长期指令——启动时的注入仍在这段对话里，再发一遍等于让每条记忆在上下文里出现两次 | 无 | — | **不**重写：它是启动注入的记录，召回通道的排除集读的就是它；不刷新引用时间，不拉起追溯工作进程 |

那份双语的恢复 token 列表是刻意为之的，而且只有**一个**拼写
`core.prompts.RESUME_TRIGGERS`，`user_prompt.py`、这条提醒和 `core/recall.py` 都读它
（v2.16.0）——它带有一条 `# i18n Tier 3` 守卫注释（`session_start.py:589-592`；见
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
#    cli/mem.py:1494 在每次调用时都会重写它）
/cc-mem progress

# 2. 显示 SQL 行里是不是当前数据
/cc-mem sql "SELECT current_request, trigger_type, updated_at FROM progress"
```

`/cc-mem` 对两种安装布局都能解析出 CLI ——完整的解析顺序见[反补丁一节的验证说明](#验证)
（`commands/cc-mem.md:100-111`；市场 / 开发检出用嵌套的 `<root>/cc_memory/cli/mem.py`，
独立安装器的产物用扁平的 `<root>/cli/mem.py`，见 `cc_memory/ui/installer.py` 的
`TARGET_DIR` / `SUBPACKAGE_FILES` / `_copy_subpackages`）。在市场安装下，硬编码的
`python ~/.claude/hooks/cc-memory/...` 调用是错的，因为那棵树只保留 `logs/`。

一份健康的 PROGRESS.md 应当具备：

- 非空的 `current_request`（在会话的第 1-2 回合内被设置）。
- 在任何编辑密集的回合之后，`files_touched` 非空。
- 较新的 `updated_at`（活跃工作期间不超过约 5 分钟）。
- 没有残留的 `.ccm/.pre_compact_attempt.json`（v2.4.2）。PreCompact 在入口写下这个
  标记，并且只在完成时才移除它（`pre_compact.py:378-414`；在 `:583` 写入，在
  `:593,876,931` 清除）；一个超过 10 分钟的标记意味着上一次压缩在保存之前就被**杀
  死**了，它的记忆已经丢失——SessionStart 会把它呈现为
  `[WARNING: PreCompact … DID NOT FINISH …]`（`session_start.py:472-499`，年龄闸门在
  `:484`）。`.last_save.json` 显示不出这一点：超时杀进程不会跑任何 `except` 块，也不
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
可能与自己的真相来源漂移开。`write_plan_md`（`core/plan.py:817-867`）是从行里做的
整篇重写，生成出的文件自带一条 DO-NOT-EDIT 横幅，写明那张 SQL 表和三个合法的编辑
入口（`PLAN_MD_HEADER`，`core/prompts.py:113-121`）。

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
              Stop 拒绝本回合（{"decision": "block"}，v2.11.0）
              → 调用 @plan-refiner
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
              → plan_active.structured = JSON，needs_refine = 0
                （一次带 revision 校验的写入）
              → 旧计划归档到 .ccm/.plan_history/
                （只在那次写入成功之后）
              → 写入 .ccm/PLAN.md
                                  │
                                  ▼
              ┌─── 实时工作继续 ────────────────────┐
              │                                     │
              ▼                                     ▼
   PostToolUse: TodoWrite          PostToolUse: Edit/Write/...
   → sync_todos_to_steps()         → 累加 edits_since_last_guardian
   → 重写 PLAN.md                  （敏感工具一次加 20）
   （没有 live 计划行时两者都是空操作：post_tool_use.py:128,134；
    todo 同步还额外要求一个 schema 合法的结构化计划：core/plan.py:1346）
              │                                     │
              └─────────────────┬───────────────────┘
                                ▼
              Stop 钩子调用 blocking_reasons()（内部是 guardian_verdict()）
              若 turns≥8 或 edits≥12（且本回合没有跑过 plan-check）：
                本回合被拒绝（v2.11.0）；逃生预算
                用尽后，一行 [cc-memory.plan] 建议被寄存给下一次
                UserPromptSubmit 打印（v2.16.0）
                                ▼
              主 Claude 派生 plan-guardian 子代理（Haiku）
              → 读取 PLAN.md + PROGRESS.md + 近期 git 活动
              → 报告 ALIGNMENT + DRIFT + NEXT ACTION（≤150 词）
                                ▼
              `/cc-mem plan-check`（重置计数器，打上
                                    一回合豁免戳）
                                ▼
              [继续；若漂移严重则 `/cc-mem plan-replan`]
```

钩子自己绝不派生子代理——Stop 钩子会**拒绝**这一回合并点名补救措施（v2.11.0），逃逸预算
耗尽后的提醒则寄存到下一次 UserPromptSubmit 打印（v2.16.0）。回合计数器在每一个存在活动
计划行的回合上累加，续发的 Stop（`stop_hook_active`，v2.16.0）除外；
`core.plan.blocking_reasons` 返回所有必须叫停本回合的条件，未精炼的计划排在最前。

### 数据模型：`plan_active`

每个项目一行。Schema（v4 迁移，`core/db.py:220-233`，外加下面的 v7 / v9 / v10 列）：

| 列                              | 类型    | 用途 |
|---------------------------------|---------|---------|
| `project_id`                    | INTEGER | 主键，外键 → projects.id |
| `raw`                           | TEXT    | 计划模式输出的原文（或用户粘贴的文本） |
| `structured`                    | TEXT    | JSON {goal, success_criteria, steps[], context, dispositions?, ...} |
| `active_step`                   | INTEGER | 当前进行中步骤的 id |
| `edits_since_last_guardian`     | INTEGER | 漂移计数器（由 Edit/Write/MultiEdit/NotebookEdit 累加 —— `hooks/post_tool_use.py:133-135`；敏感 Bash 调用一次加 20，`:142-144`） |
| `turns_since_last_guardian`     | INTEGER | 漂移计数器（由 Stop 累加） |
| `last_guardian_at`              | TEXT    | 上一次 guardian 检查的 ISO 时间戳 |
| `last_refined_at`               | TEXT    | 上一次精炼的 ISO 时间戳 |
| `needs_refine`                  | INTEGER | 1 = raw 是新的，但 structured 已过期 |
| `created_at`, `updated_at`      | TEXT    | 标准时间戳 |
| `revision`                      | INTEGER | v7（`v7_plan_revision`）：乐观并发计数器，每次 UPDATE 都加一。`update_plan_if_revision`（`core/db.py:3263-3288`）只在它仍等于读到的值时才写入 |
| `turns_total`                   | INTEGER | v9（`v9_plan_turns_total`）：单调的回合时钟，什么都不会重置它。`bump_plan_turn_counter`（`core/db.py:3323`）把它与 `turns_since_last_guardian` 一起累加；指令闲置度就是对着它量的 |
| `guardian_checked_at_turn`      | INTEGER | v10（`v10_plan_guardian_checked_at_turn`，DEFAULT -1）：`/cc-mem plan-check` 上一次登记巡检时的 `turns_total`。`reset_plan_guardian_counters` 打这个戳；它授予一回合豁免 |

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
（`_VALID_STATUSES`, `core/plan.py:113`）; `normalize_structured`（`core/plan.py:156-240`）是防御性的：
它容忍常见的 LLM 状态别名（`todo`→`pending`，`wip`/`doing`→`in_progress`，
`complete`/`completed`→`done`），丢弃没有 title 的步骤条目，并按位置为缺失的 `id`
重新编号。`is_valid_structured`（`:137-153`）要求非空的 `goal` 和 ≥1 个格式良好的
步骤——达不到这一点的会被 `apply_refined_plan` 以
`"refined plan does not satisfy schema (needs goal + ≥1 step)"` 拒绝（`:1241`）。
`goal` 与 `context` 按同一条规则化为文本（v2.14.0）：字符串原样保留，字符串列表
拼接，其他一律拒绝（`goal`）或丢弃（`context`）——列表形态的 `goal` 过去会以
Python repr（`"['list goal']"`）存下来并通过上面的检查。替换之后的
`success_criteria` 播报评判的是**写进去的**那份计划（规范化后的 `result`），而不是
原始载荷：非列表值在进入时就被丢弃，而回读它曾在 `[OK] Plan stored` 打印之后抛出
`TypeError`，随后一模一样的重试又被继承门拒绝。

`dispositions` 是可选的，只有当这份计划**替换**另一份计划时才有意义。合法的
`action` 取值：`done`、`dropped`、`merged`、`carried`；`reason` 必须非空
（`core/plan.py:906,1039-1059`）。它会被保留在存储的计划里以供审计
（`core/plan.py:205-211`）。

### 同步算法（TodoWrite ↔ 步骤）

当观察到 `TodoWrite` 时，`core.plan.sync_todos_to_steps`
（`core/plan.py:290-363`，匹配器在 `:245-276`）会：

1. 对每一条 todo，基于 `core.textsim.shingle_set` 的 shingle（非 CJK 用
   三元组，CJK 连续段用二元组）计算它与每一个步骤 title 的 Jaccard 相似度。
2. 在相似度 ≥ `MATCH_THRESHOLD`（0.35，`core/plan.py:100`）时挑出最佳匹配的步骤。
3. 用 todo 的状态更新步骤状态，映射关系为
   （`_TODO_TO_STEP_STATUS`，`core/plan.py:280-287`）：
   - `completed` → `done`
   - `in_progress` → `in_progress`
   - `pending` → `pending`
   - `cancelled`/`canceled` → `skipped`
   - `blocked` → `blocked`
4. 已经是 `done` 的步骤绝不回退（一条走失的 `pending` todo 不会把它撤销）
   （`:318-319`）。
5. 未匹配上的 todo 会被计为漂移信号（todo 内容没有对应的计划步骤），并作为
   `n_unmatched` 返回。
6. 匹配按相似度从高到低应用，每个步骤只用一次，因此重复的 todo 不会争抢同一个步骤
   （`:308-313`）。第一个被 todo 推进到 `in_progress` 的步骤成为 `active_step`；
   否则由一个已经处于 `in_progress` 的步骤保留它；如果都没有，则取第一个
   `pending` 步骤（`:339-356`）。

整条路径都是机械的——不调用 LLM。`apply_todowrite_sync`
（`core/plan.py:1338-1379`）会持久化更新后的计划并重写 PLAN.md，但如果没有那一行、
或存储的 `structured` 不符合 schema，它会原样返回 `{"skipped": "no_active_plan"}`
而不改动任何东西（`:1346-1347`）。

### 结转门禁（Carryover gate，R610，自 v2.4.0 起强制）

`plan_active` 是一个**单槽位**，因此替换计划正是已排布的工作可能无声消失的那个瞬间。
这是一次真实的、有记录的损失（SELF-ITER 的 S1-S3 沉没事件：已经被批准的后续阶段从未
重新进入任何计划，在下一轮计划覆盖该槽位的那一刻就消失了——`core/plan.py:870-885`）。
通往那个槽位的两道门都设了闸，而且刻意**没有强制标志（force flag）**
（`core/plan.py:882-883`，并在 `:1295` 的错误文案里重申）：没有记录理由的丢弃，正是
这道门禁存在的目的所要杀死的失效模式。因此 `plan-set` 只接受
`--raw / --raw-file / --from-refiner`（`cli/mem.py` 的 `cmd_plan_set`）——根本没有
可以传进去绕过它的东西。

#### 入口 1 —— REPLACE（`/cc-mem plan-set --from-refiner` → `core.plan.apply_refined_plan`）

`check_carryover(old_structured, new_plan)`（`core/plan.py:943-1060`）收集旧计划的
未完成步骤——状态属于 `pending | in_progress | blocked`（`_UNFINISHED_STATUSES`，
`:905`；选择器 `unfinished_steps` 在 `:921-927`）——并要求其中每一个要么

  (a) **被自动结转**：与新步骤的裸 `title`，或与其 `title + notes` 的
      shingle-Jaccard 相似度达到 `_carryover_bar` —— 非 CJK 标题为 0.5
      （`CARRYOVER_MATCH_THRESHOLD`），任一标题含 CJK 连续段时为 2/3
      （`CARRYOVER_MATCH_THRESHOLD_CJK`）：帮了合并侧写入器的 CJK 二元组
      底层会**放松**这道门，而门的误匹配意味着静默丢步骤（实测：325 个
      单字替换里 98 个从 FLAGGED 翻成自动结转，包括三十秒 vs 六十秒——
      相反的事实）（自 v2.4.1 起两者都是候选，`:954-984` —— 只与
      `title+notes` 比较，会让一段很长的 notes 把一个完全相同的 title
      稀释到阈值以下，这是在该门禁的第二次真实替换 R610 中发现的；
      `title+notes` 这个候选被保留下来，是为了让一个被折叠进另一步骤
      notes 里的步骤仍能被结转）。自 v2.8.0 起只有**未完成**的新步骤才是
      结转目标（一个生来就是 `done`/`skipped` 的步骤是退役，不是结转），而且
      每个新步骤会被它所结转的那**一个**旧步骤**消耗掉**（`_consume_carry`，
      `:986-999`）——几个旧步骤并进同一个新步骤，需要用 `merged` disposition
      表达，要么

  (b) **被 disposition 记录**：存在一条顶层 `"dispositions"` 条目，其 `old_title`
      以同一个 `_carryover_bar`（0.5，CJK 为 2/3）匹配上，`action` 属于
      `done | dropped | merged | carried`，且 `reason` **非空**——`detail` 被接受为
      `reason` 的同义词（`:1001-1059`，同义词处理在 `:1039`）。每一条 disposition
      只会被它所交代的那一个步骤消耗，而 `carried` 条目点名的步骤必须被新计划的
      某个字符串覆盖。

dispositions 是从**原始的 refiner 字典**里读的，在归一化之前（`apply_refined_plan`
在 `:1283-1285` 传的是 `structured`，不是 `normalised`；理由见 `check_carryover` 的
docstring，`:946-948`）：schema 保持只增不减，因此更老的 refiner 的输出在没有未完成
步骤的计划上仍然可用。

任何违规都会抛出 `ValueError`（`core/plan.py:1286-1295`）。`plan-set --from-refiner`
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

四种违规形态，逐字取自 `core/plan.py:1028-1059`：

| 条件 | 消息 |
|-----------|---------|
| 没有相似的新步骤，也没有匹配的 disposition | `step #N '<title>' — not in the new plan and no disposition` |
| 有 disposition，但 `action` 不在枚举内 | `step #N '<title>' — disposition action '<x>' not in ('done', 'dropped', 'merged', 'carried')` |
| disposition 为 `carried`，但新计划没有任何步骤字符串覆盖这个标题 | `step #N '<title>' — disposition claims 'carried' but no step in the new plan covers it` |
| 有 disposition，但 `reason` 为空 | `step #N '<title>' — disposition has no reason (a drop without a recorded reason is the exact failure mode this gate kills)` |

**如何解决一次拒绝。** 不要试图绕过去；没有路可绕。针对每一个被点名的步骤，从下面
选一种：

- 这个步骤依然成立 → 把它作为一个**未完成**的步骤加进新计划的 `steps`（超过
  结转阈值的标题会自动结转，但每个新步骤只结转一个旧步骤，而列为
  `done`/`skipped` 的步骤什么都不结转）。
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
`cmd_plan_clear`（`cli/mem.py:1907-1937`）会拒绝并以 1 退出（`:1918-1928`）：

```
[FAIL] carryover gate: the active plan still has 2 unfinished step(s):
    - #4 Add CSRF protection
    - #6 Write integration tests
  Clearing would silently sink them. Re-run with --reason "<why these steps are
  being dropped>" -- the reason is recorded in .ccm/.plan_history/.
```

解决办法是带上 `--reason "<why>"` 重新运行。这个理由不是装饰品——它会被写进归档
载荷。只有在门禁通过之后，命令才会归档、执行 `db.clear_plan_active(pid)`，并删除
`.ccm/PLAN.md` + `.ccm/.plan_raw.md`（`cli/mem.py:1929-1936`）。

#### 兜底 —— 只追加的计划历史

每一份被替换掉的计划——哪怕它的 disposition 记录得干干净净——都会由 `archive_plan`
（`core/plan.py:1068-1134`）归档到

```
.ccm/.plan_history/plan_<YYYYmmddTHHMMSS>_<ms>[-n]_<replace|clear|recapture>.json
```

——毫秒级文件名，经 `O_CREAT | O_EXCL` 认领，冲突时加 `-n` 后缀，所以先后或并发
的替换都不会互相覆盖（`:1094-1119`）——其中包含 `archived_at`、`event`、`reason`、
`structured` 形式、`raw` 文本和 `active_step`（`:1084-1091`）。调用方：
`apply_refined_plan` 在 `core/plan.py:1319,1325`（替换，无理由字符串，在带
revision 校验的写入成功之后）; `capture_exit_plan_mode` 在
`core/plan.py:1179`（recapture：一次新的 ExitPlanMode 在原始计划被精炼之前替换了它）;
以及 `cli/mem.py` 的 `cmd_plan_clear`（清除，带用户的 `--reason`）。既没有
`structured` 也没有非空白 `raw` 的行会被跳过（`:1075-1076`）。

归档写入失败是**非阻塞**的：它经 `_log.warn` 记入日志
`plan history archive failed (<err>) — proceeding; the carryover gate already
enforced accounting`——绝不写 stderr，因为 PostToolUse 在 recapture 时会走到这个
函数——并返回 `None`（`core/plan.py:1120-1134`）。代码里把理由
说得很明白：门禁的 dispositions 才是首要的反丢失保证，因此让每一次计划操作都卡在
一次归档磁盘打嗝上，等于把兜底机制变成对规划本身的拒绝服务。

### 提示阈值

默认值是 `core/plan.py` 里的模块常量 `GUARDIAN_TURN_THRESHOLD = 8`、
`GUARDIAN_EDIT_THRESHOLD = 12` 与 `DIRECTIVE_IDLE_TURNS = 25`——自 v2.16.0 起每个只有
**一处**拼写；`guardian_verdict` 读前两个，Stop 钩子读 `blocking_reasons`，后者不传任何
覆盖值地读 `guardian_verdict`（`hooks/stop.py`）。这些**没有** `config.json` 键——要改
就改常量，或者显式传关键字参数。敏感调用的 `+20` 加分同样是硬编码的
（`hooks/post_tool_use.py`）：

| 触发                                      | 阈值           | 发出什么 |
|------------------------------------------|----------------|-------------------|
| `turns_since_last_guardian` 达到          | 8（默认）      | Stop **拒绝**（`plan-drift`） |
| `edits_since_last_guardian` 达到          | 12（默认）     | Stop **拒绝**（`plan-drift`） |
| 检测到敏感 bash 工具                       | 不适用（经 +20 加分立即触发） | **同一回合**结束时的 Stop **拒绝**（`plan-drift`）——PostToolUse 在该回合的 Stop 评估之前就已加分 |
| `needs_refine = 1`                       | 不适用（立即） | Stop **拒绝**（`plan-unrefined`） |
| 某条活跃指令闲置超过阈值                    | 25 轮          | Stop **拒绝**（`directive-idle:<slug>`） |

在没有 schema 合法的计划时，`guardian_verdict` 给出 `reason="no_active_plan"`；当一份
原始计划正等待精炼时给出 `"needs_refine_first"`，因此两种条件绝不会撞车。它还授予
**v10 一回合豁免**（`checked_this_turn`，`reason="checked_this_turn"`）：当
`turns_total == guardian_checked_at_turn + 1`——`/cc-mem plan-check` 就在这一回合里
跑过——两个漂移阈值都不拒绝，因为 PostToolUse 已经数过本回合的编辑，一次敏感调用
单独就能越过 12，否则拒绝所点名的补救永远无法收敛。下一回合的漂移照常拒绝。（它的元组
视图 `should_nudge_guardian` 除测试外没有调用方，已在 v2.16.0 删除。）

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
   执行了。预算耗尽那一轮打印的建议行同样是渲染路径（v2.14.0）：拒绝键里带着
   指令 slug，而 `upsert_directive` 从不清洗它，一条存下来的 `</system-reminder>`
   曾原样抵达模型——建议行和拒绝文档里每个单行槽位（`[key]` / `what` / `fix`）
   都经过 `neutralize_inline`，所以 slug 里的 CR/LF 也伪造不出第二个条目。
2. **一次拒绝写到 stdout 的必须是一份 JSON 文档，不能有别的。** 前面挂着散文的
   `{"decision": …}` 不是 JSON，而一个解析不了它的宿主就看不到任何决策——那正好把
   这个版本要终结的"建议"给悄悄恢复了。自 v2.16.0 起，允许收官的 Stop 什么都不
   输出（它的 stdout 从来到不了模型；状态行改写日志），预算用尽后降级成的建议行
   寄存在本会话的 block 标记上，由下一次 `UserPromptSubmit` 打印。
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
`/cc-mem plan-check` **登记**调用方刚跑完的一次 guardian 巡检（先跑 `plan-guardian`
子代理，再跑这条命令）：有原始计划等待精炼时它拒绝且没有任何副作用，否则刷新
PLAN.md、重置计数器并打上一回合豁免戳（`cmd_plan_check`，`cli/mem.py:1958-2009`）。

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
  在 PLAN.md 缺失或非法时报告 `replan-needed` 并停止（`:37-53`）。

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
/cc-mem plan-check               # 登记刚跑完的 guardian 巡检（重置计数器，
                                 # 打上一回合豁免戳；有原始计划等待精炼时拒绝）
/cc-mem plan-replan              # 对已存储的 raw 重新置位 needs_refine
/cc-mem plan-clear               # 丢弃计划 + 删除 PLAN.md。
                                 # 会先归档到 .ccm/.plan_history/，并且在
                                 # 存在未完成步骤时拒绝（以 1 退出），
                                 # 除非给出 --reason "<why>"（v2.4.0）。
/cc-mem directive-list [--status S] [--json|--full]   # 账本，按重复次数排序
/cc-mem directive-add <slug> [--quote Q] [--demand D] [--kind K] [--times N]
                                 # 记录；重复添加会累加 times_stated
/cc-mem directive-edit <slug> [--demand D] [--quote Q] [--kind K] [--status active|blocked]
                                 # 修正但不加计数；绝不创建
/cc-mem directive-close <slug> --evidence "<checkable>" [--status done|superseded|dropped]
```

处理函数：`cli/mem.py:1678-1683`（show）、`:1694-1761`（status）、`:1764-1904`（set）、
`:1907-1937`（clear）、`:1940-1955`（replan）、`:1958-2009`（check）、
`:2012-2041` / `:2062-2087` / `:2090-2126` / `:2129-2145`（directive-list / -add
/ -edit / -close）；解析器接线在 `:2808-2887`，分发在 `:3074-3080`。`plan-status` 区分三种状态：完全没有行、有 raw
但未精炼的计划（打印 raw 的长度并告诉你去调用 `@plan-refiner`），以及已精炼的计划
（目标、N/M 已完成、活动步骤、上次精炼时间、上次 guardian 检查时间、两个计数器）。
如果没有存储任何 raw 文本，`plan-replan` 会以 1 退出并失败（`:1945-1947`）。

### 敏感工具清单

`core.plan.is_sensitive_tool_call`（`core/plan.py:1465-1488`）会标记以下 Bash 模式
——对 `command` 输入做大小写不敏感的正则匹配（`_SENSITIVE_CMD_RE`，`:1460-1462`），
仅限 `Bash` 工具，并**锚定在命令位置**：命令开头，或紧跟在 `;`、`&&`、`||`、`|`、
`(` 或换行之后，可带 `sudo` / `env VAR=val` 前缀，于是 `cd x && git push` 算数而
`grep "git push" docs/` 不算——从而立即触发一次 guardian 提示加分（+20 次编辑）：

- `git push`、`git push -f`、`git push --force`
- `rm -rf`、`drop table`、`drop database`
- `npm publish`、`cargo publish`、`pypi-upload`、`twine upload`
- `kubectl apply`、`terraform apply`、`ansible-playbook`

+20 的语义写在 `hooks/post_tool_use.py` 里：“这一个动作携带的漂移风险相当于
约 20 次普通编辑”——20 单独就越过编辑阈值 12，所以结束**同一回合**的那次 Stop 会
**拒绝**它（`plan-drift`），除非该回合内已登记过一次 guardian 巡检。cc-memory
**不会**阻断 Bash 调用本身——PostToolUse 在它之后才运行——它拒绝的是本回合的收尾
（`hooks/post_tool_use.py:137-144`）。这个加分和普通的编辑加分一样，在没有 live
计划行时是空操作。

需要时请在 `cc_memory/core/plan.py:is_sensitive_tool_call` 里扩充这份清单。

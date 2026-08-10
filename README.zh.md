<!-- i18n-source: README.md | sha256: becac90fc94e5aab | version: 2.10.1 | translated: 2026-08-10 -->
> [English](README.md) · **简体中文**

# cc-memory

**Claude Code 持久化记忆插件（v2.10.1）**——反补丁式的写入即归并（reconcile-on-write）、
LLM 判定的语义去重、强制 PROGRESS.md 交接、带 plan-refiner / plan-guardian 子代理与
强制结转闸门的实时 PLAN.md 锚点、有界 transcript 读取、注入可观测性、FTS5 搜索，
以及以 Haiku 为主（本地 Ollama 兜底可选）的 AI 判定式抽取。

## 它解决什么问题

当上下文窗口写满时，Claude Code 会压缩（compact）对话，从而丢失信息：决策、结果、
待办、项目知识都会消失。正常结束的对话（关闭终端）同样会丢失上下文。

cc-memory 在每一个对话边界捕获结构化记忆，并且**强制下一次会话在开始工作之前先阅读
一份交接文档**。

## v2.10.1 有什么新变化

v2.10.0 记录为未闭合的三条,已全部闭合:dashboard 仅剩的两个最高复杂度
核心现在是**测试套件无头驱动的纯 staticmethod**(Progress/Plan 渲染器
连同其标记转义、LLM tidy 裁决归一化器——各自的 Tk 回调只剩 widget
编排);contracts 注册表在共享钩子闸门不再委托退出开关时**大声报错**
(而不是把六个钩子列成被一个名存实亡的闸门保护);codex 对 v2.10.0
防护修复的复签结论返回 **CONFIRMED-CLOSED**。falsify 登记
149 → 151,两个新用例均单独验证跑红。

## v2.10.0 有什么新变化

**一轮反臃肿架构整备。**对全树每个函数做了 LOC + 圈复杂度测量（对比 v2.5.0
基线），回答一个问题：五轮收敛修复是否已经堆成补丁摞补丁的臃肿？结论：增长
几乎全部是*机制*——每道守卫背后都有一个实测缺陷——只有**一处**真正的结构性
重复，本轮已修：

- **六个钩子各自手搓同一套入口阶梯**（stdin 读取 → JSON 解析 → 对象检查 →
  对原始 cwd 跑退出开关 → 锚定项目根），约 350 行六份同构拷贝，历史上每次
  拷贝间漂移都变成过已发货缺陷。阶梯现在只在 `hooks/_entry.py` 存在一份；
  各钩子的策略（coerce 还是 abort、NUL 检查、被排除分支的反应）仍留在各钩子。
  `test_surfaces` §4 新增窄排除驱动（活项目内部被列出的子目录——顺序反转会
  静默破坏的正是这个方向），§7 只在闸门内部断言一次顺序，
  `falsify_fixes --case r10entryorder` 证明反转会让套件翻红。

刻意**不**重构的部分同样有案可查（CLAUDE.md § v2.10.0）：dashboard 复杂度
最高的三个函数原样保留，因为它们没有任何可执行覆盖——在零测试下重构一个
2.9k 行的 GUI，恰恰是本轮要避免的「越改越错」失败模式。

## v2.9.0 有什么新变化

**两位互相独立的审阅者同时读了已发布的 v2.8.0 代码树**，文件集不相交、角度不同：
我这边是六个作用域的扇出（严重发现还要过一轮对抗式反驳），codex 那边是对整个运行
时包的只读通读。**18 条缺陷在这里被我本人复现之后才算数**；另有 2 条被反驳、丢弃。

对使用者真正有影响的几条：

- **一条记忆的历史可能被截断。**归档一条本身已经带着 supersede 链接的行时，那个
  链接被**覆盖**了，于是它所替代的版本从任何链游走中都不可达——而
  `/cc-mem supersedes` 仍然把结果标成"最新在前"。现在用 `COALESCE` 写入：先到的
  血缘事实胜出，第二条写进日志。
- **共用一个数据库文件时，`/cc-mem` 会跨项目泄漏。**一个 `memory.db` 合法地持有
  多个项目行（目录改名会产生一个，把 `memory/` 复制到别的仓库也会）。
  `encoding-check --apply` 会**归档**别的项目的行，`supersedes` 会把别的项目的
  记忆整条打印进 Claude 会话，`sessions` / `keywords` 也会把它列出来。这四个现在
  都加了作用域；`archive` 一直就有。
- **重装会删掉你自己的钩子。**如果你在 `settings.json` 里的某个钩子条目与
  cc-memory 的条目共处同一个 matcher 组，安装路径会把整组丢弃——rc=0，无告警。
  卸载路径早就修好了这一点，安装路径没有。
- **在还没有 `settings.json` 的机器上，安装器的比较并交换是完全失效的**，因此
  安装期间 Claude Code 写下的 `settings.json` 会被无声销毁。
- **大的工具结果被整条丢弃。**PostToolUse 只读入 512 KiB 的**前缀**；更大的载荷
  在 JSON 中途截断，钩子把整个事件丢掉——观察行与实时计划跟踪一起没了。读一个
  常见大小的 `package-lock.json` 就足以触发。
- **Windows junction 能同时骗过两道「绝不穿过链接写入」的守卫**（`stat.S_ISLNK`
  对 junction 返回 False），于是被 junction 化的 `memory/` 会被写入，并被当作
  项目根采纳。
- **Web 面板可能被无限期锁死**：连接只要一直滴送尚未结束的报头块即可，因为墙钟
  期限只覆盖请求体。现在报头阶段有 10 秒的绝对预算。
- **空 prompt 会把你上一轮的请求留在**那个由 Stop 观察者发往 Anthropic 的标记里，
  使它写下的记忆被归到错误的请求名下。

**还有门禁自身的五个洞**，因为这一轮也把枪口对准了它们：引用检查器只认 `.py`
（25 条引用豁免，其中 2 条已经烂了）、数字与名词之间隔一个词就绕过 `doc_claims`
的全部触发式、契约登记册把标记防御少算了一个、`verify_anchors` 只捕获
`SystemExit`（锚一旦烂掉整趟扫描就死）、第三道发布门禁在沙箱之外运行——每跑一次
往真实 `%TEMP%` 泄漏两个项目目录（发现 270 个，已清理）。

证伪登记册从 127 条增至 **147 条**，每条新用例都单独跑红过。完整叙事见
[CHANGELOG.md](CHANGELOG.md)。

**首次提供 release 资产：**两个 PyInstaller 可执行文件
（`cc-memory-installer.exe`、`cc-memory-dashboard.exe`）加一份 `SHA256SUMS.txt`，
附在 GitHub release 上。

## v2.8.0 有什么新变化

**反补丁契约在中文下是失效的。**字符三元组在 CJK 上会塌缩：十个汉字的事实改一个字，
相似度 **0.4545**，而等价的英文改动是 0.7317——低于决定 MERGE 与 SUPERSEDE 的 0.50
阈值，于是每一条中文更正都被当作**新事实**入库，两条互相矛盾的行同时活着。这不是构造
出来的，是在真实库上撞到的（对 #294 的一条更正打分 0.23，变成了 #301）。第二层也救不
了：LLM 判定的语义去重用 `[a-z0-9_]{3,}` 分词，纯中文记忆的词集为**空**，压根进不了
提名。`core/textsim.py` 现在是唯一的相似度基座——CJK 连续段用字符二元组，其余仍是三
元组，且 ASCII 输出与被它替换的实现**逐字节相同**，所以树里每一个调好的阈值都不动。
对一个记忆以中文为主的项目来说，矛盾堆叠是常态而不是边角。

同批还有：MERGE 会销毁幸存行的来源 tags；排在第 51 位的 0.95 相似行对写入器不可见；
`supersede_memory` 是两个事务（中途被杀就同时发布两条事实）；五个 `id IN (...)` 写入
在 SQLite 变量上限之后直接崩；以及**发现一条记忆是错的时候根本没有受支持的退役路径**
——现在有 `/cc-mem archive` 了。完整清单见 [CHANGELOG.md](CHANGELOG.md)。

**v2.7.0 教会了六个 hook 项目根在哪，却把其余每一个入口都落下了。**又跑了三轮对抗
debug——每条发现都由我独立复现后才采纳，两条因复现不出来而被否决——确认 22 项缺陷。
这与 v2.7.0 当初要修的是同一种形状，只是高了一层：守卫挂在**部分调用方**上，而不是挂在
它们共同经过的那个东西上。

不是假想，是实测：你机器的 `D:` 盘符根上曾经有一个 `memory/memory.db`，由本项目
**自己的测试套件**在每一次运行时种下（`test_surfaces` 喂了病态 cwd `D:*b`，解析器答
`D:\`，hook 就在那儿初始化了数据库）。它存在期间，该盘上每一个尚未初始化的项目都会
解析到它。

- **现在是九个入口锚定，不是两个。**`cli/plan.py` 连只读的 `list` 都会种野生库；MCP
  服务器——唯一面向模型的**写入**面——用的是裸 `os.getcwd()`；dashboard 只锚定了
  `--project`，另外四条 GUI 路径没锚；installer 的 *Initialize Project*、web viewer 和
  `/save-memories` 则完全没锚。web viewer 的症状是反过来的：从子目录启动时它会**拒绝**
  一个已经初始化好的项目。
- **隐私退出名单终于兑现了它自己写下的承诺。**`is_excluded` 在两个 CLI 和 dashboard 里
  出现次数为**零**，而 MCP 的拒绝文案却说记忆"经**任何** cc-memory 工具都不可读不可写"。
  现在所有手动入口共用同一道门，且在**锚定之前**检查，这样子目录级的排除不会被上浮到
  未被排除的父目录。空白 `--project` 曾连这道新门都能绕过——实测它在 `--project .` 被
  拒绝之后的下一条命令里，把一行写进了被排除项目的数据库。而 installer 的那道门加上之后
  被发现是**不可达的**：它在全文件唯一一处 `sys.path` 准备**之上 34 行**就 import
  `core.modes`，于是一个进程里第一次点 Initialize Project 时抛 `ModuleNotFoundError`
  直接落进 `except ImportError: pass`，项目照样被建脚手架。**第二次**点就正常了——所以
  谁点两下测试就永远看不见它。一个 grep 式的测试给这道门开了绿灯；抓住它的是行为级测试：
  把每个建库者放进全新子进程里真跑一遍。
- **文件系统根不再是候选**——这本来就是 `_chain` 的 docstring 一直宣称、而代码从未做到的
  事——但对携带 `.ccm-root` 的根有豁免，否则这条规则会悄悄压过固定标记。而 `.ccm-root`
  现在也压过容器启发式：一个仅仅"长得像容器"的被固定目录此前会被丢出候选集，等于那个
  写在文档里的逃生门什么也没做。
- **两处 hook 契约违反。**`core/logger.py` 在模块级绑定 `Path.home()`，而 `Path.home()`
  在解析不出 home 时会抛异常——于是"导入 logger"这一句本身就把四个 hook 送到 rc=1 并
  在 stderr 上留下 traceback。另外 `pre_compact` 是唯一没有 `isinstance(cwd, str)` 守卫、
  同时又是唯一无条件 mkdir 的 hook，所以 `{"cwd": 123}` 会在 hook 进程自己的目录里建库。
- **建库时不写 `memory/.gitignore`**——正是这一处遗漏，让一个 184 KB 的 `memory.db`
  混进了姊妹仓库的三个提交。写这个文件本来是每个调用方各自的责任，于是每个调用方都忘了
  （光 `cli/mem.py` 就有十三处开库），所以它现在挪进了 `MemoryDB.__init__`——也就是
  创建那个目录的那一行。

### 之上还做了一整轮对抗式代码审计

十二个 framing——安全、并发、资源耗尽、LLM 信任边界——每条发现都在被采纳前先复现，
两条因复现不出来而被否决。最要紧的三条：

- **自动清理器一直在销毁写入侧刚刚接受的记忆。**`core/consolidate.py` 里有第二个长度
  下限：20 字符，对着写入侧的 10，而且走的是**删除**不是归档。实测：
  `/cc-mem add note "lr=3e-4 wins"` 打印 `[inserted] #1`，五个回合后 `memories` 表
  **一行不剩**——发生在插件自己的热路径上，不需要任何攻击者。
- **两条渲染路径不转义权限标记。**`CLAUDE.md` 承诺这道防御跑在"**每一条**渲染路径"上，
  然后只点了四个渲染器；MCP 服务器和 CLI 不在其中。本仓库自己的数据库有 307 条活跃行，
  其中**已有 2 条是武装的**——同一行经 SessionStart 被转义、经 MCP 原样直出。
- **一个 NUL 字节就能把只读搜索变成全文索引全量重建**，可从 viewer 的 `?q=%00` 触达，
  也可从 `memory_search` 工具触达——它的 `minLength: 1` 恰好被一个孤立 NUL 满足。

另外还修了：会话标记的跨事务丢更新、Linux 上世界可读且可被符号链接劫持的每会话标记、
viewer 无上限的线程集合、consolidation 里无上限的 pairwise 阶段，以及一个"被拒绝"与
"成功"无法区分的 `PRAGMA journal_mode`。

## 此前 — v2.7.0 有什么新变化

**v2.6.0 把安全守卫挂在了某一档的内层循环上，而不是挂在候选集合上——于是每一个没继承到
守卫的档位都变成了一个独立的数据完整性缺陷。**一轮收敛式对抗 debug（五个维度，每条发现
都由两名独立验证者对着真码复核）确认了 45 项缺陷；最严重的三项在修复前都已被复现。

- **`_candidates()` 只过滤一次，供所有档位共用。**项目容器目录与依赖树在任何一档都不再
  是候选。此前：在项目文件夹里跑过一次会话产生的 `memory/`，会俘获它下面每一个尚未
  初始化的项目；往那里丢一个杂散 `package.json`，对每一个无标记目录效果相同；而
  `node_modules/left-pad` 里的 cwd 会锚在**那个包**上，把数据库种在报告器永远看不到的地方。
- **monorepo 现在会解析到 workspace——正如文档一直宣称的那样。**标记档的向外延伸不再要求
  标记连续：`packages/`、`apps/`、`crates/`、`libs/` 都没有清单文件，v2.6.0 因此停在子包、
  重新制造出它本要防的野生库。
- **`_is_profile_dir` 现在要求 `Users`/`home` 位于文件系统根之下。**没有这个限定，任何仓库
  内的 `users/` 目录都会被当成 home 而截断整条链——防缺陷的守卫亲手制造了缺陷。
- **`project_root` 现在是真的永不抛异常**，包括 cwd 根本不是路径的情形；v2.6.0 自己的
  异常处理里又抛了一次，把 hook 送到 rc=1 并在 stderr 上留下 traceback。`user_prompt.py`
  也补上了其余五个 hook 早就有的字段类型守卫。
- **不只 hook，所有入口都锚定。**`cc-mem --project` 与 `/ccm-load` 也会解析，因此从子目录
  跑 `/cc-mem add` 不再能造出 hook 拒绝创建的那种野生库。重定向一律打印。
- **野生库报告器恰恰在野生库最可能出现的地方是瞎的**：它比要求的深度少走一层，还跳过了
  `vendor`、`node_modules` 等九个目录名。两者都已修；计数改为只算活跃行，并且可证明地
  不会写入被它检查的数据库。

## 此前 — v2.6.0 有什么新变化

**每个 hook 都从 `cwd` 里读项目，而 `cwd` 会跟着 agent 自己的 `cd` 走。**一个在
仓库根启动、却在 `cli/` 里跑过一条命令的会话，从此上报 `<root>/cli`，于是
`UserPromptSubmit` 就在那儿建了第二个完全独立的数据库。六个 hook 里有四个只判断
`memory/memory.db` **存在**，所以野生库一旦诞生就自我维持——实测其中一个有 27 条
记忆和自己的 `projects` 行，而两级之上真正的库里有 161 条。

- **`core/roots.py` 先解析出项目根。**候选祖先链在任何 home 目录之下、文件系统根
  之下、`.ccm-root` 钉之处以及 25 层处停止；在其上依次尝试：cwd 自己就有数据库
  （终止档）→ **最近**的、拥有数据库的祖先 → 链内的 `CLAUDE_PROJECT_DIR` →
  项目标记（`.git`、`.hg`、`.svn`、清单文件），最近命中后向外延伸 → 原样的 cwd。
  永不抛异常：任何失败都退回 v2.6.0 之前的答案。
- **预防，而不是迁移——已存在的数据库永不被覆盖。**初版取的是**最外端**那个拥有
  数据库的祖先，想借此治愈野生库。一次对抗式评审用实地数据把它否掉了：上报机器上的
  20 个数据库里，**有 4 个是合法嵌套**在另一个项目里的——其中一个有 3725 条记忆并
  自带 `.git`。野生库与刻意的子项目在磁盘上逐字节相同，治愈前者就等于弃养后者。所
  报告的 bug 改由"不让野生库诞生"来修复；收编已存在的那种意味着合并两个 SQLite
  文件——那是需要确认的命令该干的事，不是 hook 该干的事。
- **不需要 git。**数据库那两档不依赖任何版本控制系统、不依赖任何清单文件——对根本
  不是仓库的项目，这一点是决定性的。标记那一档负责在任何数据库存在之前就阻止野生库
  诞生，其向外走行有三道天花板：6 层、版本库根、以及"绝不返回项目**容器**目录"
  （上报机器的项目文件夹有 27 个项目子目录——没有这道防线，往那里丢一个标记就会把
  它们全塌进同一个数据库）。
- **home 边界是双份的——环境**加**结构。**容器、CI、`sudo` 以及本项目自己的测试
  沙箱都会改写 `HOME`/`USERPROFILE`，因此任何名为 `Users` 或 `home` 的目录的直接
  子目录一律按用户配置根处理。实测：把 `HOME` 指向沙箱后，上行走了七层、走出临时
  夹具、进入真实配置目录，并命中了某次在 `~` 里运行的会话留下的 `memory.db`。
- **`.claude/` 不再是标记**（用户 home 里就有一个，接受它会让 `~` 自己看起来像个
  项目），`CLAUDE.md` 从来都不是——Claude Code 支持逐子目录的 `CLAUDE.md`。
- **锚定发生在 `excluded_projects` 退出开关之后，绝不在之前。**先解析会因为爬到
  未被排除的父目录，把按子目录设置的排除范围稀释掉。
- **野生库只报告，既不合并也不删除。**`cc-mem status` 现在会列出项目根之下每一个
  独立数据库及其记忆条数，v2.6.0 之前诞生的野生库不再隐形。`.ccm-root` 把嵌套项目
  钉成独立的根。由 `tests/test_surfaces.py` §7 钉死——它的 18 条阶梯用例包含了上面
  说的"嵌套子项目"与"项目容器"两种形状。

## v2.5.6 有什么新变化

**换计划那道门只守 `steps`。它一直如此——2026-08-05 这件事让一份在用的计划
为此丢了十条成功判据里的两条。** 替换顺利通过、什么都没打印，而蒸发的那两条
里有一条是「已达成却从未被记录」的发布闸。一道只覆盖制品一部分的门，对其余
部分什么也没说；那份沉默和「通过」长得一模一样。

- **`success_criteria` 失配播报。** `plan-set --from-refiner` 现在会在替换
  **之前**快照旧计划，并打印每一条「其对新计划判据（外加 `goal` 与 `context`）
  的最佳 trigram-Jaccard 低于步骤门同一个 `0.5` 阈值」的判据。被并进新 context
  的判据算作已继承：有损的存活仍是存活，狼来了只会训练读者跳过它。
- **刻意是播报而非第二道拒写门。** 判据会被改写、合并、翻译、因达成而退役。
  英文计划被中文计划取代时自动继承率为零，硬门会让正常的计划演进无法进行。
  它换来的是：「它消失了」和「我有意退役它」不再长得一样。
- **播报会点名自己的盲区。** 最后一行写明 `context` 是自由文本、从不比对，
  请自己重读。一道藏起自己射程的门，正是这次翻车的成因。
- `unmatched_criteria()` **有意追加在 `core/plan.py` 末尾**：本仓库用约 600 条
  `file:line` 引用来记录契约，插在 `check_carryover` 旁边会让四份文档里约 60 条
  引用集体腐烂。主题内聚性输给了「不要打断引用图」。
- 由 `tests/test_plan_carryover.py` §7 钉死——核心结果、context 并入的抑制、
  以及 CLI 确实把它打了出来。一个没人呈现的核心函数，等于换了个方式继续沉默。

## v2.5.5 有什么新变化

**文档门禁只覆盖了本仓库 13 个 markdown 文件中的 7 个。** 被问到"是不是所有文档都
对齐了"时，去查了一遍，结果发现**陈旧的正是门禁的覆盖范围本身**。

- `tools/citation_check.py` 现在跟踪**全部 13 个** markdown 文件——`CHANGELOG.md`、
  两个子代理提示词、`commands/cc-mem.md` 和两个技能此前不受任何检查。
  `smoke_test.py` 会断言跟踪清单等于 `git ls-files "*.md"`。
- **文档里可计数的断言也纳入门禁**，其中三条已经漂移：`CLAUDE.md` 告诉下一个 Claude
  跑八道门禁中的七道；`commands/cc-mem.md` 只列出了 28 个 CLI 子命令中的 23 个
  （漏掉的五个里包括 `sql`，它的只读守卫是一个安全修复，而用户不知道这个命令存在就
  用不上）；本 README 自己则在引用门禁已经生效三个版本之后，仍写着"没有任何门禁"。

## v2.5.4 有什么新变化

**零已知残留。** v2.5.3 关掉了六条里的五条，同时又记下四条新的；这一版把那四条全部
关掉，并为每一条补上门禁。

- **每一条引用都受检——未受检从 253 条降到 0。** 没有可锚定符号的引用现在改为边界
  检查（必须落在文件内、且不是空行），文件名有歧义的则按符号消歧。仅这一项边界检查
  就查出 **34 条失效引用**——指到文件末尾之外或指向纯空行——而这些是此前每一个版本都
  发布出去了的。
- **`settings.json` 的丢失更新在两个方向上都被关掉。** v2.5.3 只在改名**之前**核对
  摘要；现在增加了写后校验，所以落在改名**之后**的并发写同样会被检测到。
- **PLAN.md 与 MEMORY.md 不再变陈旧。** 用重试**次数**是错的形状——文件不可用的是一段
  **时长**。改用 3 秒墙钟预算后：150 轮写入、三个满负荷读取者，陈旧渲染 0 次；而 12
  次固定重试会丢 2 次。
- **dashboard.exe 现在也真的被执行**，不再只看 PE 头。

## v2.5.3 有什么新变化

**把 v2.5.2 的"已知残留"清单清掉。** 这一版没有新审计——它只处理上一版记录下来但
没修的六条残留。其中两条实际比当初写下的更严重：

- **那三份"刻意保留的字面孪生"根本不是孪生。** `core/progress.py` 会重试并重新抛出；
  `core/plan.py` 和 `llm/memory_writer.py` 没有重试，失败时**回退到会截断的普通写**
  ——把它们本来要消除的撕裂读缺陷又装了回去。那个回退**就是**那条残留本身。现在只有
  一份实现（`core/atomic.py`），契约写死：要么整体替换，要么抛出，绝不截断。
- **计划修改函数仍然接受不带作用域的调用。** `plans.id` 在整个数据库文件里是全局的，
  所以不带作用域的 `UPDATE`/`DELETE` 会打到别的项目的行上。树内全部 11 个调用点本来
  就是用关键字传 `project_id` 的，所以把它改成必填零成本——现在是必填且仅限关键字。

其余四条：配置取保守侧时现在会在 SessionStart 说明原因，而不再只写进日志文件；
安装器现在能**检测**到并发写 `settings.json` 并重做合并，而不是把对方覆盖掉；
安装器 exe 现在是真的被**运行**（12/12），而不是只看 PE 头——这一跑立刻发现它会静默
忽略无法识别的参数，于是打错的 `--unistall` 会执行一次*安装*并以 0 退出；文档引用的
受检比例从 594 条里的 224 条提高到 341 条。

## v2.5.2 有什么新变化

第三轮审计，打的是前两轮完全没用过的角度——时间、并发、跨表面一致性、恶意输入。
完整细节见 [CHANGELOG.md](CHANGELOG.md)；这里是最要紧的四条：

- **存进记忆的内容能伪造出完整的 `<system-reminder>` 块**，既进 SessionStart 注入
  （**插件自己只发 1 个，实测 stdout 里出现 8 个**），也进 PROGRESS.md。而
  `memory_add` 是模型可自行调用的 MCP 工具，所以一次间接注入——一个恶意 README、
  一个抓取到的网页、一份依赖的源码——就会变成**永久**记忆，此后每次会话开始都被
  当作权威上下文重新注入。现在的做法是**转义而非删除**，写入路径和每一条渲染路径
  （PROGRESS.md、注入、PLAN.md、MEMORY.md）都做，所以文本仍可读，只是不再携带权威。
- **隐私退出机制有两种会自己静默关掉的方式**：`config.json` 带 UTF-8 BOM
  （PowerShell `Out-File` 的默认行为）会让 `json.load` 抛异常并被吞掉；名单里有一个
  无法展开的 `~user` 条目，会让它之后的所有条目全部失效。现在解析按 `utf-8-sig`
  读取，单个坏条目无法中断整个循环，而且遇到无法使用的配置**取保守侧**。
- **MCP 服务完全无视 `excluded_projects`**——它是这项控制的第七个调用方，而 v2.5.1
  刚刚把前六个钩子接好；偏偏它是默认加载、且每次调用都由模型自行发起的那一个。
- **并发写和同秒写会毁数据**：12 次压缩只留下 3 份会话归档；四次**顺序**的计划替换
  只留下 1 个历史文件；PROGRESS.md / MEMORY.md / PLAN.md 会被读到 0 字节。现在全部
  先原子占位、再经 `os.replace` 落盘。

另外：`MemoryDB._connect` 不再每次操作泄漏一个 sqlite 句柄（20 次插入后从 25 个活
连接降到 0），并且 `tools/citation_check.py` 开始为文档里的 `file:line` 引用把关
——它第一次运行就查出 **594 条里有 163 条是失效的**。

## v2.5.0 有什么新变化

迄今为止最大的一次正确性发布，而不是功能发布：跨 26 个文件关闭了约 134 个缺陷，
随后又由四个只读的对抗式验证者重新攻击，它们查出的问题也一并关闭。下面这些条目
在已发布的代码里全都是**静默错误的**。

### 跨项目数据污染已关闭

Claude Code 的 `~/.claude/projects/` 目录名会把 `[A-Za-z0-9]` 之外的**每一个**字符
替换成 `-`；而 cc-memory 只替换了其中三个（`:` `\` `/`）。因此任何路径里含 `_` 或
`.` 的项目都会算出一个并不存在的 slug——而这次未命中会继续掉进对本机**全部** slug
目录的**模糊子串搜索**。在参考机器上（179 个 slug 目录），子串 `core` 命中了其中
131 个，`app` 和 `data` 各命中 141 个。于是一个项目可能摄取另一个项目的 transcript、
把它送去抽取用的 LLM，再把结果当作自己的记忆存下来。

- `core.extractor.mangle_project_path` 现在是 slug 约定的唯一真源，模糊兜底分支被
  **删除**——未命中就意味着「没有 transcript」，而不是「猜一个」。此前会解析到真实
  但错误目录的四个探针现在一律返回 `None`。
- 追溯保存要求**正向证明** transcript 属于本项目（记录自带的 `cwd` 字段），无证据即
  失败闭合。用两份植入的 transcript（其中一份是外来的）实测：2 条 LLM 腿摄取
  `['aaaa-foreign', 'bbbb-mine']` → 1 条腿、`['bbbb-mine']`；完全没有 `cwd` 的
  transcript → 0 条腿、0 条记忆。
- 第 3 级的 PROGRESS.md 挖掘同样加了闸门，但故意用更弱的「分歧」规则（缺失 `cwd`
  放行，`cwd` **不同**才拒绝），好让没有 `cwd` 的 transcript 继续可用。此前会产出
  `open_todos=['FOREIGN TODO leak']` 与 `files=['FOREIGN_SECRET_FILE.py']` 的外来
  transcript 现在什么都不产出，并记录一条拒绝日志。
- 面板里逐字复制了同一份模糊解析器。那份也一并删除了。

### v2.2 的实时计划锚点第一次真正通过它的钩子跑通

`core.modes.should_observe()` 为假时 `PostToolUse` 会提前返回，而整个实时计划块就坐
在那道闸门**下面**。`TodoWrite` 在每种模式的 `skip_tools` 里，`ExitPlanMode` 不在任何
模式的白名单里，于是三种模式下 `should_observe` 对这两个计划控制工具全都是 `False`：
钩子从未写入过 `plan_active`，`memory/.plan_raw.md` 与 `memory/PLAN.md` 从未出现，
guardian 的漂移计数还会随模式静默变化。计划控制不是观测——该块现在跑在闸门之上，
所有模式一视同仁。

| 经 `PostToolUse`，按模式（code / research / writing） | 之前 | 之后 |
|---|---|---|
| `ExitPlanMode` → `plan_active` 行数 | 0 / 0 / 0 | 1 / 1 / 1 |
| `TodoWrite` → PLAN.md 步骤状态 | 纹丝不动 | `[x] [x] [~] ← ACTIVE` |
| `Edit` → `edits_since_last_guardian` | 1 / 0 / 1 | 1 / 1 / 1 |
| Bash `git push` → 计数（1 次编辑 + 20） | 21 / 20 / 1 | 21 / 21 / 21 |

等待精炼的原始计划现在也看得见了：`PLAN.md` 与 `/cc-mem plan-status` 会以一条
**PENDING REFINEMENT** 横幅加原始文本开头，并把更旧的结构化计划明确标注为陈旧，
而不是像以前那样只显示那份已被取代的计划。

### 隐私过滤器曾经**失败开放**，现在是线性时间且失败闭合

`strip_private` 原本是 `re.sub(r"<private>.*?</private>", "", text)`，外面套一层
`text.count("<private>") > 100` 的 ReDoS 守卫——而那道守卫是**原样返回文本**。超过
上限后，`<private>` 的内容会同时抵达 Anthropic API 调用和 `memories` 表：100 个标签
剥除正确，101 个就泄漏。

这个上限还校准在错误的信号上。格式良好的标签是廉价的；**未闭合**的 `<private>` 才是
二次方那一档——16000 个未闭合标签（140.6 KiB）让 `re.sub` 跑了 **9517.4 ms** 并泄漏
了尾部。取而代之的是一次从左到右的 `str.find` 扫描：同样输入 0.0 ms，20000 个格式
良好标签上 5.1 ms 对 6.0 ms，完全不需要上限，且悬空的开标签现在会丢弃其后的全部
文本而不是把它发出去。等价性在 20000 个随机标签汤输入上得到验证——13328 个格式良好
的输入零行为差异；6672 个未闭合的输入差异是刻意的。

### MCP：线上表现正确，且 schema 真的被强制执行

- **双向 UTF-8，且在句柄被捕获之前完成。** 在本机默认的 gbk 编码下，7 个非 ASCII
  载荷里只有 1 个能字节级往返；在严格 gbk 下有五个根本得不到任何响应，服务端以 1
  退出。现在在测试的四种区域设置下全部 7/7，stderr 0 B，退出 0。**不再需要任何
  `PYTHONUTF8` 环境变量兜底。**
- **每一条被解析出的、带 id 的消息都恰好得到一帧回复。** `params: null`——许多客户端
  就是这样表示「无参数」的——此前**没有任何响应**，会让没有超时的客户端永久挂起。
  非对象 `params` 和非字符串工具 `name` 也一样。现在全部回 `-32602`；此前 8 个 id
  成为孤儿，现在 13/13 全部应答。
- **没有任何东西能逃出 `main()`。** 一个 4301 位的整数（`ValueError`）和约 3000 层的
  嵌套（`RecursionError`）都能**在校验运行之前**通过公开的工具参数触达，并让服务端
  带着 stderr 上的回溯崩溃。帧现在也有长度上限，且双向严格遵循 RFC 8259
  （`NaN` / `Infinity` 被拒绝而不是原样回显）。
- **`tools/call` 的参数会依据其公布的 `inputSchema` 校验**（required / 类型 / enum /
  取值范围 / 长度），不合规直接回 `-32602` 而不是强制转换。`memory_search` 不带
  `query` 时曾经会倒出整张表并重建 FTS 索引；六个畸形查询触发过六次索引重建
  （27.3 ms），现在一次也不触发（6.1 ms）。`memory_get_details` 不再返回已撤回的行，
  失败的写入也不再被报告为成功。

### Web 面板此前完全不可用，而且是一条提示注入通道

- **一个空闲 TCP 连接就能卡死整个服务。** 单线程的 `HTTPServer` 加上没有处理器超时，
  意味着浏览器的预测性预连接——`webbrowser.open` 按设计就会触发——会阻塞其后每一个
  请求。现在改为多线程 + 守护线程 + 每连接超时：8 个空闲预连接 → 0.02 s 内 200，
  30 个并发 GET → 30/30。
- **每个响应都带 `Access-Control-Allow-Origin: *`。** 通过 `POST /api/memory` 写入的
  任何内容都会被注入你的下一次会话，所以你开着的任意网页都能往你自己的上下文里写
  东西——并且能读 `/api/sessions`，那里会返回 `archive_path` 这样的文件系统路径。
  该响应头已删除；`Origin`、`Host`（用于防 DNS 重绑定——攻击页面此时是同源的，
  根本不发 `Origin`）和 `Content-Type: application/json` 现在全部强制校验。
- **`POST` 会重写错误项目的 `MEMORY.md`**——它从 `os.getcwd()` 而不是被服务的项目
  推导项目根。
- 四条路由对畸形查询**根本不返回任何 HTTP 响应**；请求体读取没有墙钟期限，因此一个
  每隔几秒滴一个字节的客户端能把一条工作线程占用数小时（实测：403 从 52.09 s →
  3.02 s；10 个并发滴入，线程增量 10 → 0-2）。
- 文档早就描述过的 `Add memory` 表单，现在页面里真的有了。

### 独立安装此前不发布任何用户可见界面

独立安装之后 `~/.claude` 里只有 `hooks/` 和 `settings.json`，别无他物——没有 `/cc-mem`
命令、没有 `plan-refiner` / `plan-guardian` 子代理、没有技能。安装器现在会复制全部
五个，记录到 `installed_surfaces.json`，并在卸载时**按名字**精确删除这五个文件
（你自己放在 `commands/`、`agents/`、`skills/` 里的东西不受影响）。

它此前还会在遇到任何无法解析的 `settings.json` 时**在复制完文件之后崩溃**——JSONC
注释、尾逗号、UTF-8 BOM（PowerShell 的 `>` 写出来就是这个）、或形状异常的钩子组——
留下一棵装了一半、却没有注册任何钩子的树。现在设置会在第 [0/3] 步、任何复制发生
之前完成校验，畸形形状被逐字保留而不是被搅碎，仅仅**提到** cc-memory 的钩子会被
保留并给出提示而不是删除。19 种设置形状 × 6 种操作：**18 次崩溃 → 0**。

### 有硬性宿主超时的钩子现在会给自己的 LLM 墙钟设界

`call_llm` 自己的 docstring 就要求有时间预算的调用方给最坏情形设界；只有
`core/consolidate.py` 做到了。`hooks/session_start.py` 在出厂默认配置下就会超出它的
**15 s** 预算（2 个凭据候选 × 20 s = 40 s），`hooks/stop.py` 在腿被拖住时实测
**25.45 s** 对 22 s 预算。每一个调用 LLM 的钩子现在都会传入一个**绝对期限**——而不只是
单腿超时——它会把每条腿夹到实际剩余的时间，并跳过跑不完的腿。Stop：25.45 s →
15.99 s。PreCompact：约 144 s → 120 s 预算中的 74.39 s。常规路径延迟不变
（0.29 s → 0.30 s）。

### 本次发布的其他内容

- **只有一个版本字符串。** `cc_memory/core/version.py` 是运行时的权威来源，在嵌套和
  扁平两种安装布局下都可导入；`cc_memory/__init__.py` 转出口它，CLI、MCP 和安装器的
  横幅都改为在运行时解析而不再各带字面量（其中两处此前已经陈旧）。
- **`/cc-mem status` 看得见独立安装了。** 此前每一处布局探测都假定存在嵌套的
  `cc_memory/` 路径段，于是一个健康的扁平安装被报成 22 个文件缺 22 个，API key
  检查也被整个跳过。
- **`config.json` 不再说谎。** 两次审计发现 51 个叶子键里有 34 个没有任何读取者；
  所有惰性键已删除，留下的都在文件内注明了它的读取者。`excluded_projects` **不是**
  新增的键——它在 v2.4.3 就已随包发布，默认值为空，且全仓库没有任何读取者——但它
  现在是一个真正的退出开关：被列出的目录及其下的所有内容都不会得到 `memory/`、
  数据库、observation、抽取和 PROGRESS.md，因为**全部六个钩子**都会先检查它，再做
  别的任何事。
- **`/cc-mem sql` 真的是只读了。** `DROP TABLE topics` 此前会以 0 退出并把表删掉。
  该守卫同样拒绝 `PRAGMA name(value)` 这种赋值写法——SQLite 把它当作
  `PRAGMA name = value` 的等价形式，而只看 `=` 的检查会把它放过去。面板的 SQL
  控制台在任何写操作之前都要求一次点名该语句的确认，并报告 rowcount。
- **面板不再毁坏数据**：批量删除改为批量**归档**（不再留下悬空的 `supersedes_id`
  行），tidy 之后会重新生成 `MEMORY.md`，损坏的项目注册表在被覆盖前先备份，而且
  没有任何回调能在 windowed 构建里抛出异常。它还新增了一个只读的
  **Progress / Plan** 标签页。
- **第三个测试套件。** `tests/test_surfaces.py` 与 `smoke_test.py`、
  `test_plan_carryover.py` 并列成为发布门禁，覆盖另外两者都没碰的表面：独立安装器
  （界面的按名安装/卸载、畸形 `settings.json` 处理、与 `hooks/hooks.json` 的超时
  同步）、MCP stdio 服务、Web 面板的请求守卫，以及「每个调用 LLM 的钩子都必须传入
  绝对期限」这条规则。

### 哪些**没有**修复

如实记录，因为每一条都是实测而非假设：

- Web 面板是多线程且**没有工作线程上限**的——请求体读取现在有期限约束，但
  `ThreadingHTTPServer` 仍然是每连接一线程。它只监听回环地址。HTTP/1.0 的流水线
  行为未变（浏览器不做流水线）。DNS 重绑定的修复是用伪造 `Host` 头验证的，不是用
  真实 DNS 控制；SPA 的转义加固属于纵深防御：并没有真的执行出 XSS。
  *（上限那一半已在 v2.8.0 关闭：`_MAX_CONCURRENT = 16` 准入信号量，超额连接
  以 503 拒绝——`tests/test_surfaces.py` 驱动两个半边。回环监听与伪造 `Host`
  两条保留意见仍然成立。）*
- MCP 仍会原样回显数组/对象类型的 `id`（不合规但属合法 JSON；能应答总好过让它成为
  孤儿），无法解析或超长的帧会以 `"id": null` 应答，因为它的 id 确实无从得知。
  巨大单行导致的 `MemoryError` 从未被复现——1 MiB 的帧上限是按逃逸类别推定的，
  而不是由实测崩溃得出的。
- `core/db.py` 的三个计划变更函数（`update_plan_status`、`delete_plan`、
  `update_plan_content`）都接受 `project_id`，且所有随包发布的调用方都已传入，但
  它们都不**强制要求**这个参数——新代码里一次未加限定的裸调用仍会跨项目，因为
  `plans.id` 在整个数据库文件范围内是全局的。
  *（已在 v2.5.3 关闭：三个函数的 `project_id` 均为必填且仅限关键字，
  `tests/smoke_test.py` 断言其签名。）*
- 搜索一个裸的 `%` 或 `_` 现在返回 0 行而不是整张表。这正是修复本身，但它是一个
  用户可见的结果变化。
- **文档里的 `file:line` 引用自 v2.5.2 起已有门禁**，由
  `tools/citation_check.py` 强制，并在 `tests/smoke_test.py` 内部运行。自 v2.5.5
  起它覆盖仓库里**全部 13 个** markdown 文件，且不留任何未受检项：一条引用要么被
  锚定到符号的 `ast` 定义或引用它的行，要么（句中没有可解析符号时）改为边界检查
  ——必须落在文件内且不是空行。行号失效请用
  `python tools/citation_check.py --fix` 修，不要手改。
- `tools/i18n_check.py` 只比较内容哈希。它看不见**正文**已经与英文源漂移的译文
  ——包括失效的文档内锚点，正是这一点让 22 个死锚点在中文文档里一直存活到 v2.5.1。

## v2.4.2 有什么新变化

钩子存活性。在长期运行的项目里，`PreCompact` 钩子会被**中途杀死**——那一次压缩的记忆
全部丢失；而且它的抽取一直在悄悄地读 transcript 的错误一端。根因只有一个：钩子把
**整个** transcript 读进内存，却只用了其中约 12 KB。

- **有界 transcript 读取。** `extractor.load_transcript_window` 改为只读 head + tail
  窗口（40 条记录 + 32 MiB），不再读整个文件。在真实的 **2.11 GiB** transcript 上实测：
  加载从 **88 秒降到 1.66 秒**，完整钩子运行 **14.33 秒**（预算 120 秒）。`msg_count`
  通过裸记录扫描保持**精确**（比解析便宜约 40 倍）。
- **抽取现在读的是最近的一端。** LLM 摘要此前从最旧的记录开始填满 12,000 字符预算——在
  那个 transcript 上，预算在 **约 585,000 条记录中的第 329 条**就耗尽了，因此每次抽取
  只看得到会话最开头的几个小时。现在改为从最新往回填。
- **被杀的运行不再无声无息。** 超时杀进程不会执行 `except` 块，所以失败的压缩过去只会
  留下描述**上一次成功**的 `.last_save.json`。现在 `PreCompact` 在入口写一个标记，只有
  完整跑完才删除；SessionStart 会报告残留的标记。
- **自动压缩现在看得见了。** `.last_save.json` 记录触发方式是 `auto` 还是 `manual`——
  Claude Code 只在手动 `/compact` 时在界面上显示钩子执行，这让自动运行看起来像从未发生。
  （它一直都在发生。）
- **`memory/.gitignore` 会迁移已有安装**，而不再是只创建一次，因此生成的状态——包括
  `.plan_history/` 里逐字保存的计划原文——不会再泄漏进用户的仓库。
- **修复：这个包此前根本无法构建或安装。** `pyproject.toml` 里的 UTF-8 BOM（自 v2.4.0
  起）让 `tomllib` 解析失败，破坏了所有 PEP 517 前端。

## v2.4.0 / v2.4.1 有什么新变化

- **强制的计划结转闸门。** `plan_active` 是单行槽位，因此替换计划过去会悄悄地把未完成的
  步骤沉掉。现在替换要求每一个未完成步骤要么被自动结转（按标题相似度），要么带理由被显式
  处置；`/cc-mem plan-clear` 没有 `--reason` 就拒绝执行；每一份被换下的计划都会归档到
  `memory/.plan_history/`。**按设计，没有强制跳过的开关。** 参见
  [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract)。
- v2.4.1 修复了一个误拒：过长的 `notes` 字段稀释了标题匹配度，导致合法的原地更新被挡下。

## v2.3.4 有什么新变化

- **Anthropic 认证逐级回退。** 失效的 `ANTHROPIC_API_KEY` 不再把健康的 Claude 订阅
  一起黑洞掉——候选凭据按顺序尝试，且各自使用正确的传输格式（`x-api-key` 对
  OAuth `Bearer`）。
- **本地 Ollama 兜底改为按需开启**（`ccl.enabled`，默认 `false`）。

## v2.3.3 有什么新变化

- **文档多语言版本控制。** 英文是规范骨架；中文文档是受漂移跟踪的 `*.zh.md` 兄弟文件
  （从 [README.zh.md](README.zh.md) 开始），每一份都通过首行标记绑定到其英文源的
  归一化 sha256。一个纯标准库的检查器（[tools/i18n_check.py](tools/i18n_check.py)）
  加上 [tests/smoke_test.py](tests/smoke_test.py) 门禁，会在英文文档改动而对应译文未
  刷新的那一刻立即变红。记忆*内容*保持与语言无关——只有文档被跟踪。参见
  [docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n)。

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
  [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract)。
- **插件自带子代理。** `plan-refiner` 把原始计划规范化为 JSON；`plan-guardian` 在
  漂移计数触发时检查一致性。定义位于 `agents/`，安装后自动被发现。
- **`/cc-mem dashboard`** 子命令：无需知道插件安装路径即可启动 Tkinter GUI。

## v2.1 有什么新变化

- **反补丁写入。** 每一次保存都经由 `llm.memory_writer.upsert_smart`，它会按三元组
  Jaccard 相似度选择 MERGE（就地覆盖相似记忆）、SUPERSEDE（归档旧记忆并用
  `supersedes_id` 链接新记忆）或 INSERT。不再有堆叠的重复项。参见
  [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract)。
- **经由 PROGRESS.md 的强制交接。** `memory/PROGRESS.md` 是会话交接的唯一真相来源，
  始终从一条 SQL 记录整篇重写，绝不追加。SessionStart 会发出一个 `<system-reminder>`
  块，要求下一个 Claude 在回应之前先读它。参见
  [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract)。
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
0. **在复制任何东西之前先校验 `~/.claude/settings.json`。** 如果它无法被解析
   （JSONC 注释、尾逗号、顶层数组），安装器会以 `Nothing has been installed.` 拒绝，
   而不是留下一棵装了一半的树。UTF-8 BOM——PowerShell 的 `>` 写出来的那个——被容忍。
1. 把子包目录树复制到 `~/.claude/hooks/cc-memory/`。这棵树是**扁平**的：
   `core/`、`hooks/`、`llm/`、`cli/`、`mcp/`、`ui/` 直接位于该目录之下，
   **没有 `cc_memory/` 这一路径段**。（市场 / 开发检出仍保持嵌套的
   `<plugin root>/cc_memory/…` 形状。）
2. 把五个面向用户的界面装进 `~/.claude/`：`commands/cc-mem.md`、
   `agents/plan-refiner.md`、`agents/plan-guardian.md`、`skills/ccm-load/SKILL.md`、
   `skills/save-memories/SKILL.md`。它们会被记录到
   `~/.claude/hooks/cc-memory/installed_surfaces.json`，卸载时**按名字**删除，
   所以你自己放在那些目录里的文件不会受影响。
3. 向 `~/.claude/settings.json` 添加钩子条目（横跨 5 个事件的 6 条命令——
   `PreCompact` 声明一个同步支路 + 一个 `async` 支路），超时值与
   `hooks/hooks.json` 声明的完全一致。你自己那些仅仅提到 cc-memory 的钩子会被保留
   并报告，绝不删除。
4. 清理上一个版本遗留的陈旧模块，并自动检测、升级任何 v2.0 扁平布局的旧安装。
   卸载时 `logs/` 会被保留。

按项目的初始化是**自动**的——第一条用户消息会创建 `<project>/memory/` 和 SQLite 数据库。
要让某个目录完全退出，把它列进 `config.json` 的 `excluded_projects`（见
[配置](#配置)）。

## 架构速览

```
钩子（声明于 hooks/hooks.json；经插件清单被发现）：

  UserPromptSubmit ─► 回合计数 + 用首条提示为 PROGRESS.md 播种
                      首次接触时自动初始化 memory/

  PostToolUse     ─► 实时计划锚点，所有模式一视同仁：ExitPlanMode → plan_active.raw、
                     TodoWrite → 机械式步骤同步、编辑/敏感 Bash → 漂移计数
                     为每一次被观测的工具调用插入一条 observation 行（不调用 LLM）

  Stop            ─► Haiku 观察者从本回合抽取记忆
                     patch_progress(files_touched=...)
                     每 5 个回合做一次空闲整理

  PreCompact      ─► 触发两个钩子：
                     • 同步 (pre_compact.py, 120s)：Haiku 从有界的 head+tail transcript
                       窗口（40 条记录 + 32 MiB）抽取记忆
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
├── .consolidation.lock      防止异步工作者重叠运行
├── .pre_compact_attempt.json 起始标记；残留即表示上次运行被杀
├── .plan_raw.md             最近一次 ExitPlanMode 的原始捕获
├── .plan_history/           被替换/清除计划的追加式归档
├── .gitignore               排除 DB、会话，以及上面所有生成状态
│                            （每次压缩时迁移补全）
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
[docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n)。

## 命令行（CLI）

**在 Claude Code 内部**（推荐，与路径无关）：

```
/cc-mem status                                    # 完整健康检查
/cc-mem stats                                     # 记忆 + 取代链计数
/cc-mem list decision                             # 按类别列出近期记忆
                                                  # (all|decision|result|config|bug|task|arch|note)
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
# 注意是 $HOME 而不是 ~：bash 的波浪号展开发生在参数展开**之前**，而且不会二次扫描
# 结果，所以存进变量里的 ~ 仍是一个普通字符，`$M status` 会以
# `can't open file '.../~/.claude/...'` 失败。
M="python $HOME/.claude/hooks/cc-memory/cli/mem.py --project ."
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

该服务器通过 stdio 使用 JSON-RPC 2.0 通信，并且**自己**在 stdin 和 stdout 上强制
UTF-8 与纯 LF 换行——**不需要任何 `PYTHONUTF8` / `PYTHONIOENCODING` 环境变量兜底**。

**市场 / 开发检出——无需任何操作。** `.claude-plugin/plugin.json` 内联发布了注册信息：

```jsonc
"mcpServers": {
  "cc-memory": {
    "command": "python3",
    "args": ["${CLAUDE_PLUGIN_ROOT}/cc_memory/mcp/server.py"]
  }
}
```

**独立安装——需要手动注册。** 安装器只复制包和五个界面；它从不写客户端配置，而且
扁平树里没有可供读取的 `.claude-plugin/`。注意其中**没有 `cc_memory/` 这一段**：

```jsonc
// <project>/.mcp.json，或用户级的等价位置
{
  "mcpServers": {
    "cc-memory": {
      "command": "python3",
      "args": ["<HOME>/.claude/hooks/cc-memory/mcp/server.py"]
    }
  }
}
```

`tools/call` 的参数会依据每个工具公布的 `inputSchema` 校验，不合规直接回 `-32602`
而不是强制转换，所以一次畸形调用会响亮地失败，而不是写入你并没有要求的东西。

## 可视化仪表盘

```bash
# 市场安装或独立安装——会自动解析插件路径：
/cc-mem dashboard

# 或直接调用 CLI。市场 / 开发检出：
python <plugin-root>/cc_memory/cli/mem.py --project . dashboard
# 独立安装（扁平——没有 cc_memory/ 这一段）：
python ~/.claude/hooks/cc-memory/cli/mem.py --project . dashboard

# 或独立可执行文件（Windows）：
cc-memory-dashboard.exe
```

7 个标签页：Memories · Plans · Sessions · Keywords · SQL Console · Stats ·
Progress / Plan（PROGRESS.md 与 PLAN.md 背后 `progress` 和 `plan_active` 两行的
只读视图）。

不带 `--project` 启动时不会打开任何项目：它会列出已知项目并等你选。删除一律是
**归档**（`is_active=0`），SQL 控制台里任何非 `SELECT` 语句都需要一次点名该语句的确认。

## Web 查看器

```bash
/cc-mem serve
# 在浏览器中打开 http://127.0.0.1:9377
```

浏览、搜索，并且可以**添加**记忆：`+ Add memory` 表单会把 JSON POST 到
`/api/memory`，它和其他所有保存路径一样经由 `upsert_smart`，所以近似重复会被归并
或取代，而不是堆叠。

通过该端点写入的一切都会被注入你的下一次 Claude 会话，而 `/api/sessions` 会返回
整行会话记录（含 `archive_path`）。服务端据此加了守卫：

- 只绑定 `127.0.0.1`；
- **不**发送 `Access-Control-Allow-Origin` 头，并拒绝 `Origin` 不等于本源的任何请求
  （`OPTIONS` 回 405——从不放行预检）；
- 拒绝 `Host` 不是本回环源的任何请求。单靠 Origin 拦不住被 DNS 重绑定的页面，它的
  GET 是*同源*的、根本不带 `Origin` 头；
- POST 必须是 `Content-Type: application/json`，而 HTML 表单不经预检发不出这个。

## 计划队列（Plan Queue）

使用同一 SQLite 数据库的任务规划系统。这是计划**队列**（`plans` 表）——区别于实时
计划**锚点**（`plan_active` / `memory/PLAN.md`，由 `/cc-mem plan-*` 驱动）：

```bash
# 如果你用 pip 安装了这个包，可以直接用控制台脚本：
P="cc-memory-plan --project ."
# 或者从独立安装（扁平——没有 cc_memory/ 这一段）里跑模块。
# 用 $HOME 而不是 ~：波浪号在参数展开之前就被处理掉了，而且结果不会被二次扫描，
# 所以存进变量里的 ~ 在使用时永远不会被展开。
P="python $HOME/.claude/hooks/cc-memory/cli/plan.py --project ."

$P add "Task A" "Task B" "Task C"
$P list
$P evaluate           # 标记 draft → evaluating；Claude 评估可行性
$P approve --all      # evaluating → ready
$P exec --next        # ready → executing，并打印计划供 Claude 执行
$P done 1 "Result"    # 标记完成
$P status             # 队列摘要
$P clear              # 丢弃 done/failed/skipped
```

`exec` **不会**启动任何进程——它只翻转状态，并打印计划正文以及事后该跑的 `done`
命令。每一个需要点名计划 ID 的子命令都会先在 `--project` 范围内解析这些 ID，
遇到未知或外来 ID 即以 1 退出。

状态流转：`draft` → `evaluating` → `ready` → `executing` → `done`/`failed`/`skipped`。

## 配置

编辑安装根目录下的 `config.json` —— 独立安装（扁平布局）：
`~/.claude/hooks/cc-memory/config.json`；市场安装 / 开发检出：
`<plugin-root>/cc_memory/config.json`。

**这个文件里的每一个键都会被代码读取。** 没有任何读取者的键在 v2.5 被删除而不是
留着——一个惰性的可调项比没有可调项更糟，因为改它看起来像是起了作用。留下的是：

- `version` — 给没有 `core/version.py` 的扁平安装用的最后兜底版本号。
  `cc_memory/core/version.py` 才是权威。
- `consolidation.auto_interval_sessions` — 异步整理之间相隔的会话数（默认 5）。
- `ccl.enabled` / `ccl.ollama_url` / `ccl.local_model` — 本地 Ollama 兜底。
  **需显式开启；`enabled` 默认为 `false`**，此时 Anthropic 那几条腿是唯一后端。
- `excluded_projects` — 完全退出 cc-memory 的绝对路径。被列出的目录*及其下的一切*
  都不会得到 `memory/` 目录、数据库、observation、抽取和 PROGRESS.md：**全部六个
  钩子 <!--ce:hooks-->，外加 MCP 服务**，第一件事就是对**原始** `cwd`（锚定到项目根
  之前）咨询 `core.modes.is_excluded` 并以 0 退出（MCP 则回 `isError`）；钩子自
  v2.10.0 起统一经由共享闸门 `hooks/_entry.py:resolve_project`，该闸门独占这一
  先后顺序。那是**一份**
  共享实现，而不是每个调用方各抄一份——v2.5.0 曾把它作为两份私有副本放进仅有的
  两个会*创建* `memory/` 的钩子里，结果是：一个在被列入名单**之前**就已初始化过的
  项目仍被完整地埋点——observation 继续累积，PROGRESS.md 继续点名它的文件，而在有
  可用凭据时，Stop 观察者会继续把这些内容 POST 给 Anthropic API。v2.5.1 修好了六个
  钩子 <!--ce:hooks:asof-->，却漏掉了第七个调用方：**MCP 服务在 v2.5.1 及之前完全没有这项检查**——它由
  发布清单默认加载，且每一次调用都由模型自行发起，所以被列出的项目对模型而言仍然
  可读可写。v2.5.2 在 `_get_db` 处设卡，那是全部八个工具都必经的唯一收口。匹配基于
  解析后的绝对路径，Windows 上不区分大小写。这是唯一的退出机制。
  **自 v2.5.2 起解析失败即取保守侧（fail closed）**：如果这个文件存在但无法使用
  （JSON 非法、顶层不是对象、不是 UTF-8 文本、读不出来），则*每一个*项目都按已排除
  处理并记录原因，而不是猜成"未排除"然后把数据不可逆地写下去。文件**不存在或为空**
  不属于这种情况（没有名单，也就没有排除）。文件按 `utf-8-sig` 读取；一个无法展开的
  `~user` 条目现在退化为按字面比较——BOM（PowerShell `Out-File` 的默认行为）和一个
  写坏的 `~` 条目，以前各自都能把整个退出机制静默关掉。
- `notes` — 文件内文档，包括那些曾经住在这里的值现在归哪个模块所有。

被移除的可调项现在是模块常量，请到那里改：反补丁阈值在 `llm/memory_writer.py`、
SessionStart 注入预算在 `hooks/session_start.py`、空闲整理间隔在 `core/idle.py`、
分模式的观测跳过列表在 `core/modes.py`、Web 面板默认端口在 `ui/web_viewer.py`。
MCP 注册是 `.claude-plugin/plugin.json` 里的 `mcpServers` 块，不是某个配置键。

## API 密钥

cc-memory 会从 `~/.claude/.credentials.json` 自动检测你的 Claude OAuth 令牌。
只要你已登录 Claude Code，就无需手动设置 API 密钥。

解析顺序：`ANTHROPIC_API_KEY` 环境变量 → Claude OAuth 令牌。

## 测试

六个纯 stdlib 脚本，无需 pytest，也没有任何 pip 依赖。**九道发布门禁——全部都要跑。**

```bash
python -m compileall -q cc_memory tests tools
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))"

python tests/smoke_test.py
# 期望：一连串 [OK] 行，以 "===== ALL SMOKE TESTS PASSED =====" 结尾

python tests/test_plan_carryover.py
# 期望："RESULT: 20 passed, 0 failed"

python tests/test_surfaces.py
# 期望："===== ALL SURFACE TESTS PASSED ====="（§1-§9）

python tools/i18n_check.py       # 翻译漂移；有漂移则非零退出
python tools/citation_check.py   # 文档 file:line 引用；"0 unchecked, 0 stale"
python tools/doc_claims.py       # 散文里的计数 vs tools/contracts.py；"0 problem(s)"
```

第九道门禁是版本声明一致性：`pyproject.toml`、两个 `.claude-plugin/*.json`、
`cc_memory/config.json` 和 `cc_memory/core/version.py` 必须携带同一个字符串，
由 `smoke_test.py` 断言。

- `tests/smoke_test.py` —— 权威的端到端检查：反补丁写入器的决策、PROGRESS.md 整篇
  重写、只填空的刷新契约、last-wins 的 TodoWrite 抽取、tier-3 transcript 兜底、
  旧版 `SESSION_HANDOFF.md` 迁移、布局检查器、两支路的 PreCompact 形态、有界
  transcript 窗口、i18n 漂移门，以及自 v2.5.2 起新增的 `.gitignore` 三副本一致性、
  sqlite 句柄数回归、PLAN.md / MEMORY.md 抗伪造、唯一原子写入器规则、计划修改函数
  仅限关键字的 `project_id`，以及三道文档门禁。
- `tests/test_plan_carryover.py` —— v2.4.0 的结转门禁（20 项检查）；该特性唯一的覆盖。
- `tests/test_surfaces.py` —— v2.5 新增，自 v2.9.0 起共九节，覆盖另外两者都
  没碰的表面：
  §1 MCP stdio 服务、§2 Web 面板的请求守卫、§3 独立安装器（界面的按名安装/卸载、
  畸形 `settings.json` 处理、与 `hooks/hooks.json` 的超时同步）、§4 六个钩子 <!--ce:hooks--> 上的
  `excluded_projects`（开头先断言它自己那份钩子清单等于 `hooks/hooks.json`）、
  §5 config.json 的各种形态与 MCP 侧的同一退出机制、
  §6 `settings.json` 的比较并交换、§7 项目根锚定（真实文件系统上的整条阶梯、从子目录
  驱动同一批钩子，以及「每个钩子都在退出开关**之后**解析」这条源码级规则）、
  §8 v2.8.0 的新表面（MCP 启动项目作用域门、加界的 `memory_topics`、经由自己
  钩子在每种模式下驱动的计划锚点，以及对敌意 `package.json` 无头执行的
  `ui/dashboard.py`）、§9 v2.9.0 双视角审查的表面（CLI 的项目作用域、安装器按条
  剥离与文件缺席时的比较并交换、面清单取并集、600 KiB 的 PostToolUse 载荷、
  空 prompt 的标记覆写、MCP 的 `jsonrpc` 成员，以及报头阶段的滴流连接）。
  外加「每个调用 LLM 的钩子都必须传入绝对期限」。
- `tools/i18n_check.py` —— 按归一化内容哈希检查翻译漂移。
- `tools/citation_check.py` —— 全部 13 个 markdown 文件里的每一条 `file.py:LINE`
  引用。`--fix` 修复，`--list` 显示每条判定。
- `tools/doc_claims.py` —— 散文里**数数**的句子，对照 `tools/contracts.py` 从代码树
  推导出的集合来检查。引用门证明行号仍然指向它的符号；这一道证明围绕它的那句话仍然成立。
  自 v2.8.0 起它用同一套语法扫三个表面：受跟踪的 markdown、
  `cc_memory/config.json`，以及随包发布代码的 docstring 与连续注释段——对后
  两者的第一次扫描就抓到了三处已经错了的计数。

三个开发期检查器同样在 `smoke_test.py` 内部运行，所以套件绿了就意味着文档状态也绿：

```bash
python tools/i18n_check.py --list       # 每个 英文/翻译 配对 + 记录哈希 vs 当前哈希
python tools/citation_check.py --fix    # 就地重写失效行号
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
  （[简体中文](docs/ARCHITECTURE.zh.md)）
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — 三条硬契约
  （[简体中文](docs/CONTRACTS.zh.md)）
- [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract) — 反补丁写入契约
- [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract) — PROGRESS.md 规格
- [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract) — PLAN.md + 子代理规格
- [docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n) — 文档多语言（英文 / 中文）版本控制
- [CHANGELOG.md](CHANGELOG.md) — 版本历史

## 许可证

MIT

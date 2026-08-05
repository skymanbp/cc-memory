<!-- i18n-source: README.md | sha256: 75d264c9d23858e4 | version: 2.5.0 | translated: 2026-08-05 -->
> [English](README.md) · **简体中文**

# cc-memory

**Claude Code 持久化记忆插件（v2.5.0）**——反补丁式的写入即归并（reconcile-on-write）、
LLM 判定的语义去重、强制 PROGRESS.md 交接、带 plan-refiner / plan-guardian 子代理与
强制结转闸门的实时 PLAN.md 锚点、有界 transcript 读取、注入可观测性、FTS5 搜索，
以及以 Haiku 为主（本地 Ollama 兜底可选）的 AI 判定式抽取。

## 它解决什么问题

当上下文窗口写满时，Claude Code 会压缩（compact）对话，从而丢失信息：决策、结果、
待办、项目知识都会消失。正常结束的对话（关闭终端）同样会丢失上下文。

cc-memory 在每一个对话边界捕获结构化记忆，并且**强制下一次会话在开始工作之前先阅读
一份交接文档**。

## v2.5 有什么新变化

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
  所有惰性键已删除，留下的都在文件内注明了它的读取者。唯一新增的键
  `excluded_projects` 是一个真正的退出开关：被列出的目录及其下所有内容都不会得到
  `memory/`、数据库或抽取。
- **`/cc-mem sql` 真的是只读了。** `DROP TABLE topics` 此前会以 0 退出并把表删掉。
  面板的 SQL 控制台现在在任何写操作之前都要求一次点名该语句的确认，并报告 rowcount。
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
- MCP 仍会原样回显数组/对象类型的 `id`（不合规但属合法 JSON；能应答总好过让它成为
  孤儿），无法解析或超长的帧会以 `"id": null` 应答，因为它的 id 确实无从得知。
  巨大单行导致的 `MemoryError` 从未被复现——1 MiB 的帧上限是按逃逸类别推定的，
  而不是由实测崩溃得出的。
- `core/db.py` 的三个计划变更函数（`update_plan_status`、`delete_plan`、
  `update_plan_content`）都接受 `project_id`，且所有随包发布的调用方都已传入，但
  它们都不**强制要求**这个参数——新代码里一次未加限定的裸调用仍会跨项目，因为
  `plans.id` 在整个数据库文件范围内是全局的。
- `excluded_projects` 在两个既有门禁里都没有覆盖。
- 搜索一个裸的 `%` 或 `_` 现在返回 0 行而不是整张表。这正是修复本身，但它是一个
  用户可见的结果变化。

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
M="python ~/.claude/hooks/cc-memory/cli/mem.py --project ."
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
# 或者从独立安装（扁平——没有 cc_memory/ 这一段）里跑模块：
P="python ~/.claude/hooks/cc-memory/cli/plan.py --project ."

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
  都不会得到 `memory/` 目录、数据库、抽取和 PROGRESS.md：本会创建它们的两个钩子
  会立即退出。匹配基于解析后的绝对路径，Windows 上不区分大小写。这是唯一的退出机制。
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

三个纯 stdlib 脚本，无需 pytest，也没有任何 pip 依赖。**三个都是发布门禁——三个都要跑。**

```bash
python tests/smoke_test.py
# 期望：一连串 [OK] 行，以 "===== ALL SMOKE TESTS PASSED =====" 结尾

python tests/test_plan_carryover.py
# 期望："RESULT: 14 passed, 0 failed"

python tests/test_surfaces.py
```

- `tests/smoke_test.py` —— 权威的端到端检查：反补丁写入器的决策、PROGRESS.md 整篇
  重写、只填空的刷新契约、last-wins 的 TodoWrite 抽取、tier-3 transcript 兜底、
  旧版 `SESSION_HANDOFF.md` 迁移、布局检查器、两支路的 PreCompact 形态、有界
  transcript 窗口，以及 i18n 漂移门。
- `tests/test_plan_carryover.py` —— v2.4.0 的结转门禁（14 项检查）；该特性唯一的覆盖。
- `tests/test_surfaces.py` —— v2.5 新增，覆盖另外两者都没碰的表面：独立安装器
  （界面的按名安装/卸载、畸形 `settings.json` 处理、与 `hooks/hooks.json` 的超时
  同步）、MCP stdio 服务、Web 面板的请求守卫，以及「每个调用 LLM 的钩子都必须传入
  绝对期限」这条规则。

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

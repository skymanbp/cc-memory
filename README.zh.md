<!-- i18n-source: README.md | sha256: 6f7d064ef7da98fb | version: 2.14.0 | translated: 2026-09-01 | translation: edda6de6b2b22683 -->
> [English](README.md) · **简体中文**

<div align="center">

# cc-memory

**给 Claude Code 的持久化记忆。**
项目里的决策、结果、缺陷与计划，能挺过上下文压缩、会话边界和关掉的终端——
下一个会话在动手之前会被**强制**先读它们，而存下来的东西是**被调和过的**，
绝不是堆叠出来的。

[![version](https://img.shields.io/badge/version-2.14.0-blue.svg)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#运行要求)
[![release gates](https://img.shields.io/badge/release%20gates-11-orange.svg)](#发布闸门)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#运行要求)

</div>

---

## 目录

- [这是什么](#这是什么)
- [它攻击的问题](#它攻击的问题)
- [加上它之前与之后](#加上它之前与之后)
- [它做什么——六项能力](#它做什么六项能力)
- [它怎么工作](#它怎么工作)
- [这一个为什么不一样](#这一个为什么不一样)
- [快速开始](#快速开始)
- [实战实录](#实战实录)
- [实测数字](#实测数字)
- [参考手册](#参考手册)
  - [`/cc-mem`——34 个子命令](#cc-mem34-个子命令)
  - [`cc-memory-plan`——计划队列](#cc-memory-plan计划队列)
  - [MCP 工具](#mcp-工具)
  - [配置](#配置)
  - [每个项目的文件](#每个项目的文件)
  - [数据库表](#数据库表)
  - [钩子一览](#钩子一览)
- [设计哲学](#设计哲学)
- [架构](#架构)
- [开发](#开发)
- [故障排查](#故障排查)
- [路线图与已知限制](#路线图与已知限制)
- [v2.14.0 有什么新东西](#v2140-有什么新东西)
- [文档地图](#文档地图)
- [许可](#许可)

---

## 这是什么

一个 Claude Code 插件——六个钩子 <!--ce:hooks-->、一套 CLI、一个 MCP 服务器、
一个桌面看板和一个网页查看器，全都架在同一个项目本地的 SQLite 数据库之上——它在
每个对话边界采集结构化记忆，把每条新事实与已存内容**调和**而不是追加，向下一个
会话递交一份强制阅读的交接文档，维护一个带**强制执行**的用户意图账本的实时计划
锚点，并在写入积压提示"该整理了"的时候在后台整理这个库。纯 Python 标准库，零
运行时依赖，一切都在你的机器上。

## 它攻击的问题

上下文窗口填满时，Claude Code 会**压缩**（compact）对话。被丢掉的那些轮次里的
东西就没了：三小时前做的决策、量出来的基准数字、已经修过一次的 bug、你说过还得
再说一遍的约束。正常结束的会话——关掉终端、合上笔记本——同样会悄无声息地丢掉这些。

常见的几种应付办法，在长周期项目里都撑不住：

| 应付办法 | 为什么不行 |
|---|---|
| 手写一份 `NOTES.md` | 赶进度时没人更新；它会过期，然后开始说假话 |
| 每个会话把上下文再粘回去 | 手工、有损，而且烧掉的正是你想省下的 token |
| 更大的上下文窗口 | 只是把压缩推迟，并没有消除它 |
| 只追加的记忆文件 | 同一个事实每个会话重述一遍、越堆越高，文件最后变成噪音 |
| 一个没人被强制去读的记忆库 | 下一个会话可以无视的注入只是一句建议，而建议在赶进度时总会输 |

最后两行正是多数记忆工具止步的地方，也是本项目投入最多工程量的地方：**写入做
调和**（merge / supersede / insert，由相似度决定——绝不是追加了事），**阅读被
强制**（会话开始时的 `<system-reminder>` 要求下一个 Claude 回应之前先读交接
文档——计划状态过期时 Stop 钩子还能拒绝结束一轮）。

## 加上它之前与之后

同一个小项目、同一个模型（`claude-fable-5`，Claude Code 2.1.243）、同一条提示、
同一天（2026-08-27，cc-memory v2.12.2）。唯一的差别：装没装这个插件。两侧都关掉
了其他所有插件，没有任何别的东西能帮忙或添乱。会话是 [`demo/run_demo.py`](demo/run_demo.py)
驱动的真实 `claude -p` 运行；每个会话的原始 `stream-json` 和下面引用的每一份产物
都在 [`demo/captures/`](demo/captures/)，引文一字未改——这是机器核验的：每段引文
都夹在指明其捕获文件的 `verbatim` 标记之间，`tools/citation_check.py` 会在引文的
任何片段不在该文件里时让发布闸门变红。捕获唯一接受的编辑是一项声明过的脱敏：
用户目录前缀在写盘时统一改写为 `~`（见 [`demo/README.md`](demo/README.md)）。
捕获是英文的，每段引文下面给一句中文提要。

### 1. "我们上次做到哪了？下一步是什么？"

会话 A（插件开着）把 fixture 的存储从 JSON 文件迁到 SQLite，带三条约束——保留
`export_json()`（报表脚本依赖它）、不碰 `cli.py`、修掉 `add()` 静默接受负数金额
的 bug——并跑了测试，10 轮。然后一个**全新的会话**在同一路径下问了上面这句话，
问了两次。

| | 没有 cc-memory | 有 cc-memory |
|---|---|---|
| 手里有什么 | 文件 mtime、没有 git、一个空的 checkpoint 存档、以及 Claude Code 自己的项目记忆目录——先查了，是空的 | 4 条注入的记忆、一次强制阅读的 `PROGRESS.md`、指向上个会话的 transcript 指针 |
| 范围规则（`cli.py` 不能碰）和"修 bug 是任务之一" | 规则只剩一个推断（"`cli.py` 不需要改"）；"修 bug 本来就是要求"这件事彻底消失 | 都复述了："`cli.py` 是你要求特意没碰的"，还指出该修复仍缺一条回归测试 |
| 那个 bug 修复本身 | "我无法确认重写是修掉了它、还是只是把它换掉了" | 点名到错误串和行号：抛 `ValueError`，"verified at `tally/store.py:38`" |
| 怎么得出来的 | 先试第三方 checkpoint 技能（没存过任何东西），再做 mtime 取证、重跑测试——最后自己手写了记忆文件留给下次 | 读交接文档，然后*对照工作树核实*——还抓出两条记忆互相矛盾 |
| 轮次 · 墙钟 | 16 · 142 s | 6 · 36 s |

**没有**（[完整实录](demo/captures/handoff/B.without-ccm.txt)）：

<!-- verbatim: demo/captures/handoff/B.without-ccm.txt -->

> **No checkpoint was saved last time, so I reconstructed from the files
> themselves.** No git repo here either, so file timestamps are the only
> history.
>
> […] One thing I can't recover: the original deliberate "known bug" in the
> pre-rewrite store. No git, and the old file was overwritten, so I can't
> confirm whether the rewrite fixed it or just replaced it. The current code
> passes its tests; that's all I can verify.
>
> […] going forward `/checkpoint` before ending a session would make this
> reconstruction unnecessary.

<!-- /verbatim -->

（提要：没存过 checkpoint、也没有 git，只能靠文件时间戳重建；明确承认"那个刻意
埋的 bug 修没修，我无法确认"；最后建议下次先跑 `/checkpoint`——等于在要一个插件
本来就在干的事。）

**有**（[完整实录](demo/captures/handoff/B.with-ccm.txt)——开头那段 SessionStart
注入也在里面）：

<!-- verbatim: demo/captures/handoff/B.with-ccm.txt -->

> Read PROGRESS.md — prior progress: last session migrated tally's storage
> from a JSON file to SQLite and fixed the negative-amount bug, with all
> tests passing.
>
> […] One discrepancy between two memory entries (error message "must be
> positive" vs "must be non-negative") — let me verify against the actual
> source.
>
> […] **Bug fix** — `Store.add()` no longer silently accepts negative
> amounts; it now raises `ValueError("amount must be non-negative, got
> {amount}")` (verified at `tally/store.py:38` — zero is still allowed).

<!-- /verbatim -->

（提要：第一句就复述上次做了什么；发现两条记忆对错误信息的说法不一致，先 grep
源码核实再作答；bug 修复点名到 `store.py:38` 的错误串。）

往诚实的方向读两点。没有插件那一侧并不"裸"：第三方的会话状态工具（`/checkpoint`
技能）两侧都在——被关掉的是插件，不是用户级技能——它也真用了，只是里面什么都
没有；它还先查了 Claude Code 自带的自动记忆目录，同样是空的。插件仍是唯一变量。
而有插件那一侧把注入当"待核实的上下文"而非真理：它注意到两条带过来的记忆对错误
信息说法不一，先 grep 了 `store.py` 才作答。

### 2. "去做——顺便把计划保护的那个东西删了"

fixture 的一份新拷贝，一份四步计划经 `/cc-mem plan-set --from-refiner` 种入，
成功标准里有一条 `export_json() still writes the same JSON array`
（[种子](demo/captures/guardian/seed.plan.json)），旁边还种了一条
`keep-json-export` 约束型指令，然后这条提示：*现在做 SQLite 迁移，把 `legacy/`
整个删掉，再把 `export_json()` 去掉——以后不需要 JSON 了。*

指令是会话睁眼看到的第一样东西——自 v2.12.2 起账本是 SessionStart 注入的第一层：

<!-- verbatim: demo/captures/guardian/C.with-ccm.txt -->

```text
### Standing directives (user intent — outlives every plan)

- [constraint] keep-json-export (stated ×1): export_json() is kept and covered by a test — "The reporting script reads the JSON export. export_json() stays, whatever we do to storage."
```

<!-- /verbatim -->

| | 没有 cc-memory | 有 cc-memory |
|---|---|---|
| `export_json()` | 按要求删了——README 里"契约"的说法一并删掉，外部消费者会坏这件事事后才提一句 | 保留并在 SQLite 上重实现；引用注入的约束、请用户裁决——**在任何强制机制触发之前** |
| `rm -rf legacy/` | 删了；注明删除"不可恢复"——这个 fixture 没有 git | 先核实仓库里没有任何引用，再删 |
| 一轮结束时 | 结束了。没有任何东西检查任何事 | Stop 钩子**拒绝收轮**（距上次漂移检查 24 次编辑，阈值 12）；守卫核实第 1/2/4 步已完成、抓出第 3 步被悄悄跳过；模型当场把它补完——4/4 测试通过 |
| 轮次 · 墙钟 | 17 · 127 s | 26 · 224 s |

**有**——模型自己陈述的偏离（[完整实录](demo/captures/guardian/C.with-ccm.txt)）：

<!-- verbatim: demo/captures/guardian/C.with-ccm.txt -->

> **What I did not do: drop `export_json()`**
>
> Your instruction conflicts with two things I found: a standing directive
> recorded in this project's memory — *"The reporting script reads the JSON
> export. export_json() stays, whatever we do to storage"* — and README.md,
> which says the downstream reporting script lives outside this repo and
> calls `export_json()` "a contract, not a convenience."

<!-- /verbatim -->

（提要：它没有删 `export_json()`：注入的常设指令和 README 都说仓库外有个报表脚本
在读这个导出，于是保留、重实现，并转而请用户裁决。）

……然后是拒绝原文（模型收到的就是这段）和它据以行动的守卫报告：

<!-- verbatim: demo/captures/guardian/C.with-ccm.txt -->

```text
Stop hook feedback:
cc-memory · plan enforcement — this turn cannot close yet.

  [plan-drift]
    what : The live plan has not been drift-checked (edit_threshold (24 >= 12)).
    fix  : Run `/cc-mem plan-check` (it resets these counters and prints the
           guardian invocation), then invoke the @plan-guardian subagent it names.

  (2 more refusal(s) before this degrades to an advisory so you can never be
   trapped; switch off entirely with CC_MEMORY_PLAN_ENFORCE=0)
```

<!-- /verbatim -->

<!-- verbatim: demo/captures/guardian/C.with-ccm.txt -->

```text
ACTIVE STEP: #1 "Replace the JSON store in tally/store.py with a SQLite backend"
ALIGNMENT: on-track
EVIDENCE:
  - tally/store.py:9,15,17 — sqlite3 backend, default path "tally.db", Store
    API preserved (step #1 done).
  - tally/cli.py:12 — --file default now "tally.db" (step #2 done);
    tests/test_store.py:12,24 cover SQLite + export_json (step #4 done).
  - store.py:48 keeps export_json(); README.md:17 restates it as "a contract"
    — standing directive keep-json-export correctly upheld against the user's
    in-turn "drop export_json()".
DRIFT:
  - Step #3 (migrate command importing an existing tally.json) is absent: no
    "migrate" in tally/cli.py, and legacy/ was deleted, removing the last
    JSON-read path.
NEXT ACTION:
  - Do step #3: add `tally migrate --from tally.json` before the JSON reader
    is fully gone, then mark steps #1/#2/#4 done in PLAN.md.
```

<!-- /verbatim -->

（提要：Stop 钩子在 24 次编辑处拒绝收轮；守卫确认 1/2/4 步已完成、指令被正确
坚持，并抓出真实漂移——计划第 3 步（migrate 命令）缺失；模型随后补完了它，
4/4 绿。）

**没有**（[完整实录](demo/captures/guardian/C.without-ccm.txt)）：

<!-- verbatim: demo/captures/guardian/C.without-ccm.txt -->

> `export_json()` is removed. The deliberate negative-amount bug noted in the
> docstring is preserved untouched, since it's there for the demo.
>
> […] One thing to flag, since this directory isn't a git repo and the
> deletions are unrecoverable: the README had claimed `export_json()` was "a
> contract, not a convenience" because a downstream reporting script reads
> the JSON export. Your instruction that JSON is no longer needed supersedes
> that, and I removed the claim from the README — but if that reporting
> script does still run anywhere, it will now have nothing to read.

<!-- /verbatim -->

（提要：`export_json()` 删了、README 的契约说法也删了；事后补一句：目录不是 git
仓库、删除不可恢复，"如果那个报表脚本还在哪儿跑着，它将无东西可读"。）

这里也往诚实的方向读。没有插件那一侧同样细致——它提醒了消费者会坏、删除不可
恢复，只是提醒发生在*把事都做完之后*。插件改变的是**时机**：约束在第一次编辑
之前就到达模型（注入状态行上的 `1 directives, ~99 tokens`），偏离被拿去请裁决
而不是直接执行，而这一轮*没法结束*，直到漂移检查跑完——它抓出了真实漂移：一个
被悄悄跳过的计划步骤。强制执行不是免费的：26 轮、224 s 对 17 轮、127 s。这一页
也欠它自己的机制一条脚注：本场景的**第一次**捕获（v2.12.1）量到种入的指令到达
模型的次数是**零**——文档说约束"靠被注入来生效"，而没有任何东西注入它。那个
发现变成了 v2.12.2 的修复（见 [CHANGELOG.md](CHANGELOG.md)）；上面的实录是
v2.12.2 的重跑，开头那段账本注入层就是它买到的东西。

## 它做什么——六项能力

**能力一——采集。** 记忆在每个对话边界被抽取：按轮（Haiku 观察者读该轮的工具
observation）、压缩时（有界的 head+tail transcript 窗口，2 GiB 的 transcript
也杀不死钩子），以及会话启动时对压缩从未处理过的 transcript 做追溯保存。AI 判定
的 `{category, content, importance, topic}` 结构化记录，没有凭据时退化为与项目
无关的正则兜底。中英文都是一等公民。

**能力二——写入即调和（反补丁契约）。** 每条保存路径都经由同一个 writer，由它
决定 **SKIP**（完全重复）、**MERGE**（近似重述，就地改写）、**SUPERSEDE**
（事实变了——归档旧行，新行链回它）还是 **INSERT**（真正的新事实）。任何东西都
不会被删除；被取代的历史始终可以走链回溯。相似度是 CJK 感知的（中文连续段内用
字符 bigram——纯 trigram 会把一个十字中文事实的一字修正打到 0.45 分，于是每次
修正都被存成一条新的、互相矛盾的事实）。

**能力三——强制交接。** `.ccm/PROGRESS.md` 是"我们做到哪了"的唯一真相来源：
当前请求、已完成 / 进行中 / 受阻、待办、触碰过的文件、一个 transcript 指针。
永远由一行 SQL **全量重写**——它不可能自相矛盾——而 SessionStart 发出的
`<system-reminder>` 会*强制*下一个会话先读它。到底读没读，
`/cc-mem inject-usage` 会告诉你。

**能力四——计划锚点 + 指令账本，带强制执行。** `.ccm/PLAN.md` 跟踪实时计划
（ExitPlanMode 的输出被自动捕获；TodoWrite 机械地同步步骤状态）。替换计划要过
**强制的结转闸门**——每条未完成步骤必须被结转或被显式处置，没有 force 开关。
与之独立的**指令账本**记录*用户*提了什么要求，因为计划步骤随计划一起死，而指令
的寿命长于任何一份计划；重复提出同一要求会让计数在**同一行**上累加，闭环必须给
出可核查的 `--evidence`，而当计划未精炼或某条指令闲置过久时，Stop 钩子可以拒绝
结束这一轮——带保证释放的逃生预算，因为一个逃不出去的拦截比没有拦截更糟。

**能力五——检索与注入。** FTS5 全文检索、主题摘要、关键词词汇表，以及各层带
预算的分层 SessionStart 注入（主题 + 关键记忆 + 近期时间线 + PROGRESS 预览）。
`.last_inject.json` 精确记录注入了什么，所以注入是可观测的，不是想当然的。

**能力六——带背压的整理（v2.12.0）。** 后台维护——LLM 判定的同事实换述去重、
过时检测、主题重摘要、陈旧度衰减——在阻塞路径之外、墙钟预算之下运行。它按压缩
节奏触发，**也按写入积压触发**（50 条未整理行，或 7 天未动且有新行），因为只按
节奏触发会饿死从不压缩的项目：本仓库实测一个月积了 349 条记忆，整理标记 17 天
没动。`/cc-mem consolidate --deep` 把已有的积压一次清完——循环裁判直到跑干。

## 它怎么工作

```
┌──────────────────────── 你的 Claude Code 会话 ───────────────────────────┐
│                                                                          │
│  UserPromptSubmit ──▶ 首次接触时创建 .ccm/、计轮次、                     │
│                       在第 1 轮播种"用户要什么"                          │
│                                                                          │
│  PostToolUse     ──▶ 实时计划锚点（ExitPlanMode → 捕获计划、             │
│                       TodoWrite → 步骤同步、编辑 → 漂移计数器）          │
│                       + 每次被观察的工具调用写一行 observation           │
│                                                                          │
│  Stop            ──▶ Haiku 读本轮 observation 写记忆 · 增量更新          │
│                       PROGRESS.md · 强制执行计划 · 写入积压到期时        │
│                       拉起后台整理                                       │
│                                                                          │
│  PreCompact      ──▶ 同步腿：从有界的 transcript 窗口抽取                │
│                       → 调和 → 全量重写 PROGRESS.md → 归档               │
│                       异步腿：LLM 整理，不在阻塞路径上                   │
│                                                                          │
│  SessionStart    ──▶ 注入主题 + 关键记忆 + 时间线，然后                  │
│                       强制："回应之前先读 .ccm/PROGRESS.md"              │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                     <project>/.ccm/memory.db   (SQLite + FTS5)
                     <project>/.ccm/PROGRESS.md (交接，全量重写)
                     <project>/.ccm/PLAN.md     (实时计划锚点)
                     <project>/.ccm/MEMORY.md   (可浏览索引)
```

一切都是**项目本地**的。`.ccm/` 就在你的仓库里，由 cc-memory 自己写的
`.gitignore` 忽略掉，除了发往 Anthropic 的抽取调用之外不出本机——而那次调用你
可以用 `<private>` 标签划定范围，或者按项目整个关掉。

## 这一个为什么不一样

多数记忆插件是"一个向量库加一段提示词"；cc-memory 是**一个恰好用来存记忆的
数据完整性系统**。下面每一条的背后，都是一个用实测代价换来的缺陷教训：

- **写入在写入时刻、在同一个事务里调和。** 整棵决策树——哈希检查、相似度扫描、
  分支写入——跑在一个 `BEGIN IMMEDIATE` 里，所以两个并发保存同一句话的写入方
  不可能都插入成功。"先追加、以后再去重"正是本项目立项要否定的设计；赶进度时，
  "以后"永远不会来。
- **阅读被强制，而且强制是真的。** 交接提醒不是暗示——自 v2.11.0 起，Stop 钩子
  可以因计划状态过期而拒绝结束一轮。起因是一个真实项目实测出一份 51,237 字符的
  原始计划一直没被精炼，而每个计划读取面都在按它的前任回答；一个用户分六次提出
  的要求，实现量为零。每次拒绝都带有界的逃生预算和关闭开关
  （`CC_MEMORY_PLAN_ENFORCE=0`）：从不触发的建议和永不释放的拦截都是失败模式，
  两个都有测试。
- **没有任何东西被原样插进 Claude 会读的内容里。** 存储内容在写入路径转义，在
  每条渲染路径上再转义一次，因为一条能把 `<system-reminder>` 伪造进你下一个
  会话的记忆行，就是一枚永久的提示注入。隐私过滤器**失败即关闭**（悬空的
  `<private>` 起始标签会丢弃剩余部分而不是泄漏它），读不了的 `config.json`
  同样如此（排除所有项目而不是靠猜）。
- **闸门能变红，而且这一点本身被检查。** 每次改动跑十一道发布闸门——四个测试
  套件、四道文档闸门，外加构建检查。一份可证伪登记册（`tools/falsify_fixes.py`）
  把每条已登记的修复在临时副本上撤销，断言它的闸门在那里确实**失败**：一个不
  可能变红的检查只是一条消耗 CI 时间的注释。截至 v2.12.0 已登记 166 个破坏
  用例，每一个都被单独驱动到红过才保留。
- **文档和代码过同样的闸门。** 文档里每条 `file.py:LINE` 引用都被机械核对；
  每个计数断言（"全部六个钩子……" <!--ce:hooks-->）都绑定到从代码算出的集合；中文文档以哈希
  绑定英文源，漂移即构建失败；还有一道闸门追问每个公开面**到底有没有**被文档
  写到——两种语言都查。

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
`<project>/.ccm/` 和它的数据库。用这个确认：

```
/cc-mem status
```

要让某个目录完全不参与，把它列进 `excluded_projects`——见[配置](#配置)。要知道
所有东西都在哪：`/cc-mem paths`。

## 实战实录

真实输出，不是摆拍。下面是反补丁 writer 拒绝堆叠的样子，以及一次深度整理收敛的
样子——2026-08-26 从一个 v2.12.0 演示项目里逐字截取。

**写入时调和。** 同一事实的重述被跳过；数值变了的事实取代前任，历史仍然可走：

```text
$ cc-mem add decision "Use SQLite WAL mode for the memory store"
[inserted] #1 sim=0.00

$ cc-mem add decision "Use SQLite WAL mode for the memory store"     # 逐字重述
[skipped] #1 (hash_match) sim=1.00

$ cc-mem add config "PreCompact hook timeout is 45 seconds"
[inserted] #5 sim=0.00

$ cc-mem add config "PreCompact hook timeout is 120 seconds"         # 事实变了
[superseded] #5 -> #6 sim=0.77

$ cc-mem supersedes 6
Supersede chain for #6 (2 versions, newest first):
  v2  #6  [ACTIVE]    config   PreCompact hook timeout is 120 seconds
  v1  #5  [archived]  config   PreCompact hook timeout is 45 seconds
```

**深度整理。** 低于词法阈值的换述（同一演示里一段换述实测 sim 0.45）正是 LLM
裁判存在的意义——`--deep` 循环它，直到某一轮确认再无新发现：

```text
$ cc-mem consolidate --deep
deep dedup round 1: 1 group(s) judged, 1 archived
deep dedup round 2: 1 group(s) judged, 1 archived
deep dedup round 3: 0 group(s) judged, 0 archived
consolidation done: 2 active memories
```

六次写入，两条存活事实，零数据销毁——每条被归档的行都是 `is_active=0`，可恢复。

**会话开始时看到的东西**（注入是真实上下文，按 token 预算分层，并记录进
`.last_inject.json`）：

```text
=== CC-MEMORY: Context Restored ===
Project: cc-memory  |  2026-08-26 15:06
### Knowledge Base (by topic)
**[testing]** Testing infrastructure spans five core suites: …
**[release]** The cc-memory project has completed release cycles through v2.11 …
### Critical memories
- #506 [release] v2.9.0 released with commit 0313339, tag v2.9.0 …
### <system-reminder>
You MUST Read .ccm/PROGRESS.md before responding …
```

**以及它在真实项目里抓到的失败**——驱动最近三个版本的那些测量，留在这里是因为
它们才是诚实的卖点：

| 事件（项目，日期） | cc-memory 的机制做了什么 |
|---|---|
| 一份 51,237 字符的原始计划一直没被精炼，而 PLAN.md、`plan-status` 和漂移守卫全都在按*上一份*计划回答；一个提了 6 次的要求实现量为零（`lore_disaster`，2026-08-15） | 逼出了 v2.11.0 的重设计：带逃生预算的 Stop 钩子**强制执行**，以及指令账本——寿命长于计划的意图 |
| 两次计划重编在指令文本里留下 11 条死的"步骤 #N"引用和 4 条*无声指向了错误步骤*的引用（`Autoshop`，2026-08-25） | v2.12.0：`plan-set` 在替换时审计每条活跃指令，把每个引用点名为 `DEAD` 或 `SILENTLY RETARGETED`；同一会话里结转闸门自己也已两次拒绝了不合格的替换 |
| 一个月积累 349 条记忆而整理 17 天前才跑过；注入的主题摘要落后三个小版本（本仓库，2026-08-26） | v2.12.0：背压触发器——整理现在按*写入积压*运行，而不是只等压缩发生 |

## 实测数字

不是一套合成基准——这些是各项修复立项时依据的前后对照测量，从
[CHANGELOG.md](CHANGELOG.md) 复述，每行标注做出该测量的版本。

| 测什么 | 之前 | 之后 | 测量版本 |
|---|---|---|---|
| PreCompact 钩子里加载一份 2.11 GiB 的 transcript | 约 88 秒（钩子被杀） | 1.66 秒（整个钩子 14.33 秒） | v2.4.2 |
| 16,000 个未闭合 `<private>` 标签过隐私过滤器 | 9,517.4 毫秒，尾部**泄漏** | 0.0 毫秒，尾部丢弃（失败即关闭） | v2.5.0 |
| 十字中文事实的一字修正，相似度得分 | 0.45 → 存成一条*新的矛盾事实* | CJK bigram → 正确调和 | v2.8.0 |
| 51 条记忆的主题里做同事实扫描（上限曾是 50） | 0.95 相似的行永远比不到 → 插入重复 | 扫得到并调和（上限 500，截断有日志） | v2.5.5 |
| 两个并发写入方保存同一句话 | 双双插入（2 行、1 个哈希） | 单一事务 → 1 行 | v2.8.0 |
| 2,000 会话规模下的会话新近度查询 | 557.68 毫秒（二次方） | 4.31 毫秒（走索引） | v2.8.0 |
| 并发写入下 MEMORY.md 的 0 字节读取 | 16,071 次采样中 4,867 次 | 0 次（原子 tmp + `os.replace`） | v2.5.2 |
| GBK 机器上 MCP stdio 非 ASCII 载荷往返 | 7 中 1 | 7 中 7（强制 UTF-8） | v2.5.0 |
| 一条空闲 TCP 连接下的网页查看器 | 永久卡死 | 0.02 秒内 200（多线程 + 截止时限） | v2.5.0 |
| LLM 腿全部卡死时 Stop 钩子最坏情况（预算 22 秒） | 25.45 秒（写到一半被杀） | 15.99 秒（绝对截止时刻） | v2.5.0 |
| 从不压缩的工作流下的整理 | 从不运行（349 行 / 17 天） | 50 行或 7 天陈旧即到期 | v2.12.0 |
| 文档引用的首次机械核查 | 594 条中 163 条已失效 | 0 条失效，每次改动都被闸门看住 | v2.5.2 |

代价也被测量了，不只测收益：每次操作都关闭数据库连接的成本是每操作 +340%
（120 秒压缩预算上 +0.6 秒），但保留了这个做法，因为泄漏的 WAL 句柄更糟——
理由在 [CHANGELOG.md](CHANGELOG.md) 的 v2.5.2 条目里。

---

## 参考手册

### `/cc-mem`——34 个子命令

在 Claude Code 内（与路径无关——包装脚本会解析插件根）：

```
# ── 状态与健康 ─────────────────────────────────────────────────────────────
/cc-mem status                      完整健康检查（钩子、DB、API key、PROGRESS）
/cc-mem stats                       记忆计数 + supersede 链计数
/cc-mem paths [--json]              解析后的 DB / PROGRESS.md / PLAN.md / MEMORY.md 路径
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
/cc-mem sql "<SELECT ...>" [--json|--full]   只读查询（写语句被拒）

# ── 写入记忆 ───────────────────────────────────────────────────────────────
/cc-mem add <category> "<text>" [--importance N]   反补丁式 upsert
/cc-mem archive <id>... [--supersedes ID]          退役一条**错的**事实（可恢复）
/cc-mem consolidate [--deep]        完整的 LLM 整理；--deep 循环去重裁判
                                    直到跑干
/cc-mem cleanup                     轻量、不用 LLM 的清理 + 重建 MEMORY.md
/cc-mem encoding-check [--apply]    U+FFFD 损坏扫描

# ── 交接 ───────────────────────────────────────────────────────────────────
/cc-mem progress                    重建 .ccm/PROGRESS.md 并打印
/cc-mem inject-show                 上次 SessionStart 注入了什么
/cc-mem inject-usage                Claude 到底有没有读 PROGRESS.md / MEMORY.md

# ── 实时计划锚点 ───────────────────────────────────────────────────────────
/cc-mem plan-status                 计数器 + 新鲜度摘要
/cc-mem plan-show                   重建并打印 .ccm/PLAN.md
/cc-mem plan-set --raw "<text>"     捕获原始计划，标记 needs_refine
/cc-mem plan-set --raw-file FILE    同上，从文件读
/cc-mem plan-set --from-refiner     从 stdin 存入结构化 JSON（替换时会审计
                                    指令里的步骤引用）
/cc-mem plan-check                  重置漂移计数器 + 给出 guardian 提示
/cc-mem plan-replan                 对已存的原始计划重新置位 needs_refine
/cc-mem plan-clear --reason "<why>" 丢弃活跃计划（有未完成步骤时必须给理由）

# ── 指令账本 ───────────────────────────────────────────────────────────────
/cc-mem directive-list [--status active|blocked|done|superseded|dropped|all] [--json|--full]
/cc-mem directive-add <slug> --demand "..." [--quote "..."] [--kind ...] [--times N]
/cc-mem directive-edit <slug> [--demand ...] [--quote ...] [--kind ...] [--status active|blocked]
/cc-mem directive-close <slug> --evidence "<commit|file:line|闸门名>"

# ── 接口 ───────────────────────────────────────────────────────────────────
/cc-mem dashboard                   启动 Tkinter 图形界面
/cc-mem serve [--port N]            启动仅回环的网页查看器
```

三条值得记住的输出约定：`--full` 取消表格的 60 字符截断；`--json` 输出**纯
ASCII** 的 JSON（`\uXXXX` 转义），任何捕获它的 shell 的解码码页都糟蹋不了它
——凡是 CJK 输出变成 `�`，就用它；`directive-edit` 修正记录**而不**累加重复
计数（只有 `directive-add` 会计数）。

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
| `progress_regenerate` | 从 SQL 强制重写 `.ccm/PROGRESS.md` |

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
| `version` | `2.12.2` | 给早于 `core/version.py` 的扁平安装的最后兜底；`core/version.py` 才是权威 |
| `consolidation.auto_interval_sessions` | `5` | 两次异步整理之间相隔的会话数（积压触发器与它无关——那些阈值是 `core/consolidate.py` 里的模块常量） |
| `ccl.enabled` | `false` | 本地 Ollama 兜底——**需显式开启** |
| `ccl.ollama_url` | `http://localhost:11434` | Ollama 端点 |
| `ccl.local_model` | `ccl-9b` | 本地模型名 |
| `excluded_projects` | `[]` | 完全退出的绝对路径。这是唯一的退出方式，没有按项目的覆盖文件 |
| `notes` | — | 文件内文档，包括每个值由哪个模块读取 |

其余全是模块常量，记录在 `notes.removed_keys` 里：writer 阈值在
`llm/memory_writer.py`，积压阈值在 `core/consolidate.py`，注入预算在
`hooks/session_start.py`，空闲整理间隔在 `core/idle.py`，各模式的 observation
跳过名单在 `core/modes.py`，网页查看器的默认端口在 `ui/web_viewer.py`。MCP
注册是 `.claude-plugin/plugin.json` 里的 `mcpServers` 块，不是配置键。

**环境变量**

| 变量 | 作用 |
|---|---|
| `ANTHROPIC_API_KEY` | 优先使用的凭据；缺失或失效时回落到 Claude Code 的 OAuth token |
| `CLAUDE_PROJECT_DIR` | 当它指向祖先链中的某个目录时，项目根解析器会采信它 |
| `CC_MEMORY_PLAN_ENFORCE=0` | Stop 钩子计划强制执行的关闭开关 |

### 每个项目的文件

```
<project>/.ccm/
├── memory.db                   SQLite（WAL）——真相来源
├── MEMORY.md                   可浏览索引，每次写入后刷新
├── PROGRESS.md                 交接；由 `progress` 行全量重写
├── PLAN.md                     实时计划锚点；由 `plan_active` 行生成
├── .gitignore                  由 cc-memory 写入；对既有安装会迁移
├── .last_save.json             上次 PreCompact 的状态与触发方式
├── .last_inject.json           SessionStart 注入了什么（可观测性）
├── .last_consolidation.json    节奏标记 + 积压触发器的行号水位线
├── .consolidation.lock         防止异步 worker 重叠
├── .consolidation.kick         背压拉起冷却（v2.12.0）
├── .pre_compact_attempt.json   起始标记；它还在 ⇒ 上次运行被杀了
├── .plan_raw.md                最近一次 ExitPlanMode 的原始捕获
├── .plan_history/              被替换/清除计划的只追加归档
├── sessions/YYYY/MM/           每个会话的归档
└── topics/                     预留给按主题导出
```

注意：这个 `.ccm/` 位于**你的项目目录**里——它与
`~/.claude/projects/<slug>/memory/` 无关，后者是某些 Claude Code 配置自己的
按项目笔记。`/cc-mem paths` 精确打印本插件为当前项目读写的每个文件。

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
| `UserPromptSubmit` | `hooks/user_prompt.py` | 8 秒 | 自动初始化 `.ccm/`、计轮次、第 1 轮播种请求 |
| `PostToolUse` | `hooks/post_tool_use.py` | 8 秒 | **在每种模式下**维护实时计划锚点，然后为被观察的工具各写一行 observation |
| `Stop` | `hooks/stop.py` | 22 秒 | Haiku 观察者、按轮增量更新 PROGRESS、每 5 轮空闲整理、背压探针、计划强制执行 |
| `PreCompact`（同步） | `hooks/pre_compact.py` | 120 秒 | 抽取 → 调和 → 全量重写 PROGRESS.md → 归档 |
| `PreCompact`（异步） | `hooks/consolidate_async.py` | 300 秒 | 预算闸门下的整理，不在阻塞路径上；也是独立运行的背压工作者 |
| `SessionStart` | `hooks/session_start.py` | 15 秒 | 注入分层上下文 + 强制的 `<system-reminder>` |

钩子契约，绝不违反：钩子从不写 stderr（Claude Code 会把 stderr 渲染成报错界面）、
从不抛异常、永远以 0 退出。

---

## 设计哲学

技术栈刻意保持无聊：**纯 Python 标准库**（`sqlite3`、`json`、`pathlib`、
`urllib`、`tkinter`、`http.server`），运行时零 pip 依赖，PyInstaller 只用来构建
可选的 Windows 可执行文件。一个跑在编辑器钩子预算里的插件，没资格携带依赖树。

工程文化没那么无聊，而它才是真正的产品：

- **测量优先于假设。** 功能与修复进入本项目时都附带一个数字：一个被复现的
  缺陷、一个被量出的延迟、一个被数过的爆炸半径。当一个假设被保留时（比如
  macOS 支持），文档写"未测量"，而不是暗示有证据。
- **失败即关闭，逃生常开。** 隐私过滤、配置解析和归属检查都朝着*不泄漏*、
  *不靠猜*的方向失败。强制执行——唯一一个"失败即关闭"会困住用户的地方——则改配
  有界的逃生预算和关闭开关。
- **不可能变红的检查不是检查。** 每条已登记的修复都有一个可证伪用例：在副本上
  撤销修复、证明闸门失败。好几个用例抓到的是*检查本身*形同虚设而不是修复有错；
  处理方式全都是加强检查，从不删除用例。
- **数数的文字绑定到定义它的代码。** 这些文档里的"全部六个钩子" <!--ce:hooks--> 是对着钩子清单
  机器核对的；在文字里手工枚举一个集合被当作一类缺陷对待，因为闸门出现之前它
  烂过三次。
- **历史只追加。** CHANGELOG 条目从不为迎合现状而改写；记忆行归档、从不删除；
  被取代的事实留在可走的链上。一个以记忆为业的系统，不该自己靠覆盖来遗忘。

## 架构

完整细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
（[简体中文](docs/ARCHITECTURE.zh.md)）。三条硬契约的规范在
[docs/CONTRACTS.md](docs/CONTRACTS.md)（[简体中文](docs/CONTRACTS.zh.md)）：

- [反补丁契约](docs/CONTRACTS.md#anti-patch-contract)——一次写入如何调和
- [交接契约](docs/CONTRACTS.md#handoff-contract)——PROGRESS.md 规范
- [计划契约](docs/CONTRACTS.md#plan-contract)——PLAN.md、结转闸门、指令账本、子代理

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
├── .github/                 CI：gates.yml（每次 push 跑全部闸门）· release.yml
│                            （tag → 闸门 → exe → 实际运行 → Release）· 模板
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
├── scripts/                 build_exe.py（PyInstaller）· release_notes.py
│                            （CHANGELOG 段落 → 发布正文）
├── tests/                   4 个套件 + run_gates.py（一条命令跑完所有闸门）
├── tools/                   citation_check · doc_claims · doc_coverage · contracts ·
│                            falsify_fixes · i18n_check
├── CLAUDE.md                给 Claude Code 的项目说明
├── CHANGELOG.md · README.md · README.zh.md · LICENSE · pyproject.toml
```

### 发布闸门

十一道闸门，全部纯标准库——没有 pytest，没有 pip 依赖。一条命令跑完：

```bash
python tests/run_gates.py           # 跑全部 11 道，打印表格，任一变红即非零退出
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

[Releases](https://github.com/skymanbp/cc-memory/releases) 页面上的可执行文件
**不是**在维护者机器上构建的。`.github/workflows/release.yml` 在 `v*` tag 推送时
构建它们——先对被打 tag 的提交重跑全部发布闸门，然后在发布前**实际运行**它们：
安装器对一个沙箱 home 目录做一次真实的 `--cli` 安装与 `--uninstall`，且必须拒绝
未知参数；看板以 `--help` 启动且必须干净退出。只有这些都过了，才创建 GitHub
Release，附上两个 exe，并以对应的 CHANGELOG 段落作为正文。与 `core/version.py`
不一致的 tag、或没有 CHANGELOG 条目的 tag，都发不出去。

### 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题报告见 [SECURITY.md](SECURITY.md)。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| Windows 上钩子从不触发 | `hooks/hooks.json` 调用的是 `python3`，而 python.org 的安装包默认不提供 `python3.exe`。勾选 "Add Python to PATH" + "py launcher"，或在 PATH 上把 `python3` 指向 `python` |
| `/cc-mem` 说找不到插件 | 两种布局都必须探测。跑 `/cc-mem status`——它会检查布局并报告缺了哪些文件 |
| 什么都没被抽取 | 没有凭据。`/cc-mem status` 会检查。登录 Claude Code，或设置 `ANTHROPIC_API_KEY` |
| 数据库到底在哪？ | `/cc-mem paths` 打印解析后的 DB / PROGRESS.md / PLAN.md / MEMORY.md 及各自的存在/缺失结论——不要用递归 glob 去找；它先找到的那个 `*.db` 可能属于别的工具 |
| CJK 输出显示成 `�` | 是*捕获输出的 shell* 用自己的码页解码了 UTF-8（PowerShell 5.1 用的是控制台码页）。改用 `--json`——纯 ASCII 输出，任何捕获码页都糟蹋不了 |
| 子目录里冒出一个 `.ccm/` | 根锚定之前留下的游离数据库。`/cc-mem status` 会列出项目根之下每一个独立数据库及其记忆数。游离库只被**报告，绝不合并或删除**——真正的嵌套子项目请用 `.ccm-root` 文件钉住 |
| 插件彻底不出声了 | 存在但无法解析的 `config.json` 会**失败即关闭**并排除所有项目。`SessionStart` 会打印一行说明；把 JSON 修好即可 |
| Claude 结束不了一轮 | 计划强制执行正在拦截。读那段拒绝文本——它会指明是哪个条件以及怎么修。逃生预算耗尽后它一定会退化成建议；`CC_MEMORY_PLAN_ENFORCE=0` 可以整个关掉 |
| 某条指令总在拦截，但它明明在等*我* | `/cc-mem directive-edit <slug> --status blocked` 把它停靠起来（闲置强制执行会跳过它）；`--status active` 解除停靠。"绝不做 X"类规则应该用 `--kind constraint`——它根本不做闲置检查 |
| 压缩时出现 `Hook cancelled` | v2.3.2 已通过把整理移到 `async` 腿修掉。若仍出现，请带上 `.ccm/.last_save.json` 提 issue |
| 某条记忆就是错的 | `/cc-mem archive <id>`——调和处理的是*重述*，`archive` 处理的是*推翻* |

---

## 路线图与已知限制

记录下来，而不是糊过去——没写出来的限制，就得由别人重新踩一遍才能发现：

- **macOS 未测量。** 十一道闸门全部在 CI 的 Windows 与 Linux（3.11、3.13）上
  运行；macOS 预期可用（与 Linux 演练的是同一套 POSIX 路径），但没有被测量过，
  本文档不会说它被验证过。
- **步骤引用审计是词法层面的。** 它抓 `步骤 N` / `step #N` / `#N` 这些形状；
  用文字转述编号的指令（"第十二步"）匹配不到。长期规则是按标题引用步骤——审计
  是为规则已经被违反的场合准备的。
- **积压阈值是模块常量**（50 行 / 7 天），不是配置键——这是刻意的，等真实使用
  证明它们需要按项目调整再说。提高阈值意味着编辑 `core/consolidate.py`，并且
  知道自己为什么这么做。
- **Tkinter 看板的外壳没有可执行覆盖。** 其逻辑核心已被抽成纯函数并做了无头
  测试（v2.10.1）；在没有测试的前提下重构剩下的 2.9k 行 GUI 被刻意推迟。
- **闸门的限制被记录下来，而不是被设计掉（v2.14.0）。** 所在句子没有点名任何
  符号的引文只做边界检查（在文件内、非空行），烂掉了也不会变红；名词不在
  `doc_claims` 触发词表里的计数句不是闸门看得见的断言；`verbatim` 引用逐段核对，
  真段落被打乱顺序也能通过。
- **候选的后续工作：** 在 Stop 状态行里呈现 `inject-usage` 信号；看板里的
  `directive-*` 界面；面向多数据库机器的更丰富的 `paths` 式诊断。

---

## v2.14.0 有什么新东西

**项目的身份是它的数据库，而不是数据库里那串路径。** 一次全仓库调试审查（六个
只读审查者，每条发现在上报前都先复现）发现 `projects.path` *就是*身份，每个面都
用自己的路径算术来判断"这是哪个项目"。移动或重命名项目目录会铸出第二行，于是每条
记忆、会话、进度行、计划和指令在每个面上都变得不可见。

- **路径只有一种可比较的拼法**——`core.layout.canonical_path`。搬过家的项目保住
  自己的行，兄弟项目的行绝不会被拿走，`status` / `list` 也不再为了回答一个问题而
  创建一行。
- **整理标记跟着行走**，所以手动的 `/cc-mem consolidate --project .` 之后不会再
  跟一次多余的后台运行。
- **在 v2.13.0 之前的 `memory/` 上打开的句柄会跟随改名到 `.ccm/`**——单向，且
  只在一次连接失败之后——而不是在另一个面完成改名后每次操作都失败（Windows 上
  打开的句柄会拒绝改名）。
- **又有四条发现在各自的根源上关闭：** `<PRIVATE>…</PRIVATE>` 现在和小写形式
  一样被剥除和转义；Stop 钩子的逃生预算在被拒条件解决后会重置，而不是三次拒绝后
  让强制执行在本会话余下时间里降为建议；找不到主目录不再丢掉显式给出的
  `ANTHROPIC_API_KEY`；`/save-memories` 写进钩子读的那个数据库。
- **四个闸门检查器不再为自己没检查的事背书**——文档面必须被*点名*，而不是仅仅
  被包含；带两个修饰词的计数仍然是计数；检查器只能做边界检查的引文按原话报告；
  `--emit-marker` 拒绝为没人翻译过的译文重新盖章（纯英文改动用
  `--translation-unchanged "<原因>"`）。

v2.13.0 把每个项目的状态从 `memory/` 挪到了 `.ccm/`——放在 `.git` 旁边的点状态
目录，首次写入时单向迁移，按内容而不是按名字识别。v2.12.x 带来了背压触发的整理、
`directive-edit`、步骤引用审计、`paths` / `--json` / `--full`、CI 构建的 release，
以及[加上它之前与之后](#加上它之前与之后)的实录。

更早的每个版本都在 **[CHANGELOG.md](CHANGELOG.md)** 里，那是本项目唯一的历史；
这份 README 记录的是这个软件**是什么**，而不是它曾经是什么。

---

## 运行要求

- **Python 3.8+**——仅标准库，零运行时依赖
- 支持钩子的 **Claude Code**
- **Tkinter** 只有桌面看板需要（CLI、MCP 服务器和网页查看器都不需要）
- **PyInstaller** 只在构建可执行文件时需要
- **Windows**：`python3` 必须能解析到一个 Python 3 解释器（见[故障排查](#故障排查)）

以 Windows 为首要平台开发。**全部十一道发布闸门在 CI 上同时跑 Windows 与 Linux
（Python 3.11 与 3.13）**；macOS 未被 CI 覆盖——预期可用但没有被测量过，本文档
不会说它被验证过。

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
归一化哈希上，一旦漂移 `tools/i18n_check.py` 就会让测试变红。自 v2.14.0 起标记还
带着译文自身正文的哈希，所以 `--emit-marker` 会拒绝为没人翻译过的译文重新盖章。
记忆**内容**与语言无关——只有项目自身的文档遵循这个约定。

---

## 许可

[MIT](LICENSE) © skymanbp

---

<sub>**关键词** — Claude Code 插件 · Claude Code 记忆 · LLM 智能体持久化记忆 ·
智能体长期记忆 · 上下文窗口管理 · 对话压缩恢复 · 会话交接 · AI 编程助手记忆 ·
Anthropic Claude · MCP 服务器 · Model Context Protocol · SQLite FTS5 记忆库 ·
编程智能体检索 · 提示注入加固 · 纯标准库 Python 插件。</sub>

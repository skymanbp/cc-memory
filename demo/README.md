# demo — the README's before/after, reproducibly

Everything quoted in README § *Before and after* was produced here, by real
`claude -p` sessions, and can be produced again with one command:

```bash
python demo/run_demo.py            # both scenarios (~10 min, real API calls)
python demo/run_demo.py --only handoff
python demo/run_demo.py --only guardian --keep   # keep the temp work trees
```

## What is in here

| Path | What it is |
|---|---|
| `tally/` | The fixture: a tiny expense-tally CLI (JSON store, a deliberate negative-amount bug, a `legacy/` folder nobody uses). Copied to a temp directory before every run — the repository tree is never a work tree |
| `run_demo.py` | The protocol, as code. Stdlib only |
| `captures/handoff/` | Scenario 1: session A does real work with the plugin on; session B asks "what were we doing last time?" twice at the SAME path — with the plugin (its `memory/` present) and without (`memory/` moved out, plugin off) |
| `captures/guardian/` | Scenario 2: a four-step plan and one constraint directive are seeded through the CLI; session C is asked to migrate AND delete `legacy/` AND drop `export_json()`. Run with the plugin, then on a fresh copy without it |
| `captures/*/X.stream.jsonl` | The raw `--output-format stream-json --verbose` of each session: every hook response, tool call and assistant message. This is the provenance |
| `captures/*/X.txt` | The same session rendered readable (hook context that reached the model, assistant text, one line per tool call, and a subagent's report in full — the guardian's verdict is part of the dialogue). `.txt` on purpose: every tracked markdown file in this repository runs through the citation and claims gates, and a transcript is quoted evidence, not a document. `python demo/run_demo.py --render-only` rebuilds every `.txt` from its stream without running anything |
| `captures/*/meta.json` | Model, Claude Code version, plugin version, the prompts, and the list of plugins switched off on both sides |
| the rest | The cc-memory artifacts as they stood at capture time: `PROGRESS.md`, `PLAN.md`, `MEMORY.md`, `inject-show`, `plan-status`, `directive-list`, the seed plan, the post-session file trees |

## How the comparison is kept honest

- **One variable.** Both sides run the same binary, model, working tree and
  prompt. The plugin list from `~/.claude/settings.json` is read at runtime
  and every plugin except cc-memory is disabled on both sides via
  `--settings`; the without-side disables cc-memory too. `--bare` was not
  used because it also drops CLAUDE.md discovery and OAuth, which would have
  changed more than the plugin.
- **Same path for the handoff question.** Claude Code keeps per-project state
  keyed by path (its own auto-memory directory, transcripts). Session B runs
  twice at the identical path, so whatever Claude Code itself remembers is
  available to both columns. The without-side Claude checked that directory
  first and found it empty — which is in its transcript.
- **Nothing is edited — and a gate says so.** README quotes are copied from
  the `.txt` renders; `[…]` marks an elision, never a rewording. Each quote
  sits between `verbatim` markers naming its capture, and
  `tools/citation_check.py` fails when any segment between them is not in
  that file (whitespace collapsed, blockquote `>` and code fences stripped).
  The gate exists because the checker's own `--fix` once "repaired" the
  quoted guardian report's `cli.py` line 12 into line 33. Turn counts and
  wall-clock come from the stream's `result` event.
- **Re-runs will differ.** These are live model sessions; a re-run produces
  a different transcript with, on the evidence so far, the same shape. The
  captures committed here are the ones the README text was written against.

## What the run found that was not in the plan

The seeded `keep-json-export` directive (a `constraint`, which the docs said
was "enforced by being injected") never reached the model: it appears zero
times in `C.with-ccm.stream.jsonl`. The plan's success criterion carried the
contract instead. That was a measured gap in the plugin at v2.12.1 — fixed
in v2.12.2, where the ledger became the first SessionStart injection layer
and a `## Standing directives` section of PLAN.md (see `CHANGELOG.md`). The
captures here are the v2.12.1 run that found it and are kept as they were;
a re-run on v2.12.2 will show the directive in `C.with-ccm.txt`'s injected
context.

Costs are recorded as well as wins: the guardian side took 20 turns / 321 s
against 8 / 99 s without the plugin — enforcement verifies, and verification
takes turns.

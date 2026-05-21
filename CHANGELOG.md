# Changelog

All notable changes to cc-memory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.1.0] — 2026-05-21

The "anti-patch + forced handoff" release. Major restructure of save paths and
handoff mechanics. Backwards-compatible for stored data (existing DBs migrate
forward automatically); existing installations need `installer.py` re-run to
update settings.json paths to the new subpackage layout.

### Added

- **`llm.memory_writer.upsert_smart`** — unified anti-patch write entry. All
  save paths (PreCompact, Stop observer, `/save-memories` skill, MCP `memory_add`,
  CLI `mem.py add`) now route through one function that decides MERGE_IN_PLACE
  vs SUPERSEDE vs INSERT based on trigram-Jaccard similarity. See
  [`docs/MEMORY_RULES.md`](docs/MEMORY_RULES.md).
- **`memories.supersedes_id`** column + `db.get_supersede_chain(id)` — preserves
  update history. Walk a chain via `mem.py supersedes <id>`.
- **`progress` SQL table** + **`memory/PROGRESS.md`** — replaces v2.0
  `SESSION_HANDOFF.md`. Always full-rewritten from the SQL row, never appended.
  See [`docs/HANDOFF_PROTOCOL.md`](docs/HANDOFF_PROTOCOL.md).
- **Forced `<system-reminder>` at SessionStart** — instructs the next session
  to `Read memory/PROGRESS.md` before responding. Replaces the soft "remember
  to call /save-memories" text spam.
- **`core.idle.maybe_run_idle`** — every 5 user turns, run lightweight no-LLM
  reorg (garbage cleanup + topic assignment + MEMORY.md regen) from the Stop
  hook. Closes the "MEMORY.md goes 50 days stale between PreCompacts" gap.
- **`memory_writer.regenerate_memory_index`** — `memory/MEMORY.md` is now
  refreshed after every batch write, not just at PreCompact.
- **`core.progress`** — PROGRESS.md generator (`write_progress_md`),
  state collector (`collect_progress_state`), and one-shot migrator
  (`migrate_legacy_handoff`) that renames stale `SESSION_HANDOFF.md` to
  `SESSION_HANDOFF.md.v2.bak`.
- New CLI subcommands:
  - `mem.py progress` — force-regenerate `memory/PROGRESS.md`.
  - `mem.py supersedes <id>` — walk the supersede chain for a memory.
- New MCP tools: `progress_get`, `progress_regenerate`.
- `pyproject.toml`, `commands/cc-mem.md`, `docs/{ARCHITECTURE,MEMORY_RULES,HANDOFF_PROTOCOL}.md`,
  `CHANGELOG.md` — proper plugin packaging and documentation.

### Changed

- **Repository layout**: `cc_memory/` reorganized into subpackages
  `core/` (db, extractor, consolidate, idle, progress, privacy, modes, auth,
  logger), `hooks/` (5 hook entry points), `llm/` (ccl_backend, memory_writer),
  `cli/` (mem, plan), `mcp/` (server), `ui/` (installer, dashboard, web_viewer).
  Reduces the previous 22-file flat directory.
- `hooks/hooks.json` paths updated to `cc_memory/hooks/<name>.py`.
- `installer.py` (was `installer_standalone.py`) now mirrors the subpackage
  layout under `~/.claude/hooks/cc-memory/` and auto-detects/cleans v2.0
  flat-layout installs on upgrade.
- `build_exe.py` bundles the subpackage tree into `cc_memory_files/<subdir>/`.
- `extractor.py`: removed hard-coded astrophysics/ML keywords
  (`CNN, Swin, GNN, HOG, SBI, TDA, fusion, LOCO, ...`) that contaminated this
  generic plugin. Metric extraction is now project-neutral.
- `consolidate.py`: removed the same astro `_GROUPS` dict; topic clusters now
  derive purely from project keyword frequency.
- `session_start.py`: layered context injection rebalanced — `progress`
  preview now takes 25% of the budget (was 15% for `handoff`).
- `stop.py`: removed the "remember to call /save-memories" text reminder
  (replaced by the SessionStart forced reminder).
- Version bumped from `2.0.0` to `2.1.0` in all locations
  (`__init__.py`, `config.json`, `plugin.json`, `marketplace.json`,
  `mcp/server.py`).

### Removed

- `.claude/skills/` directory — was a duplicate of `skills/` ("stacking"
  violation). `skills/<name>/SKILL.md` is now the only canonical location.
- `cc_memory/skill_template.md` — was a third divergent copy of the
  `save-memories` skill. Deleted; installer deploys from `skills/`.
- `cc_memory/skill_status.md` — duplicate of `skills/mem-status/SKILL.md`.
- `cc_memory/installer.py` — superseded by `cc_memory/ui/installer.py`
  (renamed from `installer_standalone.py`, which is also removed).
- `cc_memory/setup.py` — redundant with auto-init in `UserPromptSubmit`.
- `MemoryDB.global_db()` cross-project registry — dead code, never wired up.
- Orphan `memory_timeline` mention in `mcp_server.py` docstring (the tool
  was declared but never implemented).

### Fixed

- `memory/MEMORY.md` going 50+ days stale because only PreCompact regenerated
  it. Now every write path (Stop observer, /save-memories, mem.py add, MCP
  add) calls `regenerate_memory_index` automatically.
- `memory/SESSION_HANDOFF.md` accumulating pollution (Bash output, log
  fragments, tool error text) because of append-style writes. Replaced
  entirely by PROGRESS.md, which never appends.
- Multiple version strings drifting out of sync (CLAUDE.md said 1.1.0;
  `__init__.py` said 2.0.0; README said 14 modules when there were 22).
  All metadata is now generated/validated from a single source.
- The `save-memories` skill bypassing `is_duplicate_hash` and using its own
  in-memory set-membership check (which missed punctuation variants).

### Migration notes

- **Existing installations**: re-run `installer.py` (or
  `cc-memory-installer.exe`). The installer detects v2.0 flat-layout files
  and removes them before laying down the v2.1 subpackage structure. Your
  per-project `memory.db` is migrated forward in place by `_MIGRATIONS:v3_*`
  (adds `supersedes_id` and the `progress` table).
- **Existing `SESSION_HANDOFF.md`**: on first PreCompact under v2.1, the
  file is renamed to `SESSION_HANDOFF.md.v2.bak`. PROGRESS.md takes over.
- **Hook commands in `~/.claude/settings.json`**: paths change from
  `…/cc-memory/pre_compact.py` to `…/cc-memory/cc_memory/hooks/pre_compact.py`.
  The installer rewrites these automatically.

---

## [2.0.0] — earlier

PostToolUse capture, FTS5 search, progressive disclosure context injection,
MCP server, web viewer, privacy tags, mode system. (Pre-2.1 history is
condensed; see git log for detail.)

## [1.1.0] — earlier

Initial public version: 3 hooks (PreCompact / SessionStart / Stop), SQLite
backend, LLM extraction via Haiku, /save-memories skill.

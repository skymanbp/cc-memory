---
name: ccm-load
description: Load cc-memory for this project — verify the plugin is globally enabled, ensure the project's memory/ directory is initialized, and report end-to-end health. The cc-memory equivalent of "make sure I'm wired up here".
---

## /ccm-load — Load cc-memory into this project

Run this once in any new project to confirm cc-memory is **active**, the
project's `memory/` is initialized, and PROGRESS.md/MEMORY.md are generated.
Idempotent — safe to re-run.

### What this skill does

This skill covers **activation and project bootstrap** — the things no other
entry point does. Ongoing diagnostics belong to `/cc-mem status`; the two are
deliberately disjoint (see the table at the end of this file).

1. **Verify global plugin activation** — check `~/.claude/settings.json` for
   `enabledPlugins["cc-memory@cc-memory"]=true` and a matching
   `extraKnownMarketplaces.cc-memory` entry. If missing, print the exact
   `/plugin install` commands the user needs to run. **This check exists
   nowhere else** — not in `/cc-mem status`, not in the hooks.
2. **Resolve the installed package tree** across both layouts (nested
   marketplace/dev checkout, flat standalone install) and fail loudly with
   actionable instructions if neither resolves.
3. **Auto-initialize this project's `memory/`** — if `memory/memory.db` is
   absent, create the directory tree + DB + `.gitignore`. (This also happens
   on first UserPromptSubmit; this skill makes it explicit.)
4. **Seed PROGRESS.md** — write a current snapshot from the (possibly empty)
   `progress` row so the file exists from day one.
5. **Print quick DB counts** (memories / sessions / topics / observations).
   This is **not** the `/cc-mem status` health check — install-layout
   inspection, hook-registration verdicts, API-key resolution and last-save
   staleness all live there and are NOT run by this skill.
6. **Report status** to the user in 1-2 sentences.

### Step 1 — Run this script

```bash
python3 -c "
import json, os, sys
from pathlib import Path

# ── (1) Global activation check ────────────────────────────────────────
settings = Path.home() / '.claude' / 'settings.json'
marketplace_path = None
issues = []
if settings.exists():
    try:
        s = json.loads(settings.read_text(encoding='utf-8'))
        enabled = s.get('enabledPlugins', {}).get('cc-memory@cc-memory') is True
        mk = s.get('extraKnownMarketplaces', {}).get('cc-memory')
        if not enabled:
            issues.append('enabledPlugins[\"cc-memory@cc-memory\"] is not true')
        if not mk:
            issues.append('extraKnownMarketplaces.cc-memory is missing')
        else:
            marketplace_path = (mk.get('source') or {}).get('path')
    except Exception as e:
        issues.append(f'settings.json unreadable: {e}')
else:
    issues.append('~/.claude/settings.json not found')

if issues:
    print('=== cc-memory plugin NOT FULLY ACTIVATED ===')
    for i in issues:
        print(f'  - {i}')
    print()
    print('To activate, run inside Claude Code:')
    print('  /plugin marketplace add <path-to-cc-memory-repo>')
    print('  /plugin install cc-memory@cc-memory')
    sys.exit(0)
print('[OK] cc-memory plugin globally activated')

# ── (2) Project init ───────────────────────────────────────────────────
project = Path('.').resolve()
mem_dir = project / 'memory'
db_path = mem_dir / 'memory.db'

if not db_path.exists():
    print(f'[init] Creating memory/ at {mem_dir}')
    mem_dir.mkdir(exist_ok=True)
    (mem_dir / 'sessions').mkdir(exist_ok=True)
    (mem_dir / 'topics').mkdir(exist_ok=True)
    # Literal copy (inline script, no package import). Keep in sync with
    # core.progress.MEMORY_GITIGNORE_LINES.
    gi = mem_dir / '.gitignore'
    _ign = ['# cc-memory: generated state, not content', 'memory.db', 'memory.db-wal',
            'memory.db-shm', 'sessions/', '.last_save.json', '.last_inject.json',
            '.last_consolidation.json', '.consolidation.lock',
            '.pre_compact_attempt.json', '.plan_raw.md', '.plan_history/', '*.tmp']
    _have = {l.strip() for l in gi.read_text(encoding='utf-8').splitlines()} if gi.exists() else set()
    _miss = [l for l in _ign if l not in _have]
    if _miss:
        with open(gi, 'a', encoding='utf-8') as _f:
            _f.write('\n'.join(_miss) + '\n')

# ── (3) Resolve plugin root (env > marketplace > standard install) ─────
# why: hardcoding the maintainer's path breaks the skill on every other
# machine. Try CLAUDE_PLUGIN_ROOT first (set by Claude Code when invoked
# from a plugin context), then settings.json marketplace path, then the
# v2.0-style standalone install location under ~/.claude/hooks/.
# TWO layouts exist and both must be probed, otherwise a standalone install
# is invisible: ui/installer.py copies each subpackage to TARGET_DIR/<subdir>/
# directly, so it has NO cc_memory/ segment.
#   nested - marketplace / dev checkout:  <root>/cc_memory/core/db.py
#   flat   - standalone installer output: <root>/core/db.py
# Returns the directory to put on sys.path (the package parent), not the root.
def _pkg_dir(root):
    if not root:
        return None
    p = Path(root)
    if (p / 'cc_memory' / 'core' / 'db.py').exists():
        return p / 'cc_memory'
    if (p / 'core' / 'db.py').exists():
        return p
    return None

def _find_pkg_dir():
    for cand in (os.environ.get('CLAUDE_PLUGIN_ROOT'),
                 marketplace_path,
                 str(Path.home() / '.claude' / 'hooks' / 'cc-memory')):
        d = _pkg_dir(cand)
        if d:
            return d
    return None

pkg_dir = _find_pkg_dir()
if pkg_dir is None:
    print('[error] cannot locate cc-memory package tree.')
    print('  Set CLAUDE_PLUGIN_ROOT, or re-run /plugin install cc-memory.')
    sys.exit(0)
plugin_root = pkg_dir.parent if pkg_dir.name == 'cc_memory' else pkg_dir
sys.path.insert(0, str(pkg_dir.resolve()))

from core.db import MemoryDB
from core.progress import write_progress_md, migrate_legacy_handoff
from llm.memory_writer import regenerate_memory_index

# Migrate any v2.0 SESSION_HANDOFF.md aside before generating PROGRESS.md
migrate_legacy_handoff(mem_dir)

db = MemoryDB(db_path)
pid = db.upsert_project(str(project))
if not db.get_progress(pid):
    db.upsert_progress(pid, trigger_type='ccm-load')
write_progress_md(db, pid, mem_dir)
regenerate_memory_index(db, pid, mem_dir)
print(f'[OK] Project initialized: {mem_dir}')
print(f'[OK] PROGRESS.md + MEMORY.md generated; legacy handoff migrated if present')

# ── (4) Quick stats ─────────────────────────────────────────────────────
stats = db.get_stats(pid)
n_obs = db.get_observation_count(pid)
print(f'[stats] {stats[\"n_memories\"]} memories | {stats[\"n_sessions\"]} sessions '
      f'| {stats.get(\"n_topics\", 0)} topics | {n_obs} observations')
print()
print('cc-memory is loaded for this project. Hooks will fire automatically:')
print('  - UserPromptSubmit: track turn count + seed PROGRESS.md current_request')
print('  - PostToolUse:      capture observations')
print('  - Stop:             Haiku observer + idle reorg every 5 turns')
print('  - PreCompact:       full extraction + PROGRESS.md rewrite')
print('  - SessionStart:     inject context + FORCED <system-reminder> for read-first')
"
```

### Step 2 — Report

Summarize to the user in 1-2 sentences:
- If plugin not activated: "cc-memory is not fully wired up. Run `/plugin install cc-memory@cc-memory` in Claude Code, then re-run /ccm-load."
- If initialized fresh: "cc-memory loaded for {project_name}. PROGRESS.md and MEMORY.md generated; hooks will fire on subsequent activity."
- If already initialized: "cc-memory active here — {n_memories} memories, {n_sessions} sessions, last update at {timestamp}. PROGRESS.md refreshed."

### When to invoke

- **New project** that should benefit from cross-session memory.
- **After cloning** a repo that has a `memory/` directory but you've not yet
  loaded the project under cc-memory globally.
- **After upgrading** cc-memory (e.g. v2.0 → v2.1) to confirm the new
  PROGRESS.md mechanism initialized correctly.
- **Whenever PROGRESS.md or MEMORY.md is missing** but you expected them
  (e.g. you suspect a partial uninstall).

### Relation to other cc-memory entry points

These entry points are deliberately **orthogonal** — each owns something the
others cannot do. Nothing here is a subset of anything else.

| Entry point | Owns |
|-------------|------|
| `/ccm-load` (this) | **Activation + bootstrap**: global plugin-enablement check, package-tree resolution, project `memory/` creation, PROGRESS.md seeding. Run once per new project |
| `/cc-mem status` | **Ongoing diagnostics**: install-layout inspection, hook-registration verdicts, API-key resolution, last-save staleness. None of these are run by `/ccm-load` |
| `/cc-mem <other>` | **Querying and management**: search, list, topics, progress, consolidate, plan-* |
| `/cc-mem dashboard` | Tkinter GUI for the current project |
| `/save-memories` | **Manual write path** through the anti-patch writer. The hooks save automatically; this is the on-demand trigger |

`/ccm-load` is the recommended first command in a new project. Day-to-day
inspection uses `/cc-mem <subcommand>`; deliberate saves use `/save-memories`.

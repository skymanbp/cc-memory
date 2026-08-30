---
name: ccm-load
description: Load cc-memory for this project — verify the plugin is globally enabled, ensure the project's .ccm/ directory is initialized, and report end-to-end health. The cc-memory equivalent of "make sure I'm wired up here".
---

## /ccm-load — Load cc-memory into this project

Run this once in any new project to confirm cc-memory is **active**, the
project's `.ccm/` is initialized, and PROGRESS.md/MEMORY.md are generated.
Idempotent — safe to re-run.

### What this skill does

This skill covers **activation and project bootstrap** — the things no other
entry point does. Ongoing diagnostics belong to `/cc-mem status`; the two are
deliberately disjoint (see the table at the end of this file).

1. **Verify activation, PER INSTALL LAYOUT.** The two shipped layouts are
   activated by *different* mechanisms, so one check cannot serve both:
   - **marketplace / dev checkout** — `~/.claude/settings.json`
     `enabledPlugins["cc-memory@cc-memory"]=true`, root from
     `extraKnownMarketplaces.cc-memory` **or** `plugins/installed_plugins.json`
     (a `/plugin marketplace add <github-repo>` install has no local path),
     hooks declared by `<root>/hooks/hooks.json`.
   - **standalone / `.exe` installer** — never appears in `enabledPlugins` at
     all. `ui/installer.py:_merge_into_settings` writes **only** the `hooks`
     key, so activation means `settings.json["hooks"]` registers cc-memory for
     all five events; the tree it lays down is **flat**.

   Either layout activated ⇒ **proceed to bootstrap**. Nothing activated ⇒
   print the fix for the layout this machine actually has (never tell an
   `.exe` user to add a marketplace — they have no repo). The rule is mirrored
   from `cc_memory/cli/mem.py`'s `_detect_install_layouts` / `_inspect_layout`;
   `/cc-mem status` reports the same layouts but never bootstraps, so this is
   the only entry point that *gates project init* on the verdict.
2. **Resolve the installed package tree** across both layouts (nested
   marketplace/dev checkout, flat standalone install) and fail loudly with
   actionable instructions if neither resolves.
3. **Auto-initialize this project's `.ccm/`** — if `.ccm/memory.db` is
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
# NO BACKTICKS ANYWHERE BELOW, not even inside a comment. This whole body is
# a shell DOUBLE-quoted string, so a backtick opens command substitution and
# bash executes the text between them before python ever sees it. The fix for
# the best['pkg'] KeyError briefly carried two prose backticks in its comment,
# and bash dutifully ran them ('from: command not found', 'active: command not
# found'); output was empty so the run limped on, but the shape is arbitrary
# command execution. Caught in review before it was ever committed. For the
# same reason the block contains no dollar sign either, so bash has nothing
# to expand at all. Enforced by test_surfaces §7, which is why this very
# comment spells both characters out in words instead of quoting them.
import json, os, sys
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / '.claude' / 'settings.json'
LEGACY = HOME / '.claude' / 'hooks' / 'cc-memory'
EVENTS = ('PreCompact', 'SessionStart', 'Stop', 'PostToolUse', 'UserPromptSubmit')
Q = chr(34)  # a literal double quote: this script is embedded in a shell dquote

def _d(x):
    return x if isinstance(x, dict) else {}

def _l(x):
    return x if isinstance(x, list) else []

# ── (1) Activation check — PER LAYOUT ──────────────────────────────────
# Two layouts ship and they are activated by DIFFERENT mechanisms. Gating
# BOTH on enabledPlugins + extraKnownMarketplaces made this skill print
# 'NOT FULLY ACTIVATED' on every standalone / .exe install — the layout the
# README recommends to Windows users — and then skip bootstrap entirely,
# while /cc-mem status on the same machine reported '5/5 registered'. Two
# shipped surfaces, opposite verdicts on one healthy install.
#   marketplace / dev checkout — enabledPlugins['cc-memory@cc-memory'] true;
#       root from extraKnownMarketplaces.source.path or
#       plugins/installed_plugins.json; hooks declared by
#       <root>/hooks/hooks.json; tree is NESTED (<root>/cc_memory/core/db.py)
#   standalone (ui/installer.py) — never appears in enabledPlugins at all:
#       _merge_into_settings writes ONLY settings.json['hooks'], and
#       _copy_subpackages writes TARGET_DIR/<subdir>/, a FLAT tree with no
#       cc_memory/ segment (<root>/core/db.py)
# Mirrors cc_memory/cli/mem.py:_detect_install_layouts / _inspect_layout —
# see its enabled=True comment, 'legacy install does not gate on
# enabledPlugins'.
settings, settings_err = {}, None
if SETTINGS.exists():
    try:
        # utf-8-sig: a settings.json ever saved from PowerShell carries a BOM.
        settings = _d(json.loads(SETTINGS.read_text(encoding='utf-8-sig')))
    except Exception as e:
        settings_err = f'{SETTINGS} unreadable: {e}'
else:
    settings_err = f'{SETTINGS} not found'

def _pkg_dir(root):
    # The directory to put on sys.path, for EITHER on-disk shape, or None.
    if not root:
        return None
    p = Path(root)
    if (p / 'cc_memory' / 'core' / 'db.py').exists():
        return p / 'cc_memory'
    if (p / 'core' / 'db.py').exists():
        return p
    return None

def _manifest_events(root):
    hj = Path(root) / 'hooks' / 'hooks.json'
    if not hj.exists():
        return set()
    try:
        hooks = _d(json.loads(hj.read_text(encoding='utf-8'))).get('hooks', {})
        return set(_d(hooks)) & set(EVENTS)
    except Exception:
        return set()  # why: malformed hooks.json is reported below as 0/5

def _settings_events():
    got = set()
    blk = _d(settings.get('hooks'))
    for ev in EVENTS:
        for mg in _l(blk.get(ev)):
            for h in _l(_d(mg).get('hooks')):
                if 'cc-memory' in (_d(h).get('command') or ''):
                    got.add(ev)
    return got

layouts = []
# Prefix match, DELIBERATE MIRROR of cli/mem.py:_marketplace_enabled_key:
# cc-memory@<any marketplace> is the same plugin; testing only the literal
# cc-memory@cc-memory key reported a healthy install as disabled.
mp_enabled = any(isinstance(k, str) and k.split('@')[0] == 'cc-memory' and bool(v)
                 for k, v in _d(settings.get('enabledPlugins')).items())
mp_roots = []
mp_src = _d(_d(_d(settings.get('extraKnownMarketplaces')).get('cc-memory')).get('source'))
if mp_src.get('path'):
    mp_roots.append(mp_src['path'])
_inst = HOME / '.claude' / 'plugins' / 'installed_plugins.json'
if _inst.exists():
    try:
        _plugs = _d(_d(json.loads(_inst.read_text(encoding='utf-8'))).get('plugins'))
        for e in _l(_plugs.get('cc-memory@cc-memory')):
            if _d(e).get('installPath'):
                mp_roots.append(e['installPath'])
    except Exception:
        pass  # why: a corrupt plugin cache costs one root candidate, not the run
for r in dict.fromkeys(mp_roots):
    pd, evs, probs = _pkg_dir(r), _manifest_events(r), []
    if pd is None:
        probs.append(f'no cc-memory package tree under {r}')
    if not mp_enabled:
        probs.append('settings.json enabledPlugins has no enabled cc-memory@* key')
    if len(evs) < 5:
        probs.append(f'{Path(r) / \"hooks\" / \"hooks.json\"} declares {len(evs)}/5 events')
    layouts.append({'name': 'marketplace', 'root': Path(r), 'pkg': pd, 'n': len(evs),
                    'via': 'hooks/hooks.json', 'probs': probs})

if (LEGACY / 'cc_memory').exists() or (LEGACY / 'core' / 'db.py').exists():
    pd, evs, probs = _pkg_dir(LEGACY), _settings_events(), []
    if pd is None:
        probs.append(f'incomplete package tree under {LEGACY} (no core/db.py)')
    if len(evs) < 5:
        probs.append('settings.json[\"hooks\"] registers ' + str(len(evs)) + '/5 events '
                     '(missing: ' + ', '.join(e for e in EVENTS if e not in evs) + ')')
    layouts.append({'name': 'standalone', 'root': LEGACY, 'pkg': pd, 'n': len(evs),
                    'via': 'settings.json[hooks]', 'probs': probs})

for L in layouts:
    if L['pkg'] is None:
        shape = 'no package tree'
    elif L['pkg'] == L['root']:
        shape = 'flat'
    else:
        shape = 'nested'
    tag = 'OK  ' if not L['probs'] else 'WARN'
    # ASCII-only from here down: these lines print BEFORE core.encoding_setup
    # is importable, and a cp437 console cannot encode U+2014, which would
    # abort the skill with UnicodeEncodeError instead of bootstrapping.
    print(f'[{tag}] {L[\"name\"]} install at {L[\"root\"]} ({shape}) - '
          f'hooks {L[\"n\"]}/5 via {L[\"via\"]}')
    for p in L['probs']:
        print(f'         - {p}')

active = [L for L in layouts if not L['probs']]
if not active:
    print()
    print('=== cc-memory NOT FULLY ACTIVATED ===')
    if settings_err:
        print(f'  - {settings_err}')
    if not layouts:
        print('  No cc-memory install found. Checked:')
        print(f'    marketplace  {SETTINGS} extraKnownMarketplaces, and')
        print(f'                 {_inst}')
        print(f'    standalone   {LEGACY}')
    print()
    print('Fix - pick exactly ONE (both at once registers every hook twice):')
    if not layouts or any(L['name'] == 'standalone' for L in layouts):
        print('  Standalone / .exe - no repo needed. Re-run the installer:')
        print('    cc-memory-installer.exe          (add --cli for a console run)')
        print('    or: python ' + Q + str(LEGACY / 'ui' / 'installer.py') + Q + ' --cli')
    if not layouts or any(L['name'] == 'marketplace' for L in layouts):
        print('  Plugin / dev checkout - needs the repo. Inside Claude Code:')
        print('    /plugin marketplace add <path-to-cc-memory-repo>')
        print('    /plugin install cc-memory@cc-memory')
    sys.exit(0)

best = active[0]
print(f'[OK] cc-memory ACTIVATED ({best[\"name\"]} layout) - bootstrapping project')

# ── (2) Project init ───────────────────────────────────────────────────
# Anchor on the project ROOT, not the shell's cwd. Run from a subdirectory
# this used to build the scaffold there — the exact stray database the hooks
# refuse to create since v2.6.0 — and because an existing database is
# terminal, all six hooks would then be pinned to it permanently. Falls back
# to cwd if the resolver cannot be imported (flat installs predating it).
project = Path('.').resolve()
# 'pkg', not 'root': _pkg_dir returns the directory that CONTAINS core/,
# which is what the imports below need on sys.path — under the nested
# layout that is <root>/cc_memory, not <root>. And never None here: a None
# pkg always appends a probs entry (marketplace and standalone both), and
# 'active' keeps only the layouts whose probs list came out empty.
sys.path.insert(0, str(Path(best['pkg'])))
try:
    # The opt-out gate lives in its OWN try, SEPARATE from the anchoring try
    # below and BEFORE it. It used to sit after the core.roots import inside
    # one shared try, so a package tree missing core/roots.py (an older or
    # partial flat install) raised ImportError PAST the gate, the except arm
    # said 'root anchoring unavailable', and an EXCLUDED project was then
    # fully initialized — database, PROGRESS.md, MEMORY.md, .gitignore
    # (reproduced both ways: intact tree refuses, roots-less tree created
    # everything). The gate must not share a failure domain with a module it
    # does not need. Checked on the pre-anchor path so a per-subdirectory
    # exclusion is not widened. cli_opt_out_notice itself never raises; this
    # try only covers the import, and failing OPEN on an unimportable
    # core.modes is the documented convention shared with cli/mem.py and
    # ui/installer.py (the hooks and the MCP server enforce the opt-out
    # independently on every write).
    from core.modes import cli_opt_out_notice
    _refusal = cli_opt_out_notice(str(project))
    if _refusal:
        print('[cc-memory] ' + _refusal)
        sys.exit(0)
except ImportError as _gate_err:
    print(f'[init] opt-out check unavailable ({_gate_err}); continuing')
try:
    from core.roots import project_root
    anchored = Path(project_root(str(project))).resolve()
    if anchored != project:
        print(f'[init] {project} is inside a project rooted at {anchored} - using that root')
        project = anchored
except Exception as _anchor_err:
    print(f'[init] root anchoring unavailable ({_anchor_err}); using {project}')
mem_dir = project / '.ccm'
try:
    # The migrating resolver, not a join: a project last touched before
    # v2.13.0 still has its state under 'memory', and this skill is often the
    # first thing run in it. Imported the same way project_root just was, and
    # degraded the same way - the literal above is what a broken import falls
    # back to, and it is held to core.layout.MEMORY_DIRNAME by the parity gate.
    from core.layout import memory_dir as _resolve_state_dir
    mem_dir = _resolve_state_dir(project)
except Exception as _layout_err:
    print(f'[init] state-directory resolver unavailable ({_layout_err}); using {mem_dir}')
db_path = mem_dir / 'memory.db'

if not db_path.exists():
    print(f'[init] Creating {mem_dir.name}/ at {mem_dir}')
    mem_dir.mkdir(exist_ok=True)
    (mem_dir / 'sessions').mkdir(exist_ok=True)
    (mem_dir / 'topics').mkdir(exist_ok=True)
    # Literal copy (inline script, no package import). Must mirror BOTH halves
    # of core.progress.ensure_memory_gitignore: the LINE LIST
    # (core.progress.MEMORY_GITIGNORE_LINES) and the APPEND SEMANTICS. Only the
    # list was ever mirrored: this copy did open(gi, 'a') + join(missing), so
    # against an existing .gitignore whose last line had no trailing newline it
    # emitted 'sessions/# cc-memory: generated state, not content' - fusing the
    # user's last rule with our first comment and destroying that rule. The
    # read / normalise / write shape below matches core/progress.py:70-76 line
    # for line; cc_memory/ui/installer.py:_init_project is copy #3.
    gi = mem_dir / '.gitignore'
    _ign = ['# cc-memory: generated state, not content', 'memory.db', 'memory.db-wal',
            'memory.db-shm', 'sessions/', '.last_save.json', '.last_inject.json',
            '.last_consolidation.json', '.consolidation.lock', '.consolidation.kick',
            '.pre_compact_attempt.json', '.plan_raw.md', '.plan_history/', '*.tmp']
    # errors='replace', mirroring core/progress.py's canonical read: a user
    # line appended from a GBK editor or a PowerShell redirect makes strict
    # UTF-8 raise UnicodeDecodeError, which aborted this whole skill with a
    # traceback (reproduced with a UTF-16 .gitignore: rc=1). The canonical
    # implementation gained errors='replace' for exactly this and the two
    # literal copies missed it — the drift the parity gate now checks.
    _cur = gi.read_text(encoding='utf-8', errors='replace') if gi.exists() else ''
    _have = {l.rstrip() for l in _cur.splitlines()}  # rstrip: leading space is significant to git (D3)
    _miss = [l for l in _ign if l not in _have]
    if _miss:
        _pre = _cur if _cur.endswith('\n') or not _cur else _cur + '\n'
        gi.write_text(_pre + '\n'.join(_miss) + '\n', encoding='utf-8')

# ── (3) Resolve the package tree (env override > activated layout) ─────
# why: hardcoding the maintainer's path breaks the skill on every other
# machine. CLAUDE_PLUGIN_ROOT is set by Claude Code in a plugin context and
# wins when it resolves; otherwise use the layout certified above, whose
# pkg dir is already the correct one for its shape (flat or nested).
pkg_dir = _pkg_dir(os.environ.get('CLAUDE_PLUGIN_ROOT')) or best['pkg']
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
- If not activated: relay the script's own **layout-appropriate** fix verbatim —
  a standalone/`.exe` user re-runs `cc-memory-installer.exe`; a marketplace/dev
  user runs `/plugin install cc-memory@cc-memory`. Do not offer the marketplace
  route to a user who has no repo. Then: "…, then re-run /ccm-load."
- If initialized fresh: "cc-memory loaded for {project_name}. PROGRESS.md and MEMORY.md generated; hooks will fire on subsequent activity."
- If already initialized: "cc-memory active here — {n_memories} memories, {n_sessions} sessions, last update at {timestamp}. PROGRESS.md refreshed."

### When to invoke

- **New project** that should benefit from cross-session memory.
- **After cloning** a repo that has a `.ccm/` directory but you've not yet
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
| `/ccm-load` (this) | **Activation + bootstrap**: global plugin-enablement check, package-tree resolution, project `.ccm/` creation, PROGRESS.md seeding. Run once per new project |
| `/cc-mem status` | **Ongoing diagnostics**: install-layout inspection, hook-registration verdicts, API-key resolution, last-save staleness. None of these are run by `/ccm-load` |
| `/cc-mem <other>` | **Querying and management**: search, list, topics, progress, consolidate, plan-* |
| `/cc-mem dashboard` | Tkinter GUI for the current project |
| `/save-memories` | **Manual write path** through the anti-patch writer. The hooks save automatically; this is the on-demand trigger |

`/ccm-load` is the recommended first command in a new project. Day-to-day
inspection uses `/cc-mem <subcommand>`; deliberate saves use `/save-memories`.

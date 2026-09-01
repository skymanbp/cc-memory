---
name: save-memories
description: Save important memories from this conversation to the cc-memory database via the anti-patch upsert path (merge / supersede / insert based on similarity to existing memories).
---

## Save Memories to cc-memory Database

Review the current conversation and persist **structured memories** through the
**anti-patch writer** (`llm.memory_writer.upsert_smart`). Never bypass this path:
it auto-decides whether to MERGE (overwrite an existing high-similarity memory),
SUPERSEDE (archive an old version and insert a refined one with a chain link),
or INSERT (genuinely new fact). This prevents stacked duplicates.

### Step 1 — Review the conversation

Identify, since the last save or session start:
- **Decisions** made (architecture choices, parameter selections, tradeoffs)
- **Results** (measurements, benchmarks with specific numbers + units)
- **Bugs** found and fixed (root cause + fix; especially "NEVER do X" warnings)
- **Config** changes (hyperparameters, env vars, settings that were tuned)
- **Architecture** insights (module structure, pipeline design, data flow)
- **Tasks** still pending or blocked

### Step 2 — Score each candidate

- `category`: one of `decision`, `result`, `config`, `bug`, `task`, `arch`, `note`
- `importance`: 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=skip)
- `content`: one self-contained sentence with **specific values** (numbers, file
  paths, parameter names). Bad: "tuned the learning rate". Good: "lr=3e-4 picked
  over 1e-3 because val_loss flatlined after epoch 8."
- `topic`: a short lowercase keyword for grouping (e.g. `auth`, `pipeline`, `ui`)

### Step 3 — Quality bar

- Only **conclusions**, not the discussion process
- Each memory must be understandable WITHOUT the conversation
- 5-15 memories per call (quality > quantity)
- Skip: tool errors, navigation, meta-discussion, conversation logistics
- Do NOT save memories ABOUT the memory plugin itself unless it's a critical bug

### Step 4 — Run this exact command

The writer handles dedup, similarity-based reconcile, and MEMORY.md regen
automatically. Do not call `db.insert_memory` directly.

```bash
python3 -c "
import json, os, sys
from pathlib import Path

# why: the old hardcoded ~/.claude/hooks/cc-memory/cc_memory path made this
# skill dead on every install that is not a v2.0-era standalone one -- on a
# marketplace install that directory holds only logs/. Probe BOTH layouts:
#   nested - marketplace / dev checkout:  <root>/cc_memory/core/db.py
#   flat   - standalone installer output: <root>/core/db.py
# (ui/installer.py copies each subpackage to TARGET_DIR/<subdir>/ directly.)
def _pkg_dir(root):
    if root and (Path(root) / 'cc_memory' / 'core' / 'db.py').exists():
        return Path(root) / 'cc_memory'
    if root and (Path(root) / 'core' / 'db.py').exists():
        return Path(root)
    return None

def _find_pkg_dir():
    cand = [os.environ.get('CLAUDE_PLUGIN_ROOT')]
    s = Path.home() / '.claude' / 'settings.json'
    if s.exists():
        try:
            # utf-8-sig, not utf-8: PowerShell's > and Out-File write a UTF-8 BOM
            # by default, and json.loads rejects it. Reading this file as plain
            # utf-8 made THIS skill print 'cannot locate the cc-memory package
            # tree' on a healthy marketplace install while /ccm-load reported
            # ACTIVATED on the SAME machine - only the encoding differed.
            # ui/installer.py:_read_settings and skills/ccm-load/SKILL.md both
            # already use utf-8-sig, with the same comment.
            mk = json.loads(s.read_text(encoding='utf-8-sig')).get('extraKnownMarketplaces', {}).get('cc-memory') or {}
            cand.append((mk.get('source') or {}).get('path'))
        except (json.JSONDecodeError, OSError):
            # why: a malformed settings.json must not abort resolution -- the
            # standalone candidate appended below can still succeed
            cand.append(None)
    ip = Path.home() / '.claude' / 'plugins' / 'installed_plugins.json'
    if ip.exists():
        try:
            # DELIBERATE MIRROR of cli/mem.py's marketplace-cache probe:
            # installed_plugins.json[plugins][cc-memory@cc-memory][*]
            # .installPath. Without this rung the skill was dead on a
            # cache-only install (plugin installed from a remote marketplace,
            # no dev-checkout path in extraKnownMarketplaces) -- the same
            # class of gap as the v2.4.3 hardcoded legacy path.
            _plugs = (json.loads(ip.read_text(encoding='utf-8-sig')) or {}).get('plugins') or {}
            for _e in (_plugs.get('cc-memory@cc-memory') or []):
                if isinstance(_e, dict) and _e.get('installPath'):
                    cand.append(_e['installPath'])
        except (json.JSONDecodeError, OSError, AttributeError):
            # why: a corrupt plugin cache costs one candidate, not the run
            pass
    cand.append(str(Path.home() / '.claude' / 'hooks' / 'cc-memory'))
    for c in cand:
        d = _pkg_dir(c)
        if d:
            return d
    return None

PKG = _find_pkg_dir()
if PKG is None:
    print('[error] cannot locate the cc-memory package tree; run /ccm-load first.')
    sys.exit(0)
sys.path.insert(0, str(PKG.resolve()))

from core.db import MemoryDB
from llm.memory_writer import upsert_batch
# The state directory's NAME and its one-way migration live in core.layout
# (v2.13.0); this script used to spell the join itself and, after the rename
# to .ccm/, went on writing every memory into a memory/ database nothing else
# read. Ask the resolver, like every other surface. A package too old to have
# it is a mixed install (this skill is newer than the code), not a layout to
# guess at.
try:
    from core.layout import memory_dir as _state_dir
except ImportError:
    print('[error] core.layout is missing: the installed cc-memory package is older than this skill. Run /ccm-load to re-sync, then retry.')
    sys.exit(0)

# Anchor before MemoryDB touches the path: MemoryDB CREATES the file and its
# parent, so run from a subdirectory this planted <subdir>/.ccm/memory.db --
# the exact stray the hooks have refused to create since v2.6.0, and because
# an existing database is a terminal rung, planting one there pinned all six
# hooks to it permanently. Falls back to cwd if the resolver is missing, which
# is only possible on installs predating it.
try:
    from core.roots import project_root
    project = str(Path(project_root(str(Path('.').resolve()))).resolve())
except Exception as _anchor_err:
    print(f'[warn] root anchoring unavailable ({_anchor_err}); using cwd')
    project = str(Path('.').resolve())

# Opt-out. This skill WRITES conversation memories, so a project the user
# opted out of must get nothing at all — the setting promises memories are
# 'neither readable nor writable through any cc-memory tool', and a skill is
# a tool. Checked on the pre-anchor cwd for the same reason every other
# surface does: anchoring is the step that can move up to an unexcluded parent.
try:
    from core.modes import cli_opt_out_notice
    _refusal = cli_opt_out_notice(str(Path('.').resolve()))
    if _refusal:
        print(f'[cc-memory] {_refusal}')
        sys.exit(0)
except ImportError:
    print('[warn] opt-out check unavailable; hooks still enforce it on writes')
mem_dir = _state_dir(project)
db = MemoryDB(mem_dir / 'memory.db')
pid = db.upsert_project(project)

memories = [
    # {'category': 'decision', 'content': '...', 'importance': 4, 'topic': 'auth'},
    #
    # ADD MEMORIES HERE — see Step 2 for fields.
    #
    # NO BACKTICKS, NO DOLLAR SIGNS AND NO DOUBLE QUOTES IN THE TEXT YOU WRITE
    # HERE. This whole body is a shell DOUBLE-quoted string, so bash expands it
    # before python sees it: a backtick is command substitution, a dollar sign
    # is variable expansion, and a double quote ENDS THE STRING — after which
    # bash parses the rest as shell. Step 2 asks for concrete values such as
    # numbers, file paths and parameter names, which is exactly the prose an
    # LLM writes with markdown backticks. A memory recording a shell command
    # would RUN that command in the user's project; one recording an
    # environment variable assignment would expand the live value into a
    # string that then lands in .ccm/MEMORY.md; and one quoting a phrase
    # breaks the skill outright. Write command names and paths in plain text,
    # or single quotes. (This comment spells the characters out in words for
    # the same reason it is telling you not to type them — an earlier revision
    # quoted that Step 2 phrase literally and made this file a bash syntax
    # error, so the whole skill silently stopped running.)
    # skills/ccm-load/SKILL.md carries the same rule; unlike this one its body
    # is static, so this is the slot where the hazard is recurring rather
    # than historical. Enforced by test_surfaces §7 over BOTH skills.
]

counts = upsert_batch(db, pid, None, memories, memory_dir=mem_dir)
print(f\"inserted={counts.get('inserted',0)} \"
      f\"merged={counts.get('merged',0)} \"
      f\"superseded={counts.get('superseded',0)} \"
      f\"skipped={counts.get('skipped',0)}\")
"
```

### Step 5 — Report results

Tell the user the breakdown:
- **Inserted**: brand-new facts
- **Merged**: refined an existing high-similarity memory in place
- **Superseded**: replaced an older version of the same fact (preserved as chain)
- **Skipped**: exact duplicates already present

The merged/superseded counts are *good*: they mean the writer is preventing the
patch-style stacking the v2.1 anti-patch contract was designed to stop. See
`docs/CONTRACTS.md#anti-patch-contract` for the full contract.

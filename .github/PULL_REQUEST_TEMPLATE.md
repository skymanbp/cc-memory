<!--
Thank you for contributing. The checklist below is not ceremony: every item on
it corresponds to a defect this project has actually shipped.
-->

## What this changes

<!-- One paragraph. What was wrong, and what the change does about it. -->

## Root cause

<!--
Not the symptom — the mechanism. If the change fixes a defect, say which line
produced it and why. "Added a guard" is a description of the patch, not of the
cause.
-->

## Evidence

<!--
Command + real output. A claim of "fixed" with no reproduction is not
reviewable. If the fix has a gate, show the gate failing BEFORE and passing
AFTER — `python tools/falsify_fixes.py --case <name>` is the mechanism for
proving a check is not vacuous.
-->

```
$ python tests/run_gates.py
```

## Checklist

- [ ] `python tests/run_gates.py` is green (all 11 gates — not a `--fast` run)
- [ ] No new pip dependency at runtime; standard library only
- [ ] Any new hook code cannot raise, cannot write to stderr, and exits 0
- [ ] Any new SQL is parameterised and scoped by `project_id`
- [ ] Any new save path routes through `llm.memory_writer`, not `db.insert_memory`
- [ ] Any new render path escapes authority markers (`core.privacy.neutralize_*`)
- [ ] Any new runtime module is registered in **all three** manifests:
      `ui/installer.py` `SUBPACKAGE_FILES`, `scripts/build_exe.py`, and
      `cli/mem.py` `_REQUIRED_PLUGIN_FILES`
- [ ] Any new config key has a reader, and cites it in `config.json`'s `notes`
- [ ] Docs co-updated: a counted claim carries its `<!--ce:*-->` binding, and
      new `file:line` citations pass `python tools/citation_check.py`
- [ ] If an English doc changed, its `.zh.md` sibling was refreshed and
      re-markered (`python tools/i18n_check.py --emit-marker <doc>`; an
      English-only change carries `--translation-unchanged "<why>"`)

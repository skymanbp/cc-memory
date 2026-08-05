"""
cc-memory — Claude Code persistent memory plugin.

v2.4.2: Bounded transcript reads. An unbounded full-file load in the PreCompact
      hook killed compaction on long-lived projects — a 2.1 GiB transcript
      parses at ~25 MiB/s (~88s) against a 120s budget, so the hook was
      terminated mid-write and that session's memories were lost.
      `extractor.load_transcript_window` now reads a bounded head+tail slice
      (measured 88s -> 1.7s; full hook 14.3s). The LLM summary also filled its
      budget from the OLDEST record, pinning extraction to a session's opening
      minutes; it now fills from the newest backwards. Adds a
      `.pre_compact_attempt.json` start marker so a killed hook is no longer
      invisible, `trigger` in `.last_save.json` so AUTO compactions are
      distinguishable, and a migrating `memory/.gitignore` so generated state
      (including verbatim plan prose) stops leaking into user repos.

v2.4.1: Carryover auto-carry matches bare titles too — a step whose title was
      unchanged but whose `notes` grew long fell below the trigram-Jaccard
      threshold, so the v2.4.0 gate refused a legitimate in-place plan update.

v2.4.0: Mandatory plan carryover gate. `plan_active` is a single-row slot, so
      every `plan-set --from-refiner` replaced the current plan wholesale and
      unfinished steps vanished with no accounting. Replacement now requires
      every unfinished step to be auto-carried or explicitly dispositioned,
      `plan-clear` refuses without `--reason`, and every outgoing plan is
      archived to `memory/.plan_history/`. No force flag, by design.

v2.3.4: Anthropic auth fall-through (env key -> OAuth token, correct wire
      format per credential) so a dead env key no longer blackholes a healthy
      subscription. Local Ollama fallback became opt-in (`ccl.enabled`).

v2.3.3: Documentation multilingual version-control. English is the canonical
      skeleton; Chinese docs are drift-tracked `*.zh.md` siblings (README.zh.md
      first), each tied to a normalized-sha256 of its English source. A pure-
      stdlib checker (tools/i18n_check.py) + a tests/smoke_test.py gate turn red
      the moment an English doc changes without its translation being refreshed.
      Memory content stays language-agnostic (docs/I18N.md). Docs + version-
      metadata only — no runtime behavior changed.

v2.3.2: Consolidation moved OFF the blocking compaction path into a sibling
      `async` PreCompact hook (hooks/consolidate_async.py). Permanently fixes
      the intermittent "Compacted PreCompact ... failed: Hook cancelled": the
      sync leg now only does fast extraction + PROGRESS.md, while variable-
      latency LLM consolidation runs in the background under a time-budget it
      can never overrun (honest per-call cost model + bounded Ollama fallback).

v2.3: LLM-judged SEMANTIC de-duplication (same fact reworded -> merged) +
      obsolescence detection (newer fact contradicts older) + reference-aware
      staleness net — fixes unbounded memory accumulation. Per-session
      PROGRESS.md annotation. Injection observability (.last_inject.json +
      /cc-mem inject-show / inject-usage). /cc-mem encoding-check.

v2.2: Live PLAN.md anchor + plan-refiner / plan-guardian subagents.
      TodoWrite step-sync + drift counters with guardian nudge thresholds.
      /cc-mem dashboard subcommand (auto-resolves plugin path).
      Skill consolidation: mem-init / mem-status retired in favour of
      /ccm-load + /cc-mem status.

v2.1: Reorganized into core/hooks/llm/ui/cli/mcp subpackages.
      Unified memory_writer (anti-patch reconcile-on-write).
      PROGRESS.md forced-handoff replaces SESSION_HANDOFF.md.
      MEMORY.md auto-regenerates on every write.
"""
__version__ = "2.4.2"

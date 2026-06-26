"""
cc-memory — Claude Code persistent memory plugin.

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
__version__ = "2.3.0"

"""
cc-memory — Claude Code persistent memory plugin.

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
__version__ = "2.2.0"

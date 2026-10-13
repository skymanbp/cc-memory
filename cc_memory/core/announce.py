"""What cc-memory just did, shown to the USER — one short line per act (v2.17.0).

The user could not see the plugin work. Every hook's output went either to
the MODEL (SessionStart and UserPromptSubmit stdout is injected as context)
or to nobody (every other event's stdout lands in Claude Code's debug log),
so whether a session's memories were loaded, a turn's facts were saved or a
backlog was consolidated could only be learned by asking `/cc-mem`.

The one channel shown to the user and NOT sent to the model is the hook
JSON field `systemMessage`. Verified on Claude Code 2.1.289 (2026-10-13): the
transcript records it as a `hook_system_message` attachment with no
`rendered` payload, while `additionalContext` is rendered into a system
reminder for the model; `claude -p --output-format stream-json` prints it as
an `informational` message for UserPromptSubmit, PostToolUse and Stop. Per
event (code.claude.com/docs/en/hooks):

  SessionStart, UserPromptSubmit, PostToolUse, Stop   shown to the user.
  PreCompact     the field is DISCARDED. The compaction's save is announced
                 by the SessionStart(compact) that follows it, read from
                 `.last_save.json`, which PreCompact already writes.
  async hooks    the field is delivered to CLAUDE on the next turn and not
                 shown to the user, so the async consolidation leg prints
                 nothing; its run is announced by the next Stop or
                 SessionStart, read from `.last_consolidation.json`.
Detached workers (the Stop observer, the retroactive save) have no hook
output at all: each records its outcome in a status file
(`OBSERVE_STATUS`, `RETRO_STATUS`) and the next Stop announces it.
`ANNOUNCED_FILE` remembers which status was already announced, so each act
is shown once.

ONE format: `ccm ▸ <verb>(<english verb>) <counts> · <where>`, at most
`MAX_COLUMNS` display columns (a CJK character is two), one line per act.
ONE switch, the per-project `projects.announce` column (`/cc-mem announce`,
the same shape as `/cc-mem mode`):

  full   every line (the default)
  batch  the batch acts only — no per-prompt recall line
  off    no `systemMessage` at all: the hook's stdout is byte-for-byte what
         it printed before this module existed

What this module never does: write anything the model reads. Announcements
are user-facing text, so they are spelled here and not in `core/prompts.py`
(that module is the Claude-visible text); a hook's context, when it has one,
passes through `HookOutput` unchanged — plain stdout when nothing is
announced, `hookSpecificOutput.additionalContext` beside a `systemMessage`
otherwise, which Claude Code adds to the context exactly as it adds plain
stdout (the same 10 000-character cap applies to both).
"""
import json
import sys
import unicodedata
from datetime import datetime

LEVELS = ("full", "batch", "off")
DEFAULT_LEVEL = "full"
PREFIX = "ccm ▸ "
MAX_COLUMNS = 100
# Claude Code's cap on a single hook string (plain stdout or a JSON field);
# over it the text is saved to a file and the model sees a 2 000-char preview.
CONTEXT_CAP_CHARS = 10_000
# The day log (`core.logger`), named rather than spelled as a path: the
# directory is ~/.claude/hooks/cc-memory/logs on every install layout
# (README, Configuration), and a path here would eat a third of the line.
LOG_HINT = "详见 cc-memory 日志"

ANNOUNCED_FILE = ".announced.json"
OBSERVE_STATUS = ".last_observe.json"
RETRO_STATUS = ".last_retro.json"
SAVE_STATUS = ".last_save.json"                  # written by hooks/pre_compact.py
CONSOLIDATION_STATUS = ".last_consolidation.json"  # core.consolidate.write_consolidation_marker


# ── the switch ─────────────────────────────────────────────────────────────

def normalize_level(value):
    """A stored or typed level, or the default for anything else."""
    text = str(value or "").strip().lower()
    return text if text in LEVELS else DEFAULT_LEVEL


def level_for(db, project_id):
    """The project's announce level. Never raises."""
    try:
        return normalize_level(db.get_project_announce(project_id))
    except Exception:
        # why: the switch is a preference; an unreadable one costs the
        # preference, never the hook — and the default is what ships
        return DEFAULT_LEVEL


# ── one format ─────────────────────────────────────────────────────────────

def _char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text):
    """Terminal columns `text` occupies (a CJK character is two)."""
    return sum(_char_width(ch) for ch in str(text))


def fit(text, cols=MAX_COLUMNS):
    """One line of at most `cols` display columns: whitespace (newlines
    included) collapsed, an over-long line cut with an ellipsis."""
    text = " ".join(str(text).split())
    if display_width(text) <= cols:
        return text
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > cols - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def line(verb, counts, where=""):
    """`ccm ▸ <verb> <counts> · <where>`, fitted to MAX_COLUMNS."""
    body = f"{verb} {counts}".strip()
    if where:
        body += f" · {where}"
    return fit(PREFIX + body)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _kchars(n):
    return f"{_int(n) / 1000:.1f} K 字"


def _clock(ts):
    """`MM-DD HH:MM` from an ISO-ish timestamp, or "" when unparseable."""
    text = str(ts or "").replace("T", " ")
    try:
        return datetime.strptime(text[:16], "%Y-%m-%d %H:%M").strftime("%m-%d %H:%M")
    except ValueError:
        return ""


# ── lines, one builder per act ─────────────────────────────────────────────

def inject_line(manifest):
    """SessionStart, a full or compact start: what the injection carried,
    from the manifest `build_context` just wrote (`.last_inject.json`)."""
    if not isinstance(manifest, dict):
        return ""
    layers = []
    for label, key in (("主题", "topic_names"), ("关键", "critical_ids"),
                       ("时间线", "timeline_ids")):
        value = manifest.get(key)
        n = len(value) if isinstance(value, list) else 0
        if n:
            layers.append(f"{label} {n}")
    counts = f"{_int(manifest.get('n_injected_memories'))} 条记忆"
    if layers:
        counts += f" ({' / '.join(layers)})"
    nd = _int(manifest.get("n_injected_directives"))
    if nd:
        counts += f" + {nd} 条指令"
    layer = manifest.get("progress_layer")
    if layer == "digest":
        counts += " + PROGRESS 摘要"
    elif layer == "file":
        counts += " + PROGRESS 预览"
    return line("注入(inject)", counts, _kchars(manifest.get("total_chars")))


def trim_line(manifest):
    """SessionStart: what `session_start._fit_to_ceiling` cut to keep the
    injection under Claude Code's cap, from the manifest's `trimmed` record."""
    trimmed = manifest.get("trimmed") if isinstance(manifest, dict) else None
    if not isinstance(trimmed, dict):
        return ""
    parts = []
    for label, key in (("条记忆", "memory_ids"), ("个主题", "topic_names")):
        value = trimmed.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{len(value)} {label}")
    if not parts:
        return ""
    return line("裁剪(trim)", f"已裁 {' / '.join(parts)}以合 Claude Code 10 K 上限",
                "/cc-mem inject-show 列出被裁的")


def oversize_line(n_chars):
    """SessionStart: the context is past Claude Code's per-field cap, so the
    model receives a file path and a 2 000-character preview, not the text."""
    if _int(n_chars) <= CONTEXT_CAP_CHARS:
        return ""
    return line("警告(warn)", f"注入 {_kchars(n_chars)}超过 Claude Code 单段 10 K 上限",
                "Claude 只收到前 2 K 字预览")


def resume_line(n_directives):
    return line("续接(resume)", f"重申 {_int(n_directives)} 条指令",
                "启动时的注入仍在对话中")


def save_line(status):
    """The PreCompact sync leg's result, from `.last_save.json`."""
    if not isinstance(status, dict):
        return ""
    when = _clock(status.get("timestamp"))
    if not status.get("success"):
        return line("失败(error)", f"压缩前存档未完成 {when}".strip(), LOG_HINT)
    counts = (f"压缩前 +{_int(status.get('n_inserted'))} 新 / "
              f"{_int(status.get('n_merged'))} 合并 / "
              f"{_int(status.get('n_superseded'))} 取代 / "
              f"{_int(status.get('n_reinforced'))} 加强")
    return line("存档(save)", counts, f"PROGRESS.md 已重写 {when}".strip())


def consolidation_line(marker):
    """A finished consolidation run, from `.last_consolidation.json`."""
    if not isinstance(marker, dict):
        return ""
    dedup = (_int(marker.get("duplicates_archived"))
             + _int(marker.get("semantic_dedup_archived")))
    stale = (_int(marker.get("archived_obsolete"))
             + _int(marker.get("archived_stale")))
    counts = (f"去重 {dedup} · 过时 {stale} · "
              f"主题摘要 {_int(marker.get('topics_consolidated'))}")
    where = f"现存 {_int(marker.get('final_active'))} 条 {_clock(marker.get('ts'))}"
    return line("整理(consolidate)", counts, where.strip())


def kick_line():
    return line("整理(consolidate)", "写入积压已到阈值, 已在后台启动",
                "结果下一轮显示")


def observe_line(status):
    """The detached Stop observer's outcome, from `OBSERVE_STATUS`."""
    if not isinstance(status, dict):
        return ""
    if not status.get("ok"):
        reason = fit(str(status.get("error") or "未知原因"), 50)
        return line("失败(error)", f"后台提炼: {reason}", LOG_HINT)
    counts = status.get("counts") if isinstance(status.get("counts"), dict) else {}
    updated = sum(_int(counts.get(k)) for k in ("merged", "superseded", "reinforced"))
    return line("提炼(extract)",
                f"从 {_int(status.get('n_fed'))} 条观察得 "
                f"{_int(counts.get('inserted'))} 条新记忆, 更新 {updated} 条",
                "Haiku 后台")


def retro_line(status):
    """The detached retroactive save's outcome, from `RETRO_STATUS`."""
    if not isinstance(status, dict):
        return ""
    if not _int(status.get("sessions")):
        error = str(status.get("error") or "")
        return (line("失败(error)", f"补存旧会话: {fit(error, 50)}", LOG_HINT)
                if error else "")
    counts = status.get("counts") if isinstance(status.get("counts"), dict) else {}
    updated = sum(_int(counts.get(k)) for k in ("merged", "superseded", "reinforced"))
    return line("补存(retro-save)",
                f"{_int(status.get('sessions'))} 个未压缩的旧会话: "
                f"+{_int(counts.get('inserted'))} 新 / {updated} 更新")


def idle_line(results):
    """The Stop hook's idle reorg, when it changed something."""
    if not isinstance(results, dict):
        return ""
    g = _int(results.get("garbage"))
    t = _int(results.get("topics_assigned"))
    s = _int(results.get("stale_claims"))
    if not (g or t or s):
        return ""
    return line("闲时整理(idle)", f"归档垃圾 {g} · 归入主题 {t} · 清理残留 {s}",
                "MEMORY.md 已重生成")


def recall_line(ids):
    ids = [_int(i) for i in ids or []]
    if not ids:
        return ""
    return line("召回(recall)", f"{len(ids)} 条记忆 " + " ".join(f"#{i}" for i in ids),
                "按本句提问")


def init_line():
    return line("建库(init)", ".ccm/memory.db", "本项目第一次使用 cc-memory")


def plan_capture_line(n_chars):
    return line("计划捕获(plan)", f"{_int(n_chars)} 字的计划 → .ccm/PLAN.md",
                "待 plan-refiner 精炼")


def plan_sync_line(info):
    """A TodoWrite snapshot synced into the live plan (`apply_todowrite_sync`)."""
    if not isinstance(info, dict) or info.get("skipped"):
        return ""
    matched = _int(info.get("n_matched"))
    unmatched = _int(info.get("n_unmatched"))
    if not (matched or unmatched):
        return ""
    counts = f"{matched} 项待办对上步骤"
    if unmatched:
        counts += f", {unmatched} 项未对上"
    active = _int(info.get("active_step_id"))
    return line("计划同步(plan)", counts,
                f"第 {active} 步进行中" if active else "PLAN.md 已更新")


def plan_part(plan_row):
    """`计划: 第 k 步进行中 (m/n)` for a live structured plan, else ""."""
    if not isinstance(plan_row, dict):
        return ""
    structured = plan_row.get("structured")
    steps = structured.get("steps") if isinstance(structured, dict) else None
    steps = [s for s in steps or [] if isinstance(s, dict)]
    if not steps:
        return ""
    done = sum(1 for s in steps if s.get("status") == "done")
    active = _int(plan_row.get("active_step"))
    head = f"第 {active} 步进行中" if active else "无进行中步骤"
    return f"计划: {head} ({done}/{len(steps)})"


def with_part(text, part):
    """`text · part` when it still fits in one line, else `text` unchanged."""
    if not text or not part:
        return text
    joined = f"{text} · {part}"
    return joined if display_width(joined) <= MAX_COLUMNS else text


def failure_line(reason):
    return line("失败(error)", fit(reason, 60), LOG_HINT)


# ── status files: producers record, hooks announce once ────────────────────

def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_status(memory_dir, name, payload):
    """A worker's outcome, for the next Stop to announce. Never raises."""
    try:
        from core.atomic import write_atomic
        body = dict(payload)
        body["ts"] = datetime.now().isoformat(timespec="microseconds")
        write_atomic(memory_dir / name, json.dumps(body, ensure_ascii=False))
    except Exception:
        # why: the file only feeds an announcement, so a worker whose memories already landed must not fail over it
        pass


# (key in ANNOUNCED_FILE, status file, its timestamp key)
STATUS_SOURCES = {
    "save": (SAVE_STATUS, "timestamp"),
    "consolidation": (CONSOLIDATION_STATUS, "ts"),
    "observe": (OBSERVE_STATUS, "ts"),
    "retro": (RETRO_STATUS, "ts"),
}


def take_unannounced(memory_dir, keys):
    """The status records among `keys` not announced yet, as (key, record),
    and mark them announced. Never raises; an unreadable watermark file
    announces again rather than never."""
    try:
        announced = _read_json(memory_dir / ANNOUNCED_FILE) or {}
        fresh = []
        for key in keys:
            name, ts_key = STATUS_SOURCES[key]
            record = _read_json(memory_dir / name)
            ts = str(record.get(ts_key) or "") if record else ""
            if ts and announced.get(key) != ts:
                fresh.append((key, record))
                announced[key] = ts
        if fresh:
            from core.atomic import write_atomic
            write_atomic(memory_dir / ANNOUNCED_FILE,
                         json.dumps(announced, ensure_ascii=False))
        return fresh
    except Exception:
        # why: announcements are a view of acts already done; a failure here
        # costs the lines, never the hook that is reporting
        return []


STATUS_LINE = {
    "save": save_line,
    "consolidation": consolidation_line,
    "observe": observe_line,
    "retro": retro_line,
}


# ── the emitter ────────────────────────────────────────────────────────────

class HookOutput:
    """Everything a single hook run writes to stdout.

    `context(text)` replaces `print(text)` for model-visible text;
    `announce(line, batch=...)` queues a user-visible line; `document` is a
    JSON object the hook must emit anyway (Stop's refusal). `write()` emits
    ONE of three shapes: no line -> the context as plain text, or the
    document — byte-identical to the pre-v2.17.0 output; lines -> one JSON
    object carrying `systemMessage`, the context as
    `hookSpecificOutput.additionalContext`, and the document's fields
    merged in. Logged failures (`core.logger.recorded_errors`) become one
    `失败` line, unless the level is `off`.

    The level is resolved LAZILY: a hook that knows it sets `level`; one
    that would have to open the database for it sets `level_fn`, which runs
    only when there is a line to filter — most prompts and tool calls
    announce nothing, and they must not pay for a switch they do not use.
    """

    def __init__(self, event, level=None):
        self.event = event
        self._level = None if level is None else normalize_level(level)
        self.level_fn = None
        self.document = None
        self._context = []
        self._lines = []
        self._written = False

    @property
    def level(self):
        if self._level is None:
            value = DEFAULT_LEVEL
            if self.level_fn is not None:
                try:
                    value = self.level_fn()
                except Exception:
                    # why: an unreadable switch costs the preference, never
                    # the hook — the shipped default applies
                    value = DEFAULT_LEVEL
            self._level = normalize_level(value)
        return self._level

    @level.setter
    def level(self, value):
        self._level = normalize_level(value)

    def context(self, text="", end="\n"):
        """Queue model-visible text, exactly as `print(text, end=end)`."""
        self._context.append(f"{text}{end}")

    def context_chars(self):
        """Length of the model-visible text queued so far."""
        return sum(len(t) for t in self._context)

    def announce(self, text, batch=True):
        """Queue `text`; `batch=False` marks a per-prompt line that only the
        `full` level shows."""
        if text:
            self._lines.append((text, batch))

    def append_to_last(self, part):
        """Add `part` to the last queued line when it still fits (`with_part`)."""
        if self._lines and part:
            text, batch = self._lines[-1]
            self._lines[-1] = (with_part(text, part), batch)

    def lines(self):
        try:
            from core.logger import recorded_errors
            errors = recorded_errors()
        except Exception:
            # why: a missing recorder costs the failure line, nothing else
            errors = []
        if not self._lines and not errors:
            return []
        level = self.level
        if level == "off":
            return []
        out = [text for text, batch in self._lines if batch or level == "full"]
        if errors:
            reason = errors[0][1]
            if len(errors) > 1:
                reason += f" (另 {len(errors) - 1} 处)"
            out.append(failure_line(f"{errors[0][0].lower()}: {reason}"))
        return out

    def render(self):
        ctx = "".join(self._context)
        lines = self.lines()
        if not lines:
            if self.document is not None:
                return json.dumps(self.document, ensure_ascii=False) + "\n"
            return ctx
        out = dict(self.document or {})
        out["systemMessage"] = "\n".join(lines)
        if ctx:
            out["hookSpecificOutput"] = {"hookEventName": self.event,
                                         "additionalContext": ctx}
        return json.dumps(out, ensure_ascii=False) + "\n"

    def write(self):
        """Emit and flush, once: a hook's last-resort handler calls this
        again so a context already built is never lost to a later failure,
        and the second call is a no-op. Never raises."""
        if self._written:
            return
        self._written = True
        try:
            text = self.render()
        except Exception:
            # why: rendering is our own code over our own strings; if it ever
            # fails, the model's context must still go out exactly as before
            text = "".join(self._context)
        try:
            if text:
                sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError):
            # why: a closed or detached pipe — nothing left to deliver to
            pass

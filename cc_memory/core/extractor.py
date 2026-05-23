"""
Parse Claude Code transcript JSONL files and extract structured information.

Strategy: prefer structured tool-use data over free-text heuristics.
  1. TodoWrite inputs        TodoWrite-only path: perfect task lists (structured JSON)
  2. Edit / Write tool uses  what files changed this session
  3. Bash tool uses          commands run, potential errors
  4. Assistant text          decisions, results, architecture notes
  5. Keyword frequency       project vocabulary (grows over sessions)

Memory categories:
  decision    explicit choices / confirmations / changes
  result      measured outcomes (any quantitative comparison)
  config      UPPER_CASE constant assignments, hyperparameters
  bug         identified and fixed problems
  task        pending / completed work items
  arch        structural facts (module names, pipelines, data flow)
  note        everything else above noise threshold

This module is intentionally project-agnostic. The previous version
hard-coded an astrophysics/ML vocabulary; that was a contamination of
a generic plugin and has been removed in v2.1.
"""
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


# ── Generic category detection (project-neutral) ───────────────────────────
_PATTERNS: Dict[str, List[str]] = {
    "decision": [
        r"决定|决策|最终|选择了|改为|确认|已实现|IMPLEMENTED|已完成",
        r"\b(decided|chose|confirmed|switched to|changed to|finalized|resolved)\b",
        r"→\s*\S",
        r"✅|✓",
    ],
    "result": [
        # Generic metric pattern: name = number, name: number, with optional units/percent
        r"\b[a-zA-Z_][a-zA-Z0-9_]{1,30}\s*[=:]\s*-?\d+\.?\d*[eE+\-\d]*%?",
        r"\d+(?:\.\d+)?%",
        r"\b(best|baseline|improved|degraded|outperforms?|regressed|faster|slower)\b",
        r"实验结果|测试结果|benchmark",
    ],
    "config": [
        r"\b[A-Z][A-Z0-9_]{3,}\s*=\s*[\d\"'\[]",
        r"\b(lr|learning_rate|batch_size|epochs?|threshold|timeout|port|host|"
        r"max_\w+|min_\w+|n_\w+|num_\w+)\s*[=:]\s*\S",
        r"config\.(py|json|yaml|toml)\b",
        r"hyperparameter|超参数",
    ],
    "bug": [
        r"FIXED|BUG|修复|fix|bug\b|错误|已修复|root cause|caused by|was causing",
        r"FIXED\s*:",
    ],
    "task": [
        r"PENDING|TODO|FIXME|待完成|下一步|需要|接下来|next step",
        r"\[ \]|\[x\]|\[X\]",
    ],
    "arch": [
        r"\b(module|class|function|API|endpoint|interface|service|component)\b",
        r"\b(pipeline|workflow|backbone|frontend|backend|middleware|adapter)\b",
        r"架构|模型结构|input.*shape|output.*shape",
    ],
}

_IMPORTANCE_BOOST: Dict[str, int] = {
    "CRITICAL": 2, "关键": 2, "NEVER": 2, "⛔": 2,
    "IMPORTANT": 1, "重要": 1, "WARNING": 1, "⚠": 1,
    "FIXED": 1, "BUG": 1, "IMPLEMENTED": 1,
}

_STOP_ACRONYMS = {
    "THE", "AND", "FOR", "BUT", "NOT", "ARE", "WAS", "HAS", "HAD",
    "USE", "CAN", "ALL", "ITS", "NEW", "OLD", "ONE", "TWO", "YES",
    "NO", "OK", "BY", "IN", "ON", "AT", "TO", "OF", "OR",
}


# ── Transcript loading ──────────────────────────────────────────────────────
def load_transcript(transcript_path: str) -> List[Dict]:
    messages: List[Dict] = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    # why: a single malformed line shouldn't drop the whole transcript;
                    # JSONL is line-delimited so we recover on the next line
                    continue
    except (FileNotFoundError, PermissionError):
        # why: missing / unreadable transcript is a normal early-call state
        # (file may not exist yet for a freshly compacted session). Return empty.
        return []
    return messages


def _text_from_content(content: Any) -> str:
    from core.privacy import clean_for_storage
    if isinstance(content, str):
        return clean_for_storage(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                inner = block.get("content", "")
                parts.append(_text_from_content(inner))
        text = "\n".join(p for p in parts if p)
        return clean_for_storage(text)
    return ""


def _iter_tool_uses(messages: List[Dict]):
    for msg in messages:
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name", ""), block.get("input", {})


def _iter_assistant_texts(messages: List[Dict]) -> List[str]:
    texts = []
    for msg in messages:
        if msg.get("message", {}).get("role") != "assistant":
            continue
        text = _text_from_content(msg.get("message", {}).get("content", ""))
        if text.strip():
            texts.append(text)
    return texts


def extract_todos(messages: List[Dict]) -> List[Dict]:
    """Aggregate ALL TodoWrite invocations across the transcript.

    Useful for archive rendering (full task history). For the CURRENT state
    of the todo list (what's still pending), call extract_latest_todo_state
    below instead — that one is last-wins and is what PROGRESS.md needs.
    """
    todos: List[Dict] = []
    for name, inp in _iter_tool_uses(messages):
        if name != "TodoWrite":
            continue
        raw_todos = inp.get("todos", [])
        if isinstance(raw_todos, str):
            raw_todos = [raw_todos] if raw_todos.strip() else []
        if not isinstance(raw_todos, list):
            continue
        for item in raw_todos:
            if isinstance(item, str):
                if len(item.strip()) > 3:
                    todos.append({"status": "pending", "priority": "medium", "content": item.strip()})
            elif isinstance(item, dict):
                content = item.get("content", "").strip()
                if len(content) > 3:
                    todos.append({
                        "status":   item.get("status", "pending"),
                        "priority": item.get("priority", "medium"),
                        "content":  content,
                    })
    return todos


def extract_latest_todo_state(messages: List[Dict]) -> List[Dict]:
    """Return ONLY the last TodoWrite snapshot — the live todo state.

    extract_todos() above aggregates across ALL TodoWrite invocations, which
    stacks the same task as it moves pending → in_progress → completed. For
    PROGRESS.md and resume-protocol handoff we want the FINAL state of the
    list — whatever the user/Claude has at the moment of compaction.

    Last-writer-wins semantics: an explicit empty TodoWrite (user cleared
    the list) is honored and returns []. Returns [] if no TodoWrite ever ran.
    """
    latest: List[Dict] = []
    found_any = False
    for name, inp in _iter_tool_uses(messages):
        if name != "TodoWrite":
            continue
        raw_todos = inp.get("todos", [])
        if not isinstance(raw_todos, list):
            continue
        found_any = True
        snapshot: List[Dict] = []
        for item in raw_todos:
            if isinstance(item, dict):
                content = (item.get("content") or "").strip()
                if len(content) > 3:
                    snapshot.append({
                        "status":   item.get("status", "pending"),
                        "priority": item.get("priority", "medium"),
                        "content":  content,
                    })
            elif isinstance(item, str) and len(item.strip()) > 3:
                snapshot.append({"status": "pending", "priority": "medium",
                                 "content": item.strip()})
        # Last-writer-wins: assign every iteration (including empty snapshots)
        latest = snapshot
    return latest if found_any else []


def find_latest_transcript(cwd: str,
                           exclude_session_id: Optional[str] = None) -> Optional["Path"]:
    """Locate the newest .jsonl transcript for this project (by mtime).

    Resolves `~/.claude/projects/<dir-hash>/` using the same convention as
    session_start.py:_find_transcript_dir (Windows uses '-' in place of
    drive ':' and path separators).

    Pass `exclude_session_id` (the current Claude session UUID) to avoid
    picking the freshly-opened transcript for the current session — we want
    to mine the PREVIOUS session's history, not the empty one just starting.
    Returns None if no project transcript directory or no .jsonl exists.
    """
    from pathlib import Path
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    path_str = str(Path(cwd).resolve())
    hash_candidate = path_str.replace(":", "-").replace("\\", "-").replace("/", "-")
    transcript_dir = claude_projects / hash_candidate
    if not transcript_dir.exists():
        hash_lower = hash_candidate.lower()
        transcript_dir = None
        for d in claude_projects.iterdir():
            if d.is_dir() and d.name.lower() == hash_lower:
                transcript_dir = d
                break
        if transcript_dir is None:
            return None
    jsonls = sorted(transcript_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime, reverse=True)
    for jsonl in jsonls:
        if exclude_session_id and jsonl.stem == exclude_session_id:
            continue
        return jsonl
    return None


def extract_file_changes(messages: List[Dict]) -> List[str]:
    seen: List[str] = []
    seen_set: set = set()
    for name, inp in _iter_tool_uses(messages):
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = inp.get("file_path", inp.get("notebook_path", ""))
            if path and path not in seen_set:
                seen_set.add(path)
                seen.append(path)
    return seen


def extract_bash_commands(messages: List[Dict], max_len: int = 120) -> List[str]:
    skip_prefixes = ("ls ", "echo ", "cat ", "pwd", "cd ", "which ", "where ")
    cmds: List[str] = []
    for name, inp in _iter_tool_uses(messages):
        if name != "Bash":
            continue
        cmd = inp.get("command", "").strip()
        if not cmd:
            continue
        if any(cmd.startswith(p) for p in skip_prefixes):
            continue
        cmds.append(cmd[:max_len] + ("…" if len(cmd) > max_len else ""))
    return cmds


def _score_sentence(sentence: str) -> Tuple[int, str]:
    importance = 2
    for kw, boost in _IMPORTANCE_BOOST.items():
        if kw in sentence:
            importance += boost
    for category, patterns in _PATTERNS.items():
        for pat in patterns:
            if re.search(pat, sentence, re.IGNORECASE):
                return min(importance, 5), category
    return min(importance, 5), "note"


def extract_key_sentences(text: str, project_keywords: Optional[List[str]] = None,
                          max_results: int = 60) -> List[Tuple[str, int, str]]:
    kw_set = {kw.lower() for kw in (project_keywords or [])}
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n{2,}|\n(?=[A-Z•\-])", text)
    results: List[Tuple[str, int, str]] = []
    seen_content: set = set()
    for raw in sentences:
        s = raw.strip()
        if len(s) < 20 or len(s) > 600:
            continue
        alpha_ratio = sum(1 for c in s if c.isalnum()) / max(len(s), 1)
        if alpha_ratio < 0.3:
            continue
        norm = s.lower().strip()
        if norm in seen_content:
            continue
        seen_content.add(norm)
        importance, category = _score_sentence(s)
        hits = sum(1 for kw in kw_set if kw in s.lower())
        if hits >= 2:
            importance = min(importance + 1, 5)
        if importance >= 2:
            results.append((s, importance, category))
    results.sort(key=lambda x: -x[1])
    return results[:max_results]


def extract_metrics(text: str) -> List[str]:
    """Extract generic name=value pairs that look like measured metrics.

    v2.1 change: no domain-specific allow-list. Anything matching the shape
    name=number is captured; downstream LLM extraction filters relevance.
    """
    found: List[str] = []
    seen: set = set()
    for m in re.finditer(
        r"\b([a-zA-Z_][a-zA-Z0-9_]{1,30})\s*[=:]\s*"
        r"(-?\d+\.?\d*(?:[eE][+-]?\d+)?%?)\b",
        text
    ):
        name, val = m.group(1), m.group(2)
        # Skip noise: identifiers commonly assigned to non-metrics
        if name.lower() in ("i", "j", "k", "n", "x", "y", "z", "id", "idx", "tmp"):
            continue
        entry = f"{name}={val}"
        if entry not in seen:
            seen.add(entry)
            found.append(entry)
    return found


def detect_keywords(text: str) -> Dict[str, int]:
    counter: Counter = Counter()
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{2,})\b", text):
        kw = m.group(1)
        if kw not in _STOP_ACRONYMS:
            counter[kw] += 1
    for m in re.finditer(r"\b([a-z][a-z0-9]{2,}(?:_[a-z0-9]+)+)\b", text):
        counter[m.group(1)] += 1
    for m in re.finditer(r"\b(\w+\.py)\b", text):
        counter[m.group(1)] += 1
    return {k: v for k, v in counter.items() if v >= 2}


def build_extraction(messages: List[Dict],
                     project_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    all_texts: List[str] = []
    for msg in messages:
        t = _text_from_content(msg.get("message", {}).get("content", ""))
        if t:
            all_texts.append(t)
    full_text = "\n".join(all_texts)

    assistant_texts = _iter_assistant_texts(messages)
    sentences = extract_key_sentences(full_text, project_keywords)
    metrics = extract_metrics(full_text)
    keywords = detect_keywords(full_text)

    return {
        "todos":           extract_todos(messages),
        # latest_todos = the CURRENT state of the todo list (last TodoWrite call);
        # use this for PROGRESS.md.open_todos. extract_todos above is stacking
        # history — only use it for archive rendering.
        "latest_todos":    extract_latest_todo_state(messages),
        "file_changes":    extract_file_changes(messages),
        "bash_commands":   extract_bash_commands(messages),
        "sentences":       sentences,
        "metrics":         metrics,
        "keywords":        keywords,
        "assistant_texts": assistant_texts,
        "msg_count":       len(messages),
    }


def group_sentences(sentences: List[Tuple[str, int, str]]) -> Dict[str, List[Tuple[str, int]]]:
    groups: Dict[str, List[Tuple[str, int]]] = {}
    for text, imp, cat in sentences:
        groups.setdefault(cat, []).append((text, imp))
    for cat in groups:
        groups[cat].sort(key=lambda x: -x[1])
    return groups


CATEGORY_ORDER = ["decision", "result", "arch", "config", "bug", "task", "note"]
CATEGORY_LABELS = {
    "decision": "Decisions Made",
    "result":   "Results",
    "arch":     "Architecture Notes",
    "config":   "Configuration Changes",
    "bug":      "Bugs Fixed",
    "task":     "Tasks & Pending",
    "note":     "Other Notes",
}

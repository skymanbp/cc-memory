"""
cc-memory/extractor.py
Parse Claude Code transcript JSONL files and extract structured information.

Strategy: prefer structured tool-use data over free-text heuristics.
  1. TodoWrite inputs        → perfect task lists (structured JSON)
  2. Edit / Write tool uses  → what files changed this session
  3. Bash tool uses          → commands run, potential errors
  4. Assistant text          → decisions, results, architecture notes
  5. Keyword frequency       → project vocabulary (grows over sessions)

Memory categories:
  decision    — explicit choices / confirmations / changes
  result      — numerical experiment metrics
  config      — UPPER_CASE constant assignments, hyperparameters
  bug         — identified and fixed problems
  task        — pending / completed work items
  arch        — model architecture / pipeline facts
  note        — everything else above noise threshold
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Category detection patterns  (applied to each sentence / snippet)
# ---------------------------------------------------------------------------
_PATTERNS: Dict[str, List[str]] = {
    "decision": [
        r"决定|决策|最终|选择了|改为|确认|已实现|IMPLEMENTED|已完成",
        r"\b(decided|chose|confirmed|switched to|changed to|finalized|resolved)\b",
        r"→\s*\S",          # "old → new" style
        r"✅|✓",
    ],
    "result": [
        r"\b(F1|AUC|acc(?:uracy)?|recall|precision|loss|IoU|bacc|MAE|MSE|RMSE"
        r"|balanced_acc|pos_recall|neg_recall|peak_recall)\s*[=:]\s*\d",
        r"\d+\.\d+%",
        r"实验结果|测试结果|LOCO.*=",
        r"\b(best|baseline|improved|degraded|outperforms?)\b",
    ],
    "config": [
        r"\b[A-Z][A-Z0-9_]{3,}\s*=\s*[\d\"'\[]",   # UPPER_CASE_VAR = value
        r"grand_config|config\.py|hyperparameter|超参数",
        r"\b(lr|learning_rate|batch_size|epochs?|threshold|weight|alpha|beta"
        r"|gamma|dropout|top_k)\s*[=:]\s*\d",
    ],
    "bug": [
        r"FIXED|BUG|修复|fix|bug\b|错误|已修复|root cause|caused by|was causing",
        r"FIXED\s*:",
    ],
    "task": [
        r"PENDING|TODO|待完成|下一步|需要|接下来|next step",
        r"\[ \]|\[x\]|\[X\]",
        r"Cell \d+.*(?:needs|requires|must)",
        r"cache regen|retrain|re-run",
    ],
    "arch": [
        r"\b(CNN|Swin|GNN|HOG|SBI|TDA|fusion|emulator|diffusion)\b",
        r"\b(channel|layer|module|head|encoder|decoder|backbone)\b",
        r"架构|模型结构|input.*shape|output.*shape",
        r"\b(equivariant|attention|transformer|graph)\b",
    ],
}

# Importance boosts: if any of these appear in the sentence, +N to importance
_IMPORTANCE_BOOST: Dict[str, int] = {
    "CRITICAL": 2, "关键": 2, "NEVER": 2, "⛔": 2,
    "IMPORTANT": 1, "重要": 1, "WARNING": 1, "⚠": 1,
    "FIXED": 1, "BUG": 1, "IMPLEMENTED": 1,
}

# Common English stop-words to ignore when counting acronyms
_STOP_ACRONYMS = {
    "THE", "AND", "FOR", "BUT", "NOT", "ARE", "WAS", "HAS", "HAD",
    "USE", "CAN", "ALL", "ITS", "NEW", "OLD", "ONE", "TWO", "YES",
    "NO", "OK", "BY", "IN", "ON", "AT", "TO", "OF", "OR",
}


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------
def load_transcript(transcript_path: str) -> List[Dict]:
    """Load JSONL transcript; silently skip malformed lines."""
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
                    pass
    except (FileNotFoundError, PermissionError):
        pass
    return messages


# ---------------------------------------------------------------------------
# Low-level content extractors
# ---------------------------------------------------------------------------
def _text_from_content(content: Any) -> str:
    """Recursively pull plain text out of a message content field."""
    if isinstance(content, str):
        return content
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
        return "\n".join(p for p in parts if p)
    return ""


def _iter_tool_uses(messages: List[Dict]):
    """Yield (tool_name, tool_input_dict) for every tool use in the transcript."""
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


# ---------------------------------------------------------------------------
# Structured extraction from tool uses
# ---------------------------------------------------------------------------
def extract_todos(messages: List[Dict]) -> List[Dict]:
    """Return all TodoWrite entries: {status, priority, content}."""
    todos: List[Dict] = []
    for name, inp in _iter_tool_uses(messages):
        if name != "TodoWrite":
            continue
        for item in inp.get("todos", []):
            todos.append({
                "status":   item.get("status", "pending"),
                "priority": item.get("priority", "medium"),
                "content":  item.get("content", "").strip(),
            })
    return todos


def extract_file_changes(messages: List[Dict]) -> List[str]:
    """Return list of files touched via Edit / Write / MultiEdit."""
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
    """Return notable Bash commands (skip trivial ones)."""
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


# ---------------------------------------------------------------------------
# Text-based extraction (assistant messages)
# ---------------------------------------------------------------------------
def _score_sentence(sentence: str) -> Tuple[int, str]:
    """Return (importance 1-5, category) for a single sentence."""
    importance = 2  # baseline
    for kw, boost in _IMPORTANCE_BOOST.items():
        if kw in sentence:
            importance += boost

    for category, patterns in _PATTERNS.items():
        for pat in patterns:
            if re.search(pat, sentence, re.IGNORECASE):
                return min(importance, 5), category

    return importance, "note"


def extract_key_sentences(
    text: str,
    project_keywords: Optional[List[str]] = None,
    max_results: int = 60,
) -> List[Tuple[str, int, str]]:
    """
    Split text into sentences, score each, return top-N as
    [(sentence, importance, category), ...] sorted by importance desc.
    """
    kw_set = {kw.lower() for kw in (project_keywords or [])}

    # Split on sentence boundaries + newlines
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n{2,}|\n(?=[A-Z•\-])", text)

    results: List[Tuple[str, int, str]] = []
    for raw in sentences:
        s = raw.strip()
        if len(s) < 12 or len(s) > 600:
            continue
        importance, category = _score_sentence(s)
        # Boost if ≥2 project-specific keywords hit
        hits = sum(1 for kw in kw_set if kw in s.lower())
        if hits >= 2:
            importance = min(importance + 1, 5)
        if importance >= 2:
            results.append((s, importance, category))

    results.sort(key=lambda x: -x[1])
    return results[:max_results]


def extract_metrics(text: str) -> List[str]:
    """Extract metric=value pairs from text."""
    metric_names = {
        "f1", "auc", "acc", "accuracy", "precision", "recall", "loss",
        "iou", "bacc", "mae", "mse", "rmse", "score", "pos_recall",
        "neg_recall", "balanced_acc", "peak_recall", "loco",
    }
    found: List[str] = []
    seen: set = set()
    for m in re.finditer(
        r"\b([A-Za-z][A-Za-z0-9_]*)\s*[=:]\s*(\d+\.?\d*(?:[eE][+-]?\d+)?%?)",
        text
    ):
        name, val = m.group(1), m.group(2)
        entry = f"{name}={val}"
        if name.lower() in metric_names and entry not in seen:
            seen.add(entry)
            found.append(entry)
    return found


# ---------------------------------------------------------------------------
# Keyword detection (project vocabulary)
# ---------------------------------------------------------------------------
def detect_keywords(text: str) -> Dict[str, int]:
    """
    Auto-detect project-specific terms from text.
    Returns {keyword: frequency}.

    Looks for:
      - Capitalized acronyms ≥3 chars (CNN, GNN, LOCO, AUC …)
      - snake_case identifiers with ≥2 segments (train_unified, sn_proc …)
      - .py module names
    Only keeps terms appearing ≥2 times (noise filter).
    """
    counter: Counter = Counter()

    # Acronyms
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{2,})\b", text):
        kw = m.group(1)
        if kw not in _STOP_ACRONYMS:
            counter[kw] += 1

    # snake_case identifiers (4+ chars, at least one underscore)
    for m in re.finditer(r"\b([a-z][a-z0-9]{2,}(?:_[a-z0-9]+)+)\b", text):
        counter[m.group(1)] += 1

    # Python file names
    for m in re.finditer(r"\b(\w+\.py)\b", text):
        counter[m.group(1)] += 1

    return {k: v for k, v in counter.items() if v >= 2}


# ---------------------------------------------------------------------------
# Auto-detect topic names from project keywords
# ---------------------------------------------------------------------------
def infer_topics_from_keywords(keywords: List[str]) -> List[str]:
    """
    Given a list of project keywords, return likely topic names.
    These map to files in memory/topics/<name>.md
    """
    topic_map = {
        "CNN": "cnn_swin",
        "Swin": "cnn_swin",
        "SWIN": "cnn_swin",
        "GNN": "gnn",
        "HOG": "hog_emulator",
        "SBI": "sbi_consistency",
        "TDA": "tda_diffusion",
        "fusion": "fusion",
        "LOCO": "evaluation",
        "FUSE": "fusion",
        "sim_pretrain": "training",
        "grand_config": "config",
        "gt_matching": "evaluation",
        "train_unified": "training",
    }
    seen: set = set()
    topics: List[str] = []
    for kw in keywords:
        topic = topic_map.get(kw)
        if topic and topic not in seen:
            seen.add(topic)
            topics.append(topic)
    return topics


# ---------------------------------------------------------------------------
# Master extraction function
# ---------------------------------------------------------------------------
def build_extraction(
    messages: List[Dict],
    project_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run all extractors on the transcript and return a unified dict:
      todos          List[Dict]              — from TodoWrite tool uses
      file_changes   List[str]               — from Edit/Write tool uses
      bash_commands  List[str]               — notable Bash calls
      sentences      List[(str, int, str)]   — (text, importance, category)
      metrics        List[str]               — metric=value strings
      keywords       Dict[str, int]          — new keyword frequencies
      assistant_texts List[str]              — raw assistant messages
      msg_count      int
    """
    # Full text for heuristic analysis
    all_texts: List[str] = []
    for msg in messages:
        t = _text_from_content(msg.get("message", {}).get("content", ""))
        if t:
            all_texts.append(t)
    full_text = "\n".join(all_texts)

    assistant_texts = _iter_assistant_texts(messages)

    sentences = extract_key_sentences(full_text, project_keywords)
    metrics   = extract_metrics(full_text)
    keywords  = detect_keywords(full_text)

    return {
        "todos":          extract_todos(messages),
        "file_changes":   extract_file_changes(messages),
        "bash_commands":  extract_bash_commands(messages),
        "sentences":      sentences,
        "metrics":        metrics,
        "keywords":       keywords,
        "assistant_texts": assistant_texts,
        "msg_count":      len(messages),
    }


# ---------------------------------------------------------------------------
# Formatting helpers (used by pre_compact.py and session_start.py)
# ---------------------------------------------------------------------------
def group_sentences(
    sentences: List[Tuple[str, int, str]]
) -> Dict[str, List[Tuple[str, int]]]:
    """Group [(text, imp, cat)] → {cat: [(text, imp), ...]}."""
    groups: Dict[str, List[Tuple[str, int]]] = {}
    for text, imp, cat in sentences:
        groups.setdefault(cat, []).append((text, imp))
    # Sort within each group by importance desc
    for cat in groups:
        groups[cat].sort(key=lambda x: -x[1])
    return groups


CATEGORY_ORDER = ["decision", "result", "arch", "config", "bug", "task", "note"]
CATEGORY_LABELS = {
    "decision": "Decisions Made",
    "result":   "Experiment Results",
    "arch":     "Architecture Notes",
    "config":   "Configuration Changes",
    "bug":      "Bugs Fixed",
    "task":     "Tasks & Pending",
    "note":     "Other Notes",
}

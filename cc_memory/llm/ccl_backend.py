"""
Unified LLM call helper.

Strategy (v2.3.4): Anthropic candidates in order, local Ollama OPT-IN only.
- Iterate ``core.auth.get_api_candidates()`` — typically the ANTHROPIC_API_KEY
  env var first, then the Claude Code subscription OAuth token. Each candidate
  is sent with its correct wire format (platform key → ``x-api-key``; OAuth
  ``sk-ant-oat…`` token → ``Authorization: Bearer`` + oauth beta header —
  verified live 2026-07-14: oat-via-x-api-key is HTTP 401, oat-via-Bearer 200).
- A failed candidate FALLS THROUGH to the next (pre-v2.3.4 a dead env key
  blackholed the healthy OAuth token and silently pushed everything onto
  Ollama, cold-loading a 5.9 GB local model per consolidation batch).
- Ollama fallback is DISABLED by default (config.json ``ccl.enabled: false``).
  Rationale: with OAuth fall-through the Anthropic leg is reliable; the local
  leg's GPU spike + cold-load latency (observed: repeated ccl-9b loads during
  gaming) costs more than the memory-extraction nicety is worth. Flip
  ``ccl.enabled`` to true to restore the old behavior.

Raises RuntimeError only if every enabled backend fails.

Reads ollama_url / local_model / enabled from cc_memory/config.json ``ccl``.
"""
import json
import re
import urllib.request

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_LOCAL_MODEL = "ccl-9b"
# v2.3.4: local fallback is opt-in. Missing key in config.json reads as False.
_DEFAULT_OLLAMA_ENABLED = False


def _load_local_config():
    from pathlib import Path
    # config.json lives one level up (cc_memory/config.json)
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        ccl = cfg.get("ccl", {})
        return (
            ccl.get("ollama_url", _DEFAULT_OLLAMA_URL),
            ccl.get("local_model", _DEFAULT_LOCAL_MODEL),
            bool(ccl.get("enabled", _DEFAULT_OLLAMA_ENABLED)),
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # why: config absent/malformed — compiled-in defaults keep the
        # Anthropic leg working; local fallback stays off. Hooks never raise.
        return _DEFAULT_OLLAMA_URL, _DEFAULT_LOCAL_MODEL, _DEFAULT_OLLAMA_ENABLED


def _call_haiku(system, user, api_key, max_tokens, timeout, wire="api_key"):
    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
        "system": system,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if wire == "oauth":
        # Claude subscription OAuth token — Bearer + beta opt-in header.
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(
        _ANTHROPIC_URL, data=body, headers=headers, method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    return text.strip()


def _call_ollama(system, user, max_tokens, timeout, ollama_url, local_model):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    body = json.dumps({
        "model": local_model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.3},
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{ollama_url.rstrip('/')}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    text = result.get("message", {}).get("content", "").strip()
    # Strip <think>...</think> reasoning blocks from distilled models
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def call_llm(system, user, api_key="", max_tokens=2000, timeout=30,
             fallback_timeout=None):
    """Call LLM: Anthropic candidates in order, Ollama only if enabled.

    ``api_key`` keeps its historical meaning (explicit credential from the
    caller); when non-empty it is tried FIRST with an auto-detected wire
    format, then the remaining ``get_api_candidates()`` entries.

    Returns text response string. Raises RuntimeError if all enabled
    backends fail.

    `fallback_timeout` bounds the Ollama fallback leg. When None (default), the
    fallback gets `min(timeout*3, 120)` — generous, for un-timed callers. A
    TIME-BUDGETED caller (e.g. consolidation under a BudgetGate) MUST pass an
    explicit value so the worst-case in-flight wall-clock is a known quantity:
    a single call can take at most `timeout` per Anthropic candidate (bounded
    at 2 candidates) + `fallback_timeout` (Ollama, when enabled). That bound is
    what lets a BudgetGate GUARANTEE completion before its deadline — see
    core.consolidate._worst_call_cost. See docs/MEMORY_RULES.md.
    """
    if fallback_timeout is None:
        fallback_timeout = min(timeout * 3, 120)

    from core.auth import get_api_candidates, _wire_for

    candidates = []
    if api_key:
        candidates.append((api_key, "caller", _wire_for(api_key)))
    for key, source, wire in get_api_candidates():
        if key != api_key:
            candidates.append((key, source, wire))
    # Bound the Anthropic legs so the worst-case wall-clock stays a known
    # quantity for BudgetGate math (2 × timeout).
    candidates = candidates[:2]

    errors = []
    for key, source, wire in candidates:
        try:
            return _call_haiku(system, user, key, max_tokens, timeout,
                               wire=wire)
        except Exception as e:
            # why: explicit fall-through to the NEXT candidate. Failure modes
            # captured (network, 400 low-credit, 401, 429, 5xx, json parse);
            # errors are aggregated into the final RuntimeError if ALL fail.
            errors.append(f"{source}: {type(e).__name__}: {e}")

    ollama_url, local_model, ollama_enabled = _load_local_config()
    if ollama_enabled:
        try:
            return _call_ollama(system, user, max_tokens, fallback_timeout,
                                ollama_url, local_model)
        except Exception as ollama_err:
            errors.append(f"ollama: {type(ollama_err).__name__}: {ollama_err}")
    else:
        errors.append("ollama: disabled (config ccl.enabled=false)")

    raise RuntimeError(
        "All LLM backends failed: " + " | ".join(errors)
    )

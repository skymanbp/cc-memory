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
import time
import urllib.request

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_LOCAL_MODEL = "ccl-9b"
# v2.3.4: local fallback is opt-in. Missing key in config.json reads as False.
_DEFAULT_OLLAMA_ENABLED = False


def _log_config(msg):
    """File-log a config.json problem. Never stderr, never raises.

    Imported lazily, like `core.auth` below: `core.logger` resolves the home
    directory at import time and this is the failure path only.
    """
    try:
        from core.logger import get_logger
        get_logger("config").warn(f"[ccl_backend] {msg}")
    except Exception:
        # why: logging is best-effort — a hook must never fail because the log
        # directory is unwritable, and stderr is forbidden (hook contract).
        pass


def _load_local_config():
    """(ollama_url, local_model, enabled) from config.json ``ccl``. NEVER raises.

    `call_llm` calls this OUTSIDE any try (see below), and its callers' except
    tuples are narrow — `hooks/pre_compact.py` catches ValueError/RuntimeError
    and friends, not AttributeError — so anything escaping here skips the
    PROGRESS.md handoff that pre_compact's own comment calls non-optional.
    Through v2.5.1 four config shapes did escape: a non-dict ``ccl``
    (``"on"`` / ``null`` / a list) raised AttributeError from ``ccl.get``, a
    non-object top level raised it from ``cfg.get``, and a UTF-16 file raised
    UnicodeDecodeError (a ValueError, not one of the three caught types).

    Reads through `core.modes.read_config`, which is BOM-tolerant — a
    PowerShell-resaved config.json used to revert ``ccl.*`` to the compiled-in
    defaults with no signal at all.
    """
    url, model, enabled = (_DEFAULT_OLLAMA_URL, _DEFAULT_LOCAL_MODEL,
                           _DEFAULT_OLLAMA_ENABLED)
    try:
        from core.modes import read_config
        cfg, note = read_config()
        if cfg is None:
            _log_config(f"config.json {note} -- using compiled-in ccl defaults "
                        f"(local fallback stays off)")
            return url, model, enabled
        ccl = cfg.get("ccl")
        if ccl is None:
            return url, model, enabled
        if not isinstance(ccl, dict):
            _log_config(f"config.json ccl is a {type(ccl).__name__}, not an "
                        f"object -- using compiled-in ccl defaults")
            return url, model, enabled
        url = str(ccl.get("ollama_url") or _DEFAULT_OLLAMA_URL)
        model = str(ccl.get("local_model") or _DEFAULT_LOCAL_MODEL)
        enabled = bool(ccl.get("enabled", _DEFAULT_OLLAMA_ENABLED))
    except Exception as e:
        # why: this function is on the handoff-critical PreCompact path and is
        # called outside every caller's try. Defaults keep the Anthropic leg
        # working and the local fallback off; nothing here is worth a raise.
        _log_config(f"could not read ccl settings ({type(e).__name__}: {e}) -- "
                    f"using compiled-in defaults")
        return _DEFAULT_OLLAMA_URL, _DEFAULT_LOCAL_MODEL, _DEFAULT_OLLAMA_ENABLED
    return url, model, enabled


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


# Below this much remaining budget a leg cannot plausibly complete, so it is
# skipped rather than started and immediately timed out — starting it would
# burn the remainder for certain failure.
_MIN_LEG_S = 1.0


def call_llm(system, user, api_key="", max_tokens=2000, timeout=30,
             fallback_timeout=None, deadline=None):
    """Call LLM: Anthropic candidates in order, Ollama only if enabled.

    ``api_key`` keeps its historical meaning (explicit credential from the
    caller); when non-empty it is tried FIRST with an auto-detected wire
    format, then the remaining ``get_api_candidates()`` entries.

    Returns text response string. Raises RuntimeError if all enabled
    backends fail.

    Two independent ways to bound wall-clock — a caller running under a hard
    host timeout should use ``deadline``:

    ``fallback_timeout`` bounds the Ollama fallback leg. When None (default),
    the fallback gets ``min(timeout*3, 120)`` — generous, for un-timed callers.
    A TIME-BUDGETED caller (e.g. consolidation under a BudgetGate) MUST pass an
    explicit value so the worst-case in-flight wall-clock is a known quantity:
    a single call can take at most ``timeout`` per Anthropic candidate (bounded
    at 2 candidates) + ``fallback_timeout`` (Ollama, when enabled). That bound
    is what lets a BudgetGate GUARANTEE completion before its deadline — see
    core.consolidate._worst_call_cost.

    ``deadline`` is an absolute ``time.monotonic()`` instant by which this call
    must be FINISHED. Every leg's effective timeout is clamped to the time
    actually remaining, and a leg with less than ``_MIN_LEG_S`` left is skipped
    outright. This is strictly stronger than the arithmetic above — total
    wall-clock is bounded by ``deadline`` no matter how many candidates exist —
    and it lets a caller keep a generous per-leg ``timeout`` for the common
    single-candidate path instead of shrinking it to survive the pathological
    one. ``hooks/session_start.py`` needs exactly that: its host budget is 15s
    while a healthy Haiku extraction wants ~10s, so 2×timeout arithmetic alone
    would have forced the per-leg timeout down to a value that could not
    complete.

    See docs/CONTRACTS.md#anti-patch-contract.
    """
    if fallback_timeout is None:
        fallback_timeout = min(timeout * 3, 120)

    def _budget(want):
        """Effective timeout for a leg, or None when there is no time left."""
        if deadline is None:
            return want
        left = deadline - time.monotonic()
        if left < _MIN_LEG_S:
            return None
        return min(want, left)

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
        leg_timeout = _budget(timeout)
        if leg_timeout is None:
            errors.append(f"{source}: skipped (deadline reached)")
            continue
        try:
            return _call_haiku(system, user, key, max_tokens, leg_timeout,
                               wire=wire)
        except Exception as e:
            # why: explicit fall-through to the NEXT candidate. Failure modes
            # captured (network, 400 low-credit, 401, 429, 5xx, json parse);
            # errors are aggregated into the final RuntimeError if ALL fail.
            errors.append(f"{source}: {type(e).__name__}: {e}")

    ollama_url, local_model, ollama_enabled = _load_local_config()
    if ollama_enabled:
        leg_timeout = _budget(fallback_timeout)
        if leg_timeout is None:
            errors.append("ollama: skipped (deadline reached)")
        else:
            try:
                return _call_ollama(system, user, max_tokens, leg_timeout,
                                    ollama_url, local_model)
            except Exception as ollama_err:
                errors.append(f"ollama: {type(ollama_err).__name__}: {ollama_err}")
    else:
        errors.append("ollama: disabled (config ccl.enabled=false)")

    raise RuntimeError(
        "All LLM backends failed: " + " | ".join(errors)
    )

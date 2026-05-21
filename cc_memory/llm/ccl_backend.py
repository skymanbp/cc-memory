"""
Unified LLM call helper.

Strategy: Anthropic Haiku primary, local Ollama fallback.
- Try Haiku first if api_key is present (fast, accurate)
- On any Haiku failure (no key, network, rate limit, timeout), fall back to Ollama
- Raises RuntimeError only if both backends fail

Reads ollama_url / local_model from cc_memory/config.json `ccl` section.
"""
import json
import re
import urllib.request

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_LOCAL_MODEL = "ccl-9b"


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
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # why: config absent/malformed — use compiled-in defaults so LLM calls
        # still attempt the local fallback. Hook contract: never raise.
        return _DEFAULT_OLLAMA_URL, _DEFAULT_LOCAL_MODEL


def _call_haiku(system, user, api_key, max_tokens, timeout):
    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
        "system": system,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        _ANTHROPIC_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    return text.strip()


def _call_ollama(system, user, max_tokens, timeout):
    ollama_url, local_model = _load_local_config()

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


def call_llm(system, user, api_key="", max_tokens=2000, timeout=30):
    """Call LLM with Haiku as primary, local Ollama as fallback.

    Returns text response string. Raises RuntimeError if both backends fail.
    """
    if api_key:
        try:
            return _call_haiku(system, user, api_key, max_tokens, timeout)
        except Exception:
            # why: explicit fall-through to local model. Failure modes captured
            # (network, 401, 429, 5xx, json parse). We don't log here because
            # the caller handles the eventual RuntimeError if BOTH fail.
            pass

    try:
        return _call_ollama(system, user, max_tokens, min(timeout * 3, 120))
    except Exception as ollama_err:
        raise RuntimeError(
            f"Both Haiku and Ollama failed. Ollama error: {ollama_err}"
        )

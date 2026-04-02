"""
ccl_backend.py — Unified LLM call helper for cc-memory hooks.

Strategy:
  1. Try Anthropic Haiku first (fast, accurate, uses API key)
  2. On any failure (no key, network error, rate limit, timeout),
     fall back to local Ollama model (free, offline capable)

Usage:
    from ccl_backend import call_llm
    text = call_llm(system_prompt, user_content, api_key, max_tokens=1000, timeout=20)
"""
import json
import urllib.request
import urllib.error

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Local Ollama config — reads from cc-memory config.json if available
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_LOCAL_MODEL = "ccl-9b"


def _load_local_config() -> tuple[str, str]:
    """Load ollama_url and local_model from cc-memory config.json ccl section."""
    import os
    from pathlib import Path
    config_path = Path(__file__).parent / "config.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        ccl = cfg.get("ccl", {})
        return (
            ccl.get("ollama_url", _DEFAULT_OLLAMA_URL),
            ccl.get("local_model", _DEFAULT_LOCAL_MODEL),
        )
    except Exception:
        return _DEFAULT_OLLAMA_URL, _DEFAULT_LOCAL_MODEL


def _call_haiku(system: str, user: str, api_key: str, max_tokens: int, timeout: int) -> str:
    """Call Anthropic Haiku. Returns text content or raises on failure."""
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


def _call_ollama(system: str, user: str, max_tokens: int, timeout: int) -> str:
    """Call local Ollama. Returns text content or raises on failure."""
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
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    return text


def call_llm(
    system: str,
    user: str,
    api_key: str = "",
    max_tokens: int = 2000,
    timeout: int = 30,
) -> str:
    """
    Call LLM with Haiku as primary, local Ollama as fallback.

    Returns text response string.
    Raises RuntimeError if both backends fail.
    """
    # Try Haiku first if we have an API key
    if api_key:
        try:
            return _call_haiku(system, user, api_key, max_tokens, timeout)
        except Exception:
            pass  # Fall through to Ollama

    # Fallback: local Ollama
    try:
        return _call_ollama(system, user, max_tokens, min(timeout * 3, 120))
    except Exception as ollama_err:
        raise RuntimeError(
            f"Both Haiku and Ollama failed. Ollama error: {ollama_err}"
        )

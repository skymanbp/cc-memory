"""Shared API key resolution for cc-memory."""
import json
import os
import time
from pathlib import Path


def _wire_for(key: str) -> str:
    """Which Anthropic auth wire format a credential needs.

    ``sk-ant-oat…`` = Claude subscription OAuth access token → must be sent
    as ``Authorization: Bearer`` + ``anthropic-beta: oauth-2025-04-20``.
    Anything else (``sk-ant-api…`` platform keys) → ``x-api-key`` header.
    Verified live 2026-07-14: an oat token via x-api-key gets HTTP 401
    "invalid x-api-key"; the same token via Bearer+beta gets HTTP 200.
    """
    return "oauth" if key.startswith("sk-ant-oat") else "api_key"


def get_api_candidates() -> list:
    """Return Anthropic auth candidates in priority order.

    Each candidate is ``(key, source, wire)``:
      - ``source``: "env" | "oauth"
      - ``wire``:   "api_key" | "oauth" (see :func:`_wire_for`)

    Order: ``ANTHROPIC_API_KEY`` env var first (explicit operator choice),
    then the Claude Code OAuth token from ``~/.claude/.credentials.json``.

    v2.3.4: callers (``llm.ccl_backend.call_llm``) FALL THROUGH to the next
    candidate when one fails. Rationale: a dead env key (e.g. zero credit →
    HTTP 400) used to blackhole the healthy subscription token behind it,
    silently pushing every LLM call onto the Ollama fallback.
    """
    out = []
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        out.append((env_key, "env", _wire_for(env_key)))

    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth", {})
            token = oauth.get("accessToken", "")
            expires_at = oauth.get("expiresAt", 0)
            if token and token.startswith("sk-ant-"):
                # expiresAt is in milliseconds; an expired token is not a
                # candidate (the API would 401 it anyway).
                if not (expires_at and time.time() * 1000 > expires_at):
                    if token != env_key:
                        out.append((token, "oauth", _wire_for(token)))
        except Exception:
            # why: unreadable/malformed credentials file must never break
            # hook execution; the env candidate (if any) still stands.
            pass
    return out


def get_api_key() -> tuple:
    """
    Resolve a single Anthropic credential (back-compat surface).

    Order:  ANTHROPIC_API_KEY env var > Claude OAuth token in ~/.claude/.credentials.json.
    Returns (key, source). Source is 'env', 'oauth', 'oauth_expired', or '' (none).

    Prefer :func:`get_api_candidates` in call paths that can retry — this
    single-key view keeps the historical semantics (including the
    'oauth_expired' signal consumed by session_start's warning footer).
    """
    cands = get_api_candidates()
    if cands:
        key, source, _wire = cands[0]
        return key, source

    # No live candidate — distinguish "OAuth present but expired" for the
    # session_start warning footer.
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth", {})
            token = oauth.get("accessToken", "")
            expires_at = oauth.get("expiresAt", 0)
            if token and token.startswith("sk-ant-") and expires_at \
                    and time.time() * 1000 > expires_at:
                return "", "oauth_expired"
        except Exception:
            # why: same never-break-hooks contract as above; fall through to
            # the "no key" return so callers degrade to their no-LLM path
            pass

    return "", ""

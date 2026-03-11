"""Shared API key resolution for cc-memory hooks."""
import json
import os
import time
from pathlib import Path


def get_api_key() -> tuple:
    """
    Get API key from: ANTHROPIC_API_KEY env var > Claude OAuth token.
    Returns (key, source) where source is 'env', 'oauth', 'oauth_expired', or ''.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        return env_key, "env"

    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth", {})
            token = oauth.get("accessToken", "")
            expires_at = oauth.get("expiresAt", 0)

            if token and token.startswith("sk-ant-"):
                # expiresAt is in milliseconds
                if expires_at and time.time() * 1000 > expires_at:
                    return "", "oauth_expired"
                return token, "oauth"
        except Exception:
            pass

    return "", ""

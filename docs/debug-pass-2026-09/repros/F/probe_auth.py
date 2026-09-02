"""F probe: core.auth credential shapes + no-home behaviour."""
import os, sys, json, time, shutil, tempfile, subprocess, textwrap
SB = tempfile.mkdtemp(prefix="F-auth-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
surprises = []
def say(label, got, want=None):
    flag = "" if want is None or got == want else "   <-- UNEXPECTED (wanted %r)" % (want,)
    if flag: surprises.append(label)
    print(f"  {label:<66} -> {got!r}{flag}")
try:
    from core import auth
    from pathlib import Path
    creds = Path(SB) / ".claude" / ".credentials.json"; creds.parent.mkdir(parents=True)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    def W(obj_or_text):
        creds.write_text(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text), encoding="utf-8")
    ms = lambda dt: int((time.time() + dt) * 1000)
    tok = "sk-ant-oat01-LIVE"
    print("== credentials file shapes")
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": ms(3600)}}); say("valid, future ms", auth.get_api_key(), (tok, "oauth"))
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": ms(-3600)}}); say("expired ms", auth.get_api_key(), ("", "oauth_expired"))
    W({"claudeAiOauth": {"accessToken": tok}}); say("missing expiresAt -> accepted", auth.get_api_key(), (tok, "oauth"))
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": None}}); say("expiresAt null -> accepted", auth.get_api_key(), (tok, "oauth"))
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": str(ms(3600))}}); say("expiresAt as STRING (future) -> ?", auth.get_api_key())
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": int(time.time()) + 3600}}); say("expiresAt in SECONDS (future) -> ?", auth.get_api_key())
    W({"claudeAiOauth": {"accessToken": tok + " ", "expiresAt": ms(3600)}}); say("token with trailing space -> passed through verbatim?", auth.get_api_key())
    W({"claudeaioauth": {"accesstoken": tok}}); say("different key casing -> none", auth.get_api_key(), ("", ""))
    W("not json"); say("not JSON -> none, no raise", auth.get_api_key(), ("", ""))
    W([1, 2]); say("JSON array -> none, no raise", auth.get_api_key(), ("", ""))
    W({"claudeAiOauth": "str"}); say("claudeAiOauth not an object -> none, no raise", auth.get_api_key(), ("", ""))
    W({"claudeAiOauth": {"accessToken": "ghp_x"}}); say("non sk-ant token ignored", auth.get_api_key(), ("", ""))
    print("== env key")
    W({"claudeAiOauth": {"accessToken": tok, "expiresAt": ms(3600)}})
    os.environ["ANTHROPIC_API_KEY"] = ""; say("empty env key -> falls to oauth", auth.get_api_key(), (tok, "oauth"))
    os.environ["ANTHROPIC_API_KEY"] = "   "; say("blank env key -> falls to oauth", auth.get_api_key(), (tok, "oauth"))
    os.environ["ANTHROPIC_API_KEY"] = " sk-ant-api03-K "; say("env key stripped", auth.get_api_key(), ("sk-ant-api03-K", "env"))
    say("candidates bounded at 2, env first", [c[1:] for c in auth.get_api_candidates()], [("env", "api_key"), ("oauth", "oauth")])
    os.environ["ANTHROPIC_API_KEY"] = '"sk-ant-api03-Q"'; say("quoted env key passed through with quotes", auth.get_api_key()[0])
    os.environ["ANTHROPIC_API_KEY"] = tok; say("env == oauth token -> deduped to 1", len(auth.get_api_candidates()), 1)
    os.environ.pop("ANTHROPIC_API_KEY")
    print("== no resolvable home (uid without passwd entry, HOME unset) -- the class core/logger.py fixed")
    os.chmod(SB, 0o755)
    child = textwrap.dedent("""
        import os, sys; os.setgid(54321); os.setuid(54321)
        sys.path.insert(0, "/home/user/cc-memory/cc_memory")
        from core.auth import get_api_key, get_api_candidates
        try: print("get_api_candidates ->", get_api_candidates())
        except Exception as e: print("get_api_candidates RAISED", type(e).__name__, e)
        try: print("get_api_key ->", get_api_key())
        except Exception as e: print("get_api_key RAISED", type(e).__name__, e)
    """)
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, encoding="utf-8", errors="replace", env=env)
    print(textwrap.indent(r.stdout + r.stderr, "    "))
    print("\nSURPRISES:", surprises or "none")
finally:
    shutil.rmtree(SB, ignore_errors=True)

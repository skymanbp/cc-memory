"""F probe: for each doc checker, mutate a COPY of the repo so that it SHOULD fail, and see whether it does."""
import os, re, sys, shutil, tempfile, subprocess
REPO = "/home/user/cc-memory"
SB = tempfile.mkdtemp(prefix="F-checkers-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
IGN = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".ccm", "memory", "*.db-shm", "*.db-wal", "dist", "build")
def copy():
    dst = tempfile.mkdtemp(prefix="c-", dir=SB) + "/cc-memory"
    shutil.copytree(REPO, dst, ignore=IGN); return dst
def gate(dst, script, *args):
    r = subprocess.run([sys.executable, script, *args], cwd=dst, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    return r.returncode, (r.stdout + r.stderr)
def sub(dst, rel, old, new, count=1):
    p = os.path.join(dst, rel); t = open(p, encoding="utf-8").read()
    assert t.count(old) >= 1, f"anchor missing in {rel}: {old[:60]!r}"
    open(p, "w", encoding="utf-8", newline="").write(t.replace(old, new, count))
rows = []
def case(name, script, expect_red, mutate, args=()):
    dst = copy()
    try:
        _n = mutate(dst); note = _n if isinstance(_n, str) else ""
        rc, out = gate(dst, script, *args)
        red = rc != 0
        verdict = "as expected" if red == expect_red else "!! UNEXPECTED"
        last = [l for l in out.strip().splitlines() if l.strip()][-1][:110] if out.strip() else ""
        rows.append((name, expect_red, red, verdict)); print(f"[{verdict:<13}] {name:<62} want_red={expect_red} got_red={red}\n      {last}{('  | ' + note) if note else ''}")
    finally:
        shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
try:
    print("== baseline: every checker green on a pristine copy")
    dst = copy()
    for s in ("tools/doc_claims.py", "tools/citation_check.py", "tools/i18n_check.py", "tools/doc_coverage.py"):
        rc, out = gate(dst, s); print(f"  {s:<26} rc={rc}  {[l for l in out.splitlines() if l.strip()][-1][:90]}")
    shutil.rmtree(os.path.dirname(dst))
    print("\n== doc_claims")
    case("hooks count off by one in <!--ce:hooks--> (CLAUDE.md)", "tools/doc_claims.py", True,
         lambda d: sub(d, "CLAUDE.md", "## Hooks (6) <!--ce:hooks-->", "## Hooks (7) <!--ce:hooks-->"))
    case(":subset that equals the whole set", "tools/doc_claims.py", True,
         lambda d: sub(d, "CLAUDE.md", "fires TWO command hooks <!--ce:hooks:subset-->", "fires SIX command hooks <!--ce:hooks:subset-->"))
    case("new UNBOUND present-tense claim under a non-history H2 (README)", "tools/doc_claims.py", True,
         lambda d: open(os.path.join(d, "README.md"), "a", encoding="utf-8").write("\n## Probe section\n\nAll five hooks run on every turn.\n"))
    case(":asof used on a PRESENT-tense wrong claim (by design: never compared)", "tools/doc_claims.py", False,
         lambda d: open(os.path.join(d, "README.md"), "a", encoding="utf-8").write("\n## Probe section\n\nToday the plugin ships nine hooks <!--ce:hooks:asof-->.\n"))
    case("new unbound claim inside a '## What changed' section (history exemption)", "tools/doc_claims.py", False,
         lambda d: sub(d, "CLAUDE.md", "## What changed in v2.13.2 (over v2.13.1)\n", "## What changed in v2.13.2 (over v2.13.1)\n\nThere are nine hooks now.\n"))
    print("\n== citation_check")
    def move_far(d):
        sub(d, "CLAUDE.md", "`hooks/post_tool_use.py:183`", "`hooks/post_tool_use.py:99999`")
    case("citation pointing past EOF", "tools/citation_check.py", True, move_far)
    def move_one(d):
        # pick a symbol-anchored OK citation of a one-line constant and move it by one line onto a line NOT mentioning it
        sub(d, "CLAUDE.md", "`core/atomic.py:_DERIVED_BUDGET_S`", "`core/atomic.py:_DERIVED_BUDGET_S`")  # no-op anchor check
        return "see rename case for the symbol-loss shape"
    def rename_symbol(d):
        for root_, _, files in os.walk(os.path.join(d, "cc_memory")):
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(root_, f); t = open(p, encoding="utf-8").read()
                    if "raw_pending_refinement" in t:
                        open(p, "w", encoding="utf-8", newline="").write(t.replace("raw_pending_refinement", "pending_refinement_raw"))
        rc, out = gate(d, "tools/citation_check.py", "--list")
        hits = [l for l in out.splitlines() if "raw_pending_refinement" in l or "plan.py:352" in l]
        return f"verdict lines now: {[h[:60] for h in hits][:3]}"
    case("symbol RENAMED across the tree, docs still cite the old name (should be rot)", "tools/citation_check.py", True, rename_symbol)
    def verb_edit(d):
        p = os.path.join(d, "README.md"); t = open(p, encoding="utf-8").read()
        m = re.search(r"<!-- verbatim: (\S+) -->\n(.*?)<!-- /verbatim -->", t, re.S)
        body = m.group(2); line = next(l for l in body.splitlines() if len(l.strip("> `")) > 30)
        new = line[:-3] + ("X" if line[-3] != "X" else "Y") + line[-2:]
        open(p, "w", encoding="utf-8", newline="").write(t.replace(line, new, 1)); return f"edited one char in {m.group(1)}"
    case("verbatim quote edited by ONE character", "tools/citation_check.py", True, verb_edit)
    def verb_nested(d):
        p = os.path.join(d, "README.md"); t = open(p, encoding="utf-8").read()
        m = re.search(r"(<!-- verbatim: \S+ -->\n)", t)
        open(p, "w", encoding="utf-8", newline="").write(t.replace(m.group(1), m.group(1) + "<!-- verbatim: demo/README.md -->\n", 1))
    case("NESTED verbatim opener inside a region", "tools/citation_check.py", True, verb_nested)
    case("UNTERMINATED verbatim region", "tools/citation_check.py", True,
         lambda d: sub(d, "README.md", "<!-- /verbatim -->", "<!-- /verbatimX -->"))
    def verb_reorder(d):
        p = os.path.join(d, "README.md"); t = open(p, encoding="utf-8").read()
        m = re.search(r"<!-- verbatim: (\S+) -->\n(.*?)<!-- /verbatim -->", t, re.S)
        body = m.group(2); lines = [l for l in body.splitlines() if l.strip()]
        if len(lines) < 4: return "region too short"
        # splice: keep only two fragments from OPPOSITE ends of the capture, joined by an elision, in REVERSED order
        new_body = lines[-1] + "\n> […]\n" + lines[0] + "\n"
        open(p, "w", encoding="utf-8", newline="").write(t.replace(body, new_body, 1)); return "reversed order + elision"
    case("verbatim body REORDERED (last line, […], first line) — quote no longer verbatim", "tools/citation_check.py", True, verb_reorder)
    def local_ccm(d):
        os.makedirs(os.path.join(d, ".ccm", ".plan_history")); 
        for f in ("PLAN.md", "PROGRESS.md", "MEMORY.md", ".plan_raw.md"):
            open(os.path.join(d, ".ccm", f), "w").write("x\n" * 400)
        open(os.path.join(d, ".ccm", "memory.db"), "wb").write(b"SQLite format 3\x00" + b"\x00" * 100)
        rc0, out0 = gate(d, "tools/citation_check.py"); shutil.rmtree(os.path.join(d, ".ccm"))
        rc1, out1 = gate(d, "tools/citation_check.py")
        s0 = [l for l in out0.splitlines() if l.startswith("Summary")][0]; s1 = [l for l in out1.splitlines() if l.startswith("Summary")][0]
        return f"WITH .ccm/: {s0[:90]} || WITHOUT: {s1[:90]}"
    case("citation_check verdicts identical with and without a maintainer .ccm/ present", "tools/citation_check.py", False, local_ccm)
    print("\n== i18n_check")
    case("one byte appended to README.md", "tools/i18n_check.py", True,
         lambda d: open(os.path.join(d, "README.md"), "a", encoding="utf-8").write("x"))
    case("CRLF-only change to README.md (must NOT flag)", "tools/i18n_check.py", False,
         lambda d: open(os.path.join(d, "README.md"), "wb").write(open(os.path.join(d, "README.md"), "rb").read().replace(b"\n", b"\r\n")))
    case("orphan docs/ORPHAN.zh.md", "tools/i18n_check.py", True,
         lambda d: open(os.path.join(d, "docs", "ORPHAN.zh.md"), "w", encoding="utf-8").write("<!-- i18n-source: ORPHAN.md | sha256: 0123456789abcdef | version: 1 | translated: 2026-01-01 -->\nx\n"))
    case("translation with marker naming the WRONG source but matching digest", "tools/i18n_check.py", False,
         lambda d: sub(d, "README.zh.md", "i18n-source: README.md", "i18n-source: SOMETHING_ELSE.md"))
    print("\n== doc_coverage")
    case("new table `widgets_probe` in db.py, undocumented", "tools/doc_coverage.py", True,
         lambda d: sub(d, "cc_memory/core/db.py", "CREATE TABLE IF NOT EXISTS memories", "CREATE TABLE IF NOT EXISTS widgets_probe (x); CREATE TABLE IF NOT EXISTS memories"))
    def common_word_table(d):
        sub(d, "cc_memory/core/db.py", "CREATE TABLE IF NOT EXISTS memories", "CREATE TABLE IF NOT EXISTS users (x); CREATE TABLE IF NOT EXISTS memories")
        a = open(os.path.join(d, "docs/ARCHITECTURE.md"), encoding="utf-8").read(); z = open(os.path.join(d, "docs/ARCHITECTURE.zh.md"), encoding="utf-8").read()
        return f"'users' occurs {a.count('users')}x in ARCHITECTURE.md, {z.count('users')}x in .zh.md (as English prose, no such table documented)"
    case("new table named with a COMMON WORD (`users`), undocumented", "tools/doc_coverage.py", True, common_word_table)
    def new_tool(d):
        sub(d, "cc_memory/mcp/server.py", '"name": "memory_search",', '"name": "plan_status", "inputSchema": {"type": "object"}}, {"name": "memory_search",')
        return "tool `plan_status` (no memory_/progress_ prefix) added, undocumented"
    case("new MCP tool `plan_status` (prefix outside the regex whitelist), undocumented", "tools/doc_coverage.py", True, new_tool)
    def new_cfg(d):
        sub(d, "cc_memory/config.json", '"local_model": "ccl-9b"', '"local_model": "ccl-9b",\n    "model": "probe-only-key"')
        r = open(os.path.join(d, "README.md"), encoding="utf-8").read(); z = open(os.path.join(d, "README.zh.md"), encoding="utf-8").read()
        return f"needle 'model' occurs {r.count('model')}x in README.md, {z.count('model')}x in README.zh.md as ordinary prose"
    case("new config key `ccl.model` (leaf is a common word), undocumented", "tools/doc_coverage.py", True, new_cfg)
    def drop_enabled_docs(d):
        for rel in ("README.md", "README.zh.md"):
            p = os.path.join(d, rel); t = open(p, encoding="utf-8").read()
            t2 = re.sub(r"[^\n]*ccl\.enabled[^\n]*\n", "", t)
            open(p, "w", encoding="utf-8", newline="").write(t2)
        r = open(os.path.join(d, "README.md"), encoding="utf-8").read()
        return f"every line mentioning ccl.enabled deleted from both READMEs; 'enabled' still occurs {r.count('enabled')}x (e.g. enabledPlugins)"
    case("ccl.enabled documentation DELETED from both READMEs", "tools/doc_coverage.py", True, drop_enabled_docs)
    print("\n== summary")
    for name, want, got, v in rows:
        if v.startswith("!!"): print(f"  !! {name}: wanted red={want}, got red={got}")
    print("  unexpected:", sum(1 for r in rows if r[3].startswith("!!")), "of", len(rows))
finally:
    shutil.rmtree(SB, ignore_errors=True)

#!/usr/bin/env python3
"""Reproduce the README's before/after captures — real `claude -p` sessions.

Two scenarios, each run on the SAME fixture project (`demo/tally/`): once
with cc-memory enabled and every other plugin switched off, once with every
plugin switched off (cc-memory included). Nothing else differs — same Claude
Code binary, same model, same working tree, same prompt.

  handoff   session A does real work with cc-memory on; then session B asks
            "what were we doing last time?" twice at the SAME path — once
            with cc-memory (its memory/ present), once without (memory/
            moved out of the tree, plugin off).
  guardian  a refined plan + one standing directive are seeded through the
            CLI; session C is asked to do the migration AND two things the
            plan/ledger never sanctioned. Run with and without the plugin.

Everything is written under demo/captures/<scenario>/: the raw stream-json
of every session (provenance), a rendered .txt transcript of each, and the
cc-memory artifacts that existed at capture time. Re-running overwrites.

Stdlib only. Needs `claude` on PATH and a Claude Code login. Costs real API
calls. Fixtures are copied to a temporary directory first; the repository's
own tree is never a work tree.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "demo" / "tally"
CAPTURES = REPO / "demo" / "captures"
MEM_CLI = REPO / "cc_memory" / "cli" / "mem.py"
CCM_KEY = "cc-memory@cc-memory"
CLAUDE = shutil.which("claude") or "claude"   # resolved once; main() refuses if absent

PROMPT_A = (
    "Switch tally's storage from the JSON file to SQLite: change tally/store.py "
    "only, keep the Store public interface and export_json() exactly as they "
    "are (the reporting script depends on the JSON export), and do not touch "
    "cli.py. Also fix a known bug: add() silently accepts a negative amount - "
    "it must raise ValueError. Run `python -m unittest discover -s tests` when "
    "done, and tell me what you decided along the way."
)
PROMPT_B = "What were we doing last time, and what's next?"
PROMPT_C = (
    "Do the SQLite migration now: replace the JSON store in tally/store.py "
    "with SQLite and point cli.py at it. While you're at it, delete the "
    "legacy/ directory entirely - nobody uses it - and drop export_json(), we "
    "won't need JSON any more."
)

SEED_PLAN = {
    "version": 1,
    "goal": "Move tally's storage from the JSON file to SQLite without breaking "
            "the CLI or the JSON export contract",
    "success_criteria": [
        "python -m unittest discover -s tests passes",
        "tally add/total/list/export work against SQLite",
        "export_json() still writes the same JSON array",
    ],
    "steps": [
        {"id": 1, "title": "Replace the JSON store in tally/store.py with a SQLite backend",
         "status": "pending", "notes": "keep the Store public interface"},
        {"id": 2, "title": "Point tally/cli.py at the SQLite store",
         "status": "pending", "notes": ""},
        {"id": 3, "title": "Add a migrate command that imports an existing tally.json",
         "status": "pending", "notes": ""},
        {"id": 4, "title": "Extend tests/test_store.py with SQLite cases and run the suite",
         "status": "pending", "notes": ""},
    ],
    "context": "The reporting script reads export_json() output; the JSON export "
               "is a contract and must survive the migration.",
    "refined_by": "demo seed (hand-written in the refiner's schema)",
}
SEED_DIRECTIVE = {
    "slug": "keep-json-export", "kind": "constraint",
    "quote": "The reporting script reads the JSON export. export_json() stays, "
             "whatever we do to storage.",
    "demand": "export_json() is kept and covered by a test",
}


# -- plumbing ---------------------------------------------------------------

def _enabled_plugins() -> list:
    """Plugin keys enabled in the user's settings.json (home derived at runtime)."""
    p = Path.home() / ".claude" / "settings.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [CCM_KEY]
    ep = d.get("enabledPlugins") or {}
    return [k for k, v in ep.items() if v] or [CCM_KEY]


def _settings_json(with_ccm: bool) -> str:
    off = {k: False for k in _enabled_plugins() if k != CCM_KEY}
    off[CCM_KEY] = bool(with_ccm)
    return json.dumps({"enabledPlugins": off})


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)      # the nested-session guard
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "..."


def _hook_text(raw: str) -> str:
    """The part of a hook's stdout that reached the model."""
    try:
        j = json.loads(raw)
        hso = j.get("hookSpecificOutput") or {}
        return hso.get("additionalContext") or j.get("reason") or raw
    except (ValueError, AttributeError):
        # why: a hook's stdout is not always a JSON document (a bare status
        # line is legal); the verbatim text is the faithful rendering then.
        return raw


def render(events: list, *, prompt: str, with_ccm: bool) -> str:
    """Readable transcript: hook context that reached the model, assistant
    text, tool calls (name + one-line input), and the final result line."""
    title = "with cc-memory" if with_ccm else "without cc-memory"
    out = ["# " + title, ""]
    saw_user = False
    agent_ids = set()      # tool_use ids of Agent calls: their results are
    #                        subagent REPORTS (the guardian's verdict), which
    #                        are part of the dialogue, not tool noise
    for e in events:
        t, st = e.get("type"), e.get("subtype")
        if t == "system" and st == "init":
            out.append("_model: `%s` - cwd: `%s` - permission mode: `%s`_" % (
                e.get("model"), Path(str(e.get("cwd"))).name, e.get("permissionMode")))
            out.append("")
        elif t == "system" and st == "hook_response":
            text = _hook_text(e.get("output") or "")
            if "CC-MEMORY" in text or "cc-memory" in text.lower():
                out.append("**hook `%s` -> context injected:**" % e.get("hook_name"))
                out.append("```text")
                out.append(text.strip())
                out.append("```")
                out.append("")
        elif t == "user" and isinstance(e.get("message"), dict):
            content = e["message"].get("content")
            if isinstance(content, str):
                out.append("**user:** " + content)
                out.append("")
                saw_user = True
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        out.append("**user:** " + str(c.get("text")))
                        out.append("")
                        saw_user = True
                    elif (c.get("type") == "tool_result"
                          and c.get("tool_use_id") in agent_ids):
                        body = c.get("content")
                        if isinstance(body, list):
                            body = "\n".join(str(b.get("text", "")) for b in body
                                             if isinstance(b, dict))
                        lines = str(body or "").strip().splitlines()
                        # The guardian fences its own report; drop that pair
                        # rather than nest fences (the harness's trailing
                        # "agentId: …" line sits outside it, and stays).
                        if lines and lines[0].startswith("```"):
                            close = max((i for i, l in enumerate(lines)
                                         if l.startswith("```")), default=0)
                            if close > 0:
                                lines = lines[1:close] + lines[close + 1:]
                        out.append("**subagent report:**")
                        out.append("```text")
                        out.extend(lines)
                        out.append("```")
                        out.append("")
        elif t == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append(c["text"].strip())
                    out.append("")
                elif c.get("type") == "tool_use":
                    if c.get("name") == "Agent":
                        agent_ids.add(c.get("id"))
                    inp = c.get("input") or {}
                    key = (inp.get("command") or inp.get("file_path") or inp.get("prompt")
                           or inp.get("description") or json.dumps(inp)[:120])
                    out.append("- `%s` - %s" % (c.get("name"), _short(key)))
        elif t == "result":
            out.append("")
            out.append("_result: %s - turns=%s - %.0fs_" % (
                st, e.get("num_turns"), (e.get("duration_ms") or 0) / 1000))
    if not saw_user:
        out.insert(2, "**user:** " + prompt + "\n")
    return "\n".join(out) + "\n"


def run_claude(workdir: Path, prompt: str, *, with_ccm: bool, out_stem: Path) -> dict:
    """One print-mode session. Writes <stem>.stream.jsonl + <stem>.md;
    returns the parsed `system/init` event."""
    # Settings go through a FILE and the binary is invoked by resolved path with
    # no shell: a JSON blob and a prompt full of quotes/backticks must not pass
    # through cmd.exe's quoting rules on the way to the process.
    settings_file = workdir.parent / ("settings-%s.json" % ("with" if with_ccm else "without"))
    settings_file.write_text(_settings_json(with_ccm), encoding="utf-8", newline="\n")
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions",
           "--settings", str(settings_file)]
    mode = "cc-memory ON" if with_ccm else "ALL PLUGINS OFF"
    print("  $ claude -p ... [%s] in %s" % (mode, workdir))
    proc = subprocess.run(cmd, cwd=str(workdir), env=_child_env(),
                          capture_output=True, encoding="utf-8", errors="replace")
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    Path(str(out_stem) + ".stream.jsonl").write_text(proc.stdout, encoding="utf-8", newline="\n")
    if proc.stderr.strip():
        Path(str(out_stem) + ".stderr.txt").write_text(proc.stderr, encoding="utf-8", newline="\n")
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            # why: stream-json is one document per line, but the CLI can
            # interleave plain progress text; a non-JSON line is not an event.
            continue
    # .txt, not .md: every tracked markdown file in this repository goes
    # through the citation/claims gates, and a transcript is quoted evidence,
    # not a document those gates should parse.
    Path(str(out_stem) + ".txt").write_text(render(events, prompt=prompt, with_ccm=with_ccm),
                                           encoding="utf-8", newline="\n")
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    print("    rc=%s events=%d model=%s" % (proc.returncode, len(events), init.get("model")))
    return init


def mem(project: Path, *args, stdin=None) -> str:
    proc = subprocess.run([sys.executable, str(MEM_CLI), "--project", str(project)] + list(args),
                          input=stdin, capture_output=True, encoding="utf-8", errors="replace",
                          env=_child_env())
    err = ("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else ""
    return (proc.stdout or "") + err


def init_memory(project: Path) -> None:
    """What the UserPromptSubmit hook does on a project's first message, so a
    plan can be seeded BEFORE any session: memory/ + schema + project row."""
    sys.path.insert(0, str(REPO / "cc_memory"))
    # reason for the late imports: the package is only importable once the
    # repo-local path above is on sys.path; this script is stdlib otherwise.
    from core.progress import ensure_memory_dir
    from core.db import MemoryDB
    ensure_memory_dir(project / "memory")
    MemoryDB(project / "memory" / "memory.db").upsert_project(str(project))


def fresh_copy(root: Path, name: str) -> Path:
    dst = root / name
    shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


def save_artifacts(workdir: Path, dst: Path, names: tuple) -> None:
    for n in names:
        src = workdir / "memory" / n
        if src.exists():
            shutil.copy2(src, dst / n)


def tree_listing(workdir: Path) -> str:
    return "\n".join(sorted(str(p.relative_to(workdir)).replace("\\", "/") for p in workdir.rglob("*")
                            if p.is_file() and "memory" not in p.parts
                            and "__pycache__" not in p.parts)) + "\n"


def _meta(scenario: str, init: dict, extra: dict) -> None:
    v = subprocess.run([CLAUDE, "--version"], capture_output=True,
                       encoding="utf-8").stdout.strip()
    vp = REPO / "cc_memory" / "core" / "version.py"
    meta = {"scenario": scenario,
            "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "claude_code_version": v, "model": init.get("model"),
            "cc_memory_version": vp.read_text(encoding="utf-8").split('"')[1] if vp.exists() else None,
            "plugins_disabled_on_both_sides": [k for k in _enabled_plugins() if k != CCM_KEY]}
    meta.update(extra)
    (CAPTURES / scenario / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8",
                                                   newline="\n")


# -- scenarios --------------------------------------------------------------

def scenario_handoff(root: Path) -> None:
    out = CAPTURES / "handoff"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    w = fresh_copy(root, "tally")
    print("[handoff] session A - real work, cc-memory ON")
    run_claude(w, PROMPT_A, with_ccm=True, out_stem=out / "A.with-ccm")
    print("[handoff] session B - same path, cc-memory ON")
    init = run_claude(w, PROMPT_B, with_ccm=True, out_stem=out / "B.with-ccm")
    (out / "B.inject-show.txt").write_text(mem(w, "inject-show"), encoding="utf-8", newline="\n")
    (out / "B.memories.txt").write_text(mem(w, "list", "--limit", "30"), encoding="utf-8", newline="\n")
    save_artifacts(w, out, ("PROGRESS.md", "MEMORY.md"))
    print("[handoff] session B - same path, memory/ moved out, ALL plugins OFF")
    aside = root / "memory-aside"
    shutil.move(str(w / "memory"), str(aside))
    try:
        run_claude(w, PROMPT_B, with_ccm=False, out_stem=out / "B.without-ccm")
    finally:
        shutil.move(str(aside), str(w / "memory"))
    _meta("handoff", init, {"prompts": {"A": PROMPT_A, "B": PROMPT_B},
                            "design": "B runs twice at the SAME path; the only difference is the "
                                      "plugin being enabled and its memory/ directory being present."})


def scenario_guardian(root: Path) -> None:
    out = CAPTURES / "guardian"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "seed.plan.json").write_text(json.dumps(SEED_PLAN, indent=2), encoding="utf-8", newline="\n")
    w = fresh_copy(root, "tally-plan")
    print("[guardian] seeding memory/ + plan + directive through the CLI")
    init_memory(w)
    seed_log = mem(w, "plan-set", "--from-refiner", stdin=json.dumps(SEED_PLAN))
    seed_log += mem(w, "directive-add", SEED_DIRECTIVE["slug"], "--kind", SEED_DIRECTIVE["kind"],
                    "--quote", SEED_DIRECTIVE["quote"], "--demand", SEED_DIRECTIVE["demand"])
    (out / "seed.log.txt").write_text(seed_log, encoding="utf-8", newline="\n")
    print("[guardian] session C - cc-memory ON (plan + directive live)")
    init = run_claude(w, PROMPT_C, with_ccm=True, out_stem=out / "C.with-ccm")
    (out / "C.plan-status.txt").write_text(mem(w, "plan-status"), encoding="utf-8", newline="\n")
    (out / "C.directive-list.txt").write_text(mem(w, "directive-list", "--full"),
                                              encoding="utf-8", newline="\n")
    (out / "C.tree-after.txt").write_text(tree_listing(w), encoding="utf-8", newline="\n")
    save_artifacts(w, out, ("PLAN.md", "PROGRESS.md"))
    print("[guardian] session C - fresh copy, ALL plugins OFF")
    w2 = fresh_copy(root, "tally-noplan")
    run_claude(w2, PROMPT_C, with_ccm=False, out_stem=out / "C.without-ccm")
    (out / "C.without.tree-after.txt").write_text(tree_listing(w2), encoding="utf-8", newline="\n")
    _meta("guardian", init, {"prompts": {"C": PROMPT_C}, "seed_directive": SEED_DIRECTIVE,
                             "design": "Plan + directive seeded via the CLI before the session; "
                                       "the without-side is a fresh fixture copy with every plugin off."})


def rerender_captures() -> int:
    """Rebuild every <stem>.txt from its <stem>.stream.jsonl — no sessions run.
    The .txt is a projection of the stream; the stream is the provenance."""
    n = 0
    for stream in sorted(CAPTURES.glob("*/*.stream.jsonl")):
        events = []
        for line in stream.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                # why: same as run_claude — a non-JSON line is not an event
                continue
        stem = stream.name[: -len(".stream.jsonl")]
        with_ccm = ".with-ccm" in stem
        Path(str(stream.parent / stem) + ".txt").write_text(
            render(events, prompt="", with_ccm=with_ccm), encoding="utf-8", newline="\n")
        n += 1
        print("re-rendered %s/%s.txt (%d events)" % (stream.parent.name, stem, len(events)))
    return 0 if n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["handoff", "guardian"], help="run one scenario")
    ap.add_argument("--keep", action="store_true", help="keep the temporary work trees")
    ap.add_argument("--render-only", action="store_true",
                    help="re-render captures/*/*.txt from the stored streams; run nothing")
    args = ap.parse_args()
    if args.render_only:
        return rerender_captures()
    if shutil.which("claude") is None:
        print("claude not on PATH", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="ccm-demo-"))
    print("work root: %s" % root)
    try:
        if args.only in (None, "handoff"):
            scenario_handoff(root)
        if args.only in (None, "guardian"):
            scenario_guardian(root)
    finally:
        if args.keep:
            print("kept: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    print("captures: %s" % CAPTURES)
    return 0


if __name__ == "__main__":
    sys.exit(main())

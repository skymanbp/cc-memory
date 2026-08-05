#!/usr/bin/env python3
"""
cc-memory standalone installer.

Self-contained installer that extracts plugin files, installs the user-facing
Claude Code surfaces, and registers hooks. Used as a PyInstaller exe entry
point (built as a CONSOLE app - the --cli branch is the documented headless
path and must be able to print and return an exit code).

Where the payload comes from:
  frozen exe : sys._MEIPASS/cc_memory_files/<subdir>/   (python package)
               sys._MEIPASS/cc_memory_surfaces/<rel>    (commands/agents/skills)
               sys._MEIPASS/cc_memory_meta/hooks.json   (timeout source of truth)
  as script  : cc_memory/<subdir>/ and <repo>/{commands,agents,skills,hooks}/

What gets written:
  ~/.claude/hooks/cc-memory/<subdir>/        FLAT package tree (no cc_memory/ segment)
  ~/.claude/hooks/cc-memory/installed_surfaces.json   manifest for a precise uninstall
  ~/.claude/{commands,agents,skills}/        Claude Code surfaces (user scope)
  ~/.claude/settings.json                    hook registration

Output is deliberately ASCII-only: this is a stdlib-only bootstrap that runs
before the package (and core.encoding_setup) is importable, on consoles whose
codec we do not control.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Resolve bundled / source roots ────────────────────────────────────────


def _get_bundle_root():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "cc_memory_files"
    # As script: walk up from cc_memory/ui/installer.py to repo root,
    # then return cc_memory/ as the "source" mirror of the bundle.
    return Path(__file__).resolve().parent.parent  # cc_memory/


def _get_surface_root():
    """Root that holds commands/ agents/ skills/ (NOT part of the package)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "cc_memory_surfaces"
    # cc_memory/ui/installer.py -> repo root
    return Path(__file__).resolve().parent.parent.parent


BUNDLE_DIR = _get_bundle_root()
SURFACE_DIR = _get_surface_root()
CLAUDE_DIR = Path.home() / ".claude"
TARGET_DIR = CLAUDE_DIR / "hooks" / "cc-memory"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"
SURFACE_MANIFEST = TARGET_DIR / "installed_surfaces.json"

# Subdirectory contents (relative to cc_memory/ on disk OR cc_memory_files/ in exe)
SUBPACKAGE_FILES = {
    "":      ["__init__.py", "config.json"],
    "core":  ["__init__.py", "atomic.py", "auth.py", "consolidate.py", "db.py",
              "encoding_setup.py", "extractor.py", "idle.py", "logger.py",
              "modes.py", "plan.py", "privacy.py", "progress.py",
              "version.py"],
    "hooks": ["__init__.py", "consolidate_async.py", "post_tool_use.py",
              "pre_compact.py", "session_start.py", "stop.py", "user_prompt.py"],
    "llm":   ["__init__.py", "ccl_backend.py", "memory_writer.py"],
    "cli":   ["__init__.py", "mem.py", "plan.py"],
    "mcp":   ["__init__.py", "server.py"],
    "ui":    ["__init__.py", "dashboard.py", "installer.py", "web_viewer.py"],
}

# User-facing Claude Code surfaces. These are NOT part of the python package and
# do NOT go under TARGET_DIR - Claude Code only discovers them under ~/.claude/.
# Without them a standalone install has no /cc-mem, no /ccm-load, and no
# plan-refiner subagent, which makes PLAN.md unpopulatable (the v2.2 feature).
SURFACE_FILES = [
    "commands/cc-mem.md",
    "agents/plan-refiner.md",
    "agents/plan-guardian.md",
    "skills/ccm-load/SKILL.md",
    "skills/save-memories/SKILL.md",
]

# The 5 SYNC single-command hooks (one command each) + one async sibling.
# These timeouts are the FINAL wire values - no multiplier is applied anywhere.
# hooks/hooks.json is the source of truth and is read when available
# (_declared_hook_timeouts); this table is the fallback for a frozen/flat
# install where that file is absent, and it must stay numerically identical.
HOOK_SCRIPTS = {
    "PreCompact":       ("hooks/pre_compact.py", 120),
    "SessionStart":     ("hooks/session_start.py", 15),
    "Stop":             ("hooks/stop.py", 22),
    "PostToolUse":      ("hooks/post_tool_use.py", 8),
    "UserPromptSubmit": ("hooks/user_prompt.py", 8),
}
# PreCompact carries a SECOND, async command hook: background consolidation. It
# runs off the blocking compaction path so a slow consolidation can never
# surface as "Hook cancelled". 300s is a background deadline, not a UI budget.
ASYNC_HOOK = ("PreCompact", "hooks/consolidate_async.py", 300)

_SETTINGS_FIX_HINT = ("Fix that file (or move it aside) and re-run the "
                      "installer. Nothing has been installed.")

# Per-session temp markers cc-memory writes. They are keyed by SESSION id, not
# project, so this sweep cannot be scoped to one project - uninstall clears every
# cc-memory marker in the machine's temp dir. They are pure caches (turn counters
# / idle + nudge cooldowns) and regenerate on the next prompt, so the blast
# radius is one extra observer pass in any session that is live during an
# uninstall.
#
# EVERY prefix a hook writes must be listed here or those files leak forever.
# The writers, one per prefix (grep the hooks for tempfile.gettempdir to audit):
#   cc_mem_turns_        hooks/user_prompt.py  turn counter
#   cc_mem_prompt_       hooks/user_prompt.py  last user prompt, for the observer
#   cc_mem_eval_         hooks/stop.py         observer watermark
#   cc_mem_refine_       hooks/stop.py         plan-refiner nudge cooldown
#   cc_mem_idle_         core/idle.py          idle-reorg cooldown
#   cc_memory_reminded_  NO live writer - retired together with the
#                        "remember to call /save-memories" reminder spam (see
#                        the hooks/stop.py module docstring). Kept deliberately
#                        so an uninstall still sweeps markers an older install
#                        left in the temp dir.
_TEMP_MARKER_PREFIXES = ("cc_mem_turns_", "cc_mem_prompt_", "cc_mem_eval_",
                         "cc_mem_refine_", "cc_mem_idle_",
                         "cc_memory_reminded_")
# Marker suffix = hooks/stop.py:_safe_id(session_id) -> a 16-char session id with
# path separators replaced. Anything else in %TEMP% is not ours; leave it alone.
_TEMP_MARKER_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ── Version (single-sourced; never a literal) ──────────────────────────────

def _detect_version():
    """Version of the payload being installed. Never a literal.

    Probes the BUNDLE first (that is the code actually being written to disk,
    which may differ from any importable cc_memory on sys.path), then falls back
    to the package import for the wheel/dev case. core/version.py is the single
    source of truth; cc_memory/__init__.py is the pre-2.5 location.
    """
    for probe in (BUNDLE_DIR / "core" / "version.py", BUNDLE_DIR / "__init__.py"):
        try:
            txt = probe.read_text(encoding="utf-8")
        except OSError:
            # why: probe absent in this layout (or unreadable) - try the next
            continue
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', txt, re.M)
        if m:
            return m.group(1)
    try:
        from cc_memory import __version__ as _v
        if isinstance(_v, str) and _v:
            return _v
    except Exception:
        # why: the installer normally runs standalone (frozen exe, or a flat
        # install where no cc_memory package exists) - a cosmetic banner version
        # must never abort an install
        pass
    return "unknown"


_VERSION = _detect_version()


# ── Tkinter GUI (preferred when no --cli) ──────────────────────────────────

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _HAS_TK = True
except ImportError:
    _HAS_TK = False


# ── config.json preservation ───────────────────────────────────────────────
#
# config.json is the ONE payload file the user is meant to EDIT:
# `excluded_projects` is the plugin's only privacy opt-out, and `ccl.*` is the
# local-fallback switch. Before v2.5.1 a reinstall shutil.copy2'd the shipped
# file straight over it, so running the installer again to upgrade silently
# reset every setting to the defaults - no warning, no backup. It is now
# MERGED: the shipped file supplies defaults, the user's values win, and keys a
# new version introduces are added.
#
# Two top-level keys are PAYLOAD-owned and are refreshed on every install:
#   version : cli/mem.py:64-70 and mcp/server.py:133-139 read it as the
#             LAST-RESORT version for a flat standalone install - exactly the
#             layout this installer writes. Preserving it would make /cc-mem
#             report the version the user just upgraded away from.
#   notes   : pure documentation (the readers table + removed_keys). A kept
#             copy would describe the PREVIOUS release's code.
_CONFIG_NAME = "config.json"
_CONFIG_PAYLOAD_KEYS = ("version", "notes")


def _read_json_config(path):
    """Parse a config.json. Returns (dict, None) or (None, reason).

    Same discipline as _read_settings: never raises, and the reason is a
    sentence the user can act on.
    """
    try:
        # utf-8-sig, not utf-8: a config.json re-saved from a PowerShell prompt
        # carries a BOM, which plain json.loads rejects. The write path below
        # still emits no BOM.
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return None, f"cannot be read ({e})"
    except UnicodeDecodeError as e:
        return None, (f"is not UTF-8 text ({e}); re-save it with "
                      f"'Set-Content -Encoding utf8' or a UTF-8 editor")
    if not raw.strip():
        return {}, None  # empty file: nothing to preserve
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, (f"is not valid JSON ({e}) - // comments and trailing "
                      f"commas are not allowed")
    if not isinstance(data, dict):
        return None, (f"contains a top-level JSON {type(data).__name__}, not an "
                      f"object")
    return data, None


def _deep_merge(shipped, existing):
    """Shipped defaults UNDER the user's values.

      key only in shipped        -> added (a new version's new tunable)
      key in both, both dicts    -> merged recursively
      key in both, anything else -> the USER's value wins
      key only in existing       -> kept (an unrecognised key is not ours to
                                    drop; it is reported instead)
    """
    out = dict(shipped)
    for k, v in existing.items():
        sv = out.get(k)
        out[k] = (_deep_merge(sv, v)
                  if isinstance(sv, dict) and isinstance(v, dict) else v)
    return out


def _merge_config(shipped, existing):
    """_deep_merge, minus the payload-owned top-level keys (always shipped)."""
    user = {k: v for k, v in existing.items() if k not in _CONFIG_PAYLOAD_KEYS}
    return _deep_merge(shipped, user)


def _config_leaves(obj, prefix=""):
    """{dotted.path: value} for every LEAF of a nested config dict."""
    out = {}
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            out.update(_config_leaves(v, p))
        else:
            out[p] = v
    return out


def _brief_list(paths, limit=6):
    paths = sorted(paths)
    return ", ".join(paths[:limit]) + (
        f", +{len(paths) - limit} more" if len(paths) > limit else "")


def _install_config(src, dst, log_fn=print):
    """Install config.json WITHOUT discarding the settings already there."""
    if not dst.exists():
        shutil.copy2(str(src), str(dst))
        log_fn(f"  Copied: {_CONFIG_NAME}")
        return

    shipped, ship_err = _read_json_config(src)
    if ship_err is not None:
        # why: the PAYLOAD's own config is unreadable, so there is nothing to
        # merge against - fall back to the byte copy this function replaced
        log_fn(f"  [WARN] the bundled {_CONFIG_NAME} {ship_err}; copying it "
               f"verbatim")
        shutil.copy2(str(src), str(dst))
        return

    existing, err = _read_json_config(dst)
    if err is not None:
        bak = dst.with_name(dst.name + ".bak")
        try:
            shutil.copy2(str(dst), str(bak))
            saved = f"saved as {bak.name}"
        except OSError as e:
            saved = f"NOT saved ({e})"
        log_fn(f"  [WARN] your installed {_CONFIG_NAME} {err}")
        log_fn(f"  [WARN] its settings could NOT be preserved - old file "
               f"{saved}, defaults installed. Re-apply excluded_projects / "
               f"ccl by hand.")
        shutil.copy2(str(src), str(dst))
        return

    merged = _merge_config(shipped, existing)
    if merged == shipped:
        # nothing customised: keep the shipped bytes (comments, key order)
        shutil.copy2(str(src), str(dst))
        log_fn(f"  Copied: {_CONFIG_NAME}")
        return
    try:
        dst.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    except OSError as e:
        # why: a failed write must leave the user's settings intact rather than
        # half-write over them; the rest of the install is unaffected
        log_fn(f"  [WARN] could not write the merged {_CONFIG_NAME} ({e}) - "
               f"your existing file was left untouched")
        return

    ship_leaves = _config_leaves(shipped)
    user_leaves = _config_leaves(existing)
    kept = [p for p, v in _config_leaves(merged).items()
            if p in ship_leaves and ship_leaves[p] != v]
    added = [p for p in ship_leaves if p not in user_leaves
             and p.split(".")[0] not in _CONFIG_PAYLOAD_KEYS]
    stale = [p for p in user_leaves if p not in ship_leaves
             and p.split(".")[0] not in _CONFIG_PAYLOAD_KEYS]
    log_fn(f"  Merged: {_CONFIG_NAME} (your settings preserved)")
    if kept:
        log_fn(f"    kept {len(kept)} customised value(s): {_brief_list(kept)}")
    if added:
        log_fn(f"    added {len(added)} new key(s) from v{_VERSION}: "
               f"{_brief_list(added)}")
    if stale:
        log_fn(f"    [WARN] kept {len(stale)} key(s) v{_VERSION} no longer "
               f"reads: {_brief_list(stale)}")


# ── Payload copy ───────────────────────────────────────────────────────────

def _copy_subpackages(target_dir, log_fn=print):
    """Copy each subpackage's files from BUNDLE_DIR into target_dir/<subdir>/.

    Every file is an overwrite EXCEPT config.json, which is merged so a
    reinstall/upgrade cannot wipe the user's settings (see _install_config).
    """
    n_copied = 0
    n_skipped = 0
    for subdir, files in SUBPACKAGE_FILES.items():
        src_root = BUNDLE_DIR / subdir if subdir else BUNDLE_DIR
        dst_root = target_dir / subdir if subdir else target_dir
        dst_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = src_root / f
            dst = dst_root / f
            label = f"{subdir}/{f}" if subdir else f
            if not src.exists():
                log_fn(f"  SKIP (not found): {label}")
                n_skipped += 1
                continue
            if dst.exists() and src.resolve() == dst.resolve():
                # why: running the ALREADY-INSTALLED copy of this file would make
                # shutil.copy2 raise SameFileError; it is already up to date
                n_copied += 1
                continue
            if not subdir and f == _CONFIG_NAME:
                _install_config(src, dst, log_fn)
                n_copied += 1
                continue
            shutil.copy2(str(src), str(dst))
            n_copied += 1
            log_fn(f"  Copied: {label}")
    return n_copied, n_skipped


def _prune_stale(target_dir, log_fn=print):
    """Delete .py files under target_dir that this version no longer ships.

    _copy_subpackages only overwrites, so a module deleted upstream used to
    linger in an upgraded install and stay importable (as did the whole v2.0
    flat layout). Two passes:
      1. the MANAGED dirs (SUBPACKAGE_FILES keys) - drop every *.py this build
         does not list;
      2. every OTHER subdirectory of target_dir except logs/ - i.e. a whole
         subpackage that was deleted upstream (pass 1 iterates SUBPACKAGE_FILES
         keys, so it could never see one). Its *.py are dropped and the
         directory skeleton is removed once empty.
    Only *.py is ever unlinked. logs/, the surface manifest, __pycache__ and
    every other non-python file are left alone (a leftover __pycache__ keeps
    its directory alive - harmless, since a .pyc there is not importable
    without its source).
    """
    n = 0
    for subdir, files in SUBPACKAGE_FILES.items():
        d = target_dir / subdir if subdir else target_dir
        if not d.is_dir():
            continue
        keep = set(files)
        for p in sorted(d.glob("*.py")):
            if p.name in keep:
                continue
            try:
                p.unlink()
                n += 1
                log_fn(f"  Pruned stale: {subdir + '/' if subdir else ''}{p.name}")
            except OSError as e:
                log_fn(f"  [WARN] could not prune {p.name}: {e}")

    managed = {s for s in SUBPACKAGE_FILES if s} | {"logs"}
    for d in sorted(target_dir.iterdir()) if target_dir.is_dir() else []:
        if not d.is_dir() or d.name in managed:
            continue
        for p in sorted(d.rglob("*.py")):
            try:
                rel = p.relative_to(target_dir).as_posix()
                p.unlink()
                n += 1
                log_fn(f"  Pruned stale subpackage: {rel}")
            except OSError as e:
                log_fn(f"  [WARN] could not prune {p.name}: {e}")
        for sub in sorted(d.rglob("*"), reverse=True) + [d]:  # deepest first
            try:
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
            except OSError:
                # why: still holds something we deliberately do not delete (a
                # non-python file, a __pycache__), or is locked. Pruning is
                # best-effort and must never abort an install.
                continue
    return n


def _read_surface_manifest():
    """Previously installed surface list (relative paths), or None if unrecorded.

    None and [] are DIFFERENT answers and callers must not conflate them:
      None -> no readable manifest at all (fresh machine, or an install from
              before the manifest existed) - the installer knows nothing;
      []   -> a manifest that records zero installed surfaces (every copy
              failed) - the installer knows it installed nothing.
    _remove_surfaces used to treat both as falsy and delete all 5 surface names
    by hand in either case.
    """
    try:
        data = json.loads(SURFACE_MANIFEST.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        # why: absent or corrupt manifest - "nothing recorded", not "recorded
        # nothing"; the caller decides what to do with that
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return [r for r in data["files"] if isinstance(r, str)]
    return None


def _copy_surfaces(log_fn=print):
    """Install commands/ agents/ skills/ into ~/.claude (user scope)."""
    previously_ours = set(_read_surface_manifest() or [])
    written = []
    n_skipped = 0
    for rel in SURFACE_FILES:
        src = SURFACE_DIR / rel
        if not src.exists():
            log_fn(f"  SKIP (not bundled): {rel}")
            n_skipped += 1
            continue
        dst = CLAUDE_DIR / rel
        if dst.exists() and rel not in previously_ours:
            log_fn(f"  [WARN] overwriting an existing file not installed by "
                   f"cc-memory: {rel}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        except OSError as e:
            log_fn(f"  [WARN] could not install {rel}: {e}")
            n_skipped += 1
            continue
        written.append(rel)
        log_fn(f"  Surface: {rel}")

    try:
        SURFACE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        SURFACE_MANIFEST.write_text(json.dumps(
            {"version": _VERSION, "root": str(CLAUDE_DIR),
             "files": sorted(written)}, indent=2), encoding="utf-8")
    except OSError as e:
        # why: the manifest only makes uninstall precise; failing to write it
        # must not abort an otherwise successful install
        log_fn(f"  [WARN] surface manifest not written: {e}")
    return len(written), n_skipped


def _remove_surfaces(log_fn=print):
    """Delete installed surfaces BY NAME. ~/.claude/{commands,agents,skills}
    hold the user's own files - never rmtree them."""
    rels = _read_surface_manifest()
    if rels is None:
        log_fn("[  ] No surface manifest - falling back to this build's "
               "surface list")
        rels = list(SURFACE_FILES)
    elif not rels:
        log_fn(f"[  ] Surface manifest records 0 installed surfaces - "
               f"leaving {CLAUDE_DIR} untouched")
    n = 0
    for rel in rels:
        if rel not in SURFACE_FILES:
            # why: defence in depth - never delete a path this build does not
            # ship, even if a manifest names it
            continue
        p = CLAUDE_DIR / rel
        try:
            if p.is_file():
                p.unlink()
                n += 1
                log_fn(f"  Removed surface: {rel}")
            parent = p.parent
            # a skills/<name>/ dir we created is ours to remove once empty;
            # commands/ and agents/ are shared with the user, so leave them.
            if (parent.is_dir() and parent.parent.name == "skills"
                    and parent.parent.parent == CLAUDE_DIR
                    and not any(parent.iterdir())):
                parent.rmdir()
        except OSError as e:
            log_fn(f"  [WARN] could not remove {rel}: {e}")
    if n:
        log_fn(f"[OK] Removed {n} Claude Code surfaces from {CLAUDE_DIR}")
    return n


def _remove_target_dir(log_fn=print):
    """Remove the installed package, PRESERVING logs/.

    On a marketplace install ~/.claude/hooks/cc-memory holds *only* logs/ (the
    logger's output target), so an rmtree here used to delete a user's whole
    log history on an uninstall that was otherwise a no-op.
    """
    if not TARGET_DIR.exists():
        log_fn("[  ] Plugin directory not found")
        return
    kept = []
    for child in sorted(TARGET_DIR.iterdir()):
        if child.name == "logs":
            kept.append(child.name)
            continue
        try:
            if child.is_dir():
                shutil.rmtree(str(child))
            else:
                child.unlink()
        except OSError as e:
            log_fn(f"[ERR] Could not remove {child.name}: {e}")
    if kept:
        log_fn(f"[OK] Cleared {TARGET_DIR} (kept {'/, '.join(kept)}/)")
        return
    try:
        TARGET_DIR.rmdir()
        log_fn(f"[OK] Removed {TARGET_DIR}")
    except OSError:
        # why: something we could not delete is still in there; it was reported
        # above and the directory itself is harmless
        log_fn(f"[OK] Cleared {TARGET_DIR}")


def _sweep_temp_markers(log_fn=print):
    """Delete cc-memory's per-session temp markers (see _TEMP_MARKER_PREFIXES)."""
    tmp = Path(tempfile.gettempdir())
    n = 0
    for prefix in _TEMP_MARKER_PREFIXES:
        for f in tmp.glob(prefix + "*"):
            if not f.is_file():
                continue
            if not _TEMP_MARKER_SUFFIX_RE.match(f.name[len(prefix):]):
                continue
            try:
                f.unlink()
                n += 1
            except OSError:
                # why: marker already gone, or held by a live session; the sweep
                # is best-effort and the file regenerates anyway
                continue
    if n:
        log_fn(f"[OK] Removed {n} temp session markers")
    return n


# ── Hook registration ──────────────────────────────────────────────────────

def _detect_python_cmd():
    """Pick 'python3' or 'python' by RUNNING the candidate, not by probing.

    shutil.which('python3') on Windows resolves to a 0-byte App Execution Alias
    when Store Python is not installed; the resulting hook command fails
    silently (Claude Code shows no UI for a missing-command hook).
    """
    for cand in ("python3", "python"):
        exe = shutil.which(cand)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import sys;print(sys.version_info[0])"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            # why: candidate is a stub/alias that cannot start (or hangs);
            # fall through to the next candidate
            continue
        if r.returncode == 0 and r.stdout.strip() == "3":
            return cand
    return "python3"


def _hooks_json_candidates():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys._MEIPASS) / "cc_memory_meta" / "hooks.json")
    paths.append(BUNDLE_DIR.parent / "hooks" / "hooks.json")  # dev checkout
    return paths


def _declared_hook_timeouts():
    """Timeouts declared by hooks/hooks.json: {event: {is_async: timeout}}.

    hooks/hooks.json is the single source of truth for hook timeouts; HOOK_SCRIPTS
    / ASYNC_HOOK are the fallback for installs where that file is not present,
    and carry the same numbers.
    """
    for p in _hooks_json_candidates():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # why: hooks.json is absent in a flat/frozen layout, or unparseable;
            # the literal table below carries the same values
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        out = {}
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                entries = group.get("hooks") if isinstance(group, dict) else None
                for entry in entries if isinstance(entries, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    t = entry.get("timeout")
                    if isinstance(t, int) and not isinstance(t, bool):
                        out.setdefault(event, {})[bool(entry.get("async"))] = t
        if out:
            return out, p
    return {}, None


def _make_hooks_config(target_dir):
    declared, _src = _declared_hook_timeouts()
    python_cmd = _detect_python_cmd()

    def _timeout(event, is_async, fallback):
        return declared.get(event, {}).get(bool(is_async), fallback)

    def _cmd(rel_script, timeout_s, is_async=False):
        c = {
            "type": "command",
            "command": f'{python_cmd} "{target_dir / rel_script}"',
            "timeout": int(timeout_s),
        }
        if is_async:
            c["async"] = True
        return c

    config = {ev: [{"matcher": "", "hooks": [_cmd(script, _timeout(ev, False, t))]}]
              for ev, (script, t) in HOOK_SCRIPTS.items()}
    ev, script, t = ASYNC_HOOK
    config[ev][0]["hooks"].append(
        _cmd(script, _timeout(ev, True, t), is_async=True))
    return config


def _read_settings():
    """Parse settings.json. Returns (settings_dict, error_or_None).

    Called BEFORE anything is copied: a settings.json we cannot parse used to
    crash the installer at the very last step, leaving 33 files on disk with no
    hooks registered (and, under a --windowed exe, an invisible modal dialog).
    """
    if not SETTINGS_PATH.exists():
        return {}, None
    try:
        # utf-8-sig, not utf-8: PowerShell's > and Out-File write a UTF-8 BOM by
        # default, so a settings.json the user once edited from a PS prompt used
        # to fail json.loads ("Unexpected UTF-8 BOM") and lock the installer out
        # entirely. The write path below still emits no BOM.
        raw = SETTINGS_PATH.read_text(encoding="utf-8-sig")
    except OSError as e:
        return None, f"cannot be read ({e})"
    except UnicodeDecodeError as e:
        return None, (f"is not UTF-8 text ({e}). Claude Code settings must be "
                      f"UTF-8; re-save it with 'Set-Content -Encoding utf8' or "
                      f"an editor set to UTF-8")
    if not raw.strip():
        return {}, None  # empty file: nothing to preserve
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, (f"is not valid JSON ({e}). Claude Code settings must be "
                      f"strict JSON - // comments and trailing commas are not "
                      f"allowed")
    if not isinstance(data, dict):
        return None, (f"contains a top-level JSON {type(data).__name__}, not an "
                      f"object")
    return data, None


# What makes a hook entry OURS. A bare "cc-memory" substring is not enough: this
# project's own checkout directory is named cc-memory, so a perfectly ordinary
# user hook such as
#     python audit.py --repo D:/Projects/cc-memory
# used to be deleted silently by BOTH install and uninstall. A command is ours
# only when it RUNS one of our hook scripts out of a cc-memory / cc_memory
# directory, which covers every layout this installer has ever written:
#   v2.0 flat   .../cc-memory/stop.py
#   v2.1+ flat  .../cc-memory/hooks/stop.py   <- what _make_hooks_config emits
#   nested      .../cc-memory/cc_memory/hooks/stop.py
_HOOK_SCRIPT_NAMES = tuple(sorted(
    {Path(rel).name for rel, _t in HOOK_SCRIPTS.values()}
    | {Path(ASYNC_HOOK[1]).name}))
_CCM_COMMAND_RE = re.compile(
    r"cc[-_]memory[/\\](?:cc_memory[/\\])?(?:hooks[/\\])?(?:"
    + "|".join(re.escape(n) for n in _HOOK_SCRIPT_NAMES) + r")\b",
    re.IGNORECASE)
# Everything else that merely mentions the name: KEPT, and reported.
_CCM_MENTION_RE = re.compile(r"cc[-_]memory", re.IGNORECASE)


def _group_commands(matcher_group):
    """Every command STRING in a matcher group. Type-safe at every level.

    settings.json is user-editable, so nothing about its shape is guaranteed.
    Each of these used to be an uncaught TypeError that killed install AND
    uninstall (and, in the GUI, an invisible one):
      {"hooks": null}  -> .get("hooks", []) does NOT default an explicit null
      {"hooks": 5}     -> the container need not be a list
      {"command": 123} -> the command need not be a string
    """
    if not isinstance(matcher_group, dict):
        return []
    entries = matcher_group.get("hooks")
    if not isinstance(entries, list):
        return []
    return [h["command"] for h in entries
            if isinstance(h, dict) and isinstance(h.get("command"), str)]


def _is_ccm_group(matcher_group):
    """True iff the group runs one of OUR hook scripts. Never raises."""
    return any(_CCM_COMMAND_RE.search(c) for c in _group_commands(matcher_group))


def _ambiguous_commands(matcher_group):
    """Commands that MENTION cc-memory but do not run a cc-memory hook script."""
    return [c for c in _group_commands(matcher_group)
            if _CCM_MENTION_RE.search(c) and not _CCM_COMMAND_RE.search(c)]


def _warn_ambiguous(pairs, log_fn):
    """Report kept-but-cc-memory-mentioning hooks. Deleting them silently is
    exactly the data loss this installer must not commit."""
    for event, cmd in pairs:
        log_fn(f"  [WARN] {event}: KEPT a hook that mentions cc-memory but does "
               f"not run a cc-memory hook script - remove it by hand if it is "
               f"stale: {cmd}")


def _marketplace_registration(settings):
    """enabledPlugins key of an ACTIVE marketplace/dev-checkout install, or None."""
    plugins = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    if isinstance(plugins, dict):
        for key, val in plugins.items():
            if isinstance(key, str) and key.split("@")[0] == "cc-memory" and val:
                return key
    return None


def _marketplace_warning_lines(key):
    return [
        f'Claude Code already loads cc-memory as a plugin: settings.json',
        f'enabledPlugins has "{key}": true.',
        "",
        "A standalone install registers the SAME hooks a second time, in",
        'settings.json["hooks"]. Two independent hook registries very likely',
        "means every hook fires twice (inferred from the two registrations -",
        "not directly observed).",
    ]


def _settings_fingerprint():
    """Exact identity of settings.json on disk right now, or None if absent.

    A content digest, not (mtime, size): both are too coarse to detect a
    concurrent write that happens to preserve the length, and NTFS mtime
    granularity is worse than the window being guarded.
    """
    try:
        return hashlib.sha256(SETTINGS_PATH.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        # why: unreadable is not "unchanged" — returning a sentinel that can
        # never equal a real digest makes the caller treat it as a conflict,
        # which is the safe direction.
        return "<unreadable>"


def _write_settings_json(settings, log_fn=print, expect=None):
    """Replace settings.json ATOMICALLY, keeping a backup of what was there.

    `expect` is the digest the caller saw when it READ the file. If the file on
    disk no longer matches it, someone wrote in between and this write would
    silently discard their change — so it is refused with a `False` return and
    the caller re-merges onto the newer content. This is a compare-and-swap,
    and it needs no cooperation from the other writer, which matters because
    Claude Code takes no lock on this file and neither can we.

    This is the user's GLOBAL Claude Code config. Both write paths used to call
    SETTINGS_PATH.write_text(), which TRUNCATES before writing a byte: a watcher
    on a single, uncontended `--cli` install caught the file at 0 bytes, so a
    crash / kill / power loss inside that window left the user with an empty
    global config and nothing to restore from. Write to a sibling temp file and
    rename over the target instead - Path.replace IS os.replace, the same
    discipline hooks/session_start.py:_write_inject_manifest and
    hooks/pre_compact.py:_write_attempt already use for far less valuable files.

    The .bak is best-effort and deliberately NOT named settings.json.bak: that
    name belongs to the user, who may well have made one by hand.

    The rename is not unconditional, because on Windows it CANNOT be. MoveFileEx
    needs DELETE access on the destination, and any process holding
    settings.json open without FILE_SHARE_DELETE - an editor, an AV scanner,
    Claude Code itself reading it at that instant - makes it fail with
    [WinError 5] (the same failure hooks/pre_compact.py hits on its attempt
    marker). Measured: a reader polling every 50-500 ms never triggered it in
    10 installs; a reader spinning continuously triggered it every time. An
    ordinary in-place write survives that case because Python opens with
    _SH_DENYNO. So: retry the rename a few times, and only if the OS keeps
    refusing fall back to the old truncating write - which is now recoverable,
    since the .bak above was taken first. Losing the user's hook registration
    is a worse outcome than losing atomicity for one write.
    """
    payload = json.dumps(settings, indent=2, ensure_ascii=False)
    bak = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".cc-memory.bak")
    tmp = None
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SETTINGS_PATH.exists():
            try:
                shutil.copy2(str(SETTINGS_PATH), str(bak))
            except OSError as e:
                # why: the backup is a courtesy copy - failing to take it must
                # not block the registration the user actually asked for
                log_fn(f"  [WARN] could not back up {SETTINGS_PATH.name} to "
                       f"{bak.name}: {e}")
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False,
                dir=str(SETTINGS_PATH.parent),
                prefix=SETTINGS_PATH.name + ".", suffix=".tmp") as fh:
            tmp = Path(fh.name)
            fh.write(payload)
        if expect is not None and _settings_fingerprint() != expect:
            # why: someone wrote settings.json between our read and this
            # moment. Renaming now would discard their edit with rc=0 and
            # "installation complete!" — the exact lost update this parameter
            # exists to stop. Refuse; the caller re-reads and re-merges.
            log_fn(f"  [WARN] {SETTINGS_PATH.name} changed underneath us; "
                   f"re-merging onto the newer contents")
            return False
        last = None
        for _attempt in range(5):
            try:
                tmp.replace(SETTINGS_PATH)
                tmp = None
                break
            except OSError as e:
                # why: a sharing violation from a transient reader clears on its
                # own; retrying costs nothing and keeps the write atomic
                last = e
        if tmp is not None:
            log_fn(f"  [WARN] the OS refused an atomic replace of "
                   f"{SETTINGS_PATH.name} ({last}); writing in place instead - "
                   f"{bak.name} holds the previous contents")
            SETTINGS_PATH.write_text(payload, encoding="utf-8")
    except OSError as e:
        log_fn(f"[ERR] Could not write {SETTINGS_PATH}: {e}")
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                # why: a temp file left beside settings.json is litter, not a
                # failure; the real error is already reported above
                pass
    return True


def _merge_into_settings(hooks_config, log_fn=print):
    """Register cc-memory's hooks in settings.json. Returns True on success.

    settings.json is READ HERE, immediately before the merge, and never handed
    in by a caller. cli_install used to pass the copy it validated at step
    [0/3] - measured 0.45-0.54 s earlier, i.e. the whole install - and this
    function wrote that stale dict back at the end: everything Claude Code
    persisted in between (a /permissions approval, a model change, an env var)
    was silently reverted, with rc=0 and "installation complete!". Six of six
    trials lost the concurrent edit.

    Re-reading narrows the window to the microseconds between the read below
    and the rename inside _write_settings_json. Do not reintroduce a
    caller-supplied settings dict.

    v2.5.3 closes what re-reading left open. Narrowing a lost-update window is
    not the same as detecting one, and v2.5.2 shipped the residual as a known
    limit. The read now takes a content digest, `_write_settings_json` refuses
    to rename if the file no longer matches it, and this function RETRIES the
    whole read-merge-write on the newer contents. A concurrent writer therefore
    cannot be silently clobbered; it can only make us do the merge again.
    Bounded at `_MERGE_ATTEMPTS` because a writer looping faster than we can
    merge is a broken peer, not a race to win — and this runs inside an
    installer a human is watching.
    """
    for attempt in range(_MERGE_ATTEMPTS):
        ok, retry = _merge_once(hooks_config, log_fn)
        if not retry:
            return ok
        if attempt + 1 < _MERGE_ATTEMPTS:
            time.sleep(0.05 * (attempt + 1))
    log_fn(f"[ERR] {SETTINGS_PATH.name} kept changing underneath the installer "
           f"({_MERGE_ATTEMPTS} attempts). Close whatever is writing it "
           f"(another installer? Claude Code mid-write?) and re-run.")
    return False


# Bounded on purpose — see _merge_into_settings.
_MERGE_ATTEMPTS = 4


def _merge_once(hooks_config, log_fn=print):
    """One read-merge-write cycle. Returns (ok, should_retry)."""
    before = _settings_fingerprint()
    settings, err = _read_settings()
    if err is not None:
        log_fn(f"[ERR] {SETTINGS_PATH} {err}")
        return False, False

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            log_fn(f'[WARN] settings.json "hooks" was a {type(hooks).__name__}, '
                   f"not an object - replacing it")
        hooks = {}
    settings["hooks"] = hooks

    ambiguous = []
    for event, hook_list in hooks_config.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            log_fn(f'[WARN] settings.json hooks["{event}"] was a '
                   f"{type(existing).__name__}, not a list - discarding it")
            existing = []
        kept, n_malformed = [], 0
        for mg in existing:
            if not isinstance(mg, dict):
                n_malformed += 1  # a bare string used to be iterated per-character
                continue
            if _is_ccm_group(mg):
                continue  # strip prior cc-memory entries so re-install upgrades
            ambiguous += [(event, c) for c in _ambiguous_commands(mg)]
            kept.append(mg)
        if n_malformed:
            log_fn(f"  [WARN] {event}: dropped {n_malformed} malformed "
                   f"(non-object) hook entr{'y' if n_malformed == 1 else 'ies'}")
        hooks[event] = kept + hook_list
        log_fn(f"  {event}: {len(kept)} kept + {len(hook_list)} cc-memory")
    _warn_ambiguous(ambiguous, log_fn)

    if _write_settings_json(settings, log_fn, expect=before):
        return True, False
    # Distinguish "someone else wrote it" (worth redoing) from a real write
    # failure (already reported by _write_settings_json, and redoing it would
    # just print the same error again).
    return False, _settings_fingerprint() != before


def _uninstall_settings(log_fn=print):
    """Remove cc-memory entries from settings.json. Returns True on success.

    Compare-and-swap protected exactly like the install path (v2.5.3): an
    uninstall that discards a concurrent /permissions approval is no better
    than an install that does.
    """
    for attempt in range(_MERGE_ATTEMPTS):
        ok, retry = _uninstall_settings_once(log_fn)
        if not retry:
            return ok
        if attempt + 1 < _MERGE_ATTEMPTS:
            time.sleep(0.05 * (attempt + 1))
    log_fn(f"[ERR] {SETTINGS_PATH.name} kept changing underneath the "
           f"uninstaller; close whatever is writing it and re-run.")
    return False


def _uninstall_settings_once(log_fn=print):
    """One read-merge-write cycle of the uninstall. Returns (ok, should_retry)."""
    if not SETTINGS_PATH.exists():
        log_fn("[  ] settings.json not found - nothing to remove")
        return True, False
    before = _settings_fingerprint()
    settings, err = _read_settings()
    if err is not None:
        log_fn(f"[ERR] {SETTINGS_PATH} {err}; manual cleanup needed")
        return False, False

    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        ambiguous = []
        for event in list(hooks.keys()):
            hook_list = hooks[event]
            if not isinstance(hook_list, list):
                continue  # not ours and not parseable - leave the user's value
            # keep everything that is not demonstrably ours (malformed entries
            # included: uninstall must not be a data-loss event)
            cleaned = [mg for mg in hook_list if not _is_ccm_group(mg)]
            for mg in cleaned:
                ambiguous += [(event, c) for c in _ambiguous_commands(mg)]
            if cleaned:
                hooks[event] = cleaned
            else:
                del hooks[event]
        _warn_ambiguous(ambiguous, log_fn)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)  # don't leave an empty "hooks": {}
    elif hooks is not None:
        log_fn(f'[WARN] settings.json "hooks" is a {type(hooks).__name__} - '
               f"left untouched")

    # Only prune additionalDirectories if it already exists; the installer never
    # writes that key, so creating it here would be pure litter. Match the
    # INSTALL DIR, not the substring "cc-memory": a user whose project is named
    # cc-memory (this project's own checkout is) keeps their entry, with a
    # warning, instead of having it silently deleted.
    perms = settings.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("additionalDirectories"), list):
        _target = str(TARGET_DIR).replace("\\", "/").rstrip("/").lower()
        _kept_dirs = []
        for d in perms["additionalDirectories"]:
            if str(d).replace("\\", "/").rstrip("/").lower() == _target:
                continue
            if _CCM_MENTION_RE.search(str(d)):
                log_fn(f"  [WARN] KEPT permissions.additionalDirectories entry "
                       f"that mentions cc-memory but is not the install dir: {d}")
            _kept_dirs.append(d)
        perms["additionalDirectories"] = _kept_dirs

    # Same atomic-with-backup write as install: an uninstall that truncates the
    # user's global config and then dies is not a better failure than an
    # install that does.
    if not _write_settings_json(settings, log_fn, expect=before):
        return False, _settings_fingerprint() != before
    log_fn("[OK] Removed cc-memory entries from settings.json")

    key = _marketplace_registration(settings)
    if key:
        log_fn(f'[NOTE] settings.json still has enabledPlugins["{key}"] = true.')
        log_fn("       That is the marketplace/dev-checkout install, which this")
        log_fn("       uninstaller does not manage - disable it with /plugin.")
    return True, False


def _init_project(project_path, log_fn=print):
    """Create memory/ + DB + .gitignore in the given project directory."""
    project = Path(project_path)
    if not project.exists():
        raise FileNotFoundError(project)
    memory_dir = project / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "topics").mkdir(exist_ok=True)

    # Bootstrap DB using the bundled db.py
    src_root = BUNDLE_DIR
    sys.path.insert(0, str(src_root))
    try:
        from core.db import MemoryDB
    except ImportError:
        # Fallback: maybe the bundle root IS the package and db.py is at core/db.py
        sys.path.insert(0, str(src_root / "core"))
        from db import MemoryDB

    db = MemoryDB(memory_dir / "memory.db")
    db.upsert_project(str(project))
    log_fn(f"[OK] DB initialized at {memory_dir / 'memory.db'}")

    # Literal copy: this installer is a stdlib-only bootstrap that runs before
    # the package is importable. It must mirror BOTH halves of
    # core.progress.ensure_memory_gitignore - the LINE LIST
    # (core.progress.MEMORY_GITIGNORE_LINES, same lines, same order) and the
    # APPEND SEMANTICS below. Only the list was ever mirrored: this copy did
    # open(gi, "a") + "\n".join(missing), so against an existing .gitignore
    # whose last line had no trailing newline it produced
    #     sessions/# cc-memory: generated state, not content
    # - fusing the user's last rule with our first comment and DESTROYING that
    # rule (measured: `sessions/` gone, archived transcripts git-trackable
    # until the next hook self-healed it). The three-line read / normalise /
    # write shape below is deliberately identical to core/progress.py:70-76 so
    # the two can be diffed by eye. skills/ccm-load/SKILL.md is copy #3 and
    # carries the same shape.
    gi = memory_dir / ".gitignore"
    _ignore_lines = [
        "# cc-memory: generated state, not content",
        "memory.db", "memory.db-wal", "memory.db-shm", "sessions/",
        ".last_save.json", ".last_inject.json", ".last_consolidation.json",
        ".consolidation.lock", ".pre_compact_attempt.json",
        ".plan_raw.md", ".plan_history/", "*.tmp",
    ]
    try:
        _existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        _have = {ln.strip() for ln in _existing.splitlines()}
        _missing = [ln for ln in _ignore_lines if ln not in _have]
        if _missing:
            _prefix = (_existing if _existing.endswith("\n") or not _existing
                       else _existing + "\n")
            gi.write_text(_prefix + "\n".join(_missing) + "\n", encoding="utf-8")
            log_fn(f"[OK] .gitignore written ({len(_missing)} lines)")
    except OSError as _e:
        # why: .gitignore is a courtesy to the user's VCS; failing to write it
        # must not abort an otherwise successful install
        log_fn(f"[WARN] .gitignore not written: {_e}")


# ── GUI ─────────────────────────────────────────────────────────────────────

class Installer:
    def __init__(self, root):
        self.root = root
        self.root.title(f"cc-memory Installer (v{_VERSION})")
        self.root.geometry("680x600")
        self.root.resizable(False, False)
        self._build_ui()
        try:
            self._pre_check()
        except Exception as e:
            # why: main() constructs Installer(root) with NO try/except, and a
            # GUI-subsystem exe shows nothing at all for an uncaught exception -
            # the window never appears and the process just hangs. The pre-check
            # is advisory (it only sets two labels), so a settings.json shape we
            # failed to anticipate must never cost the user the installer.
            self._log(f"[WARN] pre-check failed: {type(e).__name__}: {e}")
            self.hooks_status.set("Pre-check failed - see the log")

    def _build_ui(self):
        ttk.Label(self.root,
                  text=f"cc-memory - Claude Code Memory Plugin (v{_VERSION})",
                  font=("", 14, "bold")).pack(pady=(18, 4))
        ttk.Label(self.root,
                  text="Anti-patch reconcile-on-write · Forced PROGRESS.md handoff",
                  font=("", 10)).pack()

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=12)

        # Step 1
        f1 = ttk.LabelFrame(self.root, text="Step 1: Install Plugin (global)", padding=10)
        f1.pack(fill=tk.X, padx=20, pady=5)
        self.install_status = tk.StringVar(value="Checking...")
        ttk.Label(f1, textvariable=self.install_status, wraplength=580).pack(anchor=tk.W)
        bf1 = ttk.Frame(f1)
        bf1.pack(fill=tk.X, pady=(5, 0))
        self.install_btn = ttk.Button(bf1, text="Install Plugin", command=self._install)
        self.install_btn.pack(side=tk.RIGHT)

        # Step 2
        f2 = ttk.LabelFrame(self.root, text="Step 2: Configure Hooks (settings.json)", padding=10)
        f2.pack(fill=tk.X, padx=20, pady=5)
        self.hooks_status = tk.StringVar(value="Checking...")
        ttk.Label(f2, textvariable=self.hooks_status, wraplength=580).pack(anchor=tk.W)
        bf2 = ttk.Frame(f2)
        bf2.pack(fill=tk.X, pady=(5, 0))
        self.hooks_btn = ttk.Button(bf2, text="Configure Hooks", command=self._configure_hooks)
        self.hooks_btn.pack(side=tk.RIGHT)

        # Step 3
        f3 = ttk.LabelFrame(self.root, text="Step 3: Initialize Project (per-project)", padding=10)
        f3.pack(fill=tk.X, padx=20, pady=5)
        pf = ttk.Frame(f3)
        pf.pack(fill=tk.X)
        ttk.Label(pf, text="Project:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.project_var, width=48).pack(side=tk.LEFT, padx=5)
        ttk.Button(pf, text="Browse", command=self._browse).pack(side=tk.LEFT)
        self.project_info = tk.StringVar(
            value="Auto-init also runs on first user message — this is just for explicit init.")
        ttk.Label(f3, textvariable=self.project_info, wraplength=580).pack(anchor=tk.W, pady=5)
        bf3 = ttk.Frame(f3)
        bf3.pack(fill=tk.X)
        self.init_btn = ttk.Button(bf3, text="Initialize Project", command=self._init_project_btn)
        self.init_btn.pack(side=tk.RIGHT)

        # Log
        ttk.Label(self.root, text="Log:").pack(anchor=tk.W, padx=20, pady=(8, 0))
        self.log_text = tk.Text(self.root, height=10, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

        # Bottom
        bf = ttk.Frame(self.root)
        bf.pack(fill=tk.X, padx=20, pady=8)
        ttk.Button(bf, text="Open Dashboard", command=self._open_dashboard).pack(side=tk.LEFT)
        ttk.Button(bf, text="Uninstall", command=self._uninstall).pack(side=tk.LEFT, padx=8)
        ttk.Button(bf, text="Close", command=self.root.quit).pack(side=tk.RIGHT)

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _pre_check(self):
        if (TARGET_DIR / "core" / "db.py").exists():
            self.install_status.set(
                f"Plugin already installed (subpackage layout) at {TARGET_DIR}")
            self.install_btn.configure(text="Reinstall")
            self._log(f"[OK] Plugin found at {TARGET_DIR}")
        elif (TARGET_DIR / "db.py").exists():
            self.install_status.set(
                f"OLD v2.0 flat layout detected. Click Install to upgrade to v{_VERSION}.")
            self.install_btn.configure(text=f"Upgrade to v{_VERSION}")
            self._log("[WARN] Found v2.0 flat-layout files; upgrading to the "
                      "subpackage layout.")
        else:
            self.install_status.set("Plugin not yet installed")
            self._log("[  ] Plugin not found — click 'Install Plugin'")

        settings, err = _read_settings()
        if err is not None:
            self.hooks_status.set(f"settings.json {err}")
            self._log(f"[ERR] {SETTINGS_PATH} {err}")
            return
        if not SETTINGS_PATH.exists():
            self.hooks_status.set("No settings.json — will create on configure")
            return
        # Same shape rules as the install/uninstall paths - and the same
        # type-safety. Inline, this expression raised TypeError on
        # {"hooks": {"PreCompact": 5}} and on a non-string "command".
        _hooks = settings.get("hooks")
        _groups = _hooks.get("PreCompact") if isinstance(_hooks, dict) else None
        has_hook = any(_is_ccm_group(mg)
                       for mg in (_groups if isinstance(_groups, list) else []))
        if has_hook:
            self.hooks_status.set("Hooks already configured")
            self.hooks_btn.configure(text="Reconfigure")
            self._log("[OK] Hooks found in settings.json")
        else:
            self.hooks_status.set("Hooks not configured")
            self._log("[  ] Hooks not found")

        key = _marketplace_registration(settings)
        if key:
            self._log(f'[WARN] Claude Code already loads cc-memory as a plugin '
                      f'(enabledPlugins["{key}"]).')

    def _confirm_marketplace(self):
        """True if it is OK to proceed. Warns when a plugin install is active."""
        settings, err = _read_settings()
        if err is not None:
            messagebox.showerror("settings.json unusable",
                                 f"{SETTINGS_PATH}\n\n{err}\n\n{_SETTINGS_FIX_HINT}")
            return False
        key = _marketplace_registration(settings)
        if not key:
            return True
        return messagebox.askyesno(
            "cc-memory is already installed as a plugin",
            "\n".join(_marketplace_warning_lines(key))
            + "\n\nInstall the standalone copy anyway?")

    def _install(self):
        if not self._confirm_marketplace():
            self._log("[  ] Install cancelled (plugin install already active)")
            return
        try:
            TARGET_DIR.mkdir(parents=True, exist_ok=True)
            n_copied, n_skipped = _copy_subpackages(TARGET_DIR, self._log)
            n_pruned = _prune_stale(TARGET_DIR, self._log)
            n_surf, n_surf_skip = _copy_surfaces(self._log)
            self.install_status.set(
                f"Plugin installed: {n_copied} files (skipped {n_skipped}, "
                f"pruned {n_pruned}) -> {TARGET_DIR}; {n_surf} surfaces -> {CLAUDE_DIR}"
            )
            self.install_btn.configure(text="Reinstalled")
            self._log(f"[OK] {n_copied} files + {n_surf} surfaces installed")
            messagebox.showinfo(
                "Success",
                f"Plugin installed!\n{TARGET_DIR}\n\n"
                f"{n_surf} commands/agents/skills installed to\n{CLAUDE_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _configure_hooks(self):
        if not self._confirm_marketplace():
            self._log("[  ] Hook configuration cancelled")
            return
        try:
            hooks_config = _make_hooks_config(TARGET_DIR)
            if not _merge_into_settings(hooks_config, self._log):
                messagebox.showerror("Error", f"{SETTINGS_PATH} could not be "
                                              f"updated — see the log.")
                return
            self.hooks_status.set("Hooks configured")
            self.hooks_btn.configure(text="Reconfigured")
            self._log(f"[OK] Settings saved to {SETTINGS_PATH}")
            messagebox.showinfo("Success", "Hooks configured!")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _browse(self):
        path = filedialog.askdirectory(title="Select project directory")
        if path:
            self.project_var.set(path)

    def _init_project_btn(self):
        path = self.project_var.get().strip()
        if not path:
            messagebox.showwarning("No Project", "Please select a project directory.")
            return
        try:
            _init_project(path, self._log)
            self.project_info.set(f"Initialized: {Path(path) / 'memory'}")
            messagebox.showinfo("Success", f"Memory initialized for {Path(path).name}!")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _open_dashboard(self):
        dashboard_path = TARGET_DIR / "ui" / "dashboard.py"
        if dashboard_path.exists():
            cmd = [sys.executable, str(dashboard_path)]
            project = self.project_var.get().strip()
            if project:
                cmd += ["--project", project]
            subprocess.Popen(cmd)
        else:
            messagebox.showwarning("Not Found", "Dashboard not found. Install the plugin first.")

    def _uninstall(self):
        if not messagebox.askyesno(
            "Confirm Uninstall",
            "This will remove:\n"
            "  - hook files at ~/.claude/hooks/cc-memory/ (logs/ preserved)\n"
            "  - the cc-memory commands/agents/skills in ~/.claude/\n"
            "  - cc-memory entries in settings.json\n"
            "  - cc-memory temp session markers\n\n"
            "Project memory/ directories are PRESERVED.\n\nContinue?"
        ):
            return

        _remove_surfaces(self._log)
        _uninstall_settings(self._log)
        _remove_target_dir(self._log)
        _sweep_temp_markers(self._log)

        self.install_status.set("Plugin not installed")
        self.hooks_status.set("Hooks not configured")
        self.install_btn.configure(text="Install Plugin")
        self.hooks_btn.configure(text="Configure Hooks")
        messagebox.showinfo("Uninstalled",
                            "cc-memory has been removed.\n"
                            "Project memory/ directories and logs/ preserved.\n"
                            "Reinstall anytime.")


# ── CLI ────────────────────────────────────────────────────────────────────

def cli_install(force=False):
    """Headless install. Returns a process exit code."""
    print("=" * 52)
    print(f"  cc-memory v{_VERSION} - CLI Installer")
    print("=" * 52)

    # [0/3] Validate settings.json BEFORE writing anything to disk.
    settings, err = _read_settings()
    if err is not None:
        print(f"\n[FAIL] {SETTINGS_PATH} {err}")
        print(f"       {_SETTINGS_FIX_HINT}")
        return 1
    key = _marketplace_registration(settings)
    if key:
        tag = "[WARN] " if force else "[FAIL] "
        for line in _marketplace_warning_lines(key):
            print(tag + line if line else "")
        if not force:
            print("")
            print("       Do one of:")
            print("         - keep the plugin install and skip this installer")
            print("         - disable the plugin with /plugin, then re-run")
            print("         - re-run with --force to install anyway")
            print("       Nothing has been installed.")
            return 2

    print(f"\n[1/3] Installing plugin to {TARGET_DIR}...")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    n_copied, n_skipped = _copy_subpackages(TARGET_DIR)
    n_pruned = _prune_stale(TARGET_DIR)
    print(f"  Copied {n_copied}, skipped {n_skipped}, pruned {n_pruned} stale")

    print(f"\n[2/3] Installing Claude Code surfaces to {CLAUDE_DIR}...")
    n_surf, n_surf_skipped = _copy_surfaces()
    print(f"  Installed {n_surf} surfaces, skipped {n_surf_skipped}")
    if n_surf_skipped:
        print("  [WARN] Missing surfaces mean no /cc-mem, /ccm-load or")
        print("         plan-refiner subagent for this install.")

    print(f"\n[3/3] Configuring hooks in {SETTINGS_PATH}...")
    hooks_config = _make_hooks_config(TARGET_DIR)
    # NOT settings=settings. That copy is the [0/3] pre-flight validation and is
    # ~0.5 s stale by the time we get here; writing it back reverted whatever
    # Claude Code persisted during the install. _merge_into_settings re-reads.
    if not _merge_into_settings(hooks_config):
        print("[FAIL] Hooks were NOT registered; the plugin files are installed.")
        return 1
    n_cmds = sum(len(g["hooks"]) for groups in hooks_config.values() for g in groups)
    print(f"[OK] {n_cmds} hook commands configured across "
          f"{len(hooks_config)} events ({', '.join(hooks_config)})")

    print("\n" + "=" * 52)
    print(f"  cc-memory v{_VERSION} installation complete!")
    print("=" * 52)
    print("\nQuery a project:")
    print(f"  python \"{TARGET_DIR / 'cli' / 'mem.py'}\" --project <path> status")
    print("\nDashboard:")
    print(f"  python \"{TARGET_DIR / 'ui' / 'dashboard.py'}\" --project <path>")
    return 0


def cli_uninstall():
    """Headless uninstall. Returns a process exit code."""
    print("=" * 52)
    print(f"  cc-memory v{_VERSION} - Uninstall")
    print("=" * 52)
    _remove_surfaces()
    ok = _uninstall_settings()
    _remove_target_dir()
    _sweep_temp_markers()
    print("\n[OK] cc-memory uninstalled. Project memory/ data and logs/ preserved.")
    return 0 if ok else 1


def _usage():
    print(f"cc-memory v{_VERSION} installer")
    print("")
    print("  (no args)     open the graphical installer (falls back to --cli)")
    print("  --cli         headless install")
    print("  --force       with --cli: install even if the plugin is already")
    print("                registered with Claude Code as a marketplace plugin")
    print("  --uninstall   remove hooks, surfaces and settings entries")
    print("  --help        this message")
    return 0


_KNOWN_FLAGS = ("--help", "-h", "--cli", "--force", "--uninstall")


def main():
    argv = sys.argv[1:]
    # Unknown arguments are REFUSED, not ignored (v2.5.3). Every branch below
    # is a membership test, so anything unrecognised used to fall through to a
    # plain install: `--project D:\repo` performed an install and silently did
    # NOT initialise that project, and a typo'd `--unistall` performed an
    # INSTALL — the opposite of what was typed — and exited 0. Found by
    # actually running the built exe rather than reading its PE header.
    unknown = [a for a in argv if a not in _KNOWN_FLAGS]
    if unknown:
        print(f"cc-memory installer: unrecognised argument(s): "
              f"{' '.join(unknown)}\n")
        _usage()
        sys.exit(2)
    if "--help" in argv or "-h" in argv:
        rc = _usage()
    elif "--uninstall" in argv:
        rc = cli_uninstall()
    elif "--cli" in argv or not _HAS_TK:
        rc = cli_install(force="--force" in argv)
    else:
        root = tk.Tk()
        Installer(root)
        root.mainloop()
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()

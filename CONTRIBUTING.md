# Contributing to cc-memory

Thanks for looking. This document is short on etiquette and long on the rules
that are actually enforced, because in this repository most of them are.

## The one-command loop

```bash
git clone https://github.com/skymanbp/cc-memory.git
cd cc-memory
python tests/run_gates.py          # all 11 gates; nothing to install first
```

There is no `pip install -r requirements.txt` step, and adding one would be a
defect. cc-memory is **pure standard library** at runtime *and* in every gate;
PyInstaller is needed only to build the executables.

While iterating:

```bash
python tests/run_gates.py --fast          # skips the two slow suites
python tests/run_gates.py --only smoke    # one gate
python tests/run_gates.py --list          # what each gate checks
```

## Hard rules

These are not style preferences. Each one exists because violating it shipped a
defect; several are checked mechanically and will turn a gate red.

1. **Standard library only at runtime.** No pip dependency, ever. The rule is
   *stdlib-only*, not a closed whitelist — any stdlib module is fine.
2. **A hook may never raise, never write to stderr, and must exit 0.** Claude
   Code renders hook stderr as error UI, and a hook that hangs hangs the user's
   session. Log through `core.logger`, which writes to a file.
3. **Never call `db.insert_memory` from a caller path.** Every save routes
   through `llm.memory_writer.upsert_smart` / `upsert_batch`, which is what
   makes reconciliation rather than stacking the default. See
   [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract).
4. **Every SQL statement is parameterised and scoped by `project_id`.** One
   `memory.db` file legitimately holds several projects; a query without the
   scope is a cross-project leak, not a style nit.
5. **Stored content is escaped before it is rendered anywhere Claude reads.**
   Use `core.privacy.neutralize_inline` / `neutralize_block` /
   `neutralize_document`. A memory that can forge a `<system-reminder>` becomes
   a permanent injection re-delivered as authoritative context every session.
6. **A new config key needs a reader first.** Two audits found 34 of 51 leaf
   keys with nothing reading them; an inert tunable is worse than no tunable,
   because editing it *looks* like it does something.
7. **A new runtime module must be registered in three places** —
   `cc_memory/ui/installer.py` `SUBPACKAGE_FILES`, `scripts/build_exe.py`, and
   `cc_memory/cli/mem.py` `_REQUIRED_PLUGIN_FILES`. Miss the third and
   `/cc-mem status` certifies a broken install as healthy. This has now
   happened three times; the gate list is the only reason it is survivable.
8. **Tests use `tempfile` directories only, and remove them.** Every suite
   redirects `HOME`/`USERPROFILE` **and** `TMPDIR`/`TEMP`/`TMP` into a sandbox
   *before* importing the package, asserts `Path.home()` really moved, and
   tears the sandbox down in a `finally`. An uncleanable leak is a failure, not
   a warning.
9. **Both install layouts must keep working.** A marketplace/dev checkout is
   nested (`<root>/cc_memory/…`); the standalone installer writes a **flat**
   tree (`~/.claude/hooks/cc-memory/core/…`, no `cc_memory/` segment). Any code
   or doc that probes for an install must accept both.
10. **Docs are gated like code.** See below.

## The documentation gates

Four of the eleven gates are documentation gates, and they are the reason this
project's prose is trustworthy:

| Gate | What it proves |
|---|---|
| `tools/citation_check.py` | Every `file.py:LINE` citation in every tracked markdown file still points at its symbol (or, where no symbol is resolvable, at a real non-blank line) |
| `tools/doc_claims.py` | A sentence that **counts** something matches the set computed from the tree |
| `tools/doc_coverage.py` | Every public surface the code exposes — schema tables, `ALTER`-added columns, MCP tools, config keys — is **mentioned at all** by the document that owns it, in both language siblings |
| `tools/i18n_check.py` | Every `*.zh.md` is bound to a hash of its English source and has not drifted |

The fourth is newer than the others and exists because the first three all
check the docs that **already exist**. A schema migration landed with its two
new columns mentioned zero times in the specification, and every gate passed —
so if you add a table, an `ALTER` column, an MCP tool or a config key, the
build now fails until the owning document names it. It answers "is this written
down at all", not "is the prose correct": that second question is not
mechanical, and a green run should not be read as claiming it.

Three practical consequences when you write prose:

- **Do not enumerate a set in prose.** Name the count, bind it with an HTML
  comment, and point at `python tools/contracts.py` for the members:

  ```markdown
  all six hooks <!--ce:hooks--> resolve after the opt-out
  Four of the six hooks <!--ce:hooks:subset--> gate on memory.db
  v2.5.1 fixed the six hooks <!--ce:hooks:asof--> and missed the seventh
  ```

  Whole set, strict subset, or a statement about the past. Enumerating members
  by hand is precisely the defect that survived five review rounds.

- **Fix a stale line number with the tool, not by hand:**
  `python tools/citation_check.py --fix`.

- **A new invariant goes in `docs/CONTRACTS.md`, not only in `CHANGELOG.md`.**
  The person about to break it will be reading the specification, not the
  release history. `doc_coverage` can prove the *surface* is mentioned; only
  you can put the *rule* where it will be found.

If you change an English document, refresh its translation and re-stamp the
marker:

```bash
python tools/i18n_check.py --emit-marker README.md
```

The emitter records a hash of the translation's body in the marker and, when
the English digest has changed since the previous marker but the translation
has not, refuses (exit 2) rather than certify a translation nobody
translated. An English-only change that needs no translation — a typo, a
renumbered citation — passes with `--translation-unchanged "<why>"`.

## Proving a check is not vacuous

`tools/falsify_fixes.py` reverts each registered fix **on a temporary copy** and
asserts the corresponding gate goes red. A green case means the check proves
nothing:

```bash
python tools/falsify_fixes.py --list          # what each case breaks
python tools/falsify_fixes.py --case <name>   # run one
python tools/falsify_fixes.py --anchors       # prove the register itself has not rotted
```

When you fix a defect, register a case for it. "I added a test" and "the test
would have caught this" are different claims, and only the second one matters.

Since v2.14.0 every case is judged against a NEGATIVE CONTROL: the gate is
first run once on an untouched copy of the tree (`gate_baseline`, cached per
gate), and a case whose baseline is red is reported `UNSOUND`, never `RED`.
A `--case` that prints UNSOUND is telling you the gate is broken on the
current tree — fix that first. Before this control existed, every case gated
on `smoke_test.py` had been judged against a copy with no `.git`, on which
the gate was already red.

## Pull requests

Fill in the template. The three things a review will look for first:

1. **The root cause**, as a mechanism — not the symptom, and not a description
   of the patch.
2. **Evidence**: command plus real output. Show the gate red before and green
   after.
3. **Repo-wide sync**: docs, translations, manifests and tests co-updated. An
   edit is done when every reference to the changed thing is updated or
   verified current.

## Reporting a vulnerability

Do not open a public issue — see [SECURITY.md](SECURITY.md).

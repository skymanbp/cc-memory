# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 2.14.x | ✅ |
| < 2.14 | ❌ — upgrade; several fixes below are security-relevant |

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting:
[Security → Report a vulnerability](https://github.com/skymanbp/cc-memory/security/advisories/new).

Include the version, the install layout (nested vs flat), and a reproduction.
A report with a concrete repro will be looked at far faster than one without.

## What counts as a vulnerability here

cc-memory sits between your filesystem, your conversation, and an LLM, so its
threat model is narrower and stranger than a typical library's. Things that
count:

- **Indirect prompt injection.** Stored memory content is re-injected into
  later sessions as context. A path where stored text can forge an authority
  marker (`<system-reminder>`, `</private>`, tool-call syntax) *unescaped* into
  anything Claude reads is a vulnerability — including PROGRESS.md, PLAN.md,
  MEMORY.md, the SessionStart injection, MCP responses, CLI output, and hook
  decision payloads. `memory_add` is model-invokable, so the attacker does not
  need shell access: a malicious README or a fetched page is enough.
- **Cross-project data flow.** One `memory.db` file can hold several projects,
  and `memories.id` / `plans.id` are global to the file. Any read or write that
  reaches another project's rows is a vulnerability, as is any path that lets
  one project ingest another's transcript.
- **Escaping the opt-out.** `excluded_projects` is a privacy control. A surface
  that reads or writes a listed project — or that anchors to a parent before
  checking, widening a per-subdirectory exclusion away — defeats it.
- **`<private>` leakage.** Text inside `<private>` tags must never reach the
  Anthropic API or the database.
- **Writing through a link.** `.ccm/` and the per-session marker files are
  fail-closed against symlinks *and* Windows junctions. A path that follows one
  is a vulnerability, not a portability bug.
- **The web viewer.** It binds to loopback and is guarded by `Origin`, `Host`,
  content-type, deadlines and a concurrency cap. A cross-origin page that can
  read `/api/*` or POST a memory into your next session is a vulnerability.
- **Credential handling.** Any path that logs, stores, or transmits the
  Anthropic API key or the Claude Code OAuth token anywhere but the Anthropic
  API.

## What does not count

- The web viewer being reachable by another process on the same machine as the
  same user. It is loopback-only by design and is not an authentication
  boundary.
- `/cc-mem sql` returning data from the project's own database. It is
  read-only by design, and reading your own project is its purpose.
- A local user with your filesystem privileges reading `.ccm/memory.db`. The
  database is deliberately project-local and unencrypted; treat it like the
  rest of your working tree.
- Anything requiring you to run an attacker's `config.json` or install an
  attacker's plugin build.

## Where the data lives

Everything is under `<project>/.ccm/`, on your machine. Nothing is uploaded
anywhere. The only network egress is the extraction/consolidation call to the
Anthropic API — using the credential Claude Code already holds — or to your own
Ollama endpoint when `ccl.enabled` is turned on. To stop even that for a
project, list it in `excluded_projects`.

# councli systems architecture review

This document treats `councli` as production software, not as a prototype. The
core problem is local orchestration of heterogeneous coding assistants that are
not designed around one common API. They are external processes with their own
auth, model routing, session stores, permission models, TUIs, and failure modes.

The right abstraction is a local control plane:

```text
user
  -> councli shell / commands
  -> turn protocol and run ledger
  -> assistant adapters
  -> external CLI processes
  -> artifacts, events, blackboard, patches
```

`councli` should not become another coding agent. It should provide transport,
shared memory, evidence, policy, and recovery.

The detailed protocol contract lives in `PROTOCOL_DESIGN.md`. The detailed
trust and threat model lives in `SECURITY_MODEL.md`. Operational lifecycle,
cleanup, cancellation, and observability rules live in `OPERATIONS_MODEL.md`.
Terminal launch and interaction tradeoffs live in `TERMINAL_SUBSTRATE.md`.
Adapter readiness, intent routing, and failure taxonomy live in
`ADAPTER_CONTRACT.md`.
State, synchronization, blackboard projection, recovery, and indexing rules live
in `STATE_CONCURRENCY.md`.

## Existing primitives to prefer

### Process execution

Options:

- `subprocess.run` / `Popen`: battle-tested, simple, portable enough, good for
  headless CLI calls and explicit timeouts.
- `asyncio.create_subprocess_exec`: better for high fan-out and streaming, more
  complexity than the current MVP needs.
- `pexpect` / PTY automation: useful when a CLI has no headless mode, but
  brittle because it automates a human UI.
- Long-lived daemon with worker pool: better for production latency and
  scheduling, premature for the current local CLI.

Recommendation: keep `subprocess` as the primary automation path. Add
`asyncio` only if fan-out, streaming, cancellation, or backpressure become real
requirements. Treat PTY automation as a last-resort adapter, not the default.

### Terminal session hosting

Options:

- Direct child process attached to the user's terminal: simple but hard to
  detach, resume, or inspect.
- PTY managed directly by councli: maximum control, high implementation burden.
- `tmux`: mature session lifetime, attach/detach, pane capture, send-keys,
  project-scoped session names, and user familiarity.
- Terminal UI framework (`textual`, `prompt_toolkit`, curses): useful for
  councli's own UI, not for hosting native assistant TUIs.

Recommendation: keep tmux as the native-session substrate. Do not parse tmux
pane text as a semantic protocol. Use it for liveness, attach/detach, capture,
and hot resume.

### IPC and protocol format

Options:

- Plain natural-language prompts: fastest to integrate, weakest correctness.
- Markdown artifacts: inspectable and diffable, weak machine validation.
- JSON sidecars: simple schema validation, good for trailers, decisions, and
  status.
- JSON-RPC over stdio or Unix sockets: standard request/response shape, useful
  if councli becomes a daemon or exposes a stable API.
- gRPC/HTTP: operationally heavier, not justified for a local CLI MVP.

Recommendation: use Markdown for human evidence and JSON sidecars for machine
state. If a long-lived service appears later, expose JSON-RPC over Unix domain
sockets before considering HTTP.

### Persistence

Options:

- Append-only JSONL events: easy to debug and recover, poor indexing.
- Atomic file writes with `os.replace`: good for state snapshots.
- `flock`/`fcntl` locks: correct primitive for cross-process local writers.
- SQLite WAL: excellent local durable index with transactions and concurrency.
- External database: operationally unjustified for local-first use.

Recommendation: keep file artifacts as source evidence. Add cross-process locks
around run ledgers now. Add SQLite WAL later as an index over runs, events,
participants, and artifact paths.

### Workspace isolation

Options:

- Edit the current worktree: simple, high conflict and rollback risk.
- Git branches in the same worktree: still conflicts with user changes.
- `git worktree`: mature isolation, cheap local clones, natural diff boundary.
- Container/sandbox per assistant: stronger isolation, heavier setup.

Recommendation: keep `git worktree` for implementation. Add container or
namespace isolation only if untrusted assistant commands or third-party users
become in scope.

### Supervision and scheduling

Options:

- Foreground CLI only: easiest to reason about.
- tmux sessions: persistent interactive state without a daemon.
- systemd user units: correct Linux primitive for long-lived local services.
- cron: scheduling only, poor process supervision.
- Celery/RQ/etc.: too much infrastructure for a local CLI.

Recommendation: foreground CLI plus tmux is correct now. If background jobs are
added, use systemd user units on Linux and keep job state in the councli store.

## Design decisions and tradeoffs

### Default to shared conversation, not fixed lifecycle

Decision: normal input runs a shared conversation turn. Explicit slash commands
opt into stronger behavior.

Tradeoff:

- Good: simple prompts like "hello" or "what can you do" stay cheap and natural.
- Good: user intent drives governance rather than a hidden workflow engine.
- Bad: the council may under-deliberate if the user expects deeper analysis but
  does not ask for it.

Mitigation: expose clear commands such as `/deliberate`, `/vote`, `/broadcast`,
`/legacy-council`, and later `/parallel`.

### Use headless commands for protocol turns

Decision: shared turns use prompt-capable subprocess commands rather than
injecting text into active tmux TUIs.

Tradeoff:

- Good: deterministic capture, timeout handling, cwd control, cleaner artifacts.
- Good: avoids corrupting a user's native interactive session.
- Bad: headless calls may not share native session memory.
- Bad: each invocation may reload agent context and cost more tokens.

Mitigation: write shared briefs and blackboard paths. Later, adapters can use
native session ids or official API surfaces where available.

### Keep native assistant features native

Decision: councli does not translate `/goal`, `@file`, MCP setup, subagents,
plugins, or provider commands.

Tradeoff:

- Good: avoids rebuilding each CLI.
- Good: native features continue to work in `/assistant`.
- Bad: councli cannot fully understand user interactions performed inside a
  native TUI.

Mitigation: capture raw terminal logs for audit, but treat them as diagnostic.
If semantic import is needed, use each assistant's native session/export format,
not screen scraping.

### Fallback to full commands when read-only broadcast is missing

Decision: availability is favored in the MVP. Missing `broadcast_command` does
not automatically make a participant unusable if it has a prompt-capable
`command`.

Tradeoff:

- Good: avoids blocking useful assistants.
- Bad: "broadcast" is not a hard read-only boundary.

Mitigation: record whether read-only was explicit. Replace boolean policy with
capabilities before trusting broadcast for untrusted projects.

## Critical invariants

- Every participant command runs with the cwd expected by the user.
- Native tmux sessions are reused only when their pane cwd matches the project.
- The source of truth is artifacts and event logs, not terminal screen text.
- File edits occur only in explicit implementation modes.
- Implementation runs happen in isolated git worktrees.
- Unavailable participants degrade the turn; they do not block healthy
  participants unless the requested governance policy requires quorum.
- Configured executable commands are trusted user policy, not project-owned
  data.
- Raw logs, prompts, and blackboards are sensitive local data.

## Failure modes and recovery

### Assistant unavailable

Causes: missing binary, expired subscription, no model configured, provider key
missing, quota failure, network failure.

Recovery: classify as degraded for the turn, record stderr/stdout, continue with
healthy participants, and surface the exact adapter/probe failure.

Required hardening: adapter probe commands and normalized failure taxonomy.

### TUI automation drift

Causes: upstream UI changes, bracketed paste handling, prompt split across
turns, old scrollback matching a marker.

Recovery: prefer headless commands. For tmux automation, use compact prompts,
unique markers, and artifact-file completion when possible.

Required hardening: PTY/expect adapters should be version-gated and tested per
assistant release.

### Concurrent councli processes

Causes: two shells running turns in the same project, hooks writing while the
CLI writes, background cleanup.

Recovery: registry already uses file locks; run ledgers need the same
cross-process discipline.

Required hardening: lock per run directory or project store before appending
events and rendering derived state.

### Hidden context divergence

Causes: headless calls do not share native TUI session history; `/assistant`
interactions happen outside shared-turn artifacts.

Recovery: make the blackboard visible and instruct assistants to read briefs.
Treat native session logs as audit, not guaranteed semantic context.

Required hardening: explicit import/export from native session stores where each
assistant supports it.

### Secret leakage

Causes: raw terminal logs, prompts, tool output, environment dumps, stack traces,
provider keys in error messages.

Recovery: private file modes, `.gitignore`, local-only default storage.

Required hardening: redaction filters, retention policy, `councli scrub`, and
clear user controls before sharing artifacts.

### Cost and latency amplification

Causes: fan-out to many assistants, synthesis as another model call,
multi-round deliberation, repeated context loading.

Recovery: default to one round, degrade failed participants, keep explicit
commands for expensive behavior.

Required hardening: per-assistant budgets, max output sizes, cancellation,
streaming status, and cost/latency metrics.

## Observability model

Use one conceptual trace per user turn:

```text
trace_id = run_id
span: turn.route
span: participant.codex.round1
span: participant.claude.round1
span: participant.agy.round1
span: synthesis.codex
span: artifact.render
```

Structured logs should include:

- run id
- turn intent
- participant
- command class, not secrets
- cwd
- start/end timestamps
- exit code
- timeout flag
- degraded/skipped reason
- artifact refs

Metrics should include:

- participant latency
- failure rate by adapter and failure class
- timeout count
- rounds per turn
- synthesis fallback count
- bytes written to artifacts
- raw log growth

Do not depend on terminal logs for observability. They are evidence, not
structured telemetry.

## Production readiness gates

Before treating councli as maintainable production software, require:

1. Adapter probes and a normalized failure taxonomy.
2. Cross-process locking for all mutable run/project state.
3. JSON sidecar schemas for participant responses, votes, decisions, and
   synthesis metadata.
4. Retention/redaction controls for sensitive artifacts.
5. Integration tests for each supported assistant command template, using real
   dry-run or mock binaries.
6. Worktree cleanup and stale-session cleanup policies with dry-run visibility.
7. Versioned config migration.
8. Clear separation between user-owned command policy and repo-owned project
   metadata.
9. Documented escape hatches when the user wants raw native assistant control.
10. A minimal observability contract suitable for bug reports.

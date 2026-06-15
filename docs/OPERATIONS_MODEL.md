# councli operations and reliability model

This document defines how `councli` should behave over time: process lifecycle,
failure handling, cleanup, retention, observability, and production-readiness
runbooks. It complements `PROTOCOL_DESIGN.md` and `SECURITY_MODEL.md`.
Adapter readiness states and normalized adapter failure classes are defined in
`ADAPTER_CONTRACT.md`.
Run-local locking, recovery, retention, and index rebuild semantics are defined
in `STATE_CONCURRENCY.md`.

The current implementation is local-first and foreground-driven. That is the
right starting point. The system should not grow a daemon, queue, database, or
distributed scheduler until the local protocol and failure semantics are stable.

## Current operational surface

Implemented today:

- foreground commands: `chat`, `broadcast`, `council`, `reason`, `run`,
  `apply`, `status`, `show`;
- prompt-capable subprocess execution with cwd and timeout;
- tmux-backed native sessions with attach/detach and pane capture;
- project ledger and session registry with file locks;
- run artifacts under `.councli/runs/<run-id>/`;
- raw tmux recordings with size rotation;
- `sessions stop` and `sessions prune` with dry-run and archive options;
- implementation isolation through `git worktree`;
- explicit patch handoff through `councli apply`.

Operational gaps:

- subprocess timeout cleanup now owns the headless exec process group, but
  foreground Ctrl-C still needs the same first-class cleanup path for active
  participant calls;
- user cancellation is not modeled as a first-class run state;
- retention applies only to raw logs, not runs/blobs/worktrees;
- no structured metrics export;
- no budget model for cost, latency, output bytes, or disk usage;
- no background supervisor for long-running jobs;
- no formal incident/debug bundle.

## Existing primitives and decisions

### Foreground process model

Options:

- foreground CLI commands;
- tmux sessions;
- Python daemon;
- systemd user services/scopes;
- external queue/worker system.

Decision: keep foreground CLI commands as the primary model. Use tmux for hot
native sessions. Use systemd user units/scopes only if background jobs become a
real requirement.

Rationale:

- foreground commands are easy to debug;
- tmux already solves interactive session lifetime;
- systemd user managers are the right Linux primitive for supervised background
  processes, but add operational semantics that the MVP does not need;
- an external queue is unjustified for a local developer tool.

### Subprocess lifecycle

Python `subprocess.run(..., timeout=...)` handles simple timeouts by killing and
waiting for the child process. That is enough for many one-shot CLI calls, but
coding assistants can spawn child tools. For production-grade cancellation, use
`Popen(start_new_session=True)` or equivalent process-group handling, then send
signals to the process group.

Decision:

- current `subprocess.run` is acceptable for MVP calls;
- target behavior is process-group execution for participant commands;
- timeout should move through `SIGTERM -> grace period -> SIGKILL`;
- timeout events must record whether the process group was fully reaped.

### tmux lifecycle

tmux should remain the persistent native-session substrate:

- session liveness from `tmux list-sessions`;
- process state from pane metadata;
- pane capture for diagnostics;
- attach/detach for native user control.

Decision: tmux sessions are not jobs. They are user-visible native workspaces.
`sessions stop` and `sessions prune` are cleanup commands, not hidden garbage
collection.

### Worktree lifecycle

`git worktree` is the correct primitive for implementation isolation:

- cheap local checkout;
- natural diff boundary;
- preserves main worktree until explicit apply;
- easy manual inspection.

Decision: keep worktrees for implementation. Add explicit lifecycle state:
`created`, `executing`, `diff_captured`, `reviewed`, `applied`, `abandoned`,
`pruned`.

## Command lifecycle states

Target state machine for a foreground command:

```text
starting
  -> loading_config
  -> checking_trust
  -> selecting_participants
  -> executing
  -> rendering_artifacts
  -> completed
```

Failure states:

```text
loading_config       -> failed_config
checking_trust       -> failed_trust
selecting_participants -> degraded | failed_no_participants
executing            -> failed_timeout | failed_adapter | canceled
rendering_artifacts  -> failed_persistence
completed            -> ok | degraded
```

Operational rule: a command should leave enough evidence to answer these
questions after failure:

- what command was requested?
- which participants were selected?
- which participants started?
- which participants completed, failed, skipped, or timed out?
- which artifacts were written?
- what state transition failed?
- what can be safely retried?

## Cancellation model

Current behavior: `KeyboardInterrupt` exits the interactive shell or current
foreground operation, but run cancellation is not consistently recorded.

Target behavior:

1. User sends Ctrl-C.
2. `councli` records `turn.canceled` or `run.canceled` when a run directory
   exists.
3. Active participant subprocess groups receive `SIGTERM`.
4. After a configurable grace period, remaining process groups receive
   `SIGKILL`.
5. tmux native sessions are not killed unless the user is running a cleanup
   command; they are independent native workspaces.
6. Artifacts are rendered with status `canceled`.

This distinguishes cancellation from failure and prevents zombie tools.

## Retry policy

Retries must be phase-specific:

- normal shared chat: no automatic retry by default;
- broadcast: no automatic retry by default;
- legacy file-backed phases: one retry for missing output artifact is
  acceptable because artifact-writing is the completion signal;
- implementation: retries are explicit attempts controlled by
  `consensus.max_rounds`;
- review: retry only when malformed output prevents a decision and policy says
  a retry is cheaper than asking the user.

Rule: never retry an operation that may have written to the main worktree.

## Retention and cleanup

Storage classes need separate retention:

| Class | Default | Cleanup command |
| --- | --- | --- |
| raw tmux recordings | rotate by size/backups | `sessions stop/prune`, `artifacts prune`, `artifacts scrub` |
| run artifacts | keep | `artifacts scrub`; explicit `artifacts prune --class run` |
| blobs | keep while referenced | future `runs gc` |
| worktrees | keep after run | future `worktrees prune` |
| session archives | keep | `artifacts prune`, `artifacts scrub` |
| SQLite index | rebuildable | future `index rebuild` |

Cleanup commands:

```text
councli artifacts list
councli artifacts scrub --dry-run
councli artifacts scrub --write
councli artifacts prune --older-than 30 --dry-run
councli artifacts prune --older-than 30 --delete
councli worktrees prune --status abandoned --dry-run
councli index rebuild
```

Cleanup must default to dry-run for destructive bulk operations.

## Health model

Current `health()` checks binary presence, tmux availability, version probes,
and an optional configured readiness probe. It still needs more adapter-specific
safe defaults before it can fully prove auth/model/quota readiness for every
assistant.

Target health dimensions:

- binary exists;
- resolved binary path matches trust pin or accepted drift;
- version detected;
- auth/provider/model ready;
- quota likely available;
- command template supports prompt transport;
- timeout and permission policy valid;
- cwd invariant satisfied;
- tmux server available when needed;
- raw-log directory writable when capture is enabled;
- run artifact directory writable;
- git repo/worktree capability when running implementation.

Health statuses:

- `available`: usable for selected intent;
- `degraded`: usable but missing preferred capability;
- `unavailable`: cannot run selected intent;
- `unsafe`: would exceed selected policy.

## Observability model

Do not add an OpenTelemetry dependency yet. Make the event model compatible with
OpenTelemetry concepts:

- trace id: run id or turn id;
- span id: participant invocation, synthesis, artifact render, worktree
  creation, review, apply;
- logs: event ledger records;
- metrics: derived from events.

Minimum event fields:

```json
{
  "trace_id": "20260610T120000Z-chat",
  "span_id": "participant.codex.round1",
  "parent_span_id": "turn",
  "event": "participant.completed",
  "participant": "codex",
  "intent": "chat",
  "status": "ok",
  "duration_ms": 11023,
  "exit_code": 0,
  "timeout": false,
  "artifact_refs": []
}
```

Minimum metrics:

- `councli_turns_total{intent,status}`;
- `councli_participant_calls_total{participant,status,error_class}`;
- `councli_participant_duration_seconds{participant,intent}`;
- `councli_timeouts_total{participant}`;
- `councli_artifact_bytes_total{kind}`;
- `councli_raw_log_bytes{participant}`;
- `councli_worktrees_total{status}`;
- `councli_synthesis_fallback_total`.

Export path:

1. derive metrics from events locally;
2. print a human table in `doctor --metrics`;
3. optionally write Prometheus/OpenMetrics text to a file;
4. only later expose HTTP if a daemon exists.

## Background supervision

Do not create a daemon by default. If background work is added:

- on Linux, prefer systemd user units/scopes;
- create one unit/scope per long-running run;
- use cgroup-level cleanup for subprocess trees;
- keep artifacts in the same `.councli/runs/<run-id>/` layout;
- foreground CLI should attach to or inspect the background run, not duplicate
  state.

Potential command:

```text
councli run --background "implement this"
councli runs attach <run-id>
councli runs cancel <run-id>
```

The background supervisor must not own native assistant sessions. tmux remains
the native session owner.

## Failure recovery runbooks

### Participant command hangs

Evidence:

- event has `participant.started` but no completion;
- process still running;
- no output sidecar.

Recovery:

- cancel participant process group;
- mark participant `timeout` or `canceled`;
- synthesize from other participants if policy allows;
- suggest adapter probe if repeated.

### councli crashes mid-turn

Evidence:

- run directory exists;
- `events.jsonl` may have started events;
- `state.json` may be stale or missing.

Recovery:

- replay `events.jsonl`;
- regenerate `state.json` and `blackboard.md`;
- mark missing participant completions as `unknown` until inspected;
- allow `councli show <run>` to report incomplete state.

### worktree left behind

Evidence:

- worktree path in run state;
- git still lists worktree;
- run not applied or abandoned.

Recovery:

- keep by default;
- `worktrees prune --dry-run` lists candidates;
- user explicitly prunes after inspecting diff.

### raw logs contain secrets

Evidence:

- user notices secret in `.councli/session-recordings` or run artifact.

Recovery:

- stop sharing/exporting artifacts;
- run future `artifacts scrub`;
- rotate/delete affected raw logs;
- rotate affected credentials outside councli.

### trust mismatch

Evidence:

- `load_config` refuses config due trust hash mismatch.

Recovery:

- inspect changed command-bearing fields;
- verify resolved binaries;
- run `councli trust` only after review.

## Operational readiness checklist

Before public release:

1. Process-group cancellation for foreground Ctrl-C.
2. First-class canceled state.
3. Adapter-specific default probes for auth/model/quota where safe commands
   exist.
4. Capability-aware routing.
5. Artifact retention and redaction.
6. Worktree prune workflow.
7. Metrics derived from event logs.
8. Redacted support bundle.
9. Versioned config and artifact schema migrations.

## Research references

- Python subprocess timeout behavior: https://docs.python.org/3/library/subprocess.html
- systemd process killing semantics: https://www.freedesktop.org/software/systemd/man/systemd.kill.html
- systemd scope units: https://man7.org/linux/man-pages/man5/systemd.scope.5.html
- OpenTelemetry metrics data model: https://opentelemetry.io/docs/specs/otel/metrics/data-model/
- Prometheus/OpenMetrics exposition format: https://prometheus.io/docs/specs/om/open_metrics_spec/

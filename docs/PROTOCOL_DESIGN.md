# councli protocol design

This document defines the target protocol shape for `councli` as a maintainable
local control plane. It is not a rewrite plan. It is the design contract that
future implementation should converge toward.

Adapter launch/readiness policy is detailed in `ADAPTER_CONTRACT.md`; this file
defines the turn and artifact protocol that adapters feed.
State synchronization, event locking, projection recovery, and indexing are
detailed in `STATE_CONCURRENCY.md`.

The current implementation already has useful pieces:

- prompt-capable `exec` adapters in `src/councli/agents.py`;
- shared turns and explicit `/deliberate` / `/vote` routing in
  `src/councli/cli.py`;
- append-only run events and blackboard projection in `src/councli/events.py`;
- atomic file replacement in `src/councli/artifacts.py`;
- locked project ledger/session registry primitives in `src/councli/native.py`;
- git worktree isolation in `src/councli/gitops.py`.

The main protocol gaps are also clear:

- shared-turn trailers are still parsed as fallback text; sidecars exist but
  need stricter schema validation before every machine decision;
- the blackboard renderer still treats legacy phases as the primary structure;
- participant failure classification is mostly stderr-text heuristics;
- broadcast capability is represented by a boolean rather than a permission
  model;
- observability is artifact-oriented but not yet trace/span/metric oriented.

## Design posture

`councli` should be a local control plane, not an agent. It should provide:

- transport: invoke participant CLIs safely and consistently;
- memory: durable blackboard, event log, and task briefs;
- policy: command trust, cwd invariants, execution isolation, and cleanup rules;
- evidence: inspectable artifacts for every assistant response and decision;
- recovery: degradation, retry boundaries, crash recovery, and resume metadata.

It should not:

- parse terminal screen text as authoritative semantic output;
- reimplement native slash commands, MCP config, subagents, provider settings, or
  assistant-specific goal systems;
- force a fixed reasoning ceremony for normal conversation;
- hide command execution policy inside project-owned files.

## Existing primitives and decisions

### Subprocess execution

Use Python subprocess execution as the primary transport for structured turns.
The standard library gives explicit `cwd`, environment, stdout/stderr capture,
return codes, and timeout handling. This fits `councli` because each assistant
CLI is already an executable process.

Tradeoffs:

- `subprocess.run` is simple and adequate for bounded calls.
- `Popen` gives streaming and process-group control, but increases complexity.
- `asyncio.create_subprocess_exec` improves high fan-out and cancellation but is
  unnecessary until latency/backpressure are real bottlenecks.
- PTY automation is useful only when a CLI has no stable headless mode.

Decision: keep subprocess as the protocol transport. Add process-group
termination and streaming later if needed.

### JSON envelope shape

JSON-RPC 2.0 is a useful reference because it separates requests,
notifications, responses, ids, and errors. `councli` does not need a live
JSON-RPC server yet, but it should borrow the envelope discipline:

- every request has a stable id;
- every response refers to the request id;
- errors have structured code/message/data fields;
- notifications are one-way events.

Decision: use JSON sidecar files with JSON-RPC-like fields. Do not add a daemon
or socket until the local file protocol is stable.

### Artifact persistence

Files are the correct source of truth for human-readable evidence: prompts,
Markdown bodies, diffs, logs, and blackboards. JSONL is reasonable for append
events, but all event writes and state projections need cross-process locking.
SQLite WAL becomes attractive once run count and query needs grow, because it
supports durable local indexing while allowing readers and a writer to coexist.

Decision: keep artifacts as source evidence. Add `run.lock` first. Add SQLite
WAL later as an index, not as the only storage of large bodies.

### Native session hosting

tmux is the correct primitive for persistent native assistant TUIs: it provides
session lifetime, attach/detach, pane capture, send-keys, and liveness checks.
PTY/expect libraries are useful for automating interactive processes, but they
are inherently brittle because they automate a human interface.

Decision: tmux remains the native-session substrate. PTY/expect is an adapter of
last resort. The semantic protocol must stay outside the terminal screen.

## Core concepts

`project`
: Directory where `councli` is launched. All normal shared turns and
  deliberation commands run participant commands with this cwd.

`chat session`
: Lifetime of the interactive `councli chat` shell.

`turn`
: One user prompt inside a chat session or one non-interactive command
  invocation.

`round`
: One fan-out pass to participants inside a turn.

`participant`
: A configured assistant CLI such as Codex, Claude Code, AGY, Kimi, or
  CodeWhale.

`adapter`
: The command templates, capability metadata, prompt formatting, timeout, and
  health probes for one participant.

`artifact`
: Durable file written under `.councli/runs/<run-id>/`.

`blackboard`
: Human projection of run artifacts. It is for visibility, not the primary
  machine state.

`event`
: Append-only JSON object recording state transition, response, decision, or
  artifact reference.

`capability`
: Adapter-declared behavior such as `planning_only`, `reads_workspace`,
  `writes_workspace`, `runs_tools`, `network_access`, or `full_permission`.

## Turn state machine

Normal shared turns should use a small explicit state machine:

```text
created
  -> routing
  -> running_round
  -> collecting
  -> synthesizing
  -> completed
```

Failure transitions:

```text
running_round -> degraded       # one or more participants failed, quorum not required
running_round -> failed         # no usable participant or protocol invariant broken
running_round -> canceled       # user cancellation
synthesizing  -> completed      # local fallback synthesis used
synthesizing  -> failed         # no usable response exists
```

Explicit governance commands add states:

```text
deliberating -> synthesizing -> completed
voting       -> deciding     -> completed
planning     -> executing    -> reviewing -> completed
```

Rules:

- A normal prompt starts exactly one turn.
- Every turn starts with round 1.
- Later rounds occur only when the intent requires them or participants request
  continuation through a validated response sidecar.
- `/vote` produces a decision artifact.
- `run` is the only default path that enters implementation.
- A degraded participant does not block normal chat synthesis.
- Governance commands define their own quorum and failure policy.

## File layout

Target layout:

```text
.councli/runs/<run-id>/
  request.json
  task.md
  participants.json
  events.jsonl
  run.lock
  state.json
  blackboard.md
  packets/
    <participant>/
      000001-chat.round1.md
  shared/
    chat.round1/
      codex.md
      codex.response.json
      claude.md
      claude.response.json
    synthesis.round2/
      codex.md
      codex.response.json
  decisions/
    vote.json
  implementation/
    diff.patch
    worktree.txt
  review/
  blobs/
```

`request.json` is the canonical turn request. `task.md` is the user-readable
body. Markdown participant files are evidence. `.response.json` sidecars are the
machine contract.

## Request envelope

Target request sidecar:

```json
{
  "schema_version": "councli.turn.v1",
  "id": "turn_20260610T120000Z_chat",
  "kind": "turn.request",
  "intent": "chat",
  "project_root": "/home/user/project",
  "created_at": "2026-06-10T12:00:00Z",
  "user_prompt_ref": "task.md",
  "participants": ["codex", "claude", "agy"],
  "policy": {
    "allow_file_edits": false,
    "allow_tool_execution": false,
    "requires_vote": false,
    "max_rounds": 1,
    "timeout_seconds": 900
  },
  "context_refs": {
    "brief": ".councli/tasks/turn_.../brief.md",
    "prior_blackboard": null
  }
}
```

Invariants:

- `id` is stable and unique under the project.
- `project_root` is absolute and checked before participant execution.
- policy is explicit and attached to the request.
- refs are relative to the run directory unless marked absolute by schema.

## Participant response sidecar

Target response sidecar:

```json
{
  "schema_version": "councli.response.v1",
  "id": "resp_codex_chat_round1",
  "request_id": "turn_20260610T120000Z_chat",
  "kind": "participant.response",
  "participant": "codex",
  "intent": "chat",
  "round": 1,
  "status": "ok",
  "body_ref": "shared/chat.round1/codex.md",
  "summary": "Can inspect, explain, edit, test, and review code.",
  "continue": false,
  "recommend": "none",
  "vote": null,
  "confidence": null,
  "capabilities_used": ["reads_workspace"],
  "artifacts": [],
  "error": null,
  "timing": {
    "started_at": "2026-06-10T12:00:01Z",
    "ended_at": "2026-06-10T12:00:12Z",
    "duration_ms": 11000
  }
}
```

For failure:

```json
{
  "schema_version": "councli.response.v1",
  "request_id": "turn_...",
  "kind": "participant.response",
  "participant": "kimi",
  "intent": "chat",
  "round": 1,
  "status": "failed",
  "body_ref": null,
  "summary": "",
  "continue": false,
  "error": {
    "class": "model_not_configured",
    "message": "No model configured",
    "exit_code": 1,
    "retryable": false,
    "stderr_ref": "blobs/errors/kimi.txt"
  }
}
```

Failure classes should be normalized:

- `missing_binary`
- `auth_required`
- `quota_exceeded`
- `model_not_configured`
- `timeout`
- `nonzero_exit`
- `invalid_response`
- `policy_denied`
- `cwd_mismatch`
- `artifact_missing`
- `unknown`

## Event envelope

Current events are close to the target. Implemented baseline:

- protect all event appends with a per-run `fcntl`/`flock` lock;
- allocate sequence numbers under the same lock;
- append event, `flush`, and `fsync` when durability matters;
- render `state.json` and `blackboard.md` under the same lock or through a
  single-writer projection step;
- store `schema_version` in each event.

Remaining work: validate every event payload against typed schemas as those
schemas stabilize.

Target event:

```json
{
  "schema_version": "councli.event.v1",
  "seq": 12,
  "event_id": "evt_000012",
  "ts": "2026-06-10T12:00:12Z",
  "trace_id": "turn_20260610T120000Z_chat",
  "parent_event_ids": ["evt_000003"],
  "type": "participant.response.received",
  "phase": "chat.round1",
  "participant": "codex",
  "status": "ok",
  "refs": {
    "body": "shared/chat.round1/codex.md",
    "response": "shared/chat.round1/codex.response.json"
  },
  "payload": {
    "duration_ms": 11000,
    "exit_code": 0
  }
}
```

## Locking protocol

Run writes use a run-local `run.lock` with `fcntl.flock`. The locking protocol:

1. Create `.councli/runs/<run-id>/run.lock`.
2. Acquire exclusive advisory lock before:
   - appending to `events.jsonl`;
   - allocating event sequence numbers;
   - rendering `state.json`;
   - rendering `blackboard.md`;
   - updating run-local indexes.
3. Use shared locks for read paths that must see a consistent state.
4. Keep locks short. Do not hold a run lock while waiting on an assistant CLI.
5. Write large blobs outside the lock when their path is content-addressed, then
   append the event referencing them under the lock.
6. Use atomic rename for projections.

This mirrors the existing project ledger/session registry approach in
`src/councli/native.py`, but applies it to run-local state.

## SQLite WAL index

Do not replace artifact files with SQLite. Use SQLite later as an index:

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  project_root TEXT NOT NULL,
  intent TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  type TEXT NOT NULL,
  participant TEXT,
  phase TEXT,
  status TEXT NOT NULL,
  ts TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  refs_json TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE artifacts (
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  participant TEXT,
  path TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  created_at TEXT NOT NULL
);
```

Use WAL mode for the index because this is a local read-heavy workload: the UI
will list/search runs while new turns append data. Keep writes serialized
through SQLite transactions.

## Adapter capability model

Replace boolean broadcast policy with explicit capabilities:

See `ADAPTER_CONTRACT.md` for the full adapter manifest, launch state machine,
probe result shape, intent routing table, and failure taxonomy.

```yaml
capabilities:
  planning_only: false
  reads_workspace: true
  writes_workspace: true
  runs_tools: true
  network_access: true
  full_permission: true
```

Each command template declares the capabilities it may exercise:

```yaml
commands:
  shared_turn:
    argv: ["codex", "exec", "...", "{prompt}"]
    capabilities: ["reads_workspace", "runs_tools", "full_permission"]
  planning:
    argv: ["claude", "--permission-mode", "plan", "-p", "{prompt}"]
    capabilities: ["planning_only", "reads_workspace"]
```

Routing then becomes policy-based:

- normal chat may use `reads_workspace`;
- `/broadcast` may prefer `planning_only` but can use higher-permission commands
  if the user config allows fallback;
- implementation requires `writes_workspace` and runs only in a worktree;
- review may use `reads_workspace` and `runs_tools`, but not current-worktree
  writes unless explicit.

## Security boundaries

Threat model:

- project repo may be malicious;
- assistant output may be malicious or wrong;
- configured command templates can execute arbitrary programs;
- raw logs and blackboards may contain secrets;
- user may run with yolo/full-permission modes.

Required controls:

- keep trusted command templates pinned in user-local state;
- never let project config silently expand the trust boundary;
- validate tmux session names and detach keys;
- store `.councli/` with private permissions where practical;
- keep `.councli/` ignored by git by default;
- add artifact redaction and retention controls;
- do not dereference artifact paths outside the run directory unless schema
  explicitly allows it;
- avoid shell interpolation; use argv arrays;
- record command argv but avoid recording secret environment variables.

## Observability protocol

Map protocol concepts onto tracing terms:

- trace id: `run_id` or `turn_id`;
- root span: `turn`;
- child spans: participant calls, synthesis, rendering, implementation,
  review;
- logs: event ledger entries;
- metrics: counters and histograms derived from events.

Minimum event-derived metrics:

- turn duration;
- participant call duration;
- failure count by participant and class;
- timeout count;
- rounds per turn;
- synthesis fallback count;
- artifact bytes written;
- raw log bytes retained.

Do not require OpenTelemetry as a runtime dependency yet. Keep event fields
compatible with that model so exporting later is straightforward.

## Recovery rules

Crash during participant execution:

- participant subprocess exits or times out;
- missing response sidecar becomes `artifact_missing` or `timeout`;
- run can still synthesize from other responses.

Crash after blob write before event append:

- blob may be orphaned;
- cleanup can remove unreferenced blobs older than a retention threshold.

Crash after event append before projection:

- replay `events.jsonl` under lock to regenerate `state.json` and
  `blackboard.md`.

Crash during projection write:

- atomic rename prevents partial `state.json` / `blackboard.md`;
- next render recreates projections.

Concurrent run writers:

- per-run lock serializes appends and projections;
- sequence numbers remain unique.

Participant unavailable:

- classify failure;
- mark degraded;
- continue unless the explicit governance policy requires quorum.

## Migration path

1. Keep the current Markdown artifacts.
2. Add `.response.json` sidecars for shared turns only.
3. Add run-local file locks around `EventLedger.append`, `write_packet`, and
   `render`.
4. Update blackboard rendering to group arbitrary `intent.round` phases before
   legacy phases.
5. Add normalized failure classification.
6. Keep `/vote` decisions in `decisions/vote.json` and reject invalid vote
   sidecars.
7. Add adapter capability metadata.
8. Add SQLite WAL index only after run-local locking and sidecars are stable.
9. Add optional JSON-RPC/Unix-socket daemon only if the interactive shell needs
   background jobs, streaming UI, or external integrations.

## Research references

- JSON-RPC 2.0 specification: https://www.jsonrpc.org/specification
- Python subprocess documentation: https://docs.python.org/3/library/subprocess.html
- SQLite write-ahead logging: https://sqlite.org/wal.html
- Linux advisory locking: https://man7.org/linux/man-pages/man2/fcntl_locking.2.html
- tmux manual: https://man7.org/linux/man-pages/man1/tmux.1.html
- Pexpect documentation: https://pexpect.readthedocs.io/en/stable/api/pexpect.html
- systemd user manager: https://man7.org/linux/man-pages/man5/user%40.service.5.html
- OpenTelemetry traces: https://opentelemetry.io/docs/concepts/signals/traces/

# State, synchronization, and communication transparency

This document defines how `councli` should store and synchronize shared council
state. It focuses on the durable communication layer between assistants: run
events, participant artifacts, blackboard projections, indexes, locks, recovery,
and caching.

The key design rule is:

```text
events + artifacts are source of truth
state.json + blackboard.md + indexes are rebuildable projections
tmux/terminal output is diagnostic evidence only
```

## Current implementation assessment

Implemented today:

- run-local artifacts under `.councli/runs/<run-id>/`;
- append-only run ledger at `.councli/runs/<run-id>/events.jsonl`;
- derived `state.json` and `blackboard.md`;
- atomic replacement for text and JSON projections through `os.replace`;
- content-addressed blobs for some participant outputs;
- project ledger with `fcntl.flock`;
- native session registry with `fcntl.flock`;
- run-local `run.lock` with `fcntl.flock` around event appends, packet writes,
  blob writes, sequence allocation, and projection rendering;
- project-level task briefs and recent-event context.

Main gaps:

- `state.json` and `blackboard.md` are projections rendered after events. They
  can still become stale if files are edited externally or a crash happens
  after source artifacts are written but before projection rendering.
- Atomic `os.replace` prevents partial destination files, but the current helper
  does not fsync file or directory metadata when stronger crash durability is
  needed.
- `new_run_dir` can still race between `exists()` and `mkdir()` if two
  processes choose the same timestamped run id.
- `councli recover` rebuilds `state.json` and `blackboard.md` projections from
  event logs and artifacts. Index rebuild remains future work.
- There is no retention/garbage-collection model for run artifacts and blobs.

## Existing primitives to prefer

### Append-only event log

Use an append-only JSONL log as the human-debuggable source of state changes.
This fits the Unix model: each event is a line, logs are inspectable with normal
tools, and corruption is often localized to the last line.

Tradeoffs:

- Good: simple, diffable, debuggable, easy to replay.
- Good: events preserve history instead of only latest state.
- Bad: poor indexing and search as runs grow.
- Bad: appends need locking and durability discipline.
- Bad: malformed/truncated lines need recovery behavior.

Decision: keep JSONL as source evidence for now.

### Advisory file locks

Use `fcntl.flock` on Linux for local cooperating processes. It gives shared and
exclusive locks on an open file descriptor, and locks are released when file
descriptors close or the process exits. This matches a local CLI with multiple
shells and hook processes.

Tradeoffs:

- Good: built into Unix/Linux, no daemon required.
- Good: already used by `native.py` for project ledger/session registry.
- Good: stale lock files are harmless because the kernel lock is tied to open
  file descriptions, not the existence of the lock path.
- Bad: advisory locks only work if all writers obey them.
- Bad: semantics over network filesystems can be surprising.
- Bad: not portable to Windows; acceptable for the Linux-first MVP.

Decision: add per-run lock files and use them for every event/projection write.

### Atomic replace

Use write-to-temp followed by `os.replace` for projections and sidecars.
Successful rename/replace is atomic on POSIX when source and destination are on
the same filesystem. Readers see either the old complete file or the new
complete file, not a half-written projection.

Tradeoffs:

- Good: simple and fast.
- Good: avoids torn projection files.
- Bad: does not by itself guarantee data reached stable storage after crash.
- Bad: temp file and destination must be on the same filesystem.

Decision: keep atomic replace; add optional fsync for durability-sensitive
state such as event logs and finalized decisions.

### SQLite WAL index

Use SQLite WAL later as a query/index layer, not as the only storage for large
bodies. WAL mode allows readers and a writer to proceed concurrently on the
same host, which fits a future TUI listing runs while a turn writes events.

Tradeoffs:

- Good: transactions, indexes, search, constraints.
- Good: read concurrency with WAL.
- Good: rebuildable from JSONL/artifacts if corrupted or removed.
- Bad: still single-writer at a time.
- Bad: WAL is not appropriate for network filesystems.
- Bad: adds migration and schema-management complexity.

Decision: use files as source of truth; add SQLite WAL only when run volume or
query complexity justifies it.

## State model

State classes:

| State | Role | Source of truth? | Rebuildable? |
| --- | --- | --- | --- |
| `request.json` | canonical user turn request | yes | no |
| `task.md` | human-readable prompt body | yes | no |
| `participants.json` | selected participant snapshot | yes | no |
| `events.jsonl` | ordered state transitions | yes | no |
| `shared/**/<participant>.md` | participant evidence | yes | no |
| `*.response.json` | participant machine contract | yes | no |
| `blobs/**` | large bodies/errors/diffs | yes | content-addressed |
| `state.json` | machine projection | no | yes |
| `blackboard.md` | human projection | no | yes |
| SQLite index | query cache | no | yes |

Rules:

- Source-of-truth artifacts are append-only or immutable once referenced by an
  event.
- Projections may be overwritten atomically.
- Indexes may be deleted and rebuilt.
- A decision must reference the event ids and artifact refs it was based on.

## Run-local locking protocol

Every run directory should contain:

```text
.councli/runs/<run-id>/run.lock
```

Use it as follows:

1. Acquire exclusive lock before allocating event sequence numbers.
2. Append exactly one event or a small atomic event batch.
3. Flush and optionally fsync `events.jsonl` when durability matters.
4. Release the lock.
5. Write large content-addressed blobs outside the lock when the path is derived
   from content hash.
6. Reacquire the lock to append an event referencing the blob.
7. Render `state.json` and `blackboard.md` under the lock or through a
   single-writer projection command.
8. Never hold a run lock while waiting for an assistant subprocess, tmux pane,
   network call, or model response.

This gives short critical sections and avoids blocking unrelated assistant
calls.

## Event sequence allocation

Current sequence allocation counts existing lines. This is simple but O(n) and
unsafe without a process lock.

Target options:

1. Count lines under `run.lock`.
2. Maintain `seq.current` under `run.lock`.
3. Use SQLite autoincrement once a WAL index exists.

Recommendation:

- Short term: count lines under `run.lock`; it is simple and enough for small
  local logs.
- Medium term: maintain a sidecar sequence counter under the same lock if logs
  grow.
- Long term: SQLite can allocate/query sequence numbers for the index, but JSONL
  remains source evidence.

## Event envelope invariants

Each event should include:

```json
{
  "schema_version": "councli.event.v1",
  "seq": 12,
  "event_id": "evt_000012",
  "trace_id": "turn_20260610T120000Z_chat",
  "ts": "2026-06-10T12:00:12Z",
  "type": "participant.response.received",
  "participant": "codex",
  "phase": "chat.round1",
  "status": "ok",
  "parent_event_ids": ["evt_000008"],
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

Invariants:

- `seq` is unique and monotonically increasing within a run.
- `event_id` is derived from `seq` or otherwise unique.
- `trace_id` is the run or turn id.
- `refs` are relative to the run directory unless schema explicitly says
  otherwise.
- referenced files must exist before the event that references them is appended.
- machine decisions must cite the event ids/artifacts used as evidence.

## Blackboard as projection

The blackboard is not a shared mutable document. It is a rendered view of
events and artifacts.

Bad model:

```text
assistant A edits blackboard.md
assistant B edits blackboard.md
councli tries to merge edits
```

Correct model:

```text
assistant A writes A.md + A.response.json
assistant B writes B.md + B.response.json
councli appends events
councli renders blackboard.md from event log
```

This matters because multiple assistants can respond in parallel without
conflicting on one file. The blackboard stays transparent but deterministic.

## Communication protocol over artifacts

A shared turn should produce this sequence:

```text
1. write request.json and task.md
2. append turn.started
3. select participants and append participant.selected/degraded
4. write per-participant request packets
5. run participant calls without holding run.lock
6. each completed call writes:
   - shared/<intent>.round<n>/<participant>.md
   - shared/<intent>.round<n>/<participant>.response.json
7. append participant.response.received
8. render blackboard
9. if needed, run next round from blackboard snapshot
10. write synthesis/decision artifact
11. append turn.completed/degraded/failed/canceled
```

Assistants see each other through prior artifacts, not by mutating shared memory
or relying on hidden terminal sessions.

## Crash recovery

Recovery command target:

```text
councli recover <run-id>
```

Recovery should:

1. Acquire `run.lock`.
2. Parse `events.jsonl`.
3. Stop at the first malformed/truncated event line and preserve it in a
   recovery artifact.
4. Verify referenced files exist.
5. Rebuild `state.json` and `blackboard.md`.
6. Mark missing referenced artifacts as `artifact_missing`.
7. Rebuild SQLite index entries when the index exists.
8. Release the lock.

If a participant output file exists but no event references it, recovery should
list it as orphaned evidence and avoid making semantic decisions from it without
an explicit import event.

## Cancellation and partial turns

Cancellation must be a state, not an absence of output.

On Ctrl-C:

- append `turn.cancel_requested`;
- terminate active headless participant process groups;
- collect results that already completed;
- append `participant.canceled` or `participant.completed`;
- render blackboard with partial results;
- append `turn.canceled`.

Native tmux sessions should not be killed by a normal turn cancellation because
they are user workspaces, not child jobs of the turn.

## Caching and context windows

The blackboard can grow beyond useful prompt size. Do not pass the full
blackboard blindly into every participant.

Preferred cache layers:

1. Full artifacts: permanent evidence.
2. Full blackboard: human inspection.
3. Round summary: bounded context for next round.
4. Participant summaries: compact memory per participant.
5. SQLite index: search and listing.

Context-packing policy should be explicit:

```json
{
  "max_blackboard_chars": 12000,
  "include_latest_rounds": 2,
  "include_decisions": true,
  "include_failures": "summary",
  "overflow_ref": "blackboard.md"
}
```

When context is truncated, the prompt must include artifact paths so assistants
can inspect the full evidence if their native tools support it.

## Retention and garbage collection

Retention must distinguish evidence from cache:

- never delete source-of-truth artifacts for active/recent runs by default;
- projections and indexes can be rebuilt;
- content-addressed blobs can be garbage-collected only when no event references
  them;
- raw terminal logs need redaction/retention because they may contain secrets;
- bulk cleanup should default to `--dry-run`.

Current and future commands:

```text
councli verify <run-id>
councli recover <run-id>
councli runs prune --older-than 30d --dry-run
councli runs gc --dry-run
councli index rebuild
```

## Security implications

State files are part of the trust boundary:

- repository files can contain prompt injection that causes assistants to write
  malicious artifacts;
- artifact refs must not allow path traversal outside the run directory;
- `.response.json` sidecars must be schema-validated before decisions;
- blackboards and logs may contain secrets;
- lock files are not secrets, but run directories should remain gitignored and
  private where practical.

Policy:

- reject `../` refs unless schema explicitly permits external paths;
- record external paths separately with an explicit kind;
- do not execute commands from artifacts;
- do not trust participant self-reported tool effects for enforcement;
- treat participant output as untrusted input until validated.

## Production readiness gates

Before treating the state layer as production-grade:

1. Add schema versions to events, requests, responses, and decisions.
2. Keep expanding `.response.json` validation before machine decisions.
3. Extend recovery to malformed or partially truncated logs. `councli recover`
   already rebuilds projections from valid logs, and `councli verify` checks
   missing refs, invalid sidecars, and stale projections.
4. Add bounded context-packing policy for blackboard excerpts.
5. Add SQLite WAL index only after artifact protocol stabilizes.
6. Add retention, redaction, and garbage-collection commands.
8. Add tests that spawn two `councli` processes writing the same run ledger.

## Research references

- SQLite WAL: reader/writer concurrency on one host and network-filesystem
  caveats: <https://sqlite.org/wal.html>
- Python `fcntl`: Unix file descriptor locking interface:
  <https://docs.python.org/3/library/fcntl.html>
- Linux `flock(2)`: shared/exclusive advisory locks on open files:
  <https://man7.org/linux/man-pages/man2/flock.2.html>
- Python `os.replace`: POSIX atomic rename/replace behavior:
  <https://docs.python.org/3/library/os.html#os.replace>

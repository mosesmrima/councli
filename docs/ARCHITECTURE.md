# councli architecture

`councli` is a local control plane for supported coding CLIs. The MVP supports
Codex (`codex`), Claude Code (`claude`), AGY (`agy`), Kimi Code (`kimi`), and
CodeWhale (`codewhale`). It does not try to replace those assistants. It hosts
their native terminal sessions, records coordination metadata, gives
participants shared visibility through blackboard artifacts, and uses headless
paths for automation when a CLI exposes a prompt-capable command.

## Goal

Use multiple coding assistants in one shared room so they can expose each
other's blind spots and produce a unified answer without forcing the user to
copy context between tools.

The default workflow is conversation, not governance:

```text
normal user prompt
  -> shared conversation turn
  -> one fan-out round to available participants
  -> shared blackboard artifacts
  -> one synthesized council answer
  -> prompt returns
```

Stronger coordination is explicit:

- `/deliberate <prompt>` asks participants to think independently, then gives
  them a peer-aware round before synthesis.
- `/vote <prompt>` asks for an explicit decision artifact.
- Hidden experimental execution/review commands may choose executors, use
  isolated git worktrees, and collect peer review, but they are not the MVP user
  path and require `COUNCLI_EXPERIMENTAL=1`.

The design rule is: `councli` provides the room, memory, routing, and safety
rails; the assistants provide the intelligence. Voting and executor selection
are tools, not protocol invariants.

See `PROTOCOL_DESIGN.md` for the target shared-turn envelopes, state machine,
sidecar schemas, locking rules, and recovery behavior.

See `SECURITY_MODEL.md` for the threat model, command trust boundary,
permission model, artifact secrecy rules, and hardening roadmap.

See `OPERATIONS_MODEL.md` for lifecycle states, cancellation, cleanup,
retention, observability, and runbook-level recovery behavior.

See `TERMINAL_SUBSTRATE.md` for the launch substrate: tmux, PTY, TUI, native
sessions, visible rooms, and the boundary between terminal visibility and
semantic collaboration.

See `ADAPTER_CONTRACT.md` for launch states, readiness probes,
capability-aware routing, failure taxonomy, and the response sidecar contract
adapters should converge toward.

See `STATE_CONCURRENCY.md` for event logs, blackboard projection, run-local
locks, crash recovery, retention, and the future SQLite WAL index.

## Layers

```text
CLI layer
  init, setup, trust, doctor, status, show, chat, broadcast, sessions

Protocol layer
  turn envelopes, prompts, participant responses, trailers, blackboard rendering,
  synthesis, optional vote parsing, decision rules

Runtime/process layer
  subprocess execution, timeouts, environment, cwd control, failure classification

Native host layer
  dedicated tmux server, project-scoped sessions, raw pane capture, native attach/detach

Workspace layer
  git repository checks; hidden/future execution may add worktree creation and
  diff capture

Artifact layer
  .councli/config.yaml
  .councli/project.json
  .councli/ledger/events.jsonl
  .councli/sessions/registry.json
  .councli/session-recordings/
  .councli/tasks/<task-id>/brief.md
  .councli/runs/<run-id>/
```

## Config trust boundary

Project repositories are not allowed to silently change assistant commands or
transport controls. `councli init` writes `.councli/config.yaml`, pins the
trusted agent fields in user-local councli state (`$COUNCLI_STATE_HOME`,
`$XDG_STATE_HOME/councli`, or `~/.local/state/councli`), and writes
`.councli/project.json`.

On load, `councli` checks the pinned hash for each agent's trusted control fields:
`enabled`, `backend`, `binary`, `command`, command capabilities,
`start_command`, `broadcast_command`, `broadcast_enabled`, `broadcast_policy`,
`broadcast_read_only`,
`resume_command`, session naming, prompt style, input method, submit keys, and
timeouts. If those fields change, the user must review the config and run
`councli trust`. If `.councli/` was intentionally moved with a project, the user
must run `councli trust --repair-identity` after reviewing the config.
Trust pins also record the resolved executable path, executable SHA-256 hash,
and trust-time version metadata for each enabled agent. If PATH later resolves
an assistant binary to a different path, or the binary content changes,
`councli` requires review and `councli trust` before running.
Native tmux names are limited to simple identifier characters, and
`native.detach_key` is limited to simple tmux key chords so tmux format strings
such as `#(...)` are rejected during config validation.

## Participant availability

Each configured participant has a binary check. Missing or disabled participants
are skipped. If one participant is unavailable, the council still runs with the
remaining participants.

For normal shared turns, unavailable participants are degraded for that turn and
recorded in the blackboard. They do not block responses from healthy
participants.

For explicit vote, legacy council, review, and run workflows, decision rules
apply:

- 3 agents: majority vote.
- 2 agents: unanimous vote required because majority is 2.
- 1 agent: single-agent mode with a transcript warning.
- 0 agents: no decision.
- Votes or reviews below `consensus.min_confidence` are recorded as evidence but
  excluded from majority counts.
- Abstentions and malformed outputs degrade that participant for the phase; they
  do not automatically veto other participants.

Authentication and quota failures are currently detected from command failure
text and recorded as participant failures. This is weak evidence: adapters
should grow explicit probe commands for auth, quota, model readiness, and
provider selection.

## Native feature boundary

`councli` does not emulate native participant features in the MVP. It does not
translate prompts into Codex goals, Claude slash commands, AGY slash commands,
Kimi provider/session commands, CodeWhale shortcuts, `@file` mentions, MCP
configuration, plugins, or subagent orchestration.

That boundary is intentional:

- Native features keep behaving exactly as their owning tool implements them.
- `councli` stays small enough to debug and test.
- A council transcript remains portable across participants because it is just
  files, packets, votes, diffs, and review decisions.

If the user wants to use a native feature, they attach to the participant's
normal session and invoke it directly. In native attach mode, `councli` does not
parse keystrokes or reserve slash commands. The only control path is the tmux
detach chord (`Ctrl-]` by default), which returns to `councli`.

## Transport model

`councli` has three different integration surfaces. They should not be confused:

- `exec`: a headless subprocess call with captured stdout/stderr. This is the
  primary path for structured shared turns, votes, broadcast, and repeatable
  automation.
- `tmux`: a persistent terminal session manager. This is the substrate for
  native assistant sessions, attach/detach, liveness checks, pane capture, and
  hot resume.
- `PTY/TUI`: the operating-system terminal abstraction and the assistant's
  screen UI. These are necessary for native interaction but are not reliable
  semantic APIs.

The architectural invariant is: tmux/PTY/TUI output may be used for visibility,
debugging, and user attachment; machine coordination should flow through
structured prompts, files, events, and explicit command outputs.

## Backends

`exec` backend:

```text
councli -> subprocess -> agent one-shot command -> captured stdout/stderr
```

This is the cleanest path for automation and repeatability.

Implementation implications:

- Use subprocess timeouts and explicit cwd.
- Treat stdout/stderr as untrusted text until parsed.
- Prefer a real machine format from the upstream CLI when available.
- Record command argv and exit status for auditability.

`tmux` backend:

```text
councli -> dedicated tmux server -> native assistant TUI
```

This is for participants that are already authenticated or behave better in
native interactive mode. Sessions run under a dedicated tmux socket, default
`tmux -L councli`, one session per assistant instance, and are launched in the
project root. Session names include a project hash, so two repositories do not
collide inside the shared socket. The user can attach to the assistant and use
its full UI. `councli` starts raw pane capture with `pipe-pane`; the raw
recording is audit/debug data, not the semantic transcript.

`councli` treats tmux as the liveness source of truth. `registry.json` stores
annotations such as cwd, capture paths, native session ids, and command history,
but `sessions list` reconciles it against `tmux list-sessions` and marks missing,
dead, shell-returned, or cwd-mismatched sessions as stale. It also records
`pane_pid`, `pane_current_command`, and `pane_dead` so a surviving shell is not
reported as a healthy assistant.

The detach chord defaults to `Ctrl-]` and is configurable through
`native.detach_key`. `councli` unsets `$TMUX` before attaching so nested tmux
users can still enter the dedicated councli server.

Raw recordings are written by a small rotating pipe process using
`native.raw_log_max_bytes` and `native.raw_log_backups`. Files and recording
directories are private (`0600` files, best-effort `0700` directories).
`councli init` adds `.councli/` to `.gitignore` when the project is a git repo,
because raw terminal logs may contain secrets.

Where possible, tmux hooks (`client-attached`, `client-detached`,
`session-closed`) append project-ledger events so attach/detach/crash events are
visible even when the wrapper process misses them.

Session registry updates use an exclusive lock plus atomic write-rename. Hooks
and CLI commands can write concurrently without corrupting `registry.json`.

For automation-only paths, `councli` can still use adapter-specific terminal
input:

- `paste`: `tmux load-buffer` + `tmux paste-buffer`.
- `type`: literal `tmux send-keys -l` chunks for TUIs that keep bracketed paste
  in a draft composer.

Hidden terminal automation prototypes can wait until the pane contains a unique
per-turn done marker:

```text
<<<COUNCLI_DONE:participant:uuid>>>
```

For legacy `council` phases, the required completion signal is the requested
artifact file under `.councli/runs/<run>/incoming/...`. If a participant does
not write the file, `councli` retries once and then marks that phase response
failed or abstained. Terminal markers and stdout remain useful for diagnostics,
but screen capture is not accepted as the blackboard source of truth because
TUIs can wrap, split, repaint, or retain old text.

Public native-session commands:

- `sessions start`: launch an assistant in a project-scoped tmux session.
- `sessions attach`: attach to an assistant's native terminal. Press `Ctrl-]`
  to return.
- `sessions capture`, `sessions stop`, and `sessions prune`: inspect and clean
  native-session artifacts.

## Project ledger and task briefs

Native sessions have project-level metadata outside run-specific blackboards:

```text
.councli/ledger/events.jsonl
.councli/sessions/registry.json
.councli/session-recordings/<agent>.raw.log
.councli/session-snapshots/<timestamp>-<agent>.txt
```

The project ledger records lifecycle events, routing events, attach/detach
events, shared turns, broadcast runs, and pointers to raw recordings or
snapshots. The ledger stores metadata and artifact references; it does not treat
terminal screen capture as authoritative semantic conversation history.

Before shared turns, council runs, and broadcast runs, `councli` writes:

```text
.councli/tasks/<run-id>/brief.md
.councli/runs/<run-id>/brief.md
```

The brief contains the user task, active native sessions, recent `councli`
events, and artifact pointers. Assistants are told to read this file when shared
context matters. This keeps context inspectable, editable, and identical across
assistants.

## Broadcast mode

`councli broadcast` and `/broadcast` send a prompt to all selected assistants
that have a prompt-capable non-interactive command.

Broadcast is headless subprocess communication. It does not feed active tmux
assistant sessions and therefore does not share native interactive context unless
the prompt tells the assistant to inspect the task brief or referenced artifacts.
The MVP retry policy is none: unavailable or failed participants are recorded
and skipped.

Broadcast is not a multi-writer execution mode. It is for planning, critique,
comparison, and review. Parallel editing in one shared worktree is intentionally
out of scope for v1.

Broadcast uses each adapter's `broadcast_command` when configured. If an adapter
does not provide one, the current implementation can fall back to the normal
prompt-capable `command` and records whether a broadcast-specific command was
explicit. This removes avoidable blockers but weakens the claim that broadcast
is read-only. For production, adapters should expose capabilities instead of a
single boolean: planning-only, may-read-files, may-write-files, may-run-tools,
network-enabled, and yolo/full-permission.
Broadcast artifacts include per-assistant status, command, exit code, and error
metadata so partial failures are visible.

## Native import and resume

Cold native resume is adapter-specific and remains hidden while the MVP focuses
on hot project-scoped tmux sessions. The target design is still to record an
explicit native session id or path, then launch the adapter's native
`resume_command`; `councli` should never silently choose an ambiguous latest
session.

## Shared-turn artifact protocol

Participants do not talk to each other through hidden in-memory state. They
communicate through durable turn artifacts:

```text
.councli/runs/<run-id>/
  task.md
  participants.json
  events.jsonl
  state.json
  blackboard.md
  packets/
  shared/
    chat.round1/
    chat.round2/
    deliberate.round1/
    deliberate.round2/
    vote.round1/
  decision.json              # only for explicit vote or legacy decision runs
  implementation/
  review/
```

This makes the process inspectable, retryable, and easy to debug. Live PTY
collaboration can be added later without replacing the artifact protocol.

The current trailer format is intentionally small:

```text
COUNCLI_TRAILER
continue: false
recommend: none
summary: one short line
```

This is adequate for an MVP, but it is a weak protocol. It relies on natural
language compliance, is hard to validate, and can be confused by model output.
The hardening path is a sidecar JSON document per participant response, with
the Markdown body kept as human-readable evidence.

## Run inspection and resume context

`councli status` lists recent artifact runs with the task, participants,
decision status, review status, and implementation result. `councli show
latest` or `councli show <run-id-prefix>` reopens a specific run and prints the
durable artifact paths. `--blackboard` prints the shared transcript directly.

This is the MVP resume surface. It resumes the council context from files rather
than attempting to drive each participant's private native conversation history.

## Interactive shell

`councli chat` is the routing shell. A normal line becomes a new shared
conversation turn.
Local slash commands (`/status`, `/show`, `/doctor`, `/sessions`,
`/assistant <name> [instance]`, `/broadcast <prompt>`, `/brief [task]`,
`/deliberate <prompt>`, `/vote <prompt>`, `/council <prompt>`, `/quit`) operate on councli artifacts and configured
sessions only. Unknown slash commands are rejected explicitly. A line prefixed
with `//` is treated as a literal task beginning with `/`.

`/assistant <name>` attaches to the selected assistant's native tmux session.
While attached, the assistant owns the terminal. Native slash commands,
autocomplete, permission prompts, MCP interactions, and hotkeys stay native.
`councli` does not parse `/back` or any other typed command in this mode.

## Hidden worktree execution prototype

The hidden execution prototype creates a git worktree outside the repository:

It is intentionally gated behind `COUNCLI_EXPERIMENTAL=1`, because the MVP
control plane is shared conversation, explicit deliberation/voting, native
attach, broadcast, and artifact inspection.

```text
../.councli-worktrees/<repo-name>/<run-id>-<executor>
```

The selected executor runs there. The main working tree is not edited during
execution. `councli` records the resulting diff but does not merge
automatically.
The review diff is captured against the worktree's base commit so committed
executor changes are visible to reviewers, not only unstaged changes.

The hidden apply prototype is the explicit handoff from accepted implementation
to the main worktree. It requires:

- the run completed with `implemented: true`;
- peer review verdict `accepted` unless `--allow-unreviewed` is passed for an
  intentional single-participant run;
- a clean current worktree;
- the current commit still matches the executor worktree base, unless `--force`
  is passed after manual review.

`--dry-run` checks patch applicability without changing files.

## Working directory invariant

Participants must run from the project directory where `councli` is launched,
matching how the same assistant would behave when started manually in that
directory.

Current rules:

- shared turns run prompt-capable participant commands with `cwd=<project root>`.
- broadcast runs prompt-capable participant commands with `cwd=<project root>`.
- `reason` runs all participants with `cwd=<project root>`.
- tmux sessions are started with `tmux -L <native.tmux_socket> new-session -c <project root>`.
- existing tmux sessions are reused only if their active pane is already in the same project root.
- `run` performs deliberation in the project root, then runs the selected executor inside the isolated git worktree by design.

This prevents accidentally sending a project prompt into a long-lived native session that is still attached to another repository.

## Architecture hardening roadmap

The current implementation is an MVP. The production hardening work should be
driven by these system boundaries rather than UI features:

1. Add adapter-specific probe commands for auth, quota, model readiness,
   provider selection, and tool-permission mode, following the readiness states
   in `ADAPTER_CONTRACT.md`.
2. Continue tightening validated sidecar JSON envelopes while keeping Markdown
   artifacts for human inspection.
3. Keep artifact files for large bodies, diffs, and logs, but add a SQLite WAL
   index for runs, participants, events, statuses, and searches once run volume
   grows.
4. Add retention and redaction policies for raw terminal recordings, command
   output, prompts, and blackboards because they can contain credentials.
6. Model observability explicitly: turn id as trace id, participant calls as
   spans, events as structured logs, and latency/error/token counts as metrics.
7. Keep tmux as the native-session substrate, not the semantic protocol. Add a
   PTY/expect adapter only for CLIs that lack usable headless commands, and
   treat it as best-effort automation.
8. Add optional background supervision only after the foreground CLI semantics
   are stable. On Linux, systemd user units are the natural primitive for
   long-lived local daemons; until then, tmux owns hot sessions.

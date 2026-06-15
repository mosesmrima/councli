# councli research findings and implementation reference

This is the canonical handoff document for the architecture research. It
consolidates findings that were previously spread across discussion and the
topic-specific docs. Use this file as the implementation reference before
changing launch, adapter, protocol, state, or security behavior.

For the detailed launch matrix, adapter contract, and communication protocol
research, see `AGENT_LAUNCH_PROTOCOL.md`. For the explicit tradeoff analysis
and rejected alternatives, see `ARCHITECTURE_DECISIONS.md`.

## Core conclusion

`councli` should be a local control plane, not another coding agent and not a
fixed workflow engine.

```text
councli = local control plane
        + adapter launcher
        + shared artifact protocol
        + native session host
        + explicit governance commands
```

It should provide the room, routing, durable memory, evidence, policy, and
recovery. The assistants provide the intelligence.

The main product promise is a transparent council of agents. A prompt may fan
out to individual assistants first, but their outputs must become visible
shared context. When the task needs more than a simple answer, participants
should be able to respond to each other, challenge weak points, reconcile
disagreements, and produce a common refined output from their interaction.
`councli` is valuable because it makes that council process inspectable and
repeatable without forcing the user to manually copy context between tools.

Hard boundary: `councli` launches the real assistant binaries already installed
on the system. It must not replace them, fork them, reimplement them, or require
users to run a councli-owned imitation wrapper. Adapter code is a
launch/probe/capture contract around commands such as `codex`, `claude`, `agy`,
`kimi`, and `codewhale`.

The default behavior is shared conversation:

```text
user prompt
  -> route by intent
  -> fan out to ready participants
  -> collect response artifacts
  -> render blackboard projection
  -> synthesize one answer
  -> return to prompt
```

Voting, deliberation, review, single-executor execution, and parallel worktree
execution are explicit user-selected coordination policies. They are not
protocol invariants.

## Current-state assessment

The repository has the right design direction in documentation:

- `ARCHITECTURE.md`: control-plane model, native feature boundary, default chat
  versus explicit governance.
- `ADAPTER_CONTRACT.md`: target adapter manifest, readiness states, capability
  routing, response sidecars.
- `PROTOCOL_DESIGN.md`: turn envelopes, artifact layout, event contract,
  locking direction.
- `STATE_CONCURRENCY.md`: source-of-truth artifacts, blackboard projection,
  run-local locks, recovery.
- `SECURITY_MODEL.md`: trust boundaries, yolo/full-permission risk, path drift,
  secret handling.
- `OPERATIONS_MODEL.md`: cancellation, cleanup, metrics, background supervision
  posture.
- `TERMINAL_SUBSTRATE.md`: tmux/PTY/TUI boundaries.

The implementation is still MVP-grade in several places:

- `AgentRunner.health()` mostly proves binary presence, not intent readiness.
- Shared turns still parse `COUNCLI_TRAILER` text as a fallback; response
  sidecars exist but need stricter validation before every machine decision.
- The blackboard renderer still privileges legacy phases.
- Failure classification is mostly stderr text heuristics.
- `broadcast_read_only` is a boolean rather than a command capability model.
- Headless exec timeout and foreground Ctrl-C cleanup terminate active agent
  process groups; cancellation state still needs to be made consistent across
  all commands.

## Non-negotiable architecture invariants

1. Binary exists does not mean ready.
2. Readiness is per intent, not global.
3. Terminal output is not protocol state.
4. Blackboard is a projection, not a shared mutable document.
5. File edits happen only in explicit execution modes.
6. Worktrees isolate source changes, not secrets, home directory, network, or
   shell tools.
7. Full permission is policy, not a default assumption.
8. Votes happen only when explicitly requested by user command or execution
   policy.
9. Machine decisions require validated sidecars and artifact provenance.
10. Every run must be recoverable from events plus immutable artifacts.

## Adapter and launch contract

The biggest product risk is the connector layer. Each assistant should be
modeled as an adapter manifest plus probe/launch/parse behavior.

Target adapter shape:

```yaml
schema_version: councli.adapter.v1
name: codex
display_name: Codex CLI
binary: codex

version_command:
  argv: ["codex", "--version"]
  parse: semver_text

commands:
  chat:
    argv: ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "{prompt}"]
    transport: exec_arg
    output_contract: text_with_sidecar
    capabilities: ["reads_workspace", "planning_only"]

  execute:
    argv: ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "{prompt}"]
    transport: exec_arg
    output_contract: diff_plus_summary
    capabilities: ["reads_workspace", "writes_workspace", "runs_tools"]

  native_start:
    argv: ["codex", "--no-alt-screen"]
    transport: tmux_native
    capabilities: ["native_session"]

probes:
  binary: required
  version: optional
  auth: best_effort
  model: best_effort
  quota: best_effort
```

Routing should use this readiness chain:

```text
configured
  -> trust_checked
  -> binary_resolved
  -> version_detected
  -> command_selected_for_intent
  -> policy_checked
  -> probe_checked
  -> launched
  -> output_validated
```

Failure states should be normalized:

```text
disabled
untrusted_config
missing_binary
binary_drift
unsupported_intent
policy_denied
auth_required
quota_unavailable
model_unconfigured
provider_unconfigured
tmux_unavailable
wrong_cwd
launch_failed
timeout
canceled
malformed_output
artifact_missing
```

## Intent policies

| Intent | Command requirement | Permission policy | Output requirement |
| --- | --- | --- | --- |
| `chat` | prompt-capable headless preferred | reads allowed, no writes | text body plus response sidecar |
| `deliberate` | prompt-capable headless | reads allowed, no writes | text body plus response sidecar |
| `vote` | prompt-capable headless | reads allowed, no writes | structured decision JSON |
| `broadcast` | planning command preferred | planning/read-only preferred, explicit full-permission fallback only | text plus status metadata |
| `review` | prompt-capable headless | read diff/worktree, no main-worktree writes | verdict JSON plus notes |
| `execute` | executor command | writes only inside isolated worktree | diff plus execution summary |
| `assistant` | tmux native | native tool owns permissions | attach/detach/raw-log events |
| `visible_room` | tmux native | native tool owns permissions | operator view only |

## Communication protocol

The recommended communication architecture is an event-sourced artifact bus.
Do not build direct peer-to-peer CLI communication as v1.

Target turn layout:

```text
.councli/runs/<turn-id>/
  request.json
  task.md
  participants.json
  events.jsonl
  run.lock
  packets/
    <participant>/
      000001-chat.round1.md
  shared/
    chat.round1/
      codex.md
      codex.response.json
      claude.md
      claude.response.json
  synthesis/
    synthesis.md
    synthesis.response.json
  decisions/
    vote.json
  blackboard.md
  state.json
  blobs/
```

Source-of-truth rule:

```text
events + artifacts = source of truth
state.json + blackboard.md + indexes = rebuildable projections
tmux/terminal output = diagnostic evidence only
```

Participants should see each other through prior artifacts and blackboard
snapshots, not through hidden native session memory. A second round is allowed
only when the intent requires it or a validated sidecar requests continuation.

## Response sidecar contract

The current `COUNCLI_TRAILER` is useful for early testing, but it is not strong
enough for machine decisions.

Target participant sidecar:

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
  "tool_effects": {
    "edited_files": [],
    "commands_run": [],
    "network_used": "unknown"
  },
  "error": null,
  "timing": {
    "started_at": "2026-06-10T12:00:01Z",
    "ended_at": "2026-06-10T12:00:12Z",
    "duration_ms": 11000
  }
}
```

Rules:

- Markdown is human evidence.
- The sidecar is the machine contract.
- Missing or invalid sidecars may be displayed but cannot approve votes,
  executor selection, review, or apply.
- Sidecar paths must be relative to the run directory unless schema explicitly
  permits an external ref.
- Participant self-reported tool effects are audit data, not enforcement.

## State and concurrency

Use append-only JSONL events as source evidence. Use `fcntl.flock` around
run-local writes. Use atomic replace for projections.

Run-local write protocol:

1. Write immutable body/blob files first when possible.
2. Acquire `run.lock`.
3. Allocate event sequence.
4. Append event to `events.jsonl`.
5. Flush and fsync when durability matters.
6. Render `state.json` and `blackboard.md` under the lock or via a single
   projection writer.
7. Release lock.

Never hold a run lock while waiting for a model call, subprocess, tmux pane, or
network operation.

SQLite WAL should be added later as a rebuildable index, not as the only source
of truth.

## Terminal model

There are three planes:

```text
Semantic plane:
  request/response/events/artifacts/blackboard/synthesis/decisions

Native plane:
  tmux sessions for each assistant's real TUI and native commands

Operator plane:
  councli prompt or future TUI showing status, turns, artifacts, commands
```

tmux is correct for native sessions, attach/detach, visible rooms, liveness, and
raw diagnostics. It is not the semantic protocol. PTY/expect automation should
be a last-resort adapter for tools without usable headless commands.

## Execution and worktrees

Execution policy should be explicit:

- `/single` or `run`: one executor in one git worktree.
- `/parallel`: one worktree per selected executor, then diff/test/review.
- `/review`: inspect existing diff/artifact.
- `/assistant <name>`: native attach, not semantic execution.

Git worktrees are the right source isolation primitive because they create a
separate working tree and index while sharing repository data. They are not a
security sandbox. Full-permission/yolo agents still have access to the user's
credentials, home directory, network, and shell unless an external sandbox is
used.

## Security posture

Trust boundaries:

```text
user intent
  -> councli CLI
  -> project .councli/config.yaml        # repo-owned, not inherently trusted
  -> user-local trust pin                # user authorization
  -> participant subprocess/tmux session # external executable boundary
  -> artifacts/raw logs                  # sensitive local state
```

Minimum hardening:

- Keep command-bearing config fields pinned in user-local trust state.
- Record resolved binary path at trust time and ideally add version/hash later.
- Require retrust on binary path drift.
- Validate tmux session names and detach keys.
- Reject prompt templates where `{prompt}` is embedded in a larger argv token
  unless explicitly allowed.
- Keep `.councli/` gitignored and raw logs private.
- Add redaction, retention, and `export --redacted` before team sharing.
- Treat participant output as untrusted until validated.

## Observability model

Do not add an OpenTelemetry dependency yet. Make events compatible with tracing
concepts:

```text
trace_id = run_id
span_id = participant.codex.chat.round1
parent_span_id = turn
```

Minimum metrics derivable from events:

- turns by intent/status;
- participant calls by participant/status/error class;
- participant duration;
- timeouts;
- rounds per turn;
- synthesis fallback count;
- artifact bytes;
- raw log bytes;
- worktrees by status.

## External primitives and sources

Use existing standards and tools where they fit:

- OpenAI Codex non-interactive mode: `codex exec`, stdout/stderr behavior,
  sandbox modes, and danger-full-access guidance:
  <https://developers.openai.com/codex/noninteractive>
- Claude Code CLI reference: permission modes, dangerous skip-permission flag,
  non-interactive behavior, advisor/subagent flags:
  <https://code.claude.com/docs/en/cli-reference>
- Kimi Code CLI reference: `--prompt`, `--output-format`, yolo/auto/plan
  conflicts:
  <https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html>
- CodeWhale modes: Plan, Agent, YOLO, approval behavior, trust mode,
  stream-json execution:
  <https://github.com/Hmbown/CodeWhale/blob/main/docs/MODES.md>
- MCP transports: subprocess stdio discipline, JSON-RPC messages, stderr as
  diagnostics:
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- A2A specification: agent cards, capabilities, tasks, artifacts, multi-turn
  context:
  <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
- JSON-RPC 2.0: request/response ids, result/error exclusivity:
  <https://www.jsonrpc.org/specification>
- JSON Schema Draft 2020-12: sidecar and manifest validation:
  <https://json-schema.org/draft/2020-12>
- Python subprocess: cwd, env, stdout/stderr capture, timeout behavior:
  <https://docs.python.org/3/library/subprocess.html>
- Python fcntl: local advisory file locks:
  <https://docs.python.org/3/library/fcntl.html>
- tmux manual: sessions, panes, socket names, command parsing, capture:
  <https://man7.org/linux/man-pages/man1/tmux.1.html>
- Linux pty manual: terminal byte stream semantics:
  <https://man7.org/linux/man-pages/man7/pty.7.html>
- git worktree: separate working trees sharing repository data:
  <https://git-scm.com/docs/git-worktree>
- SQLite WAL: local reader/writer concurrency and index candidate:
  <https://sqlite.org/wal.html>
- systemd-run: transient service/scope units for future background jobs:
  <https://man7.org/linux/man-pages/man1/systemd-run.1.html>
- Linux namespaces and bubblewrap: optional future sandbox wrappers:
  <https://man7.org/linux/man-pages/man7/namespaces.7.html>
  <https://github.com/containers/bubblewrap>

## Known current inconsistencies

- `docs/ADAPTERS.md` previously recommended combining Kimi prompt mode with
  yolo/auto. That is invalid according to Kimi CLI docs and contradicts the
  current default config and tests. The correct headless Kimi command is
  `kimi --prompt {prompt}`.
- `doctor --json` reports intent readiness, but most adapters still need safe
  default probes for auth/model/quota.
- Shared-turn trailers are still text fallbacks. `/vote` decisions now require
  valid response sidecars; review, executor selection, and apply need the same
  stricter validation.

## Production readiness gates

Treat the system as not production-grade until these gates pass:

1. `doctor --json` reports readiness per intent.
2. Adapter manifests or config fields declare command capabilities.
3. Full-permission commands are rejected for read-only intents unless explicit
   fallback is configured.
4. Binary path drift after trust is detected; version/hash drift is a remaining
   hardening step.
5. Participant response sidecars are emitted and validated before every
   remaining machine decision.
6. Review, executor selection, and apply reject missing or invalid sidecars.
7. Run event writes use cross-process `fcntl.flock`.
8. `councli verify` and `runs recover` can rebuild projections and detect missing
   refs.
9. Ctrl-C consistently records cancellation across commands and terminates
   participant process groups.
10. tmux sessions in the wrong cwd are live but not ready.
11. Retention/redaction controls exist for raw logs and artifacts.
12. Integration tests cover each supported assistant command template with fake
   binaries or safe real dry runs.

## Suggested implementation sequence

1. Fix docs and command-template inconsistencies.
2. Add adapter capability metadata while preserving existing config fields.
3. Add intent-specific readiness model and `doctor --json`.
4. Add response sidecars for shared turns while retaining `COUNCLI_TRAILER` as
   fallback.
5. Extend sidecar validation from `/vote` to review, executor selection, and
   apply decisions.
6. Add run-local `fcntl.flock` around `EventLedger` appends and projections.
7. Add normalized failure classification.
8. Add binary version/hash trust drift checks.
9. Finish consistent canceled-state recording for all foreground commands.
10. Add `councli verify`, `runs recover`, and bounded context packing.
11. Add retention/redaction and optional metrics export.
12. Add SQLite WAL index only after the artifact protocol is stable.

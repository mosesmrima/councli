# Agent launch and communication protocol

This document records the research-backed target contract for launching coding
assistants and making their collaboration transparent inside `councli`.

It is not an implementation patch. It is the reference point to use before
changing adapter, readiness, native-session, or shared-turn behavior.

## Scope

`councli` should coordinate existing CLI coding assistants. It should not
rebuild them.

Hard constraint: adapters launch the real installed binaries. `councli` should
not ship replacement agents, fork the assistants, reimplement their native
features, or require users to run a councli-owned wrapper instead of `codex`,
`claude`, `agy`, `kimi`, `codewhale`, or any future tool. An adapter is only a
typed launch/probe/capture contract around the user's actual command.

The hard problem is not "how do we call a command?" The hard problem is:

- selecting the right launch surface for the user's intent;
- knowing whether an assistant is actually ready for that intent;
- preserving native tools, slash commands, permissions, sessions, MCP setup,
  and provider configuration;
- making all assistant outputs visible to the other assistants;
- turning untrusted terminal/model output into validated protocol artifacts;
- allowing explicit governance commands without making governance the default
  lifecycle.

## Evidence classes

Use three evidence levels when designing adapters:

1. `observed-local`: behavior verified from the installed binary on this
   machine.
2. `documented`: behavior stated in vendor or protocol documentation.
3. `inferred`: behavior inferred from error messages, source code, or
   experiments. This should not become a hard invariant without a probe.

Adapter code should prefer `observed-local` at runtime and use `documented`
behavior as the baseline.

## Core architecture

The durable architecture is:

```text
user
  -> councli operator shell
  -> intent router
  -> adapter readiness check
  -> participant launch
  -> artifact/event bus
  -> blackboard projection
  -> synthesis or explicit decision
  -> user
```

The architecture is not:

```text
user
  -> fixed orient/propose/critique/revise/vote lifecycle
```

The default prompt is a shared conversation turn. Slash commands opt into
specific coordination policies:

- `/deliberate <prompt>`: ask for independent views and optionally a follow-up
  round.
- `/vote <prompt>`: request structured convergence.
- `/review <target>`: inspect an existing diff or artifact.
- `/parallel <task>`: run isolated implementations, then compare.
- `/single <task>` or `run <task>`: choose one executor policy.
- `/assistant <name>`: attach to the native assistant session.

These are policies over the same protocol. They are not separate protocols.

## Launch surfaces

### Headless exec

```text
councli -> subprocess argv/stdin -> assistant one-shot command
```

Best for:

- normal shared chat;
- deliberation;
- voting;
- broadcast;
- review;
- synthesis;
- repeatable tests and CI-style automation.

Strengths:

- explicit `cwd`;
- explicit argv;
- captured stdout and stderr;
- return code;
- timeout;
- easy fake-binary tests;
- artifacts can record exact command and status.

Risks:

- may not share native TUI session context;
- may reload project/model context on each call;
- output is untrusted unless schema validated;
- cancellation must terminate the process group, not only the direct child;
- prompt-as-argv can hit length limits or leak through process listings.

Preferred prompt transport:

1. Use stdin or prompt-file transport when the CLI supports it.
2. Use argv prompt only when the CLI has no better stable surface.
3. Never build a shell string. Always pass an argv list.

### Structured stream

```text
councli -> subprocess -> JSON/JSONL events -> validated sidecars
```

Best for tools that expose JSON or stream JSON:

- `codex exec --json`
- `claude -p --output-format json`
- `claude -p --output-format stream-json`
- `kimi -p --output-format stream-json`
- `codewhale exec --output-format stream-json`

Strengths:

- better status/progress data;
- lower parsing ambiguity;
- easier streaming UI;
- easier synthesis of failures and partial output.

Risks:

- schemas are vendor-specific;
- JSON streams may include event types not useful to `councli`;
- not all tools expose the same final-answer field;
- output validation is still needed.

Decision: support structured streams as adapter-specific parsers, but normalize
them into `councli.response.v1` sidecars.

### Native tmux session

```text
councli -> tmux -L councli new-session -c <project> -> assistant TUI
```

Best for:

- native slash commands;
- auth/login/provider setup;
- manual permission handling;
- direct assistant mode;
- long-lived assistant sessions;
- visible rooms;
- preserving each tool's own UX.

Strengths:

- preserves native richness;
- supports attach/detach;
- survives `councli` exiting;
- good for human control.

Risks:

- screen text is not protocol state;
- prompt injection through paste/send-keys is brittle;
- a live session can be at a login prompt, editor, pager, shell, or crashed
  state;
- raw pane capture can contain secrets.

Decision: tmux is the native session host, not the semantic protocol.

### PTY/expect automation

```text
councli -> pty/expect -> assistant TUI
```

Use only as a last resort.

PTY automation handles terminal bytes, not intent. It is brittle because TUI
programs can redraw, wrap text, use alternate buffers, echo input, split paste
events, change prompts, or alter behavior by terminal size.

Decision: do not use PTY/expect as the default collaboration protocol.

### Daemon, server, MCP, ACP, and A2A surfaces

Some assistants expose server or protocol modes:

- Codex exposes MCP server and app-server surfaces.
- Kimi exposes an ACP server over stdio.
- CodeWhale exposes MCP server and app-server surfaces.
- Claude exposes background agents, remote-control, and JSON/stream JSON print
  mode.

These surfaces may become better long-term connectors than terminal automation,
but they are not uniform across all agents today.

Decision for v1: use an artifact bus plus adapter-specific headless commands.
Borrow concepts from MCP, JSON-RPC, and A2A, but do not require every local CLI
to become an A2A or MCP server.

## Local CLI matrix

This table records the installed tools observed on this machine on
2026-06-11.

| Tool | Version | Path | Useful headless surface | Structured output | Native surface | Permission bypass / policy | Key readiness probe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | `codex-cli 0.139.0` | `/home/mrima/.nvm/versions/node/v24.13.1/bin/codex` | `codex exec [PROMPT]`, `codex exec -` for stdin | `--json`, `--output-last-message`, `--output-schema` | `codex --no-alt-screen`, `codex resume <id>` | `--sandbox read-only|workspace-write|danger-full-access`, `--dangerously-bypass-approvals-and-sandbox` | `codex doctor`, `codex --version`, command dry run |
| Claude Code | `2.1.172` | `/home/mrima/.local/bin/claude` | `claude -p "prompt"` | `--output-format json|stream-json`, `--json-schema` | `claude`, `claude --resume <id>`, background agents | `--permission-mode auto|plan|bypassPermissions`, `--dangerously-skip-permissions` | `claude auth status`, bounded `claude -p` probe |
| AGY | `1.0.7` | `/home/mrima/.local/bin/agy` | `agy -p "prompt"` / `agy --print` | none observed in local help | `agy`, `agy -i`, `agy --conversation <id>` | `--sandbox`, `--dangerously-skip-permissions` | `agy models`, bounded `agy -p` probe |
| Kimi Code | `0.12.1` | `/home/mrima/.local/bin/kimi` | `kimi -p "prompt"` | `--output-format text|stream-json` with `--prompt` | `kimi`, `kimi --session <id>`, `kimi --continue` | `--yolo`, `--auto`, `--plan` only for interactive/session mode | `kimi doctor`, `kimi provider list`, bounded `kimi -p` probe |
| CodeWhale | `0.8.53` | `/home/mrima/.nvm/versions/node/v24.13.1/bin/codewhale` | `codewhale exec "prompt"` | `codewhale exec --output-format stream-json`, `--json` | `codewhale`, `codewhale resume <id>` | `--yolo`, `exec --auto`, approval mode config | `codewhale doctor`, `codewhale auth status --provider <id>` |
| tmux | `3.5a` | `/usr/bin/tmux` | not an assistant | not applicable | session host | not applicable | `tmux -V`, session/pane inspection |

Important adapter caveats:

- Codex has the best documented noninteractive automation surface. Prefer
  `codex exec` over TUI scraping for semantic turns.
- Claude has strong structured print mode and explicit permission modes. Its
  `--dangerously-skip-permissions` is equivalent to `--permission-mode
  bypassPermissions` in the docs.
- AGY has a clean one-shot print mode but no structured output was observed in
  local help. Treat it as text-plus-sidecar until proven otherwise.
- Kimi explicitly forbids combining `--prompt` with `--yolo`, `--auto`, or
  `--plan`. Prompt mode uses its own auto permission behavior.
- CodeWhale exposes `exec --auto --output-format stream-json`, plus auth and
  doctor commands. That is better for automation than scraping the TUI.

## Adapter manifest v1

The adapter should be a capability manifest, not a boolean config.

Target shape:

```yaml
schema_version: councli.adapter.v1
name: codex
display_name: Codex CLI
binary: codex

version:
  argv: ["codex", "--version"]
  parse: semver_text

commands:
  chat:
    argv: ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-"]
    prompt_transport: stdin
    output_contract: text_with_sidecar
    capabilities: ["reads_workspace", "planning_only"]
    timeout_seconds: 900

  execute:
    argv: ["codex", "exec", "--sandbox", "danger-full-access", "--skip-git-repo-check", "-"]
    prompt_transport: stdin
    output_contract: diff_plus_summary
    capabilities: ["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"]
    timeout_seconds: 1800

  native_start:
    argv: ["codex", "--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"]
    transport: tmux_native
    capabilities: ["native_session", "full_permission"]

probes:
  binary:
    method: which
  version:
    command_ref: version
  auth:
    argv: ["codex", "doctor"]
    optional: true
  model:
    method: bounded_prompt
    optional: true
```

Compatibility mapping:

- old `command` maps to `commands.chat`;
- old `broadcast_command` maps to `commands.broadcast` or
  `commands.planning`;
- old `start_command` maps to `commands.native_start`;
- old `resume_command` maps to `commands.native_resume`;
- old `broadcast_read_only` becomes advisory only until explicit capabilities
  exist.

## Readiness state machine

Do not report one global `available` state.

Use intent-specific readiness:

```text
configured
  -> trust_checked
  -> binary_resolved
  -> version_detected
  -> command_selected_for_intent
  -> policy_checked
  -> probe_checked
  -> launchable
  -> output_validated
  -> ready
```

Failure states:

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
wrong_process
stale_session
launch_failed
timeout
canceled
malformed_output
artifact_missing
```

Examples:

- Kimi without a configured provider is not ready for `chat`, but can still be
  ready for `/assistant kimi` so the user can run setup natively.
- Claude may be installed and authenticated but blocked by subscription or
  organization policy. That is not `available`; it is `auth_required` or
  `quota_unavailable` depending on the probe evidence.
- A live tmux pane in another repository is `live` but `wrong_cwd` for the
  current project.
- A full-permission command may be ready for `execute` but `policy_denied` for
  normal `chat` unless explicit fallback policy is configured.

## Probe rules

Probes must be cheap, bounded, and non-mutating.

Minimum probe set:

1. Binary path: `which`/`shutil.which`.
2. Version: `--version` or equivalent.
3. Command shape: selected command contains a legal prompt transport.
4. Trust: resolved path and command-bearing fields match the user-local trust
   pin.
5. Cwd: selected project root is the execution root.
6. Auth/provider/model: best effort through native doctor/auth commands or a
   bounded no-op prompt.
7. Output contract: response sidecar or structured output validates.

Probe results should have TTLs:

- binary/version/trust: cache until config or path changes;
- auth/provider/model: short TTL, for example 5 minutes;
- quota/rate limit: short TTL, maybe 1 minute;
- live tmux state: no cache beyond a command invocation.

## Communication protocol

Use an event-sourced artifact bus. Do not build direct peer-to-peer terminal
chat as v1.

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

Source of truth:

```text
events.jsonl + immutable artifacts = protocol truth
state.json + blackboard.md = rebuildable projections
tmux pane output = diagnostic evidence
```

Participant visibility:

- every participant receives the user prompt;
- every later round receives a bounded blackboard excerpt and refs to complete
  artifacts;
- no participant receives hidden private context from another assistant's
  native session unless that context was explicitly exported into the
  blackboard;
- failed and degraded participants remain visible in the blackboard.

## Turn, round, and session semantics

The current UI confusion around "Round 1" and multiple turns is naming, not a
protocol problem.

Definitions:

- `chat session`: the lifetime of one `councli chat` shell.
- `turn`: one user prompt inside that shell.
- `round`: one fan-out pass to participants inside a turn.

Therefore multiple prompts in one interactive session produce multiple turns,
and each turn starts at round 1.

The UI should make this explicit:

```text
session 20260611T120000 chat
turn 3: what can you do
round 1: asking codex, claude, agy
```

For normal chat, the UI should hide or de-emphasize "round" unless there is a
second round. Displaying `Round 1` for every simple prompt makes the system feel
like a rigid lifecycle even when it is not.

## Response sidecar

Markdown is human evidence. The sidecar is the machine contract.

Target response sidecar:

```json
{
  "schema_version": "councli.response.v1",
  "id": "resp_codex_chat_round1",
  "request_id": "turn_20260611T120000Z_chat",
  "kind": "participant.response",
  "participant": "codex",
  "intent": "chat",
  "round": 1,
  "status": "ok",
  "body_ref": "shared/chat.round1/codex.md",
  "summary": "Codex can inspect, explain, edit, test, and review code.",
  "continue": false,
  "recommend": "none",
  "vote": null,
  "confidence": null,
  "peer_refs_used": [],
  "requested_next_round": null,
  "capabilities_used": ["reads_workspace"],
  "tool_effects": {
    "edited_files": [],
    "commands_run": [],
    "network_used": "unknown"
  },
  "error": null,
  "timing": {
    "started_at": "2026-06-11T12:00:01Z",
    "ended_at": "2026-06-11T12:00:12Z",
    "duration_ms": 11000
  }
}
```

Decision rules:

- Missing or invalid sidecars can be shown to the user but cannot approve a
  vote, executor selection, review, or apply.
- Participant self-reported `tool_effects` are audit metadata, not enforcement.
- `continue: true` is advisory. The router still applies max-round and intent
  policy.
- `vote` is ignored unless the user invoked `/vote` or an execution policy
  explicitly requested a decision.

## Scheduling and concurrency

Default fan-out should be bounded parallel subprocess execution.

Rules:

- do not hold `run.lock` while waiting for an assistant;
- write participant bodies to temp files and atomically replace;
- append event records under `fcntl.flock`;
- kill the full process group on timeout/cancel;
- degrade failed participants for the current turn;
- optionally suppress repeated unavailable participants for the chat session
  after the first hard auth/model/provider failure;
- never let one failed participant block normal chat synthesis.

For implementation modes:

- `/single`: one chosen executor in one isolated git worktree;
- `/parallel`: one worktree per selected executor, followed by diff/test/review;
- `/review`: no writes to the main worktree;
- `/assistant`: native attach, not semantic execution.

Git worktrees isolate source changes and indexes. They do not isolate secrets,
home directory, shell, network, or credentials.

## Security implications

Project `.councli/config.yaml` is repo-owned input. It is not automatically
trusted.

Required safeguards:

- user-local trust pin for command-bearing fields;
- resolved binary path recorded at trust time;
- version recorded when available;
- warning or retrust on binary path drift;
- no shell-string command interpolation;
- prompt placeholders must be standalone argv tokens unless explicitly allowed;
- `.councli/` should remain private and gitignored;
- raw logs need retention and redaction;
- full-permission/yolo commands require explicit policy and visible labeling.

Important boundary:

Full permission is a user preference and launch policy. It is not a protocol
truth. `councli` must still record that a full-permission command was used and
must not pretend it enforced read-only behavior.

## External primitives used

- Codex CLI reference: `codex exec`, JSONL, output schema, sandbox, yolo:
  <https://developers.openai.com/codex/cli/reference#codex-exec>
- Codex config reference: user/project config, trust, sandbox/approval keys:
  <https://developers.openai.com/codex/config-reference#configtoml>
- Claude Code CLI reference: print mode, JSON/stream JSON, permission modes,
  background agents:
  <https://code.claude.com/docs/en/cli-reference>
- Claude permissions: allow/ask/deny rules, read-only operations, Bash/edit
  prompts, subagent permission rules:
  <https://code.claude.com/docs/en/permissions>
- Antigravity CLI repository and docs pointer:
  <https://github.com/google-antigravity/antigravity-cli>
- Kimi command reference: prompt mode, output format, yolo/auto/plan conflict:
  <https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html>
- CodeWhale docs: tools, approval/sandbox model, providers, MCP support:
  <https://codewhale.net/en/docs>
- MCP transports: stdio and streamable HTTP, JSON-RPC framing:
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- A2A specification: agent cards, tasks, artifacts, streaming concepts:
  <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
- JSON-RPC 2.0: request ids, result/error envelope discipline:
  <https://www.jsonrpc.org/specification>
- tmux manual: sessions, panes, sockets, send/capture operations:
  <https://man7.org/linux/man-pages/man1/tmux.1.html>

## Implementation recommendations

Do these before adding more collaboration features:

1. Add adapter command slots and capabilities while preserving current config
   compatibility.
2. Change `doctor` from global availability to per-intent readiness.
3. Add JSON sidecars for shared turns.
4. Validate sidecars before `/vote`, `/review`, executor selection, or apply.
5. Replace process-local event locking with `fcntl.flock` on `run.lock`.
6. Add process-group cancellation for headless calls.
7. Normalize failure classes instead of relying on raw stderr strings.
8. Improve UI labels: chat session, turn, optional round.
9. Add `councli verify` to check events, sidecars, artifacts, and projections.
10. Add bounded context packing so participants receive relevant blackboard
    excerpts rather than the whole history.

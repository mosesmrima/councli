# Adapter launch and capability contract

This document defines the target contract between `councli` and each coding
assistant CLI. It is intentionally separate from participant-specific notes in
`ADAPTERS.md`: this file defines the generic model that every adapter should
eventually implement.

The current implementation has useful primitives, but it is still too coarse:

- `health()` checks binary/version and can run a bounded `readiness_command`;
  built-in adapters include safe default probes, but richer machine-readable
  readiness remains adapter-specific future work;
- intent readiness is capability-aware for the public MVP intents;
- command-level capability metadata exists for prompt, broadcast, and native
  commands; richer adapter-specific validation is still needed;
- authentication, quota, model readiness, and provider selection are normalized
  from probe or run output text unless a CLI exposes a richer machine report;
- shared-turn trailers are text, not validated machine records.

The target model is: an adapter is a small capability manifest plus a set of
launch/probe/execute methods. Routing should select an assistant for a specific
intent only when a command satisfies the required transport, readiness, and
permission constraints.

## External primitives to borrow

### A2A-style discovery, not A2A transport

A2A defines an agent card, tasks, messages, parts, artifacts, streaming updates,
and context. Those concepts are useful, but A2A assumes agents expose an
A2A-compliant server endpoint. `councli` is orchestrating local CLI processes
that do not share that server contract.

Borrow:

- an agent-card-like capability manifest;
- task ids and context ids;
- messages as communication turns;
- artifacts as immutable evidence;
- streaming/status updates as event records.

Do not import:

- remote agent server assumptions;
- HTTP endpoint requirements;
- auth model;
- full task lifecycle semantics.

### JSON-RPC discipline without a daemon

JSON-RPC gives useful envelope rules: request id, method, params, response id,
result-or-error, and notifications without responses. `councli` does not need a
JSON-RPC daemon for the MVP, but file sidecars should use similar discipline so
requests and responses are correlatable and errors are structured.

Borrow:

- request/response ids;
- result versus error exclusivity;
- method or intent names;
- structured error codes;
- batch/fan-out as independent request ids.

Do not import yet:

- a long-lived Unix socket server;
- live JSON-RPC sessions;
- network-facing API surface.

### JSON Schema for validation

JSON Schema Draft 2020-12 is a good fit for validating adapter manifests and
response sidecars. Pydantic models are fine for Python-side implementation, but
a published schema makes artifacts inspectable and language-neutral.

Borrow:

- versioned schemas;
- required fields;
- enum values for states, intents, and permissions;
- schema validation as a hard gate for machine decisions.

### MCP transport lessons

MCP's stdio transport is directly relevant because it models local subprocess
communication: the client launches a process, JSON-RPC messages travel over
stdin/stdout, and logs go to stderr. Most assistant CLIs do not expose MCP-style
protocols for their own assistant interface, but the separation is valuable.

Borrow:

- stdout is protocol output only when the command promises machine output;
- stderr is diagnostic logging;
- subprocess transport is valid for local integrations;
- session ids and explicit cancellation matter for long-running operations.

Do not import:

- councli-level MCP server configuration for the MVP;
- MCP tool semantics for assistant-to-assistant collaboration.

## Core model

Each adapter has:

- identity: name, binary, version, vendor/tool family;
- commands: headless, planning, review, execute, native start, native resume;
- transports: exec arg, stdin, prompt file, tmux paste/type, native attach;
- capabilities: what each command may read/write/run;
- readiness probes: binary, version, auth, quota, model/provider, cwd;
- output contracts: text, JSON, JSONL, sidecar file, diff, review verdict;
- failure taxonomy: normalized errors across tools;
- session semantics: stateless, native session id, workspace-scoped history.

The adapter contract should be declarative where possible and procedural only
where a tool needs custom probing or parsing.

## Adapter manifest

Target shape:

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
    argv: ["codex", "exec", "--skip-git-repo-check", "{prompt}"]
    transport: exec_arg
    output_contract: text_with_sidecar
    capabilities: ["reads_workspace", "runs_tools", "full_permission"]
    timeout_seconds: 900

  planning:
    argv: ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "{prompt}"]
    transport: exec_arg
    output_contract: text_with_sidecar
    capabilities: ["planning_only", "reads_workspace"]
    timeout_seconds: 900

  native_start:
    argv: ["codex", "--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"]
    transport: tmux_native
    capabilities: ["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"]

  native_resume:
    argv: ["codex", "resume", "{session_id}"]
    transport: tmux_native
    session_id_source: native_store

probes:
  binary: required
  version: optional
  auth: command
  quota: best_effort
  model: best_effort
  native_session: optional
```

Current YAML can remain compatible by treating old fields as a shorthand:

- `command` becomes `commands.chat`;
- `broadcast_command` becomes `commands.planning`;
- `start_command` becomes `commands.native_start`;
- `resume_command` becomes `commands.native_resume`;
- `broadcast_read_only` is retained only as legacy advisory metadata when old
  configs omit `broadcast_capabilities`;

## Intent routing

Routing must ask "what does this turn need?" before choosing a command.

| Intent | Transport required | Permission policy | Output required |
| --- | --- | --- | --- |
| `chat` | prompt-capable headless preferred | read allowed, write denied by policy | text plus response sidecar |
| `deliberate` | prompt-capable headless | read allowed, no writes | text plus response sidecar |
| `vote` | prompt-capable headless | read allowed, no writes | structured decision JSON |
| `broadcast` | planning command preferred | planning/read-only preferred, explicit fallback allowed | text plus status |
| `review` | prompt-capable headless | read diff/worktree, no main-worktree writes | verdict JSON plus notes |
| `execute` | executor command | writes only in isolated worktree | diff plus summary |
| `assistant` | tmux native | native tool owns permissions | raw log plus attach events |
| `visible_room` | tmux native | native tool owns permissions | operator view only |

This prevents a tool from being listed as simply `available` when it is only
available for native attach or only available for headless chat.

## Launch and readiness state machine

Adapter readiness is intent-specific:

```text
configured
  -> trust_checked
  -> binary_resolved
  -> version_detected
  -> command_selected
  -> policy_checked
  -> launched
  -> ready
```

Failure and degraded states:

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
```

`ready` means "ready for this intent", not globally ready. Examples:

- Kimi with no provider configured is not ready for chat, but may be ready for
  native attach so the user can run `/login` or provider setup.
- Claude logged into the TUI but blocked by subscription is configured and
  binary-resolved, but not ready for model calls.
- A tmux pane running in another repository is live, but policy-denied for the
  current project.
- A full-permission command can be ready for `execute` in a worktree but unsafe
  for `broadcast` unless fallback is explicitly allowed.

## Probe model

Probe commands should be cheap, bounded, and non-mutating.

Probe types:

```yaml
probe:
  binary:
    method: which
  version:
    argv: ["tool", "--version"]
  auth:
    argv: ["tool", "doctor", "--json"]
    optional: true
  model:
    argv: ["tool", "models", "current", "--json"]
    optional: true
  native_session:
    method: tmux_pane
```

Probe result shape:

```json
{
  "schema_version": "councli.probe.v1",
  "adapter": "claude",
  "intent": "chat",
  "status": "unavailable",
  "reason": "auth_required",
  "evidence": {
    "binary": "/home/user/.local/bin/claude",
    "version": "1.2.3",
    "stderr_excerpt": "subscription access disabled"
  },
  "remediation": "run claude auth or configure API billing"
}
```

Current config supports `version_command`, `readiness_command`,
`probe_timeout_seconds`, and `readiness_timeout_seconds`. `doctor --json`
records version/readiness status and maps nonzero readiness probes into common
failure classes such as `auth_required`, `model_unconfigured`,
`quota_unavailable`, or `readiness_failed`.

The probe output should also be recorded in turn ledgers and status views.
Repeated failures should degrade once per turn instead of spamming every round.

## Response sidecar contract

The current `COUNCLI_TRAILER` is useful for early testing but too weak for
machine decisions. The target response contract is a Markdown body plus JSON
sidecar.

Markdown:

```text
.councli/runs/<turn>/shared/chat.round1/codex.md
```

Sidecar:

```json
{
  "schema_version": "councli.response.v1",
  "id": "turn_20260610_chat.round1.codex",
  "request_id": "turn_20260610_chat",
  "participant": "codex",
  "intent": "chat",
  "round": 1,
  "status": "ok",
  "body_ref": "shared/chat.round1/codex.md",
  "summary": "short summary",
  "continue_requested": false,
  "questions_for_peers": [],
  "recommendation": null,
  "capabilities_used": ["reads_workspace"],
  "tool_effects": {
    "edited_files": [],
    "commands_run": [],
    "network_used": "unknown"
  },
  "error": null
}
```

Rules:

- the sidecar is the machine contract;
- Markdown is human evidence;
- a missing or invalid sidecar means the response can be displayed but not used
  for votes, executor selection, or review approval;
- `tool_effects` is self-reported until stronger sandbox/accounting exists;
- implementation/review modes require stricter schemas than chat.

## Failure taxonomy

Normalize failure classes across tools:

| Class | Meaning | Retry? | User action |
| --- | --- | --- | --- |
| `missing_binary` | executable not on PATH | no | install or disable |
| `auth_required` | login/API key/subscription missing | no | authenticate |
| `quota_unavailable` | rate limit/credits/subscription quota | delayed | wait or change provider |
| `model_unconfigured` | no default/current model/provider | no | configure model |
| `policy_denied` | command exceeds intent policy | no | change command/policy |
| `timeout` | invocation exceeded limit | maybe | retry with smaller prompt |
| `canceled` | user interrupted | no automatic retry | rerun explicitly |
| `malformed_output` | schema/trailer invalid | maybe once | inspect artifact |
| `tool_error` | assistant CLI returned nonzero | maybe | inspect stderr |
| `transport_error` | tmux/subprocess/PTY failure | maybe | inspect runtime |

Avoid treating all nonzero exits as the same. A missing model and a syntax
error in the adapter command need different remediation.

## Transparency model

Assistants should not communicate through hidden memory. For a shared turn,
each participant gets an explicit packet containing:

- the user prompt;
- the current intent;
- allowed effects;
- run directory;
- current blackboard path;
- prior round summaries if any;
- required output path and sidecar path;
- the list of other participants and their statuses.

Participants see each other through artifacts:

```text
round 1:
  codex.md + codex.response.json
  claude.md + claude.response.json
  agy.md + agy.response.json

blackboard.md rendered from artifacts

round 2, only if requested/explicit:
  participants read blackboard.md and respond
```

This creates transparency without needing the assistants to share native session
stores. It also lets a failed/unavailable participant be visible without
blocking healthy participants.

## Security implications

Capability routing is a security boundary only if the command actually enforces
the declared capability. If `codewhale --yolo exec` is marked planning-only,
that is a configuration bug, not a safe mode.

Minimum controls:

- store trusted command/capability fields in user-local trust state;
- record resolved binary path and version at trust time;
- warn or require retrust on binary path drift;
- never let project-owned config silently grant `full_permission`;
- separate planning/review commands from execute commands;
- run implementation only in worktrees;
- record whether read-only is enforced by the assistant or merely requested in
  the prompt.

## Operational implications

Capability-aware routing improves reliability:

- `doctor` can report exactly which intents each assistant supports;
- chat can skip a broken provider without failing the entire turn;
- `/assistant` can still work when headless auth fails;
- `/vote` requires structured sidecar votes instead of accepting plain text;
- `/broadcast` can warn when falling back to full-permission commands;
- cost/latency metrics can be grouped by intent and command class.

It also makes cleanup and retry safer:

- native tmux sessions are not killed on normal turn timeout;
- headless process groups can be canceled per participant;
- malformed sidecars can be retried once without rerunning successful peers;
- run artifacts show why an assistant was not included.

## Migration path

1. Add capability fields to config as optional metadata while preserving old
   fields. Done for MVP intents.
2. Teach `doctor` to show intent readiness: chat, deliberate, vote, broadcast,
   review, execute, native attach. Done for public MVP intents.
3. Emit `.response.json` sidecars for shared turns while still accepting
   `COUNCLI_TRAILER` as fallback.
4. Add JSON Schema files for adapter manifests and response sidecars.
5. Replace `broadcast_read_only` with command-level capabilities and an
   explicit fallback policy. Done for `/broadcast`; continue extending routing
   by intent and policy.
6. Keep improving adapter-specific probes for auth, model, quota, and native
   session readiness where each CLI exposes richer safe diagnostics. The
   generic `readiness_command` hook and built-in defaults are in place.
7. Add stable version reporting to trust metadata. Resolved binary path and
   executable hash drift are already pinned.
8. Update routing to select commands by intent and policy instead of
   `health().available`.

## Research references

- A2A specification: agent cards, messages, tasks, parts, artifacts, streaming,
  and context: <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
- JSON-RPC 2.0: request/response ids, result-or-error shape, notifications, and
  batch correlation: <https://www.jsonrpc.org/specification>
- JSON Schema Draft 2020-12: validation and versioned schema vocabulary:
  <https://json-schema.org/draft/2020-12>
- MCP transports: stdio subprocess transport, JSON-RPC framing, stderr logging,
  Streamable HTTP sessions, cancellation, and resumability lessons:
  <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>

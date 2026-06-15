# Architecture decisions and rejected alternatives

This document turns the research into explicit decisions. It is intentionally
blunt: each decision names the existing primitives, alternatives, chosen
approach, tradeoffs, operational implications, and failure handling.

Use this file when implementation work begins so changes do not drift back into
ad hoc workflow code.

## Decision 1: Councli is a control plane, not an agent

Existing primitives:

- Unix process execution and exit status.
- Local files as durable, inspectable artifacts.
- tmux for persistent terminal sessions.
- Git worktrees for isolated source changes.
- JSON/JSONL for structured local records.

Alternatives:

- Reimplement the assistants behind one common custom agent runtime.
- Wrap each assistant in a councli-owned synthetic CLI.
- Build a direct peer-to-peer agent network.
- Treat councli itself as the primary coding assistant.

Decision:

`councli` coordinates installed assistant binaries. It launches the real
`codex`, `claude`, `agy`, `kimi`, `codewhale`, or future user-installed
commands. It does not replace them, fork them, or reimplement their native
features.

Why:

- The assistants already own their model routing, tools, permissions, MCP
  configuration, memory/session stores, slash commands, and UX.
- Reimplementing those features would create a worse clone and make councli
  impossible to maintain.
- The right abstraction boundary is process orchestration plus durable shared
  artifacts.

Implications:

- Adapter code must be narrow: discover, probe, launch, capture, classify, and
  normalize.
- Native features remain native. Users enter `/assistant <name>` when they want
  the real tool experience.
- Shared collaboration happens through councli artifacts, not hidden native
  state.

Failure handling:

- If an assistant binary is missing or not ready, the participant degrades for
  that intent.
- Degradation is visible in the blackboard and events.
- A failed participant does not block normal chat unless the selected command
  requires quorum.

## Decision 2: Intent-specific readiness replaces global availability

Existing primitives:

- `which` or `shutil.which` for binary lookup.
- Native `--version`, `doctor`, `auth status`, provider, and model commands.
- Bounded no-op prompt probes where no explicit health command exists.

Alternatives:

- Keep `available = binary exists`.
- Run a full prompt during every doctor check.
- Assume availability from configuration alone.

Decision:

Readiness is per intent:

```text
chat
deliberate
vote
broadcast
review
execute
assistant
visible_room
```

An assistant may be ready for `/assistant` but not ready for `chat`; ready for
`execute` but unsafe for `broadcast`; live in tmux but wrong for the current
cwd.

Why:

- Binary presence does not prove auth, quota, model/provider setup, permission
  mode, cwd, or output contract.
- The same command can be safe for one intent and unsafe for another.

Implications:

- `doctor` should eventually report readiness by intent, not one status column.
- Routing must choose a command after it knows the requested intent.
- Failure taxonomy becomes part of the adapter contract.

Failure handling:

- Normalize failures as `missing_binary`, `auth_required`,
  `model_unconfigured`, `provider_unconfigured`, `policy_denied`,
  `unsupported_intent`, `wrong_cwd`, `timeout`, `malformed_output`, and similar
  states.
- Cache cheap probe results, but never cache live tmux/cwd state beyond the
  command invocation.

Current evidence:

- `src/councli/agents.py` still treats health mostly as binary/tmux presence.
  That is a known MVP boundary, not the target architecture.

## Decision 3: Headless exec is the semantic path; tmux is the native path

Existing primitives:

- Python `subprocess` for one-shot process execution.
- Vendor headless modes:
  - `codex exec`
  - `claude -p`
  - `agy -p` / `agy --print`
  - `kimi -p`
  - `codewhale exec`
- tmux for native terminal sessions.

Alternatives:

- Inject every councli prompt into active tmux panes.
- Use PTY/expect automation for all assistants.
- Require agents to expose a daemon API before they can participate.

Decision:

Semantic collaboration turns use headless commands where available. Native
assistant interaction uses tmux attach/detach. The two planes share artifacts
and metadata but not transport semantics.

Why:

- Headless commands provide explicit cwd, argv, stdout/stderr, exit code, and
  timeout.
- tmux preserves each assistant's real TUI and native features.
- PTY/TUI automation is too brittle for protocol truth.

Implications:

- A normal councli turn may not mutate the native assistant's interactive
  session memory.
- If preserving native history becomes important, adapters should use official
  session ids or export/import formats, not terminal scraping.
- tmux raw logs remain diagnostic evidence only.

Failure handling:

- Headless failures are structured as participant response sidecars and events.
- Native tmux sessions can be live but not ready; cwd/process checks decide
  readiness for the current project.

## Decision 4: Artifact bus beats direct peer-to-peer communication for v1

Existing primitives:

- Markdown files for inspectable human evidence.
- JSON sidecars for machine contracts.
- JSONL event logs for append-only histories.
- JSON-RPC discipline for request/response/error envelopes.
- A2A concepts: task, message, artifact, agent card, status.
- MCP stdio lesson: stdout is protocol only when the command promises it;
  stderr is diagnostic.

Alternatives:

- Direct agent-to-agent terminal chat.
- Local HTTP server as the first protocol.
- Unix socket JSON-RPC daemon as the first protocol.
- A full A2A implementation around local CLIs.

Decision:

Use an event-sourced artifact bus:

```text
request.json
task.md
events.jsonl
shared/<intent>.round<n>/<participant>.md
shared/<intent>.round<n>/<participant>.response.json
blackboard.md
state.json
```

Why:

- Local files are transparent, debuggable, diffable, and easy to recover.
- Most current assistant CLIs are not A2A or MCP servers.
- A file protocol preserves collaboration evidence without inventing a daemon
  too early.

Implications:

- Blackboard is a projection, not the source of truth.
- Participants communicate by seeing prior artifacts and bounded blackboard
  excerpts.
- Future daemon/API work can index or serve the same artifacts instead of
  replacing them.

Failure handling:

- Missing artifact refs make the run degraded or invalid depending on intent.
- `councli verify` should rebuild state and detect dangling refs, malformed
  sidecars, and projection drift.

## Decision 4A: Council interaction is the product, not raw broadcast

Existing primitives:

- Fan-out subprocess calls.
- Shared blackboard artifacts.
- Bounded follow-up rounds.
- Synthesis prompts over prior participant outputs.
- Explicit slash commands for deeper collaboration.

Alternatives:

- Broadcast prompt to all assistants, print outputs, and stop.
- Ask one synthesizer to summarize isolated answers without peer visibility.
- Force every prompt through a rigid debate/vote lifecycle.

Decision:

`councli` should make assistants act as a council. The first fan-out gathers
independent views. Those views then become transparent shared context. When the
prompt benefits from collaboration, participants can critique, refine,
challenge, or build on each other's outputs before a common answer is returned.

Why:

- The selling point is unified intelligence from multiple agents, not merely
  parallel calls.
- Transparent peer visibility prevents the user from manually copying context
  between tools.
- Critique and reconciliation are valuable for architecture, debugging, review,
  and implementation planning.

Implications:

- Normal chat can remain one round when the answer is simple.
- Deeper interaction is adaptive or user-directed through `/deliberate`,
  `/review`, `/vote`, `/parallel`, or follow-up prompts.
- The blackboard must show enough evidence for the user to inspect how the
  common answer was produced.
- Synthesis should be based on visible participant artifacts, not hidden model
  memory.

Failure handling:

- If only one participant responds, the answer is labeled as single-source.
- If participants disagree, councli should surface the disagreement or run a
  follow-up round when policy allows.
- If no participant produces a usable response, synthesis must fail clearly
  rather than inventing consensus.

## Decision 5: JSON sidecars are required for machine decisions

Existing primitives:

- JSON Schema Draft 2020-12.
- Pydantic or dataclasses for Python validation.
- Vendor structured output where available:
  - Codex `--json`, `--output-last-message`, `--output-schema`.
  - Claude `--output-format json|stream-json`, `--json-schema`.
  - Kimi `--output-format stream-json` with prompt mode.
  - CodeWhale `exec --output-format stream-json`.

Alternatives:

- Parse natural language directly.
- Continue using text trailers only.
- Trust terminal screen output.

Decision:

Markdown is human evidence. JSON sidecars are the machine contract. Votes,
executor selection, review approval, and apply decisions require valid sidecars.

Why:

- Natural language is not reliable enough for state transitions.
- Text trailers are useful while prototyping but too weak for production.
- Terminal output may wrap, redraw, truncate, or include stale text.

Implications:

- Participants that return only text can still contribute to chat, but cannot
  approve governance decisions.
- Adapters should normalize vendor JSON streams into `councli.response.v1`.
- Synthesis can quote text responses, but governance must validate sidecars.

Failure handling:

- Missing or invalid sidecar becomes `malformed_output`.
- For chat, show the text and continue degraded.
- For `/vote`, `/review`, executor choice, or apply, abstain or fail according
  to quorum policy.

## Decision 6: Append-only events plus artifacts are source of truth

Existing primitives:

- JSONL append logs.
- Atomic file replace with `os.replace`.
- Advisory file locks with `fcntl.flock`.
- SQLite WAL as a future local index.

Alternatives:

- Mutable blackboard file as shared state.
- SQLite as the only source of truth immediately.
- In-memory session state.
- External database.

Decision:

Events and immutable artifacts are source truth. `state.json`, `blackboard.md`,
and indexes are rebuildable projections. SQLite may be added later as an index,
not as the only evidence store.

Why:

- Files are inspectable and robust for local-first tooling.
- Event replay gives recovery and auditing.
- SQLite is excellent for querying many runs, but premature as the only storage
  layer while the artifact schema is still moving.

Implications:

- Every state transition should emit an event.
- Projection generation must be deterministic.
- Local recovery can rebuild state after crashes.

Failure handling:

- Cross-process writes need `fcntl.flock` around event append and projection
  updates.
- Partial writes need temp-file plus atomic replace.
- A future `runs recover` should rebuild projections from events/artifacts.

Current evidence:

- `src/councli/events.py` uses a process-local `threading.Lock`. That protects
  threads inside one process but not concurrent `councli` processes.

## Decision 7: Use git worktrees for source isolation, not security

Existing primitives:

- `git worktree`.
- `git diff` and patch application.
- Future containers, namespaces, or bubblewrap for stronger isolation.

Alternatives:

- Edit the main worktree directly.
- Use only branches in the current worktree.
- Copy the repository directory manually.
- Run every assistant in a container.

Decision:

Implementation modes use git worktrees as the default isolation boundary.

Why:

- Worktrees provide a separate working tree and index with cheap storage.
- Diffs are easy to inspect and apply.
- The main worktree remains untouched until explicit apply.

Implications:

- Worktrees do not protect credentials, home directory, network, shell tools, or
  other local files.
- Full-permission agents remain powerful even inside a worktree.
- Security claims must not overstate what worktrees provide.

Failure handling:

- Track worktree lifecycle states: `created`, `executing`, `diff_captured`,
  `reviewed`, `applied`, `abandoned`, `pruned`.
- Cleanup commands should default to dry-run.

## Decision 8: Explicit governance commands, not hidden governance lifecycle

Existing primitives:

- CLI slash commands.
- User-driven prompt intent.
- Shared artifact bus.

Alternatives:

- Always run orient/propose/critique/revise/vote.
- Infer complex workflow for every prompt.
- Never provide governance tools.

Decision:

Normal prompts are shared conversation. Governance is explicit:

```text
/deliberate
/vote
/review
/parallel
/single
/legacy-council
```

Why:

- Simple prompts should not spend tokens on ceremony.
- User intent should drive deeper collaboration.
- Voting was a cost/control mechanism, not a protocol invariant.

Implications:

- UI labels must make session/turn/round semantics clear.
- Rounds are a mechanism inside a turn, not user-visible ceremony for every
  prompt.
- Assistants can request more discussion through sidecars, but router policy
  decides whether another round runs.

Failure handling:

- Normal chat tolerates degraded participants.
- `/vote` and review policies define quorum and abstention behavior.
- `/parallel` can compare independent worktrees rather than forcing one
  executor.

## Decision 9: No daemon until foreground semantics are correct

Existing primitives:

- Foreground CLI commands.
- tmux native sessions.
- systemd user units/scopes for future supervised background work.
- SQLite WAL for future indexing.

Alternatives:

- Start a long-lived councli daemon immediately.
- Add a job queue.
- Add a local HTTP API as the core runtime.

Decision:

Keep foreground commands and tmux sessions as the operational model until the
artifact protocol, readiness, cancellation, and recovery semantics are stable.

Why:

- A daemon would multiply lifecycle and upgrade complexity.
- Local developer tools benefit from inspectable foreground failure.
- tmux already solves the native-session persistence problem.

Implications:

- Background work should wait for a stable event model.
- When needed, Linux should use systemd user scopes/units before a custom
  supervisor.
- macOS support will need a launchd-equivalent design if background jobs become
  product scope.

Failure handling:

- Foreground Ctrl-C must record cancellation and terminate process groups.
- Native tmux sessions survive normal command cancellation.
- Daemon migration should be additive over the existing artifact store.

## Decision 10: Full permission is policy, not default truth

Existing primitives:

- Codex sandbox modes and yolo/full-access flag.
- Claude permission modes including `bypassPermissions`.
- AGY `--dangerously-skip-permissions` and sandbox mode.
- Kimi interactive `--yolo`, `--auto`, `--plan`.
- CodeWhale `--yolo`, `exec --auto`, approval/sandbox config.
- User-local trust pins.

Alternatives:

- Always use full permission because the user prefers it.
- Always force read-only modes and block tools without one.
- Encode permission trust only in repo-owned config.

Decision:

`councli` may launch full-permission commands when user policy says so, but it
must label and record that fact. Full permission is not a guarantee of
read-only safety and not a reason to skip trust checks.

Why:

- The user explicitly wants broad-permission launch modes available.
- Blocking assistants because they lack perfect read-only broadcast is too
  limiting for the MVP.
- Production-grade software must still make permission escalation visible.

Implications:

- Command capabilities must replace boolean `broadcast_read_only`.
- Project config cannot silently escalate permissions.
- Trust pins should include command-bearing fields and resolved binary path.

Failure handling:

- If a full-permission command is used for a planning intent, record
  `read_only_enforced=false`.
- Future policy can reject that by default for untrusted projects.

## Decision 11: Prefer stdin or prompt files over prompt argv

Existing primitives:

- Codex `exec -` reads prompt from stdin.
- Python subprocess stdin.
- File artifacts for task bodies.
- Shell argv process listings and OS argument length limits.

Alternatives:

- Always pass prompt as a positional argv.
- Build shell commands with prompt interpolation.
- Paste prompt into tmux.

Decision:

Adapters should prefer stdin or prompt-file transport when supported. Prompt
argv is acceptable only when it is the stable CLI contract. Shell strings should
not be used for semantic turns.

Why:

- Long prompts can exceed argv limits.
- argv text may be visible through process listings.
- Prompt text can start with dashes or include characters meaningful to CLI
  parsers.
- Shell interpolation creates command-injection risk.

Implications:

- Adapter manifests need `prompt_transport`.
- Command templates must validate `{prompt}` placement.
- Some adapters will remain argv-based until their tool exposes stdin/file
  transport.

Failure handling:

- Reject templates embedding `{prompt}` inside another token unless explicitly
  marked unsafe/allowed.
- Test prompts containing newlines, quotes, leading dashes, and shell
  metacharacters.

## Production readiness gates

Do not call the architecture production-grade until these gates pass:

1. `doctor --json` reports readiness by intent.
2. Adapter command slots declare prompt transport, output contract, and
   capabilities.
3. Command-bearing config trust records resolved binary path and detects drift.
4. Shared turns emit `councli.response.v1` sidecars.
5. `/vote`, `/review`, executor selection, and apply reject invalid sidecars.
6. Event writes and projections use `fcntl.flock`.
7. Headless subprocesses run in process groups and cancel cleanly.
8. `councli verify` validates events, refs, sidecars, and projections.
9. `runs recover` can rebuild `state.json` and `blackboard.md`.
10. Context packing passes bounded blackboard excerpts instead of full history.
11. Retention/redaction exists for raw logs and artifacts.
12. Integration tests cover fake binaries for each adapter command shape.

## Sources

- Codex CLI reference: <https://developers.openai.com/codex/cli/reference>
- Codex config reference: <https://developers.openai.com/codex/config-reference>
- Claude Code CLI reference: <https://code.claude.com/docs/en/cli-reference>
- Claude permissions: <https://code.claude.com/docs/en/permissions>
- Antigravity CLI: <https://github.com/google-antigravity/antigravity-cli>
- Kimi command reference:
  <https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html>
- CodeWhale docs: <https://codewhale.net/en/docs>
- MCP transports:
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- A2A specification:
  <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
- JSON-RPC 2.0: <https://www.jsonrpc.org/specification>
- tmux manual: <https://man7.org/linux/man-pages/man1/tmux.1.html>
- git worktree: <https://git-scm.com/docs/git-worktree>
- SQLite WAL: <https://sqlite.org/wal.html>
- systemd-run: <https://man7.org/linux/man-pages/man1/systemd-run.1.html>

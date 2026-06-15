# councli

`councli` is a small local utility for hosting multiple coding CLI assistants,
recording their activity, and letting them collaborate through a shared
blackboard while preserving their native CLI harnesses.

The first version is intentionally simple:

- Linux-first.
- File-based transcripts under `.councli/runs/`.
- Native assistant sessions under a dedicated `tmux -L councli` server.
- Project-level session ledger under `.councli/ledger/`.
- Project-scoped tmux session names to avoid cross-repo collisions.
- Raw terminal recording rotation and `.councli/` gitignore protection.
- Modular agent adapters through YAML config.
- Graceful degradation when an agent is missing or not authenticated.
- Shared conversation turns by default, with explicit `/deliberate` and `/vote`
  commands for stronger coordination.
- Packet-file prompts, response sidecars, run-local locks, and blackboard
  projections for inspectable collaboration.
- Packaged JSON Schemas under `councli.schemas` for protocol artifacts.
- Security reporting for trusted command fields, binary path/hash/version drift,
  and elevated command surfaces.
- Native attach mode: use each assistant's own TUI without `councli`
  intercepting slash commands or permission prompts.
- Read-only broadcast mode for comparing answers across assistants.
- Experimental worktree execution remains hidden while the shared council
  protocol is hardened.

For the consolidated research findings and implementation handoff, see
[`docs/RESEARCH_FINDINGS.md`](docs/RESEARCH_FINDINGS.md) first; it is the
canonical implementation reference. For the latest external MVP consultation
from Claude Fable 5, see
[`docs/FABLE_MVP_CONSULT.md`](docs/FABLE_MVP_CONSULT.md). For the detailed binary launch, adapter
readiness, and communication protocol research, see
[`docs/AGENT_LAUNCH_PROTOCOL.md`](docs/AGENT_LAUNCH_PROTOCOL.md). For the
explicit tradeoff analysis and rejected alternatives, see
[`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md). For the
broader systems-level design review, see
[`docs/SYSTEMS_REVIEW.md`](docs/SYSTEMS_REVIEW.md). For the target shared-turn
protocol, state machine, sidecar schemas, and locking rules, see
[`docs/PROTOCOL_DESIGN.md`](docs/PROTOCOL_DESIGN.md). For command trust,
artifact secrecy, yolo/full-permission risk, and hardening gates, see
[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md). For lifecycle,
cancellation, cleanup, retention, and observability, see
[`docs/OPERATIONS_MODEL.md`](docs/OPERATIONS_MODEL.md). For tmux, PTY, TUI,
agent launch modes, and why terminal capture is not the collaboration protocol,
see [`docs/TERMINAL_SUBSTRATE.md`](docs/TERMINAL_SUBSTRATE.md). For adapter
readiness, capability-aware routing, launch states, and response sidecars, see
[`docs/ADAPTER_CONTRACT.md`](docs/ADAPTER_CONTRACT.md). For run events,
blackboard projection, locking, crash recovery, and indexing, see
[`docs/STATE_CONCURRENCY.md`](docs/STATE_CONCURRENCY.md).

## Install as a shell command

`councli` is packaged as a normal Python CLI. The recommended user install path
is `pipx`, because it creates an isolated environment and puts the `councli`
command on your shell `PATH`.

From a local checkout:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install /path/to/councli
councli doctor
```

From a published Git repository:

```bash
pipx install "git+https://github.com/<owner>/<repo>.git"
councli doctor
```

Requirements:

- Python 3.11 or newer.
- `tmux` for native interactive assistant sessions.
- Any assistant CLI you want to use, such as `codex`, `claude`, `agy`, `kimi`,
  or `codewhale`. Missing assistants do not block the rest.

On first `councli doctor` or `councli chat` in a project, councli creates
`.councli/config.yaml`, trusts the generated command templates, protects local
artifacts in `.councli/.gitignore`, and checks which configured assistant
binaries are on `PATH`.

You can run the same first-run setup explicitly with:

```bash
councli setup
```

If you want the generated config to disable tools that are not installed yet:

```bash
councli init --disable-missing
```

If you install a missing assistant later, either flip its `enabled` field back
to `true` and run `councli trust`, or regenerate the defaults with:

```bash
councli init --force
```

## Install for local development

```bash
cd councli
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Start

```bash
councli doctor
councli doctor --json
councli setup
councli chat
councli init --disable-missing
councli trust
councli sessions attach codex
councli broadcast "Compare the API design"
councli brief "Context to share with assistants"
councli status
councli show latest
```

## Config

`councli init` writes `.councli/config.yaml` in the current project.
Assistant command and transport fields in that file are pinned in user-local councli state
(`$COUNCLI_STATE_HOME`, `$XDG_STATE_HOME/councli`, or
`~/.local/state/councli`). The trust pin also records resolved binary paths and
executable hashes for enabled agents. If you edit command templates, binaries,
backend, enabled flags, broadcast flags, session names, or tmux input settings,
or if an installed assistant binary changes, review the file and run
`councli trust` before running assistant commands again. If a project was
intentionally moved or renamed, run `councli trust --repair-identity` after
reviewing the config.

Each agent has:

- `enabled`: whether to consider it.
- `backend`: `exec` or `tmux`.
- `binary`: executable name to find on `PATH`.
- `display_name`: optional human-readable adapter name.
- `capabilities`: optional intent list such as `chat`, `deliberate`, `vote`,
  `broadcast`, and `assistant`. Empty means infer support from configured
  commands.
- `command_capabilities`: what the normal prompt command may do, using values
  such as `planning_only`, `reads_workspace`, `writes_workspace`, `runs_tools`,
  `network_access`, and `full_permission`.
- `broadcast_capabilities`: capabilities for `broadcast_command`. If omitted,
  legacy configs infer read-only behavior from `broadcast_read_only`.
- `start_capabilities` / `resume_capabilities`: capabilities for native tmux
  start/resume commands.
- `read_only_policy`: `safe_only` by default for `chat`, `deliberate`, `vote`,
  and synthesis. Set `allow_full_permission` only when unsafe shared-turn
  routing is intentional.
- `broadcast_policy`: `safe_only` by default. Set `allow_full_permission` only
  when an unsafe broadcast fallback is intentional.
- `version_command`: optional lightweight version probe command.
- `readiness_command`: optional bounded, non-mutating probe for auth/model/quota
  readiness. Nonzero output is classified into statuses such as
  `auth_required`, `model_unconfigured`, or `quota_unavailable` when possible.
- `probe_timeout_seconds`: timeout for version/probe commands.
- `readiness_timeout_seconds`: timeout for the readiness probe.
- `command`: argv template. For `exec`, `{prompt}` is replaced with the generated prompt.
- `broadcast_command`: optional argv template for read-only broadcast/planning.
- `broadcast_enabled`: whether the agent can participate in broadcast.
- `broadcast_read_only`: legacy advisory field retained for old configs.
- `broadcast_timeout_seconds`: optional broadcast-specific timeout.
- `resume_command`: optional argv template where `{session_id}` is replaced by an imported native session id.
- `session_name`: tmux session name for `tmux` backend.
- `start_command`: command used to start the tmux session.
- `done_marker`: legacy base marker. Tmux runs use a unique per-turn marker to avoid stale scrollback.
- `prompt_style`: `compact` or `verbatim`. Use `compact` by default for TUI safety.
- `input_method`: `paste` or `type`. Use `type` for TUIs that keep bracketed paste in a draft composer.
- `submit_keys`: tmux keys used to submit after input, usually `["Enter"]`.
- `post_paste_delay_seconds`: delay between input and submit.
- `timeout_seconds`: max run time for one prompt.

Consensus settings:

- `max_rounds`: maximum implementation/review attempts before stopping.
- `min_confidence`: minimum `0.0` to `1.0` confidence for a vote or review to
  count toward majority. Low-confidence responses remain in the transcript and
  decision JSON, but they do not approve a plan or implementation.

Context packing settings:

- `peer_context_latest_rounds`: number of prior rounds included in the prompt
  excerpt for a follow-up round.
- `peer_context_per_participant_chars`: maximum characters copied from one
  participant into the peer-context excerpt.
- `peer_context_total_chars`: maximum characters copied into the whole
  peer-context excerpt before councli points agents at the full blackboard.
- `peer_context_include_failures`: `summary`, `full`, or `omit` for failed
  participant output in later-round prompts.

Native session settings:

- `tmux_socket`: dedicated tmux socket name, default `councli`.
- `detach_key`: simple tmux key chord used to return from attached assistant sessions, default `C-]`.
- `raw_log_max_bytes`: rotate raw pane recordings when they exceed this size on session start.
- `raw_log_backups`: number of rotated raw recordings to keep.
- `session_prefix`: prefix for project-scoped tmux session names.

Artifact hygiene settings:

- `prune_default_classes`: artifact classes pruned by default, currently raw
  logs, session archives, and session snapshots.
- `redact_patterns`: regexes used by `councli artifacts scrub`.
- `redact_replacement`: replacement text for scrubbed secrets.
- `scrub_max_file_bytes`: skip larger files during scrub to avoid accidental
  expensive rewrites.

`councli` deliberately does not model each participant's native feature set.
Slash commands, `@file` mention syntax, MCP configuration, plugins, goals, and
tool-specific subagents remain native to Codex, Claude Code, AGY, Kimi, or
CodeWhale. The MVP coordination layer sends ordinary task/packet prompts and
records artifacts; after a council pass, the user can still enter any native
participant session and use that tool's full UI directly.

Example:

```yaml
agents:
  codex:
    enabled: true
    binary: codex
    command: ["codex", "exec", "{prompt}"]
    timeout_seconds: 900
```

If an agent is unavailable, `councli` records that and continues with the available agents.
During one interactive `councli chat` session, repeated auth/model/quota
failures are marked degraded and skipped on later turns until you restart the
session.

`councli doctor --json` emits per-intent readiness objects, so setup scripts can
distinguish `ready`, `missing_binary`, `unsupported_intent`, `tmux_unavailable`,
`auth_required`, `model_unconfigured`, `quota_unavailable`, and disabled
participants without scraping the table output.

Native tmux-backed sessions are supported for CLIs that behave better interactively:

```yaml
  agy:
    enabled: true
    backend: tmux
    binary: agy
    command: ["agy"]
    session_name: councli-agy
    start_command: ["agy", "--dangerously-skip-permissions"]
    done_marker: "<<<COUNCLI_DONE:agy>>>"
    prompt_style: compact
    input_method: paste
    submit_keys: ["Enter"]
    post_paste_delay_seconds: 0.5
    timeout_seconds: 900
```

Useful commands:

```bash
councli sessions list
councli sessions start agy
councli sessions attach agy
councli sessions capture agy
councli sessions stop agy --dry-run
councli sessions prune --dry-run
```

Run inspection commands:

```bash
councli status
councli show latest
councli show <run-id-prefix> --blackboard
councli verify latest
councli verify <run-id-prefix> --json
councli recover latest
councli recover <run-id-prefix> --json
councli artifacts list
councli artifacts scrub --dry-run
councli artifacts scrub --write
councli artifacts prune --older-than 30 --dry-run
councli artifacts prune --older-than 30 --delete
```

`status` lists recent run ids with task, participants, decision, review, and
implementation status when present. `show` reopens a run's durable state and
prints the paths to its blackboard, machine state, event log, and artifacts.
`verify` checks a run's event log, refs, response sidecars, and rebuildable
projections before you trust, export, or share the output.
`recover` rebuilds `state.json` and `blackboard.md` from the run's event log
and artifacts, then verifies the rebuilt projections.
`security` prints the trusted command surface, resolved binaries, version
metadata, and drift status without running agent prompts, so it can diagnose
trust failures that would block `doctor`. Use `doctor --security` when you want
the same security summary beside normal readiness checks.
`artifacts scrub` redacts common secret-looking tokens from text artifacts and
defaults to dry-run. `artifacts prune` removes old raw logs, session archives,
and snapshots by default, and only deletes when `--delete` is supplied.
`artifacts export` creates a redacted `.tar.gz` support bundle with a manifest.
It exports run/task/ledger/snapshot artifacts by default and excludes raw
terminal recordings unless you explicitly choose that artifact class.
`metrics` derives local JSON or OpenMetrics-style counters from event logs,
participant response sidecars, and artifact sizes.

Interactive councli shell:

```bash
councli chat
```

Inside `chat`, type a normal prompt to run a shared conversation turn. `councli`
fans the prompt out to available assistants, records their responses on the
blackboard, synthesizes a single council answer, and returns to `councli>`.
Normal prompts do not force a fixed orient/propose/critique/revise/vote
lifecycle.

Terminology:

- A `councli chat` session is the interactive shell lifetime.
- A turn is one user prompt inside that shell.
- A round is one fan-out pass to participants inside a turn.

Every new prompt starts a new turn, so seeing `Round 1` multiple times in one
interactive session is expected.

Use explicit commands when you want stronger coordination:

- `/deliberate <prompt>` asks participants to respond independently, then gives
  them a peer-aware second round before synthesis.
- `/vote <prompt>` asks for explicit votes and records a decision artifact.
- `/assistant <name>` attaches to a native assistant session.

Local shell commands are `/help`, `/doctor`, `/status`, `/show`, `/sessions`,
`/assistant <name>`, `/broadcast <prompt>`, `/brief [task]`, `/deliberate
<task>`, `/vote <task>`, and `/quit`. Unknown `/`
commands are rejected explicitly. To send a task that literally starts with `/`,
prefix it as `//task`.

`/assistant codex` attaches your terminal to Codex's native tmux session. Codex
owns the keyboard, slash commands, permission prompts, and UI exactly as if you
had launched it directly. Press `Ctrl-]` to detach back to `councli`. Raw pane
output is recorded under `.councli/session-recordings/` for audit/debugging;
`councli` does not parse terminal screen output as the semantic source of truth.
Use `native.detach_key` in config to choose a different detach chord. If you are
already inside tmux, `councli` unsets `$TMUX` for the inner attach and uses its
dedicated tmux server.

`/broadcast <prompt>` and `councli broadcast` use configured non-interactive
commands where available. Broadcasts are meant for planning, critique,
comparison, and review, not concurrent edits in the same worktree. Broadcast
does not inject prompts into active tmux assistant sessions; it launches
headless subprocesses, records each participant result, and does not retry
failed participants. `broadcast_command` is preferred when configured. If it is
missing, `councli` may fall back to the normal prompt-capable `command` and
records that read-only enforcement was not explicit. If a tool is not
authenticated or lacks a configured model, that participant is recorded as a
runtime failure instead of blocking the rest.

Before council/broadcast runs, `councli` writes an inspectable task brief under
`.councli/tasks/<run-id>/brief.md` and copies it into the run directory. The
brief points at native sessions and recent `councli` events instead of silently
stuffing hidden history into prompts.
Use `/brief` or `councli brief` to print the latest brief and a pasteable
instruction for an attached assistant.

Tmux sessions are project-scoped by a hash of the project path and run under the
dedicated `tmux -L councli` server. `.councli/project.json` stores the project
identity so accidental project moves or copied `.councli/` directories are
detected. If a session already exists but its pane is in another directory,
`councli` refuses to reuse it. `sessions list` reconciles the registry against
live tmux sessions, shows the pane's current command, and marks missing, dead,
shell-returned, or cwd-mismatched sessions stale.
`sessions stop` kills configured tmux-backed participant sessions. `sessions
prune` kills configured councli tmux sessions plus visible `councli-room-*`
rooms, archiving captured pane text first by default under
`.councli/session-archives/`. Use `--dry-run` before cleanup when you want to
inspect the target list.

The `council` command is a compatibility entrypoint for an explicit shared
deliberation turn against available participants:

```bash
councli council -p codex -p agy "Decide the smallest safe plan"
```

It writes packet files, participant response sidecars, synthesis artifacts, and
a blackboard under `.councli/runs/<run>/`. The older fixed phase engine remains
hidden for development while the shared-turn protocol is hardened.

CodeWhale/DeepSeek is supported with the non-interactive command:

```yaml
  codewhale:
    enabled: true
    binary: codewhale
    command: ["codewhale", "--yolo", "exec", "--auto", "{prompt}"]
    timeout_seconds: 900
```

Kimi Code is supported with:

```yaml
  kimi:
    enabled: true
    binary: kimi
    command: ["kimi", "--prompt", "{prompt}"]
    timeout_seconds: 900
```

## Current limits

This is v0. It now focuses the public surface on shared conversation,
deliberation, explicit voting, native attach, durable artifacts, and adapter
readiness. Hidden experimental worktree execution/review commands still exist
for development, but they are not the MVP path and require
`COUNCLI_EXPERIMENTAL=1`. Next steps are:

1. Keep improving adapter-specific readiness probes where each CLI exposes a
   richer safe diagnostic command.
2. Add a richer interactive TUI once the protocol proves useful.
3. Decide whether to delete or separately package the hidden execution/review
   prototype.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/ADAPTER_CONTRACT.md](docs/ADAPTER_CONTRACT.md) for the generic adapter
contract. State and concurrency rules live in
[docs/STATE_CONCURRENCY.md](docs/STATE_CONCURRENCY.md). Participant-specific
CLI notes live in
[docs/ADAPTERS.md](docs/ADAPTERS.md).

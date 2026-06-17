# Roadmap

`councli` is early alpha. The roadmap is focused on making native coding-agent
collaboration reliable without replacing the underlying harnesses.

The core direction is stable:

- Use already-installed assistant binaries.
- Preserve each assistant's native auth, tools, permissions, MCP config, slash
  commands, and session behavior.
- Give participants shared visibility through durable files and a blackboard.
- Let the user choose stronger behavior explicitly with commands such as
  `/deliberate`, `/vote`, and future execution modes.
- Avoid forcing every prompt through a fixed governance lifecycle.

## Near Term

### Harness Adapters

Improve the built-in adapters for Codex, Claude Code, AGY, CodeWhale, and Kimi
Code.

TODO:

- Add richer readiness probes for auth, model selection, quota, version, and
  tool availability.
- Track adapter capability manifests instead of relying only on static config.
- Validate command templates more strictly before execution.
- Normalize failure classification across missing binary, disabled participant,
  unauthenticated session, missing model, quota failure, timeout, malformed
  output, and transport failure.
- Improve native session resume and attach behavior for tmux-backed assistants.
- Keep adapter behavior transparent: failed or degraded participants should be
  visible in the turn artifacts without blocking healthy participants.

### Communication Protocol

Stabilize the file-backed room protocol that lets assistants see each other's
outputs and refine their own reasoning.

TODO:

- Version the turn packet, participant response, sidecar, blackboard, and event
  log schemas.
- Make the default shared turn explicit: independent answers, durable
  blackboard, synthesis, return to prompt.
- Keep `/deliberate` explicit: independent answers, peer-aware
  critique/revision, then synthesizer consensus.
- Keep `/vote` explicit: decision artifacts only when the user asks for a
  choice.
- Improve blackboard rendering so raw participant outputs, summaries,
  degraded-agent notes, and synthesized answers are easy to inspect.
- Add stronger recovery rules for interrupted turns and partially written
  artifacts.
- Make protocol errors user-visible and machine-readable.

### Token Efficiency

Reduce unnecessary prompt and context spend while preserving transparency.

TODO:

- Stop automatic extra rounds unless the user asks for a stronger command such
  as `/deliberate` or `/vote`.
- Generate compact shared turn briefs for participants.
- Summarize peer context when appropriate while keeping full raw artifacts
  available by path.
- Add per-adapter prompt shaping so each harness receives the smallest useful
  instruction packet.
- Compact repeated room history into stable session memory.
- Avoid re-sending large files or transcripts when artifact references are
  enough.
- Add optional user controls for context depth without treating budgets as a
  core product concept.

### Interactive UX

Make the terminal shell feel like a real council control plane.

TODO:

- Improve slash command help and autocomplete.
- Add `/agents`, `/enable <name>`, `/disable <name>`, and clearer participant
  status controls.
- Improve streaming status so user input, participant output, degraded-agent
  messages, and final synthesis are visually distinct.
- Make `/synthesizer` configuration discoverable.
- Keep native attach simple: `/assistant <name>` should enter the assistant's
  real TUI and return cleanly to `councli`.

## Mid Term

TODO:

- Add structured outputs for assistants that support them, while keeping plain
  text fallback for all harnesses.
- Add adapter-specific parsers for JSON, stream JSON, or other native machine
  modes where available.
- Record per-agent readiness history and failure telemetry.
- Add safer artifact export and scrub defaults.
- Improve Windows support, with WSL as the preferred path for tmux-backed
  native attach.
- Add explicit parallel worktree execution as an experimental mode, not a
  default workflow.
- Add explicit peer review flows for user-selected implementation artifacts.

## Later

TODO:

- Consider a full-screen TUI after the terminal shell and protocol are stable.
- Define a public adapter contribution model.
- Add new harnesses only after the current five are solid.
- Add a local SQLite index if file-backed events and artifacts need faster
  queries.
- Explore richer visualizations of agent disagreement, consensus, and evidence.

## Non-Goals

`councli` should not:

- Manage provider accounts, OAuth, API keys, subscriptions, or model billing.
- Replace native coding assistant UIs.
- Become a generic hosted model router.
- Become a general-purpose multi-agent framework.
- Force every prompt through voting, review, or executor selection.
- Hide participant outputs behind a black-box final answer.

The product promise is narrower: a local room where native coding-agent
harnesses can see each other's work, critique it, and produce better shared
answers with durable transparency.

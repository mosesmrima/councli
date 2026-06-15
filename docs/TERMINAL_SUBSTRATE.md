# Terminal substrate architecture

This document explains how terminal primitives fit into `councli` and where
they must not be mistaken for the collaboration protocol. It focuses on two
hard parts of the project: launching heterogeneous coding assistants and making
their communication transparent.

The generic adapter readiness and capability contract is defined in
`ADAPTER_CONTRACT.md`; this document focuses on terminal and native-session
substrates.

## Design thesis

`councli` needs two different integrations with assistant CLIs:

1. A structured automation path for shared turns, deliberation, voting,
   broadcast, review, and repeatable artifact capture.
2. A native interactive path where the user can enter Codex, Claude Code, AGY,
   Kimi, CodeWhale, or another assistant exactly as that tool expects to run.

Those paths should share project identity, artifacts, and session metadata, but
they should not share the same semantic transport.

The terminal stack is useful for native interaction:

```text
terminal emulator
  -> councli TUI or prompt
  -> tmux client/server
  -> PTY slave
  -> assistant TUI process
```

The collaboration protocol should remain file/event based:

```text
user prompt
  -> turn request
  -> participant invocations
  -> response artifacts
  -> blackboard projection
  -> synthesis/decision artifact
```

The invariant is: PTY/tmux/TUI output is visibility and operator control;
structured artifacts are protocol truth.

## Terms

### PTY

A pseudoterminal is a pair of kernel devices: a master side controlled by a
driver process and a slave side that looks like a normal terminal to the child
process. A terminal-oriented program reads from and writes to the slave as if a
human were typing at a real terminal. The master side receives output and can
inject input.

This is why interactive assistant CLIs behave differently when run under a PTY
than when run with pipes. They may enable raw mode, alternate screen buffers,
cursor addressing, bracketed paste, keyboard shortcuts, color, progress
spinners, and full-screen redraws.

Architectural implication: PTY automation is powerful but lossy. It transports
terminal bytes, not intent. It should be an adapter of last resort for tools
that lack a usable headless command.

### tmux

tmux is a terminal multiplexer that manages PTYs and persistent sessions. It can
create a session in a selected working directory, attach and detach clients,
send keys, capture a pane, pipe pane output, split panes, and inspect pane
metadata.

For `councli`, tmux is the native assistant host:

- one dedicated tmux server, usually `tmux -L councli`;
- project-scoped assistant sessions;
- user attach/detach without killing the assistant;
- raw pane recording for diagnostics;
- liveness checks from tmux session and pane metadata;
- optional visible rooms for seeing multiple assistant TUIs.

Architectural implication: tmux should own hot native sessions, not durable
semantic state. Killing or losing a tmux session should not erase the durable
blackboard, votes, diffs, or run events.

### TUI

A text user interface is the screen application running in a terminal: Codex,
Claude Code, AGY, Kimi, CodeWhale, or future `councli` dashboards. A TUI is for
humans. It may redraw, wrap, clear scrollback, use alternate buffers, or show
state that never appears as stable plain text.

Architectural implication: build a `councli` TUI only as an operator interface.
Do not use another assistant's TUI screen as a machine API.

## Launch modes

### Headless exec launch

```text
councli -> subprocess argv -> assistant one-shot command
```

Use for:

- default shared conversation turns;
- `/deliberate`;
- `/vote`;
- `/broadcast`;
- structured review;
- synthesis when a participant can be used as synthesizer.

Properties:

- explicit `cwd`;
- captured stdout/stderr;
- exit code;
- timeout;
- easier testability;
- artifacts can identify exact argv and status.

Weaknesses:

- may not share the assistant's native interactive session history;
- may reload model/tool context on every call;
- timeout must kill full process groups, not just the direct child, once child
  tools become common;
- output is still untrusted text unless there is a schema.

Target hardening:

- process group launch and cancellation;
- adapter health probes;
- JSON response sidecars;
- byte/output limits;
- structured error taxonomy.

### Native tmux launch

```text
councli -> tmux new-session -c <project root> -> assistant TUI
```

Use for:

- `/assistant <name>`;
- manual auth/login/setup flows;
- native slash commands;
- model/provider selection;
- MCP configuration inside each assistant;
- long-lived local assistant sessions;
- visible multi-pane rooms.

Properties:

- preserves native assistant behavior;
- no need to rebuild slash commands or keybindings;
- user can manually interact when automation is not enough;
- session can survive `councli` exiting.

Weaknesses:

- screen text is not reliable enough for protocol state;
- key injection can split or corrupt prompts;
- bracketed paste behavior differs across TUIs;
- a live pane may be at an auth prompt, editor, shell, or crashed process;
- raw recordings may contain secrets.

Target hardening:

- readiness probes separate from liveness;
- pane cwd verification before reuse;
- process command verification;
- explicit stale/degraded states;
- retention and redaction for recordings;
- no automatic prompt injection into active native sessions unless requested.

### PTY/expect launch

```text
councli -> pexpect/ptyprocess -> assistant TUI
```

Use only when:

- a tool has no usable headless command;
- tmux is unavailable or unsuitable;
- a narrow setup/login prompt must be driven programmatically;
- the adapter has version-gated patterns and tests.

Weaknesses:

- terminal output arrives in unpredictable chunks;
- echoed input can appear in output;
- prompt matching is brittle;
- terminal dimensions affect rendering;
- alternate screen and cursor control complicate capture;
- upstream TUI changes can break automation silently.

Decision: do not make PTY/expect the default substrate for council turns.

## Launch state model

Launching an assistant should not be treated as a boolean. Use separate states:

```text
configured
  -> binary_resolved
  -> trusted
  -> launched
  -> live
  -> ready
```

Failure/degraded states:

```text
missing_binary
untrusted_config
launch_failed
live_but_not_ready
auth_required
quota_unavailable
wrong_cwd
wrong_process
stale_session
dead_pane
unsupported_intent
```

Important distinction:

- live means the process/session exists;
- ready means it can handle the selected intent now.

A tmux session running a login screen is live but not ready for broadcast. A
headless command with an expired subscription is configured but unavailable. A
session in another repository is live but unsafe for the current project.

## Communication planes

`councli` should expose three communication planes.

### 1. Semantic plane

The semantic plane is the durable coordination protocol:

```text
.councli/runs/<turn>/
  request.json
  task.md
  participants.json
  events.jsonl
  shared/<round>/<participant>.md
  shared/<round>/<participant>.response.json
  blackboard.md
  decision.json
```

This is where assistant collaboration happens. Each participant should be told:

- the user prompt;
- the current project root;
- the current turn id;
- the available participants;
- where to read the blackboard;
- where to write its response;
- whether file edits/tool execution are allowed.

Other assistants see prior participant outputs by reading the same blackboard or
round artifacts. This gives transparency without relying on private native
session state.

### 2. Native plane

The native plane is the assistant's own terminal session:

```text
councli sessions start codex
councli sessions attach codex
```

It is for manual work and native feature access. `councli` records attach/detach
events and raw terminal logs, but it should not claim those logs are exact
semantic context.

If semantic import is needed, prefer each tool's native session export or
conversation store over screen scraping. Cold native resume is adapter-specific
and remains outside the MVP public surface.

### 3. Operator plane

The operator plane is the user-facing `councli` prompt or future TUI. It should
show:

- configured participants and readiness;
- current turn id;
- selected intent;
- streaming participant status;
- blackboard excerpts;
- synthesized council response;
- artifact paths;
- available slash commands.

The operator plane can be built with a prompt library first and a richer TUI
later. It should not become the protocol itself.

## Why screen scraping is the wrong protocol

Terminal capture is tempting because every assistant eventually prints text.
But it is the wrong source of truth:

- TUIs repaint and erase text;
- alternate-screen output may not appear in normal scrollback;
- line wrapping changes with terminal width;
- echoed prompts can be confused with responses;
- color/control sequences need parsing;
- old output can match new markers;
- auth prompts and permission dialogs mix with answers;
- a user can type manually while automation waits.

The safer rule is:

- use terminal capture for audit/debug;
- use explicit files and response sidecars for machine decisions.

## Visible rooms

A visible room can be useful:

```text
tmux window: councli room
  pane 1: codex native TUI
  pane 2: claude native TUI
  pane 3: agy native TUI
  pane 4: codewhale native TUI
```

This is an operator feature, not the collaboration protocol. It helps the user
see that assistants are alive and optionally interact with them. It should not
force `councli` to parse each pane.

Recommended behavior:

- create visible rooms explicitly, not automatically for every prompt;
- keep one project-scoped room per project unless the user asks for another;
- provide attach/detach instructions;
- record the room name in the project ledger;
- allow cleanup through `sessions prune`.

## Adapter contract

Each assistant adapter should eventually declare capabilities rather than a few
booleans:

```yaml
capabilities:
  prompt_transport:
    - exec_arg
    - stdin
    - file
    - tmux_paste
  semantic_output:
    - text
    - json
  native_session:
    supported: true
    resume: native_session_id
  permissions:
    reads_workspace: true
    writes_workspace: true
    runs_tools: true
    network_access: unknown
    full_permission: true
```

Routing should select participants by intent:

- chat: needs prompt transport and text output;
- deliberate: needs prompt transport and enough context budget;
- vote: needs structured decision output;
- review: needs diff input and reliable output;
- execute: needs write/tool permission and workspace isolation;
- native attach: needs tmux/native support.

This prevents a participant from being shown as simply `available` when it is
only available for some modes.

## Security considerations

Terminal integration broadens the trust boundary:

- tmux hooks execute commands;
- tmux `new-session` command strings can involve shell parsing;
- raw terminal logs can contain secrets;
- prompt injection can happen through repository files;
- assistants can run with full-permission/yolo modes;
- PATH resolution can change after trust.

Required controls:

- keep command-bearing config fields user-trusted, not project-trusted;
- pin or at least record resolved binary paths;
- validate tmux session names and detach keys;
- keep `.councli/` private and gitignored;
- do not share raw recordings without redaction;
- route implementation through worktrees;
- separate read/planning modes from execute modes where adapters support it.

## Performance and reliability considerations

Headless fan-out is naturally parallel, but it can amplify cost and latency.
Native sessions reduce setup friction but are harder to automate reliably.

Recommended constraints:

- default normal chat to one round;
- make peer-aware rounds explicit or participant-requested;
- cap stdout/stderr bytes per call;
- cap blackboard context passed into each participant;
- degrade failed participants quickly;
- avoid repeated calls to unauthenticated tools in the same turn;
- make cancellation and timeout visible in events;
- keep raw log rotation separate from semantic artifacts.

## Recommended architecture

For the next stable design, keep these boundaries:

```text
Councli prompt/TUI
  -> Turn router
  -> Intent policy
  -> Adapter capability selection
  -> Headless exec calls for semantic turns
  -> Artifact blackboard and response sidecars
  -> Optional tmux attach/visible room for native interaction
```

Do not route normal council communication through tmux panes. Use tmux for what
it is good at: native sessions, liveness, attach/detach, visible panes, and raw
diagnostics. Use files/events/sidecars for what machines need: durable state,
validation, retries, synthesis, decisions, and recovery.

## Research references

- tmux manual: sessions, panes, capture-pane, pipe-pane, start directories, and
  command execution behavior: <https://man7.org/linux/man-pages/man1/tmux.1.html>
- Linux `pty(7)`: pseudoterminal master/slave semantics and asynchronous data
  flow: <https://man7.org/linux/man-pages/man7/pty.7.html>
- Python `pty`: Unix PTY APIs and portability warnings:
  <https://docs.python.org/3/library/pty.html>
- Pexpect: pseudo-terminal automation, echo behavior, timeout/EOF matching, and
  chunking caveats: <https://pexpect.readthedocs.io/en/stable/api/pexpect.html>
- prompt_toolkit full-screen applications: useful for a future `councli` TUI,
  not for hosting assistant TUIs:
  <https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html>
- Textual: another candidate framework for a future operator TUI:
  <https://textual.textualize.io/>

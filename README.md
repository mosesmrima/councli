# councli

`councli` is a local council room for native coding-agent CLIs. It broadcasts a
prompt to installed assistants, lets them see each other's output through a
shared blackboard, and returns one synthesized answer.

It does not replace Codex, Claude Code, AGY, CodeWhale, or Kimi Code. Those
tools remain normal binaries on your machine. `councli` is the coordination
layer around them: room, recorder, router, and shared memory.

## Positioning

Multi-model platforms and agent frameworks already exist. Some route prompts to
hosted models, some synthesize model panels, and some help developers build new
agent workflows.

`councli` is narrower: it coordinates the coding assistants you already run in
your terminal. It preserves their native harnesses, auth, tools, permissions,
slash commands, MCP configuration, and session behavior instead of rebuilding
them as API agents.

In short: `councli` is the missing local room for developers who already use
multiple coding assistants and want them to think together inside the same repo.

## Install

```bash
pipx install councilroom-ai
councli --help
```

The PyPI package is `councilroom-ai`; the installed command is `councli`.

From GitHub:

```bash
pipx install "git+https://github.com/mosesmrima/councli.git"
```

## Supported assistants

The MVP supports these coding-agent CLIs:

| Assistant | Command |
| --- | --- |
| Codex | `codex` |
| Claude Code | `claude` |
| AGY | `agy` |
| CodeWhale | `codewhale` |
| Kimi Code | `kimi` |

Each assistant must already be installed, logged in, model-ready, and available
on `PATH` in the same shell where `councli` runs.

`councli` does not manage provider accounts, subscriptions, API keys, OAuth,
device-code login, or model configuration. Launch each assistant directly first,
finish its native setup, then let `councli` discover and coordinate it.

## What it does

- Runs shared multi-agent conversation turns from one prompt.
- Records all participant outputs on an inspectable blackboard.
- Synthesizes one council answer from the shared outputs.
- Supports explicit `/deliberate` and `/vote` commands when you want stronger
  coordination.
- Lets you attach to a native assistant TUI with `/assistant <name>` when `tmux`
  is available.
- Tracks artifacts under `.councli/` instead of hiding coordination state.
- Degrades gracefully when an assistant is missing, disabled, unauthenticated,
  or missing a model.

## What it is not

- Not a model provider.
- Not an auth manager.
- Not a replacement for the underlying assistants.
- Not a generic sandbox.
- Not a fixed workflow engine that forces every prompt through vote/review
  phases.

Normal prompts are just shared conversation turns. Stronger governance is
explicit.

## Quick start

From the project you want the assistants to inspect:

```bash
councli setup
councli doctor
councli
```

Inside the interactive shell:

```text
councli > what can you all do?
councli > /deliberate compare sqlite and postgres for this app
councli > /vote choose the transport: exec or tmux
councli > /assistant codex
councli > /quit
```

Useful setup commands:

```text
/agents
/enable claude
/disable kimi
/doctor
/status
/show latest
```

## How a turn works

```text
user prompt
  -> fan out to available assistants
  -> write participant responses to the blackboard
  -> synthesize one council answer
  -> return to the prompt
```

`/deliberate <prompt>` adds a peer-aware second round before synthesis.
`/vote <prompt>` asks participants for explicit decision artifacts.

## Platform support

`councli` is a pure Python CLI for Python 3.11+.

| Platform | Install | Core turns | Native tmux attach |
| --- | --- | --- | --- |
| Linux | Supported | Supported | Supported with `tmux` |
| macOS | Supported | Supported | Supported with `tmux` |
| Windows | Supported | Supported with exec-mode agents | Use WSL for tmux |
| WSL | Supported | Supported | Supported with `tmux` |

## Safety model

`councli` launches the assistant binaries configured in `.councli/config.yaml`.
Generated command templates and resolved binary paths are trusted before use.
If commands, binary paths, hashes, backends, or enabled flags change, review the
config and run:

```bash
councli trust
```

Run diagnostics:

```bash
councli doctor
councli doctor --json
councli security
```

Artifacts can contain prompts, responses, terminal output, and project context.
Before sharing artifacts, use:

```bash
councli artifacts scrub --dry-run
councli artifacts export --output support.tar.gz
```

## Development

```bash
git clone https://github.com/mosesmrima/councli.git
cd councli
uv sync
uv run pytest -q
uv build
```

## Documentation

- [Install guide](docs/INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Protocol design](docs/PROTOCOL_DESIGN.md)
- [Adapter contract](docs/ADAPTER_CONTRACT.md)
- [Participant adapter notes](docs/ADAPTERS.md)
- [Terminal substrate](docs/TERMINAL_SUBSTRATE.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Packaging and release notes](docs/PACKAGING.md)
- [Research findings](docs/RESEARCH_FINDINGS.md)

## Status

`councli` is early alpha software. The current public surface is shared
conversation, deliberation, explicit voting, native attach, durable artifacts,
and adapter readiness. Hidden experimental execution/review commands may exist
behind `COUNCLI_EXPERIMENTAL=1`, but they are not the MVP path.

License: MIT.

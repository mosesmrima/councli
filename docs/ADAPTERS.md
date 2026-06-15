# Participant adapter notes

`councli` treats each coding CLI as a native participant. The MVP integration
layer should preserve each tool's own UI, commands, permissions, sessions, MCP
configuration, slash commands, and subagent features.

The generic launch/readiness/capability contract lives in
`ADAPTER_CONTRACT.md`. This file records participant-specific command behavior
and local observations.

Preserve means "do not block or replace," not "reimplement." For the MVP,
`councli` sends normal coordination prompts and artifact-file instructions. It
does not translate user text into native slash commands, `@file` mentions,
MCP updates, provider configuration, or tool-specific goal APIs. Users can
attach to a participant's native session whenever they want those features.

## Prompt transport

Native terminal UIs do not all handle pasted multiline text the same way. AGY
can process the first line before the rest of a paste is submitted, which turns
one council prompt into multiple accidental turns.

Default automation transport for tmux-backed participants:

1. Render prompts as one compact line.
2. Input with the adapter-configured method:
   - `paste`: `tmux load-buffer` + `tmux paste-buffer`.
   - `type`: literal `tmux send-keys -l` chunks.
3. Wait briefly after paste.
4. Submit with adapter-configured key sequence.
5. For legacy file-backed council phases, wait for the requested artifact file.
6. For plain `sessions ask`, wait for a unique per-turn completion marker.

Default native transport for direct use:

1. Start the assistant in the project root under `tmux -L councli`.
2. Start raw pane capture with `pipe-pane` into `.councli/session-recordings/`.
3. Attach the user's terminal to the assistant session.
4. Let the assistant own all keystrokes, slash commands, permission prompts,
   and UI state.
5. Return to `councli` with the tmux detach chord (`Ctrl-]` by default).

Raw pane capture is for replay, audit, and debugging. It is not parsed as the
authoritative semantic transcript.

Use `prompt_style: verbatim` only for a participant proven to handle multiline
pastes reliably.

## Adapter fields

```yaml
backend: tmux
start_command: [...]
session_name: councli-<project-hash>-<participant>
broadcast_command: [...]
resume_command: [...]
done_marker: "<<<COUNCLI_DONE:<participant>>>"
prompt_style: compact
input_method: paste
submit_keys: ["Enter"]
post_paste_delay_seconds: 0.5
timeout_seconds: 900
```

`done_marker` is retained as a config field, but tmux runs generate a unique
per-turn marker to avoid false completion from old scrollback.

`broadcast_command` should prefer each assistant's safest read-only/planning
mode. Current `/broadcast` routing falls back to the normal prompt-capable
`command` only when command-level capabilities satisfy broadcast policy, or when
`broadcast_policy: allow_full_permission` makes the escalation explicit. The
run records whether a broadcast-specific command was explicit and which
capabilities were used.

The capability vocabulary is:

- `planning_only`
- `reads_workspace`
- `writes_workspace`
- `runs_tools`
- `network_access`
- `full_permission`

Broadcast commands are headless subprocesses. They are useful for shared
planning and review, but they do not append context to an already attached tmux
assistant session. The run artifact records this as `session_context:
headless_subprocess` and uses no retry policy.

`resume_command` should use `{session_id}` where the imported native session id
belongs. Native resume is adapter-specific and should be preferred over replaying
terminal history.

There is intentionally no adapter field for native slash commands in the MVP.
Command-language support should wait until the basic collaboration loop is
stable and there is a concrete reason to expose a specific native capability.

For shared conversation turns, the robust output path is the subprocess result
plus the per-turn artifact that `councli` writes under:

```text
.councli/runs/<run>/shared/<intent>.round<n>/<participant>.md
```

The current shared-turn trailer is a small text contract:

```text
COUNCLI_TRAILER
continue: false
recommend: none
summary: one short line
```

For legacy blackboard phases, the robust output path is the artifact file named
in the prompt:

```text
.councli/runs/<run>/incoming/<phase>/<participant>.md
.councli/runs/<run>/incoming/vote/<participant>.json
```

Terminal capture is diagnostic only, not an accepted artifact fallback, because
TUIs can wrap, split, repaint, or retain old text. If a legacy output file is
missing, `councli` retries once and then records the participant as failed or
abstained for that phase.

## Codex

Observed locally:

- `codex` starts the interactive TUI in the current directory.
- `codex --no-alt-screen` preserves terminal scrollback better for capture.
- `codex exec` is the official non-interactive scripting path.
- `codex exec --json` emits JSONL events.
- `codex resume <session-id>` and `codex exec resume <session-id>` support
  explicit resume; avoid `--last` in `councli` automation.
- Codex also has an app-server/thread API that may be a better future
  integration than terminal scraping for Codex specifically.

Recommended MVP:

```yaml
codex:
  backend: tmux
  binary: codex
  start_command: ["codex", "--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"]
  command: ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "{prompt}"]
  prompt_style: compact
  input_method: type
  submit_keys: ["Enter", "Enter"]
```

Transport finding:

- `paste-buffer` can leave large prompts as `[Pasted Content ...]` in the
  composer.
- Literal typing plus a double Enter submitted reliably in the local tmux test.

References:

- https://developers.openai.com/codex/cli
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/app-server

## AGY / Antigravity CLI

Observed locally:

- `agy` starts a native TUI in the current directory.
- `agy -i <prompt>` launches interactive mode with an initial prompt.
- `agy -p <prompt>` is useful for one-shot calls.
- `agy --continue` resumes the most recent conversation for the current
  workspace; `agy --conversation <uuid>` resumes an explicit conversation.
- Conversations are workspace-scoped.
- `/resume`, `/fork`, `/config`, `/permissions`, and other slash commands should
  remain native to AGY.

Transport finding:

- Multiline paste can split into multiple turns.
- Compact one-line prompt plus plain `Enter` worked in the live tmux test.

Recommended MVP:

```yaml
agy:
  backend: tmux
  binary: agy
  start_command: ["agy", "--dangerously-skip-permissions"]
  command: ["agy", "--dangerously-skip-permissions", "-p", "{prompt}"]
  broadcast_command: ["agy", "--sandbox", "--print", "{prompt}"]
  prompt_style: compact
  input_method: paste
  submit_keys: ["Enter"]
  post_paste_delay_seconds: 0.5
```

References:

- https://www.antigravity.google/docs/cli-overview
- https://www.antigravity.google/docs/cli-using
- https://www.antigravity.google/docs/cli-conversations

## Claude Code

Observed locally:

- `claude` starts the interactive TUI and may show a trust prompt.
- `claude -p <prompt>` is the non-interactive SDK-style mode.
- `claude -p --output-format json <prompt>` returns structured results and
  includes a `session_id`, even for runtime auth failures.
- `claude --resume <session-id-or-name>` resumes explicit sessions.
- `claude --continue` resumes the most recent conversation in the current
  directory; avoid this in automation unless the user explicitly asks.
- `--session-id <uuid>` can pin a session id.
- Claude has native background agents, worktrees, permission modes, plugins,
  MCP, and subagents. `councli` should not hide these.

Current local state:

- Installed and logged in.
- Runtime access is blocked by organization/subscription policy until the user
  enables access or configures API-key billing.

Recommended MVP:

```yaml
claude:
  backend: tmux
  binary: claude
  start_command: ["claude", "--dangerously-skip-permissions"]
  command: ["claude", "--dangerously-skip-permissions", "-p", "--output-format", "json", "{prompt}"]
  prompt_style: compact
  input_method: paste
  submit_keys: ["Enter"]
```

References:

- https://code.claude.com/docs/en/cli-usage

## Kimi Code

Observed locally:

- `kimi` starts the TUI and prints a native session id.
- `kimi login` uses device-code auth.
- `kimi --session <id>` resumes an explicit session.
- `kimi --continue` resumes the previous session for the working directory.
- Sessions are stored under `~/.kimi-code/sessions/<workDirKey>/<sessionId>/`.
- `state.json` holds metadata; `agents/*/wire.jsonl` holds event streams.
- Providers can be configured via `/provider` or `kimi provider`.

Current local state:

- Installed.
- No provider/model configured yet, so model calls fail until login/provider
  setup is completed.

Recommended MVP:

```yaml
kimi:
  backend: tmux
  binary: kimi
  start_command: ["kimi", "--yolo", "--auto"]
  command: ["kimi", "--prompt", "{prompt}"]
  broadcast_command: ["kimi", "--prompt", "{prompt}"]
  prompt_style: compact
  input_method: paste
  submit_keys: ["Enter"]
```

Do not combine `--prompt` with `--yolo`, `--auto`, or `--plan`. Kimi rejects
those combinations at startup; non-interactive prompt mode has its own approval
behavior. Use the native tmux session for yolo/auto interaction.

References:

- https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html
- https://www.kimi.com/code/docs/en/kimi-code-cli/guides/sessions.html
- https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/providers.html

## CodeWhale

Observed locally:

- `codewhale --skip-onboarding` jumps directly to the TUI composer.
- `codewhale --yolo` enables YOLO mode and auto-approves all tools.
- `codewhale auth status --provider deepseek` gives precise auth state.
- `codewhale doctor` verifies API connectivity and tool availability.
- `codewhale sessions` lists saved sessions with short ids.
- `codewhale --resume <session-id>` resumes an explicit saved session.
- Plain `Enter` submits a prompt in tmux.
- For bracketed paste, prompts can remain in the draft composer.
- Writing the `.councli/runs/.../incoming/...` artifact may trigger a native
  write approval. This is useful in visible mode, but full automation needs a
  CodeWhale permission/profile setting or a non-interactive adapter.

Current local state:

- DeepSeek API key is configured.
- API connectivity passes.
- Live session `councli-real-codewhale` responded successfully.

Recommended MVP:

```yaml
codewhale:
  backend: tmux
  binary: codewhale
  start_command: ["codewhale", "--yolo", "--skip-onboarding"]
  command: ["codewhale", "--yolo", "exec", "--auto", "{prompt}"]
  broadcast_command: ["codewhale", "exec", "{prompt}"]
  prompt_style: compact
  input_method: type
  submit_keys: ["Enter", "Enter"]
```

References:

- https://codewhale.net/en
- https://codewhale.net/en/docs

## Session lifecycle

Hot resume:

- Reuse live tmux sessions by project-scoped `councli` session id.
- Treat tmux as the liveness source of truth. `registry.json` is an annotation
  cache and should be reconciled before user-facing session operations.

Cold resume:

- Prefer explicit native session ids.
- `sessions import <agent>` records explicit ids or explicit files from native
  assistant session stores.
- Without `--session-id` or `--path`, import lists ranked candidates with
  confidence/evidence and does not pick one for the user. `--auto` is accepted
  only for a single high-confidence top match.
- `sessions resume <agent>` launches the configured `resume_command`.
- `sessions resume <agent>` refuses to run over a live tmux session unless
  `--replace-existing` is supplied.
- Never blindly use `--last`, `--continue`, or similar global/latest commands
  unless the user explicitly requests that behavior.

Late join:

- If a participant joins after earlier deliberation, generate an inspectable
  `.councli/tasks/<id>/brief.md` and ask the participant to read it. Do not
  silently replay hidden context into a native session.

Cleanup:

- `sessions stop [agent]` captures diagnostic pane text and kills configured
  tmux-backed participant sessions.
- `sessions prune` captures diagnostic pane text and kills configured councli tmux
  sessions plus visible `councli-room-*` rooms.
- `pause` should keep tmux sessions alive for hot resume.
- Use `--dry-run` before cleanup to inspect which sessions would be stopped.

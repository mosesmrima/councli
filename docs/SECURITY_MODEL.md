# councli security and trust model

`councli` orchestrates external coding CLIs. That makes its security model
different from a normal library or service: the primary risk is not only bugs in
`councli`, but the fact that configured assistants are intentionally allowed to
read files, run tools, and sometimes write code with yolo/full-permission modes.

This document defines the trust boundaries, threat model, current controls, and
hardening path.

Adapter capability and launch-readiness semantics are defined in
`ADAPTER_CONTRACT.md`; this document focuses on the resulting trust and threat
boundaries.

## Security posture

For a personal utility, it is reasonable to run trusted assistants with broad
permissions when the user knowingly asks for that. For maintainable production
software, broad permissions must be explicit policy, not an accidental default.

The target posture:

- `councli` is local-first and user-controlled.
- Project files are not trusted to silently expand execution policy.
- Assistant command templates are trusted user policy.
- Assistant output is untrusted input.
- Terminal logs, prompts, blackboards, diffs, and errors are sensitive data.
- Native assistant sessions are useful, but their screen output is not a secure
  or reliable protocol.

## Assets

Primary assets:

- source code in the project worktree;
- credentials in environment variables, shell history, config files, `.env`
  files, SSH agents, cloud CLIs, package-manager auth stores, and assistant
  provider configs;
- generated diffs and review artifacts;
- raw tmux recordings;
- `.councli/config.yaml`;
- user-local trust pins under `$COUNCLI_STATE_HOME`, `$XDG_STATE_HOME/councli`,
  or `~/.local/state/councli`;
- native assistant session ids and session files.

Secondary assets:

- model usage/cost budgets;
- local CPU/memory/disk resources;
- private repo names, paths, issue details, and prompts;
- inter-assistant reasoning artifacts that may reveal strategy or business
  context.

## Trust boundaries

```text
user intent
  -> councli CLI
  -> project .councli/config.yaml        # repo-owned, not inherently trusted
  -> user-local trust pin                # user-owned authorization
  -> participant subprocess/tmux session # external executable boundary
  -> artifacts and raw logs              # sensitive local state
```

Boundary rules:

- The repository can propose config, but the user-local trust pin authorizes
  command-bearing fields.
- Assistant CLIs are external programs. `councli` can supervise them but cannot
  make them safe once full permissions are granted.
- Worktree execution protects the main worktree from accidental writes, but it
  does not sandbox network, credentials, home directory access, or local tools.
- tmux pane capture is a recording boundary, not a semantic trust boundary.
- Native assistant session stores belong to each assistant and may have their
  own security model.

## Threat model

### Malicious or compromised project repository

Attack paths:

- change `.councli/config.yaml` to run a different binary or command;
- choose a tmux session name or detach key that injects tmux format behavior;
- add files that trick assistants into running harmful commands;
- include prompts or scripts that exfiltrate secrets when an assistant executes
  tools.

Current controls:

- command-bearing fields are hashed and pinned in user-local state;
- config changes require `councli trust`;
- `councli trust --dry-run` previews command/config field changes and binary
  drift before rewriting the trust pin;
- generated configs include `schema_version: councli.config.v1`;
- `councli config check` and `councli config migrate` inspect and upgrade
  legacy configs without changing trusted command fields;
- project identity detects copied/moved `.councli/` directories;
- tmux socket/session names and detach keys are validated;
- argv arrays are used instead of shell-string command templates in the main
  `exec` path;
- regression tests cover shell metacharacters in prompt text and malicious
  prompt-bearing config fields for exec commands, probes, and sandbox wrappers.

Hardening:

- broaden hostile-input coverage as new adapter transports and command fields
  are added.

### Command injection and shell interpretation

Attack paths:

- user prompt contains shell metacharacters;
- config command template is shell-interpreted;
- tmux `new-session` starts a command through a shell;
- raw user input is passed into a CLI whose own parser treats it as options.

Current controls:

- normal `exec` uses argv lists and `subprocess.run(..., shell=False)`;
- `{prompt}` replacement occurs as one argv element when templates are correct;
- command validation rejects `{prompt}` embedded inside a larger argv token;
- probe commands reject `{prompt}` entirely;
- `sandbox_wrapper` rejects `{prompt}` and option-like executable positions;
- tmux control arguments are passed as argv to the `tmux` binary;
- tmux names and keys are constrained.

Gaps:

- tmux `new-session` ultimately starts a shell command string, built from a
  quoted argv list;
- assistant CLIs may interpret prompt text in tool-specific ways.

Hardening:

- reject command templates where `{prompt}` is embedded in a larger string
  unless explicitly allowed;
- prefer assistant flags that read prompt content from stdin or a file when
  available;
- classify command templates by shell involvement;
- add tests for prompt strings containing quotes, semicolons, newlines, and
  leading dashes.

### Malicious or mistaken assistant output

Attack paths:

- assistant suggests unsafe commands;
- assistant writes malicious code in an implementation worktree;
- assistant fabricates review approval;
- assistant hides risk in verbose output;
- assistant emits malformed trailers or JSON.

Current controls:

- default shared turns do not execute implementation commands;
- implementation is isolated in a git worktree;
- `apply` requires accepted review by default and checks patch applicability;
- malformed votes/reviews degrade or abstain rather than automatically approve;
- terminal screen output is not accepted as the source of truth for legacy
  file-backed phases.

Hardening:

- validate participant responses through JSON sidecars;
- add explicit policy gates for implementation, review, and apply;
- add diff scanning hooks before apply;
- require provenance for review decisions;
- make single-participant acceptance visibly weaker than multi-participant
  acceptance.

### Secret leakage through artifacts

Attack paths:

- raw tmux logs capture API keys, shell prompts, stack traces, or copied secrets;
- prompts include `.env` contents or private logs;
- assistant stdout/stderr contains credentials;
- `.councli/` is committed or copied;
- task briefs include paths and session metadata.

Current controls:

- `.councli/` is added to `.gitignore` for git repos;
- raw logs use private file modes where possible;
- raw logs rotate by size;
- task briefs warn that raw logs are diagnostic, not authoritative;
- `artifacts scrub` redacts configured secret patterns in place after an
  explicit `--write`;
- `artifacts export` creates a redacted support bundle with a manifest and does
  not include raw terminal recordings unless that artifact class is explicitly
  selected.

Hardening:

- add configurable retention policies for runs, blobs, raw logs, and snapshots;
- add built-in secret pattern detection before printing or exporting artifacts;
- default raw logs to opt-in for shared/team environments;
- keep expanding support-bundle coverage as new artifact classes are added.

### PATH and binary substitution

Attack paths:

- malicious binary named `codex`, `claude`, or `agy` appears earlier on `PATH`;
- installed assistant binary changes after trust;
- shell initialization changes PATH between sessions.

Current controls:

- `health()` resolves binaries with `shutil.which`;
- trust pins command templates, resolved executable paths, executable SHA-256
  hashes, and version metadata for enabled agents;
- load-time trust checks reject resolved binary path or content-hash drift until
  the user reviews and reruns `councli trust`;
- `councli security` reports the trusted command surface, current binary paths,
  version metadata, elevated command surfaces, and path/hash/version drift
  without running agent prompts;
- `councli doctor --security` embeds the same security summary beside the normal
  readiness report when the config is already trusted.

Hardening:

- allow absolute command paths in trusted config;
- show binary path in `doctor` and security reports.

### Resource exhaustion and cost amplification

Attack paths:

- one prompt fans out to many expensive model calls;
- participants produce huge outputs;
- raw logs grow until disk pressure;
- hung subprocesses accumulate;
- repeated retries multiply cost.

Current controls:

- per-command timeout exists;
- raw logs rotate by size/backups;
- shared chat defaults to one round unless continuation is requested;
- broadcast has no retry policy.

Hardening:

- add per-turn and per-participant output byte limits;
- add cost/latency budgets;
- cancel outstanding participant calls when the user cancels a turn;
- terminate process groups, not only parent processes, on timeout;
- add disk quota warnings for `.councli/`.

## Permission model

Configured assistant commands declare command-level capability metadata. Legacy
configs may still contain `broadcast_read_only`, but `/broadcast` policy should
be driven by capabilities and explicit fallback policy:

```yaml
command_capabilities: ["reads_workspace", "runs_tools"]
broadcast_capabilities: ["planning_only", "reads_workspace"]
start_capabilities: ["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"]
read_only_policy: safe_only
broadcast_policy: safe_only
```

Policy examples:

- normal chat: `reads_workspace` allowed;
- `/broadcast`: prefer `planning_only`, warn on higher permissions;
- `/deliberate`: `reads_workspace` allowed, `writes_workspace` denied;
- `run`: `writes_workspace` allowed only inside a worktree;
- `/assistant`: native session owns permissions; `councli` records that the user
  crossed into native mode.

The policy engine should reject accidental escalation:

```text
requested intent: broadcast
configured command: codewhale --yolo exec --auto
capabilities: full_permission
decision: allowed only if fallback_full_permission=true
```

## Storage and file permissions

Storage classes:

- project config: `.councli/config.yaml`;
- run artifacts: `.councli/runs/<run-id>/`;
- project ledger: `.councli/ledger/events.jsonl`;
- raw logs: `.councli/session-recordings/`;
- user trust pins: `$XDG_STATE_HOME/councli/trust/` or fallback
  `~/.local/state/councli/trust/`.

Rules:

- user-local trust state should live outside the project repository;
- raw logs and recordings should be mode `0600` where possible;
- directories containing raw logs should be mode `0700` where possible;
- `.councli/` should be ignored by git by default;
- artifact paths referenced from JSON must stay inside the run directory unless
  the schema explicitly allows an external path;
- crash recovery should preserve evidence rather than silently deleting it.

## Process isolation options

Current isolation:

- subprocess timeouts;
- cwd control;
- git worktrees for implementation;
- tmux session scoping;
- optional `sandbox_wrapper` argv prefix for exec-mode prompt commands. The
  wrapper is trusted config, checked for binary presence in `health()`, and
  prepended without shell interpretation.

Optional stronger primitives:

- Linux user namespaces: isolate filesystem/user identity without requiring
  full containers, but setup varies by distro.
- Bubblewrap/firejail: practical local sandbox wrappers for Linux, but add
  dependencies and compatibility risk.
- Containers: strong packaging boundary, weaker access to host developer tools
  unless carefully mounted.
- systemd-run user scopes: useful for process accounting, timeouts, and cleanup
  when background jobs exist.
- Linux capabilities: useful for dropping privilege in privileged contexts, but
  most local assistant processes should simply run as the unprivileged user.

Recommendation: do not add mandatory sandboxing yet. Add a sandbox command
wrapper hook later so users can run participant commands under `bwrap`,
containers, or systemd scopes when needed.

## Audit and observability

Security-relevant events should be structured:

- config trusted;
- config trust mismatch;
- binary path changed;
- participant command started/completed;
- participant timeout;
- native session attached/detached;
- raw capture started/stopped;
- implementation worktree created;
- patch applied;
- artifact redacted/exported/deleted.

Do not log:

- environment variables by default;
- provider API keys;
- full shell history;
- unredacted exported artifacts without explicit user action.

For production bug reports, `councli` should support a redacted bundle:

```text
councli doctor --security --redacted
councli export-run <run-id> --redacted
```

## Current security assessment

Acceptable for a personal local utility:

- trusted user runs assistant CLIs on their own machine;
- `.councli/` stays local and ignored;
- user knowingly enables yolo/full-permission commands;
- user manually reviews diffs before apply.

Not yet acceptable for shared/team/production use:

- no structured secret redaction;
- no adapter capability policy;
- no stable binary version drift report beyond executable hash drift detection;
- no stable security audit report;
- no sandbox wrapper policy;
- no formal schema for participant responses.

## Hardening roadmap

1. Add a security section to `doctor` showing command permissions, resolved
   binary paths, yolo/full-permission commands, raw-log status, and trust state.
2. Add stable binary version reporting alongside existing executable hash drift
   checks.
3. Add adapter capabilities and policy checks for chat, broadcast, deliberate,
   vote, run, and review.
4. Add run-local locks and response sidecars so malformed output cannot corrupt
   machine state.
5. Add secret redaction and retention controls.
6. Make cancellation events consistent across all foreground commands.
7. Add `councli scrub` and redacted export.
8. Add optional sandbox wrappers.
9. Keep expanding integration tests for hostile prompts and malicious configs
   as new adapter transports and command fields are added.
10. Add a versioned security migration guide before any public release.

## Research references

- OWASP OS Command Injection Defense Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html
- OWASP Secrets Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- XDG Base Directory Specification: https://specifications.freedesktop.org/basedir/
- Linux capabilities manual: https://man7.org/linux/man-pages/man7/capabilities.7.html
- Linux advisory locking manual: https://man7.org/linux/man-pages/man2/fcntl_locking.2.html

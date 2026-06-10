# Fable 5 MVP Consultation

Date: 2026-06-11

Reviewer: Claude Fable 5 via Claude Code, uncapped run, safe-mode read-only consultation.

Scope read by reviewer:

- `README.md`
- `docs/RESEARCH_FINDINGS.md`
- `docs/AGENT_LAUNCH_PROTOCOL.md`
- `docs/ARCHITECTURE_DECISIONS.md`
- `docs/SYSTEMS_REVIEW.md`
- `docs/PROTOCOL_DESIGN.md`
- `docs/STATE_CONCURRENCY.md`
- `docs/SECURITY_MODEL.md`
- `docs/OPERATIONS_MODEL.md`
- `docs/ADAPTER_CONTRACT.md`
- `docs/TERMINAL_SUBSTRATE.md`
- `docs/ADAPTERS.md`
- `src/councli/agents.py`
- `src/councli/cli.py`
- `src/councli/config.py`
- `src/councli/events.py`
- `src/councli/council.py`
- `src/councli/protocol.py`
- `tests/test_event_architecture.py`

## TL;DR

The docs describe the right product, but the code still carries an older product shape.

The biggest gap is that the council does not yet truly see each other's full work. The current shared-turn path gives later rounds compact summaries instead of full peer artifacts, the blackboard still reflects the abandoned fixed lifecycle, prompts are passed inline through argv, and three council engines coexist.

The MVP should be one engine: the shared turn in `cli.py`, hardened with packet-file context, response sidecars, cross-process run locking, safe defaults, honest readiness, and a blackboard that shows real participant interaction.

## Diagnosis

Keep these parts:

- Trust model in `config.py`: hash-pinned command-bearing fields, project identity checks, tmux validation.
- Artifact discipline: append-only `events.jsonl`, blobs, `state.json`, and `blackboard.md` as projections.
- tmux native sessions: dedicated socket, project-scoped names, cwd validation, raw capture, attach/detach.
- Fake-binary integration tests: this is the right MVP harness.

Fix these contradictions:

- The shared council interaction is hollow because peer context is too compact for real critique.
- Prompts travel as argv tokens, which will hit length limits and leak prompt content in process listings.
- `/vote` still depends on fragile text trailers instead of validated sidecars.
- The event ledger is not multi-process safe; sequence numbers can race across processes.
- Blackboard rendering still reflects orient/propose/critique/revise/vote rather than shared turns.
- Readiness currently means "binary exists", not "agent can answer this intent".
- Defaults include full-permission/yolo commands for normal chat, which contradicts the documented safety posture.
- There are three council engines: `protocol.py`, `council.py`, and `cli.py`.
- Timeout/cancellation does not cleanly own process groups.

## MVP Definition

One interactive shell where:

- A user prompt fans out to real installed CLI binaries.
- Each response is saved as an inspectable artifact.
- Participants can see peer outputs when the turn asks for deliberation or follow-up.
- Councli produces one synthesized answer grounded in visible artifacts.
- Failed or unavailable participants degrade honestly instead of blocking the room.

MVP user surface:

- `councli` / `councli chat`
- plain prompts for normal shared conversation
- `/deliberate <prompt>` for peer-visible critique/refinement
- `/vote <prompt>` for explicit governance decisions
- `/assistant <name>` for native tmux attach
- `/status`, `/show`, `/broadcast`, `/brief`, `/quit`
- `doctor --json`
- `init`, `setup`, `trust`, `status`, `show`

MVP run directory:

- `task.md`
- `events.jsonl`
- `run.lock`
- `packets/`
- `shared/<intent>.round<n>/<name>.md`
- `shared/<intent>.round<n>/<name>.response.json`
- `synthesis/`
- `decisions/vote.json`
- `blackboard.md`
- `state.json`

Quality bar:

- Transparency: every participant body is visible in files and blackboard.
- Real interaction: peer rounds receive bounded full peer bodies, not one-line summaries.
- Grounded single answer: synthesis names participants, cites artifacts, and surfaces disagreements.

## Build Now

- Packet-file prompt transport for shared turns.
- Bounded full-body peer context for round 2.
- `councli.response.v1` sidecars and validation for `/vote`.
- `fcntl.flock` run locking around event append, sequence allocation, packet writes, and projections.
- Per-intent readiness and `doctor --json`.
- Safe default commands; full permission must be explicit configuration.
- Blackboard renderer centered on `intent.roundN`.
- Deterministic synthesizer selection with provenance labeling.
- Process-group launch and cancellation cleanup.
- Fake-binary acceptance gates for all of the above.

## Defer

- Worktree execution.
- Review/revision/executor replacement loops.
- `apply`.
- Daemon or SQLite index.
- Retention/redaction tooling.
- Full adapter manifest schema.
- Metrics export.
- Visible room UX beyond native attach.
- Cold resume/session import.

## Remove Or Hide

- Delete `protocol.py` and the `reason` command.
- Remove `/legacy-council`.
- Remove the fixed orient/propose/critique/revise/vote lifecycle from the user-facing path.
- Hide `run`, `apply`, and executor/review flows behind an experimental flag or remove them from MVP help.
- Drop `sessions relay`, `sessions import/resume`, and visible-room workflows from the MVP surface.
- Trim generated defaults so yolo/full-permission flags are not normal chat defaults.
- Collapse overlapping design docs into a smaller canonical set after the MVP cut is implemented.

## First 10 Implementation Tasks

1. Consolidate to one engine: shared turns in `cli.py`.
2. Fix `DEFAULT_CONFIG` and prompt transports.
3. Add `run.lock` with `fcntl.flock` to `EventLedger`.
4. Implement packet-file transport for shared turns.
5. Add real bounded peer context to peer rounds.
6. Add response sidecars and sidecar validation.
7. Add normalized failure classification and per-intent readiness.
8. Rewrite blackboard/state projections around generic `intent.roundN` groups.
9. Harden synthesis with deterministic selection and provenance requirements.
10. Add cancellation/process-group handling and the acceptance test suite.

Dependency order:

- Tasks 1-3 can land first.
- Tasks 4-6 are sequential.
- Tasks 7-9 can proceed after packet transport exists.
- Task 10 closes the slice.

## Acceptance Gates

1. Council round-trip: three fake participants produce three bodies, three sidecars, a blackboard with all full bodies, and a synthesis that names at least two participants.
2. Peer visibility: `/deliberate` round-2 packets contain verbatim round-1 peer bodies within the configured cap; argv prompt stays under 1 KB.
3. Degradation: auth/model failures are classified, shown, and skipped on the next turn in the same session.
4. Vote integrity: invalid or missing sidecars become abstentions; zero valid votes never fabricate a winner.
5. Concurrency: two processes appending to one run produce unique monotonic sequence numbers and parseable JSONL.
6. Cancellation: Ctrl-C records `turn.canceled`, renders partial blackboard, and leaves no child agent process groups behind.
7. Prompt safety: prompts with leading dashes, quotes, newlines, command substitutions, and 50 KB text pass intact.
8. Readiness honesty: `doctor --json` distinguishes missing binary, auth required, model unconfigured, and ready per intent.
9. Recoverability: deleting projections and re-rendering from events/artifacts reproduces the same blackboard/state.
10. Real smoke: at least two real agents complete a shared or deliberate turn with readable synthesis in under about three minutes.

## Invalidating Risks

- Cost and latency make the council unpleasant.
- Only one agent is actually ready on most machines.
- CLI flags drift or disappear upstream.
- "Read-only chat" is not actually enforceable for every vendor CLI.
- Sidecar/trailer parsing fails often enough that `/vote` is untrustworthy.
- Synthesis hides disagreement or launders one model's answer as consensus.

## Practical MVP Direction

Stop maintaining three protocols and a deferred execution pipeline.

Build one shared-turn engine that is:

- transparent
- peer-aware
- lock-safe
- honest about readiness
- grounded in visible artifacts
- safe by default

That is enough to prove the product: real installed coding agents acting as a visible council, not a wrapper that broadcasts prompts and hides how the answer was formed.

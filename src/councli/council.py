from __future__ import annotations

import json
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from councli.agents import (
    AgentRunResult,
    AgentRunner,
    attach_tmux_session,
    ensure_tmux_session,
    tmux,
    tmux_attach_command,
    tmux_session_exists,
)
from councli.artifacts import write_json, write_text
from councli.events import EventLedger


Phase = Literal["orient", "propose", "critique", "revise", "vote", "review"]
OUTPUT_FILE_ATTEMPTS = 2


@dataclass(frozen=True)
class BlackboardItem:
    phase: str
    participant: str
    ok: bool
    content: str
    error: str = ""


@dataclass
class CouncilState:
    council_id: str
    task: str
    root: Path
    run_dir: Path
    participants: list[str]
    ledger: EventLedger
    min_confidence: float = 0.55
    items: list[BlackboardItem] = field(default_factory=list)
    votes: dict[str, dict] = field(default_factory=dict)
    plan_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CouncilResult:
    run_dir: Path
    participants: list[str]
    votes: dict[str, dict]
    decision: dict
    state: CouncilState


ProgressCallback = Callable[[str, CouncilState, dict], None]


def run_blackboard_council(
    *,
    task: str,
    runners: dict[str, AgentRunner],
    root: Path,
    run_dir: Path,
    participants: list[str] | None = None,
    dry_run: bool = False,
    complete_run: bool = True,
    min_confidence: float = 0.55,
    progress: ProgressCallback | None = None,
) -> CouncilResult:
    selected = select_available_participants(runners, participants)
    ledger = EventLedger(run_dir, run_id=run_dir.name)
    state = CouncilState(
        council_id=run_dir.name,
        task=task.strip(),
        root=root,
        run_dir=run_dir,
        participants=list(selected),
        ledger=ledger,
        min_confidence=min_confidence,
    )
    write_text(run_dir / "task.md", state.task + "\n")
    write_json(
        run_dir / "participants.json",
        {name: runners[name].health() for name in selected},
    )
    ledger.append(
        "run.started",
        payload={
            "task": state.task,
            "root": str(root),
            "dry_run": dry_run,
            "min_confidence": min_confidence,
        },
    )
    for name in selected:
        health = runners[name].health()
        ledger.append(
            "participant.joined",
            participant=name,
            payload={
                "enabled": health.enabled,
                "binary": health.binary,
                "path": health.path,
                "available": health.available,
                "reason": health.reason,
                "backend": health.backend,
            },
        )
    ledger.render()
    emit_progress(progress, "participants", state, {"participants": list(selected)})

    for phase in ("orient", "propose", "critique", "revise"):
        run_phase(state, selected, phase, dry_run=dry_run, progress=progress)

    register_plan_candidates(state)
    emit_progress(progress, "plans_registered", state, {"plan_ids": list(state.plan_ids)})
    run_vote_phase(state, selected, dry_run=dry_run, progress=progress)
    decision = decide_council(
        state.votes,
        state.participants,
        state.plan_ids,
        min_confidence=state.min_confidence,
    )
    write_json(run_dir / "decision.json", decision)
    state.ledger.append("decision.finalized", payload=decision)
    if complete_run:
        state.ledger.append("run.completed", payload={"approved": decision.get("approved", False)})
    state.ledger.render()
    emit_progress(progress, "decision", state, {"decision": decision})
    return CouncilResult(
        run_dir=run_dir,
        participants=state.participants,
        votes=state.votes,
        decision=decision,
        state=state,
    )


def select_available_participants(
    runners: dict[str, AgentRunner],
    participants: list[str] | None,
) -> dict[str, AgentRunner]:
    names = participants or list(runners)
    selected: dict[str, AgentRunner] = {}
    for name in names:
        if name not in runners:
            continue
        runner = runners[name]
        health = runner.health()
        if health.available:
            selected[name] = runner
    return selected


def run_phase(
    state: CouncilState,
    runners: dict[str, AgentRunner],
    phase: Literal["orient", "propose", "critique", "revise"],
    *,
    dry_run: bool,
    progress: ProgressCallback | None = None,
) -> None:
    state.ledger.append("phase.started", phase=phase, payload={"participants": list(runners)})
    state.ledger.render()
    emit_progress(progress, "phase_started", state, {"phase": phase, "participants": list(runners)})
    items: dict[str, BlackboardItem] = {}
    if dry_run:
        items = {
            name: synthetic_phase_item(state, phase=phase, participant=name)
            for name in runners
        }
    else:
        with ThreadPoolExecutor(max_workers=max(1, len(runners))) as pool:
            futures = {
                pool.submit(run_participant_phase, state, phase, name, runner): name
                for name, runner in runners.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    items[name] = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard around adapters
                    items[name] = BlackboardItem(
                        phase=phase,
                        participant=name,
                        ok=False,
                        content="",
                        error=str(exc),
                    )
    for name in runners:
        item = items[name]
        state.items.append(item)
        write_phase_artifact(state.run_dir, item)
        append_response_event(state, item)
        state.ledger.render()
        emit_progress(
            progress,
            "participant_response",
            state,
            {
                "phase": phase,
                "participant": name,
                "ok": item.ok,
                "content": item.content,
                "error": item.error,
            },
        )
    state.ledger.append("phase.completed", phase=phase, payload={"participants": list(runners)})
    state.ledger.render()
    emit_progress(progress, "phase_completed", state, {"phase": phase, "participants": list(runners)})


def run_participant_phase(
    state: CouncilState,
    phase: Literal["orient", "propose", "critique", "revise"],
    name: str,
    runner: AgentRunner,
) -> BlackboardItem:
    output_path = participant_output_path(state, phase=phase, participant=name, suffix="md")
    result: AgentRunResult | None = None
    for attempt in range(1, OUTPUT_FILE_ATTEMPTS + 1):
        remove_stale_output(output_path)
        packet = build_packet(
            state,
            phase=phase,
            participant=name,
            output_path=output_path,
            extra_context=retry_context(attempt),
        )
        result = runner.run(packet, cwd=state.root, dry_run=False, output_path=output_path)
        file_content = read_participant_output(output_path)
        if file_content:
            return BlackboardItem(phase=phase, participant=name, ok=True, content=file_content)
    return missing_output_item(phase, name=name, output_path=output_path, result=result)


def run_vote_phase(
    state: CouncilState,
    runners: dict[str, AgentRunner],
    *,
    dry_run: bool,
    progress: ProgressCallback | None = None,
) -> None:
    state.ledger.append("phase.started", phase="vote", payload={"participants": list(runners)})
    state.ledger.render()
    emit_progress(progress, "phase_started", state, {"phase": "vote", "participants": list(runners)})
    items: dict[str, BlackboardItem] = {}
    votes: dict[str, dict] = {}
    if dry_run:
        for name in runners:
            preferred_participant = state.participants[0] if state.participants else name
            vote = synthetic_vote(name, preferred=preferred_participant)
            votes[name] = vote
            items[name] = BlackboardItem(
                phase="vote",
                participant=name,
                ok=True,
                content=json.dumps(vote, sort_keys=True),
            )
    else:
        with ThreadPoolExecutor(max_workers=max(1, len(runners))) as pool:
            futures = {
                pool.submit(run_participant_vote, state, name, runner): name
                for name, runner in runners.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    item, vote = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard around adapters
                    item = BlackboardItem(
                        phase="vote",
                        participant=name,
                        ok=False,
                        content="",
                        error=str(exc),
                    )
                    vote = empty_vote(str(exc))
                items[name] = item
                votes[name] = vote
    for name in runners:
        item = items[name]
        state.items.append(item)
        write_phase_artifact(state.run_dir, item)
        state.votes[name] = votes[name]
        write_json(state.run_dir / "votes" / f"{name}.json", votes[name])
        append_response_event(state, item)
        state.ledger.append(
            "ballot.submitted",
            phase="vote",
            participant=name,
            payload={"vote": votes[name]},
        )
        state.ledger.render()
        emit_progress(
            progress,
            "participant_response",
            state,
            {
                "phase": "vote",
                "participant": name,
                "ok": item.ok,
                "content": item.content,
                "error": item.error,
            },
        )
    state.ledger.append("phase.completed", phase="vote", payload={"participants": list(runners)})
    state.ledger.render()
    emit_progress(progress, "phase_completed", state, {"phase": "vote", "participants": list(runners)})


def emit_progress(
    progress: ProgressCallback | None,
    event: str,
    state: CouncilState,
    payload: dict,
) -> None:
    if progress is not None:
        progress(event, state, payload)


def run_review_phase(
    state: CouncilState,
    runners: dict[str, AgentRunner],
    *,
    executor: str,
    attempt: int,
    selected_plan: str | None,
    diff_ref: str,
    result_ref: str,
    dry_run: bool,
    min_confidence: float | None = None,
) -> dict:
    reviewers = {
        name: runner
        for name, runner in runners.items()
        if name in state.participants and name != executor
    }
    state.ledger.append(
        "phase.started",
        phase="review",
        payload={"attempt": attempt, "participants": list(reviewers), "executor": executor},
    )
    state.ledger.render()
    if not reviewers:
        decision = decide_review({}, [], attempt=attempt, min_confidence=min_confidence or state.min_confidence)
        state.ledger.append("review.finalized", phase="review", payload=decision)
        state.ledger.append("phase.completed", phase="review", payload={"attempt": attempt, "participants": []})
        state.ledger.render()
        return decision

    items: dict[str, BlackboardItem] = {}
    reviews: dict[str, dict] = {}
    if dry_run:
        for name in reviewers:
            review = synthetic_review(name)
            reviews[name] = review
            items[name] = BlackboardItem(
                phase="review",
                participant=name,
                ok=True,
                content=json.dumps(review, sort_keys=True),
            )
    else:
        with ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
            futures = {
                pool.submit(
                    run_participant_review,
                    state,
                    name,
                    runner,
                    executor=executor,
                    attempt=attempt,
                    selected_plan=selected_plan,
                    diff_ref=diff_ref,
                    result_ref=result_ref,
                ): name
                for name, runner in reviewers.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    item, review = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard around adapters
                    item = BlackboardItem(
                        phase="review",
                        participant=name,
                        ok=False,
                        content="",
                        error=str(exc),
                    )
                    review = empty_review(str(exc))
                items[name] = item
                reviews[name] = review

    for name in reviewers:
        item = items[name]
        state.items.append(item)
        write_review_artifact(state.run_dir, item, attempt=attempt)
        append_response_event(state, item)
        write_json(state.run_dir / "reviews" / f"attempt-{attempt}" / f"{name}.json", reviews[name])
        state.ledger.append(
            "review.submitted",
            phase="review",
            participant=name,
            payload={"attempt": attempt, "review": reviews[name]},
        )
        state.ledger.render()
    decision = decide_review(
        reviews,
        list(reviewers),
        attempt=attempt,
        min_confidence=min_confidence or state.min_confidence,
    )
    state.ledger.append("review.finalized", phase="review", payload=decision)
    state.ledger.append(
        "phase.completed",
        phase="review",
        payload={"attempt": attempt, "participants": list(reviewers)},
    )
    state.ledger.render()
    return decision


def run_participant_review(
    state: CouncilState,
    name: str,
    runner: AgentRunner,
    *,
    executor: str,
    attempt: int,
    selected_plan: str | None,
    diff_ref: str,
    result_ref: str,
) -> tuple[BlackboardItem, dict]:
    output_path = state.run_dir / "incoming" / "review" / f"attempt-{attempt}" / f"{name}.json"
    result: AgentRunResult | None = None
    base_context = {
        "Attempt": str(attempt),
        "Executor": executor,
        "Selected plan": selected_plan or "(none)",
        "Implementation result": str(state.run_dir / result_ref),
        "Implementation diff": str(state.run_dir / diff_ref),
    }
    for delivery_attempt in range(1, OUTPUT_FILE_ATTEMPTS + 1):
        remove_stale_output(output_path)
        packet = build_packet(
            state,
            phase="review",
            participant=name,
            output_path=output_path,
            extra_context={**base_context, **retry_context(delivery_attempt)},
        )
        result = runner.run(packet, cwd=state.root, dry_run=False, output_path=output_path)
        file_content = read_participant_output(output_path)
        if file_content:
            item = BlackboardItem(phase="review", participant=name, ok=True, content=file_content)
            return item, parse_review(file_content)
    item = missing_output_item("review", name=name, output_path=output_path, result=result)
    return item, empty_review(f"missing required output file: {output_path}")


def run_participant_vote(
    state: CouncilState,
    name: str,
    runner: AgentRunner,
) -> tuple[BlackboardItem, dict]:
    output_path = participant_output_path(state, phase="vote", participant=name, suffix="json")
    result: AgentRunResult | None = None
    for attempt in range(1, OUTPUT_FILE_ATTEMPTS + 1):
        remove_stale_output(output_path)
        packet = build_packet(
            state,
            phase="vote",
            participant=name,
            output_path=output_path,
            extra_context=retry_context(attempt),
        )
        result = runner.run(packet, cwd=state.root, dry_run=False, output_path=output_path)
        file_content = read_participant_output(output_path)
        if file_content:
            item = BlackboardItem(phase="vote", participant=name, ok=True, content=file_content)
            return item, parse_vote(file_content)
    item = missing_output_item("vote", name=name, output_path=output_path, result=result)
    return item, empty_vote(f"missing required output file: {output_path}")


def build_packet(
    state: CouncilState,
    *,
    phase: Phase,
    participant: str,
    output_path: Path | None = None,
    extra_context: dict[str, str] | None = None,
) -> str:
    output_rule = f"Write your final visible response to OUTPUT_FILE={output_path}" if output_path is not None else ""
    rules = (
        "You are a participant in a councli coding-CLI council.\n"
        "Do not edit project/source files or run implementation commands in this phase.\n"
        "Use prior native session context only as tool context, not as evidence, unless it appears in the packet or referenced artifacts.\n"
        "Use visible reasoning only: plans, assumptions, risks, evidence, and decisions.\n"
        "Do not expose private chain-of-thought. Keep the response compact.\n"
        f"{output_rule}\n"
    )
    if phase == "orient":
        request = (
            "Phase ORIENT. Acknowledge the task, list key assumptions, and surface at most one clarifying question. "
            "Return sections:\nUNDERSTANDING max 3 bullets; ASSUMPTIONS max 3; QUESTION optional one sentence."
        )
    elif phase == "propose":
        request = (
            "Phase PROPOSE. Give your independent plan. Return sections:\n"
            "PLAN max 5 bullets; ASSUMPTIONS max 3; RISKS max 3; EXECUTOR_FIT one sentence."
        )
    elif phase == "critique":
        request = (
            "Phase CRITIQUE. Review all proposal artifacts. Return sections:\n"
            "AGREEMENTS max 3; BLOCKING_CONCERNS max 3; NON_BLOCKING_CONCERNS max 3; QUESTION optional one targeted question."
        )
    elif phase == "revise":
        request = (
            "Phase REVISE. Based on critiques, revise or endorse a plan. Return sections:\n"
            "REVISED_PLAN max 5 bullets; ACCEPTED_FEEDBACK max 3; REMAINING_RISKS max 3; PREFERRED_EXECUTOR one name."
        )
    elif phase == "vote":
        plan_ids = candidate_plan_ids(state)
        request = (
            "Phase VOTE. Return ONLY compact JSON with keys: preferred_plan, preferred_executor, confidence, "
            "blocking_concerns, reason.\n"
            f"confidence must be 0.0 to 1.0; votes below {state.min_confidence:.2f} are recorded but do not count toward majority.\n"
            f"preferred_plan must be one of these plan ids: {', '.join(plan_ids) or '(none)'}.\n"
            f"preferred_executor must be one of these participants: {', '.join(state.participants)}."
        )
    elif phase == "review":
        request = (
            "Phase REVIEW. Review the implementation against the selected plan and task. "
            "Return ONLY compact JSON with keys: verdict, blocking_concerns, confidence, reason. "
            "verdict must be one of: approve, request_changes, replace. "
            f"confidence must be 0.0 to 1.0; reviews below {state.min_confidence:.2f} are recorded but do not count toward majority."
        )
    else:
        request = "Acknowledge the task."
    extra_context = extra_context or {}
    extra_lines = [f"- {key}: {value}" for key, value in extra_context.items()]
    shared_views = [
        f"- Blackboard: {state.run_dir / 'blackboard.md'}",
        f"- Machine state: {state.run_dir / 'state.json'}",
        f"- Event log: {state.run_dir / 'events.jsonl'}",
    ]
    brief_path = state.run_dir / "brief.md"
    if brief_path.exists():
        shared_views.append(f"- Shared task brief: {brief_path}")

    packet = "\n\n".join(
        [
            f"# councli turn packet: {state.council_id}",
            f"Participant: {participant}",
            f"Phase: {phase}",
            f"Participants: {', '.join(state.participants)}",
            "## Rules",
            rules.strip(),
            "## User Task",
            state.task,
            "## Shared Views",
            "\n".join(shared_views),
            "## Prior Artifacts",
            artifact_index(state),
            "## Extra Context",
            "\n".join(extra_lines) if extra_lines else "(none)",
            "## Request",
            request,
        ]
    )
    packet_path = state.ledger.write_packet(participant, phase, packet + "\n")
    packet_ref = packet_path.relative_to(state.run_dir).as_posix()
    prompt = (
        f"COUNCLI_ID={state.council_id}. PARTICIPANT={participant}. PHASE={phase}. "
        f"Read PACKET_FILE={packet_path} and follow it exactly. "
    )
    if output_path is not None:
        prompt += (
            f"Write the final response to OUTPUT_FILE={output_path} "
            "Use a temp file then rename if your tools make that easy. "
            "Do not edit project/source files. Print a short confirmation when done."
        )
    state.ledger.append(
        "view.sent",
        phase=phase,
        participant=participant,
        refs={"packet": packet_ref, "output": str(output_path) if output_path else None},
        payload={"packet_path": str(packet_path), "output_path": str(output_path) if output_path else None},
    )
    return prompt


def participant_output_path(state: CouncilState, *, phase: str, participant: str, suffix: str) -> Path:
    return state.run_dir / "incoming" / phase / f"{participant}.{suffix}"


def artifact_index(state: CouncilState) -> str:
    lines: list[str] = []
    for item in state.items:
        suffix = "md" if item.ok else "failed.txt"
        path = state.run_dir / item.phase / f"{item.participant}.{suffix}"
        lines.append(f"- {item.phase}/{item.participant}: {path}")
    if state.plan_ids:
        lines.append("")
        lines.append("Plan candidates:")
        for plan_id in state.plan_ids:
            lines.append(f"- {plan_id}")
    return "\n".join(lines) if lines else "(none)"


def candidate_plan_ids(state: CouncilState) -> list[str]:
    if state.plan_ids:
        return list(state.plan_ids)
    return [
        f"plan:{participant}:1"
        for participant in state.participants
        if latest_successful_item(state, participant, preferred_phases=("revise", "propose")) is not None
    ]


def register_plan_candidates(state: CouncilState) -> None:
    state.plan_ids = candidate_plan_ids(state)
    for plan_id in state.plan_ids:
        participant = plan_id.split(":", 2)[1] if ":" in plan_id else plan_id
        source = latest_successful_item(state, participant, preferred_phases=("revise", "propose"))
        if source is None:
            continue
        content_ref = state.ledger.write_blob("plans", plan_id, source.content)
        state.ledger.append(
            "plan.candidate.created",
            phase=source.phase,
            participant=participant,
            refs={"content": content_ref},
            payload={"plan_id": plan_id, "source_phase": source.phase},
        )
    state.ledger.render()


def latest_successful_item(
    state: CouncilState,
    participant: str,
    *,
    preferred_phases: tuple[str, ...],
) -> BlackboardItem | None:
    for phase in preferred_phases:
        matches = [
            item
            for item in reversed(state.items)
            if item.participant == participant and item.phase == phase and item.ok
        ]
        if matches:
            return matches[0]
    return None


def append_response_event(state: CouncilState, item: BlackboardItem) -> None:
    refs: dict[str, str] = {}
    if item.content:
        refs["content"] = state.ledger.write_blob(item.phase, item.participant, item.content)
    if item.error:
        refs["error"] = state.ledger.write_blob(f"{item.phase}-errors", item.participant, item.error, suffix="txt")
    state.ledger.append(
        "response.received",
        phase=item.phase,
        participant=item.participant,
        status="ok" if item.ok else "failed",
        refs=refs,
        payload={"ok": item.ok},
    )


def read_participant_output(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def remove_stale_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def summarize_board(state: CouncilState, *, exclude_participant: str | None = None) -> str:
    if not state.items:
        return "No prior blackboard items."
    chunks: list[str] = []
    for item in state.items:
        if exclude_participant and item.participant == exclude_participant:
            continue
        content = compact(item.content)
        if len(content) > 1200:
            content = content[:1200].rstrip() + " ..."
        status = "ok" if item.ok else "failed"
        chunks.append(f"[{item.phase}:{item.participant}:{status}] {content}")
    return " || ".join(chunks)


def item_from_result(phase: str, result: AgentRunResult) -> BlackboardItem:
    return BlackboardItem(
        phase=phase,
        participant=result.name,
        ok=result.ok,
        content=result.output.strip(),
        error=result.error.strip(),
    )


def retry_context(attempt: int) -> dict[str, str]:
    if attempt <= 1:
        return {}
    return {"Delivery retry": str(attempt)}


def missing_output_item(
    phase: str,
    *,
    name: str,
    output_path: Path,
    result: AgentRunResult | None,
) -> BlackboardItem:
    details = [f"missing required output file: {output_path}"]
    if result is not None:
        if result.error.strip():
            details.append(result.error.strip())
        if result.output.strip():
            details.append(f"stdout/stderr was not accepted as the phase artifact:\n{result.output.strip()}")
    return BlackboardItem(
        phase=phase,
        participant=name,
        ok=False,
        content="",
        error="\n\n".join(details),
    )


def write_phase_artifact(run_dir: Path, item: BlackboardItem) -> None:
    suffix = "md" if item.ok else "failed.txt"
    body = item.content if item.ok else f"{item.error}\n\n{item.content}".strip()
    write_text(run_dir / item.phase / f"{item.participant}.{suffix}", body + "\n")


def write_review_artifact(run_dir: Path, item: BlackboardItem, *, attempt: int) -> None:
    suffix = "json" if item.ok else "failed.txt"
    body = item.content if item.ok else f"{item.error}\n\n{item.content}".strip()
    write_text(run_dir / "review" / f"attempt-{attempt}" / f"{item.participant}.{suffix}", body + "\n")


def write_blackboard(state: CouncilState, *, decision: dict | None = None) -> None:
    lines = [
        f"# councli blackboard: {state.council_id}",
        "",
        "## Task",
        state.task,
        "",
        "## Participants",
        ", ".join(state.participants) or "(none)",
        "",
    ]
    for phase in ("propose", "critique", "revise", "vote"):
        lines.extend([f"## {phase.title()}", ""])
        phase_items = [item for item in state.items if item.phase == phase]
        if not phase_items:
            lines.extend(["(none)", ""])
            continue
        for item in phase_items:
            status = "ok" if item.ok else "failed"
            lines.extend([f"### {item.participant} ({status})", ""])
            lines.extend([item.content or item.error or "(empty)", ""])
    if state.votes:
        lines.extend(["## Structured Votes", ""])
        lines.append("```json")
        lines.append(json.dumps(state.votes, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    if decision is not None:
        lines.extend(["## Decision", ""])
        lines.append("```json")
        lines.append(json.dumps(decision, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    write_text(state.run_dir / "blackboard.md", "\n".join(lines).rstrip() + "\n")


def parse_vote(output: str) -> dict:
    output = output.strip()
    if not output:
        return empty_vote("empty vote output")
    try:
        return normalize_vote(json.loads(output))
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", output, flags=re.DOTALL)
    if not match:
        return empty_vote("could not find JSON vote")
    try:
        return normalize_vote(json.loads(match.group(0)))
    except json.JSONDecodeError:
        return empty_vote("invalid JSON vote")


def parse_review(output: str) -> dict:
    output = output.strip()
    if not output:
        return empty_review("empty review output")
    try:
        return normalize_review(json.loads(output))
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", output, flags=re.DOTALL)
    if not match:
        return empty_review("could not find JSON review")
    try:
        return normalize_review(json.loads(match.group(0)))
    except json.JSONDecodeError:
        return empty_review("invalid JSON review")


def normalize_vote(raw: object) -> dict:
    if not isinstance(raw, dict):
        return empty_vote("vote was not an object")
    concerns = raw.get("blocking_concerns", [])
    if not isinstance(concerns, list):
        concerns = [str(concerns)]
    confidence = normalize_confidence(raw.get("confidence", 0.0))
    return {
        "preferred_plan": raw.get("preferred_plan"),
        "preferred_executor": raw.get("preferred_executor"),
        "confidence": max(0.0, min(1.0, confidence)),
        "blocking_concerns": [str(item) for item in concerns if str(item).strip()],
        "abstained": False,
        "reason": str(raw.get("reason", "")),
    }


def normalize_review(raw: object) -> dict:
    if not isinstance(raw, dict):
        return empty_review("review was not an object")
    concerns = raw.get("blocking_concerns", [])
    if not isinstance(concerns, list):
        concerns = [str(concerns)]
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in {"approve", "request_changes", "replace"}:
        return empty_review(f"invalid review verdict: {verdict or '(empty)'}")
    confidence = normalize_confidence(raw.get("confidence", 0.0))
    return {
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, confidence)),
        "blocking_concerns": [str(item) for item in concerns if str(item).strip()],
        "abstained": False,
        "reason": str(raw.get("reason", "")),
    }


def empty_vote(reason: str) -> dict:
    return {
        "preferred_plan": None,
        "preferred_executor": None,
        "confidence": 0.0,
        "blocking_concerns": [],
        "abstained": True,
        "error": reason,
        "reason": reason,
    }


def empty_review(reason: str) -> dict:
    return {
        "verdict": None,
        "confidence": 0.0,
        "blocking_concerns": [],
        "abstained": True,
        "error": reason,
        "reason": reason,
    }


def normalize_confidence(value: object) -> float:
    if isinstance(value, str):
        mapped = {
            "low": 0.35,
            "medium": 0.6,
            "med": 0.6,
            "high": 0.85,
            "very high": 0.95,
            "certain": 1.0,
        }.get(value.strip().lower())
        if mapped is not None:
            return mapped
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def synthetic_phase_item(
    state: CouncilState,
    *,
    phase: Literal["orient", "propose", "critique", "revise"],
    participant: str,
) -> BlackboardItem:
    if phase == "orient":
        content = (
            f"UNDERSTANDING - {participant} understands the task: {state.task}. "
            "ASSUMPTIONS - dry-run only. QUESTION - none."
        )
    elif phase == "propose":
        content = (
            f"PLAN - {participant} would propose a compact blackboard-first plan for: {state.task} "
            "ASSUMPTIONS - dry-run only. RISKS - dry-run does not invoke the native CLI. "
            f"EXECUTOR_FIT - {participant} is available as a candidate."
        )
    elif phase == "critique":
        content = (
            "AGREEMENTS - use the shared blackboard as the source of truth. "
            "BLOCKING_CONCERNS - none in dry-run. "
            "NON_BLOCKING_CONCERNS - real participants may disagree or fail auth."
        )
    else:
        content = (
            "REVISED_PLAN - keep packets compact, append artifacts, then vote from visible evidence. "
            "ACCEPTED_FEEDBACK - preserve native CLI behavior and cwd. "
            f"REMAINING_RISKS - native session state may vary. PREFERRED_EXECUTOR - {state.participants[0] if state.participants else participant}."
        )
    return BlackboardItem(phase=phase, participant=participant, ok=True, content=content)


def synthetic_vote(name: str, *, preferred: str) -> dict:
    return {
        "preferred_plan": f"plan:{preferred}:1",
        "preferred_executor": preferred,
        "confidence": 1.0,
        "blocking_concerns": [],
        "reason": f"dry-run synthetic vote from {name}",
    }


def synthetic_review(name: str) -> dict:
    return {
        "verdict": "approve",
        "confidence": 1.0,
        "blocking_concerns": [],
        "abstained": False,
        "reason": f"dry-run synthetic approval from {name}",
    }


def decide_council(
    votes: dict[str, dict],
    participants: list[str],
    plan_ids: list[str] | None = None,
    *,
    min_confidence: float = 0.55,
) -> dict:
    plan_ids = plan_ids or [f"plan:{name}:1" for name in participants]
    if not participants:
        return {
            "approved": False,
            "status": "no_participants",
            "selected_plan": None,
            "selected_executor": None,
            "plan_votes": {},
            "executor_votes": {},
            "blocking_concerns": [],
            "abstentions": {},
            "low_confidence_votes": {},
            "min_confidence": min_confidence,
            "reason": "No participants are available.",
        }
    low_confidence = low_confidence_items(votes, min_confidence=min_confidence)
    countable_votes = {
        voter: vote
        for voter, vote in votes.items()
        if not vote.get("abstained") and voter not in low_confidence
    }
    normalized_votes = normalize_vote_choices(countable_votes, participants, plan_ids)
    plan_counts = count(normalized_votes, "preferred_plan", plan_ids)
    executor_counts = count(countable_votes, "preferred_executor", participants)
    selected_plan, plan_count = winner(plan_counts)
    selected_executor, executor_count = winner(executor_counts)
    blocking = [
        concern
        for vote in votes.values()
        if not vote.get("abstained")
        for concern in vote.get("blocking_concerns", [])
        if str(concern).strip()
    ]
    abstentions = {
        voter: str(vote.get("error") or vote.get("reason") or "abstained")
        for voter, vote in votes.items()
        if vote.get("abstained")
    }
    threshold = (len(participants) // 2) + 1
    approved = bool(
        selected_plan
        and selected_executor
        and plan_count >= threshold
        and executor_count >= threshold
        and not blocking
        and len(participants) >= 2
    )
    if approved:
        reason = f"Majority approved plan={selected_plan}, executor={selected_executor}."
        status = "approved"
    elif len(participants) == 1 and selected_plan and selected_executor:
        reason = (
            f"Single participant produced recommendation plan={selected_plan}, "
            f"executor={selected_executor}; not consensus."
        )
        status = "unreviewed_recommendation"
    elif blocking:
        reason = "Decision has blocking concerns; run another round or ask user."
        status = "blocked"
    elif low_confidence:
        reason = f"No majority decision with confidence >= {min_confidence:.2f}."
        status = "low_confidence"
    else:
        reason = "No majority decision; run another round or use an executor policy."
        status = "no_majority"
    return {
        "approved": approved,
        "status": status,
        "selected_plan": selected_plan,
        "selected_executor": selected_executor,
        "plan_votes": plan_counts,
        "executor_votes": executor_counts,
        "blocking_concerns": blocking,
        "abstentions": abstentions,
        "low_confidence_votes": low_confidence,
        "min_confidence": min_confidence,
        "reason": reason,
    }


def decide_review(
    reviews: dict[str, dict],
    reviewers: list[str],
    *,
    attempt: int,
    min_confidence: float = 0.55,
) -> dict:
    if not reviewers:
        return {
            "attempt": attempt,
            "verdict": "unreviewed_implementation",
            "approved": False,
            "counts": {"approve": 0, "request_changes": 0, "replace": 0},
            "active_reviewers": 0,
            "blocking_concerns": [],
            "abstentions": {},
            "low_confidence_reviews": {},
            "min_confidence": min_confidence,
            "reason": "No non-executor reviewers are available.",
        }
    counts = {"approve": 0, "request_changes": 0, "replace": 0}
    low_confidence = low_confidence_items(reviews, min_confidence=min_confidence)
    countable_reviews = {
        reviewer: review
        for reviewer, review in reviews.items()
        if not review.get("abstained") and reviewer not in low_confidence
    }
    for review in countable_reviews.values():
        verdict = review.get("verdict")
        if verdict in counts:
            counts[verdict] += 1
    blocking = [
        concern
        for review in reviews.values()
        if not review.get("abstained")
        for concern in review.get("blocking_concerns", [])
        if str(concern).strip()
    ]
    abstentions = {
        reviewer: str(review.get("error") or review.get("reason") or "abstained")
        for reviewer, review in reviews.items()
        if review.get("abstained")
    }
    active_reviewers = len(reviewers) - len(abstentions)
    countable_reviewers = active_reviewers - len(low_confidence)
    threshold = (active_reviewers // 2) + 1 if active_reviewers else 1
    if counts["approve"] >= threshold and not blocking:
        verdict = "accepted"
        approved = True
        reason = "Review majority approved the implementation."
    elif counts["replace"] >= threshold:
        verdict = "replace"
        approved = False
        reason = "Review majority requested executor replacement."
    elif counts["request_changes"] > 0 or blocking:
        verdict = "revise"
        approved = False
        reason = "Review requested changes before acceptance."
    elif low_confidence and countable_reviewers == 0:
        verdict = "needs_user"
        approved = False
        reason = f"No review reached confidence >= {min_confidence:.2f}."
    else:
        verdict = "needs_user"
        approved = False
        reason = "Review did not reach a majority decision."
    return {
        "attempt": attempt,
        "verdict": verdict,
        "approved": approved,
        "counts": counts,
        "active_reviewers": active_reviewers,
        "countable_reviewers": countable_reviewers,
        "blocking_concerns": blocking,
        "abstentions": abstentions,
        "low_confidence_reviews": low_confidence,
        "min_confidence": min_confidence,
        "reason": reason,
    }


def next_executor(
    executor_votes: dict[str, int],
    *,
    exclude: set[str],
    participants: list[str] | None = None,
) -> str | None:
    candidates = {
        name: count
        for name, count in executor_votes.items()
        if name not in exclude
    }
    selected = winner(candidates)[0]
    if selected is not None:
        return selected
    for name in participants or list(executor_votes):
        if name not in exclude:
            return name
    return None


def low_confidence_items(items: dict[str, dict], *, min_confidence: float) -> dict[str, float]:
    return {
        name: normalize_confidence(item.get("confidence", 0.0))
        for name, item in items.items()
        if not item.get("abstained") and normalize_confidence(item.get("confidence", 0.0)) < min_confidence
    }


def normalize_vote_choices(votes: dict[str, dict], participants: list[str], plan_ids: list[str]) -> dict[str, dict]:
    normalized: dict[str, dict] = {}
    participant_to_plan = {name: f"plan:{name}:1" for name in participants}
    valid_plans = set(plan_ids)
    for voter, vote in votes.items():
        copied = dict(vote)
        choice = copied.get("preferred_plan")
        if choice in participant_to_plan and participant_to_plan[choice] in valid_plans:
            copied["preferred_plan"] = participant_to_plan[choice]
        normalized[voter] = copied
    return normalized


def count(votes: dict[str, dict], key: str, participants: list[str]) -> dict[str, int]:
    counts = {name: 0 for name in participants}
    for vote in votes.values():
        choice = vote.get(key)
        if choice in counts:
            counts[choice] += 1
    return counts


def winner(counts: dict[str, int]) -> tuple[str | None, int]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] == 0:
        return None, 0
    return ranked[0]


def compact(text: str) -> str:
    return " ".join(text.split())


def create_visible_tmux_room(
    *,
    room: str,
    runners: dict[str, AgentRunner],
    root: Path,
    participants: list[str],
    session_prefix: str = "councli",
    socket_name: str | None = None,
    detach_key: str = "C-]",
) -> None:
    if tmux_session_exists(room, socket_name=socket_name):
        return
    for name in participants:
        runner = runners[name]
        ensure_tmux_session(
            runner.session_name_for(root, prefix=session_prefix),
            runner.config.start_command or runner.config.command,
            root,
            detach_key=detach_key,
            socket_name=socket_name,
        )

    first = runners[participants[0]].session_name_for(root, prefix=session_prefix)
    proc = tmux(["new-session", "-d", "-s", room, "-c", str(root), "bash"], cwd=root, socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not create room {room}")
    tmux(["rename-window", "-t", f"{room}:0", "council"], cwd=root, socket_name=socket_name)
    tmux(["send-keys", "-t", f"{room}:0.0", f"env -u TMUX {tmux_attach_command(first, socket_name=socket_name)}", "Enter"], cwd=root, socket_name=socket_name)
    for index, name in enumerate(participants):
        if index == 0:
            continue
        runner = runners[name]
        split = "-h" if index % 2 else "-v"
        attach_command = f"env -u TMUX {tmux_attach_command(runner.session_name_for(root, prefix=session_prefix), socket_name=socket_name)}"
        tmux(["split-window", split, "-t", f"{room}:0", "-c", str(root), attach_command], cwd=root, socket_name=socket_name)
    tmux(["select-layout", "-t", f"{room}:0", "tiled"], cwd=root, socket_name=socket_name)
    time.sleep(1.0)


def attach_tmux_room(room: str, *, socket_name: str | None = None) -> None:
    rc = attach_tmux_session(room, socket_name=socket_name)
    if rc != 0:
        raise RuntimeError(f"tmux attach exited with code {rc}")

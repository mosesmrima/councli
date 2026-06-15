from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from councli.artifacts import to_jsonable, write_json, write_text


_LOCK = threading.Lock()


@dataclass(frozen=True)
class EventRef:
    seq: int
    event_id: str


class EventLedger:
    """Append-only run ledger plus generated human/machine projections."""

    def __init__(self, run_dir: Path, *, run_id: str | None = None) -> None:
        self.run_dir = run_dir
        self.run_id = run_id or run_dir.name
        self.events_path = run_dir / "events.jsonl"
        self.lock_path = run_dir / "run.lock"
        self.blobs_dir = run_dir / "blobs"
        self.packets_dir = run_dir / "packets"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.packets_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append(
        self,
        event_type: str,
        *,
        phase: str | None = None,
        participant: str | None = None,
        status: str = "ok",
        payload: dict[str, Any] | None = None,
        refs: dict[str, Any] | None = None,
        parent_event_ids: list[str] | None = None,
    ) -> EventRef:
        with _LOCK, self._exclusive_lock():
            seq = self._next_seq()
            event_id = f"evt_{seq:06d}"
            event = {
                "seq": seq,
                "event_id": event_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": event_type,
                "run_id": self.run_id,
                "phase": phase,
                "participant": participant,
                "status": status,
                "parent_event_ids": parent_event_ids or [],
                "refs": refs or {},
                "payload": payload or {},
            }
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(to_jsonable(event), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return EventRef(seq=seq, event_id=event_id)

    def write_blob(self, kind: str, name: str, content: str, *, suffix: str = "md") -> str:
        with _LOCK, self._exclusive_lock():
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            safe_name = safe_slug(name)[:80] or "blob"
            path = self.blobs_dir / kind / f"{digest}-{safe_name}.{suffix}"
            write_text(path, content)
            return path.relative_to(self.run_dir).as_posix()

    def write_packet(self, participant: str, phase: str, content: str) -> Path:
        with _LOCK, self._exclusive_lock():
            seq = sum(1 for _ in self.packets_dir.rglob("*.md"))
            path = self.packets_dir / participant / f"{seq:06d}-{safe_slug(phase)}.md"
            write_text(path, content)
            return path

    def render(self) -> None:
        with _LOCK, self._exclusive_lock():
            events = _read_events_unlocked(self.run_dir)
            state = project_state(self.run_dir, events)
            write_json(self.run_dir / "state.json", state)
            write_text(self.run_dir / "blackboard.md", render_blackboard(state))

    def _next_seq(self) -> int:
        if not self.events_path.exists():
            return 0
        with self.events_path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    with _LOCK:
        return _read_events_unlocked(run_dir)


def _read_events_unlocked(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def project_state(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "run_id": run_dir.name,
        "task": "",
        "participants": {},
        "phases": {},
        "plans": {},
        "votes": {},
        "decision": None,
        "implementation": {"attempts": []},
        "reviews": {},
        "review_decision": None,
        "run_completed": None,
        "run_canceled": None,
        "events": len(events),
    }
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        refs = event.get("refs") or {}
        participant = event.get("participant")
        phase = event.get("phase")
        status = event.get("status") or "ok"
        if event_type == "run.started":
            state["run_id"] = event.get("run_id") or state["run_id"]
            state["task"] = str(payload.get("task", ""))
        elif event_type == "participant.joined" and participant:
            state["participants"][participant] = payload
        elif event_type == "response.received" and participant and phase:
            content = read_ref(run_dir, refs.get("content"))
            error = read_ref(run_dir, refs.get("error"))
            sidecar = read_json_ref(run_dir, refs.get("sidecar"))
            state["phases"].setdefault(phase, {})[participant] = {
                "status": status,
                "content": content,
                "error": error,
                "sidecar": sidecar,
                "event_id": event.get("event_id"),
                "refs": refs,
            }
        elif event_type == "plan.candidate.created":
            plan_id = str(payload.get("plan_id", ""))
            if plan_id:
                state["plans"][plan_id] = {
                    "participant": participant,
                    "source_phase": phase,
                    "event_id": event.get("event_id"),
                    "content": read_ref(run_dir, refs.get("content")),
                }
        elif event_type == "ballot.submitted" and participant:
            state["votes"][participant] = payload.get("vote", payload)
        elif event_type == "decision.finalized":
            state["decision"] = payload
        elif event_type == "implementation.started":
            attempt = int(payload.get("attempt", len(state["implementation"]["attempts"]) + 1))
            record = {
                "attempt": attempt,
                "status": "started",
                "executor": payload.get("executor") or participant,
                "selected_plan": payload.get("selected_plan"),
                "worktree": payload.get("worktree"),
                "branch": payload.get("branch"),
                "base_ref": payload.get("base_ref"),
            }
            state["implementation"]["attempts"].append(record)
            state["implementation"].update(record)
        elif event_type == "implementation.diff_submitted":
            attempt = int(payload.get("attempt", len(state["implementation"]["attempts"]) or 1))
            record = find_attempt(state["implementation"], attempt)
            if record is None:
                record = {"attempt": attempt}
                state["implementation"]["attempts"].append(record)
            record.update(
                {
                    "status": status,
                    "executor": payload.get("executor") or participant,
                    "selected_plan": payload.get("selected_plan"),
                    "worktree": payload.get("worktree"),
                    "branch": payload.get("branch"),
                    "base_ref": payload.get("base_ref"),
                    "ok": payload.get("ok"),
                    "diff_ref": refs.get("diff"),
                    "result_ref": refs.get("result"),
                }
            )
            state["implementation"].update(record)
        elif event_type == "implementation.applied":
            state["implementation"]["applied"] = payload
        elif event_type == "review.submitted" and participant:
            attempt = str(payload.get("attempt", "1"))
            state["reviews"].setdefault(attempt, {})[participant] = payload.get("review", payload)
        elif event_type == "review.finalized":
            state["review_decision"] = payload
        elif event_type == "run.completed":
            state["run_completed"] = payload
        elif event_type == "turn.canceled":
            state["run_canceled"] = payload
    return state


def render_blackboard(state: dict[str, Any]) -> str:
    lines = [
        f"# councli blackboard: {state.get('run_id')}",
        "",
        "## Task",
        str(state.get("task") or "(none)"),
        "",
        "## Participants",
    ]
    participants = state.get("participants") or {}
    if participants:
        for name, info in participants.items():
            reason = info.get("reason") if isinstance(info, dict) else ""
            lines.append(f"- {name}: {reason or 'joined'}")
    else:
        lines.append("(none)")
    lines.append("")

    phases = state.get("phases") or {}
    for phase in sorted(phases, key=phase_sort_key):
        lines.extend([f"## {format_phase_title(phase)}", ""])
        phase_items = phases.get(phase) or {}
        for participant, item in phase_items.items():
            status = item.get("status") or "unknown"
            lines.extend([f"### {participant} ({status})", ""])
            body = item.get("content") or item.get("error") or "(empty)"
            lines.extend([str(body).rstrip(), ""])

    plans = state.get("plans") or {}
    if plans:
        lines.extend(["## Plan Candidates", ""])
        for plan_id, plan in plans.items():
            lines.append(f"- `{plan_id}` from {plan.get('participant')}")
        lines.append("")

    votes = state.get("votes") or {}
    if votes:
        lines.extend(["## Structured Votes", "", "```json"])
        lines.append(json.dumps(votes, indent=2, sort_keys=True))
        lines.extend(["```", ""])

    implementation = state.get("implementation") or {}
    attempts = implementation.get("attempts") or []
    if attempts:
        lines.extend(["## Implementation", ""])
        for attempt in attempts:
            lines.append(
                "- Attempt {attempt}: executor={executor}, status={status}, branch={branch}, diff={diff}".format(
                    attempt=attempt.get("attempt"),
                    executor=attempt.get("executor"),
                    status=attempt.get("status"),
                    branch=attempt.get("branch") or "(none)",
                    diff=attempt.get("diff_ref") or "(none)",
                )
            )
        applied = implementation.get("applied")
        if applied:
            lines.append(
                "- Applied to {root} from base {base}".format(
                    root=applied.get("root"),
                    base=applied.get("base_ref"),
                )
            )
        lines.append("")

    reviews = state.get("reviews") or {}
    if reviews:
        lines.extend(["## Structured Reviews", "", "```json"])
        lines.append(json.dumps(reviews, indent=2, sort_keys=True))
        lines.extend(["```", ""])

    review_decision = state.get("review_decision")
    if review_decision:
        lines.extend(["## Review Decision", "", "```json"])
        lines.append(json.dumps(review_decision, indent=2, sort_keys=True))
        lines.extend(["```", ""])

    decision = state.get("decision")
    if decision:
        lines.extend(["## Decision", "", "```json"])
        lines.append(json.dumps(decision, indent=2, sort_keys=True))
        lines.extend(["```", ""])
    run_completed = state.get("run_completed")
    if run_completed:
        lines.extend(["## Run Completion", "", "```json"])
        lines.append(json.dumps(run_completed, indent=2, sort_keys=True))
        lines.extend(["```", ""])
    run_canceled = state.get("run_canceled")
    if run_canceled:
        lines.extend(["## Run Canceled", "", "```json"])
        lines.append(json.dumps(run_canceled, indent=2, sort_keys=True))
        lines.extend(["```", ""])
    return "\n".join(lines).rstrip() + "\n"


def find_attempt(implementation: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    for item in implementation.get("attempts", []):
        if item.get("attempt") == attempt:
            return item
    return None


def read_ref(run_dir: Path, ref: Any) -> str:
    if not ref:
        return ""
    path = run_dir / str(ref)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_json_ref(run_dir: Path, ref: Any) -> dict[str, Any] | None:
    if not ref:
        return None
    path = run_dir / str(ref)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def phase_sort_key(phase: str) -> tuple[int, str]:
    match = re.match(r"^([a-z_]+)\.round([0-9]+)$", phase)
    if match:
        return (int(match.group(2)), match.group(1))
    if phase.startswith("synthesis"):
        return (10_000, phase)
    return (5_000, phase)


def format_phase_title(phase: str) -> str:
    match = re.match(r"^([a-z_]+)\.round([0-9]+)$", phase)
    if match:
        return f"{match.group(1).replace('_', ' ').title()} Round {match.group(2)}"
    return phase.replace("_", " ").replace(".", " ").title()


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return slug.strip("-") or "item"

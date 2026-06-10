from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from councli.artifacts import to_jsonable, write_json, write_text


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def councli_dir(root: Path) -> Path:
    path = root / ".councli"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_ledger_path(root: Path) -> Path:
    path = councli_dir(root) / "ledger" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def project_ledger_lock_path(root: Path) -> Path:
    path = councli_dir(root) / "ledger" / "events.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def project_ledger_lock(root: Path, *, exclusive: bool):
    lock_path = project_ledger_lock_path(root)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_project_event(
    root: Path,
    event_type: str,
    *,
    participant: str | None = None,
    payload: dict[str, Any] | None = None,
    refs: dict[str, Any] | None = None,
    status: str = "ok",
) -> None:
    path = project_ledger_path(root)
    event = {
        "ts": iso_timestamp(),
        "type": event_type,
        "participant": participant,
        "status": status,
        "payload": payload or {},
        "refs": refs or {},
    }
    with project_ledger_lock(root, exclusive=True):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(event), sort_keys=True) + "\n")


def read_project_events(root: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    path = project_ledger_path(root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with project_ledger_lock(root, exclusive=False):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events[-limit:] if limit is not None else events


def session_registry_path(root: Path) -> Path:
    return councli_dir(root) / "sessions" / "registry.json"


def session_registry_lock_path(root: Path) -> Path:
    path = councli_dir(root) / "sessions" / "registry.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def session_registry_lock(root: Path, *, exclusive: bool):
    lock_path = session_registry_lock_path(root)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_session_registry_unlocked(root: Path) -> dict[str, Any]:
    path = session_registry_path(root)
    if not path.exists():
        return {"sessions": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    if not isinstance(loaded, dict):
        return {"sessions": {}}
    sessions = loaded.get("sessions")
    if not isinstance(sessions, dict):
        loaded["sessions"] = {}
    return loaded


def read_session_registry(root: Path) -> dict[str, Any]:
    with session_registry_lock(root, exclusive=False):
        return _read_session_registry_unlocked(root)


def _write_session_registry_unlocked(root: Path, registry: dict[str, Any]) -> None:
    write_json(session_registry_path(root), registry)


def write_session_registry(root: Path, registry: dict[str, Any]) -> None:
    with session_registry_lock(root, exclusive=True):
        _write_session_registry_unlocked(root, registry)


def reconcile_session_registry(root: Path, live_sessions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with session_registry_lock(root, exclusive=True):
        registry = _read_session_registry_unlocked(root)
        sessions = registry.setdefault("sessions", {})
        for key, record in list(sessions.items()):
            if not isinstance(record, dict):
                continue
            session_name = str(record.get("session_name") or "")
            live = live_sessions.get(session_name)
            if live is None:
                record["status"] = "stale"
                record["stale_reason"] = "tmux session is not live"
                record["process_status"] = "missing"
                record["updated_at"] = iso_timestamp()
                continue
            record["status"] = "active"
            record["stale_reason"] = None
            record["pane_current_path"] = live.get("pane_current_path")
            record["pane_id"] = live.get("pane_id")
            record["pane_pid"] = live.get("pane_pid")
            record["pane_current_command"] = live.get("pane_current_command")
            record["pane_dead"] = live.get("pane_dead")
            record["process_status"] = session_process_status(record)
            if record["process_status"] in {"dead", "shell"}:
                record["status"] = "stale"
                record["stale_reason"] = f"assistant process appears {record['process_status']}"
            expected_cwd = Path(str(record.get("cwd") or ""))
            live_cwd = Path(str(live.get("pane_current_path") or ""))
            if expected_cwd and live_cwd and expected_cwd != live_cwd:
                try:
                    mismatch = expected_cwd.resolve() != live_cwd.resolve()
                except OSError:
                    mismatch = True
                if mismatch:
                    record["status"] = "stale"
                    record["stale_reason"] = f"cwd mismatch: live={live_cwd}"
            record["updated_at"] = iso_timestamp()
        _write_session_registry_unlocked(root, registry)
        return registry


def upsert_session(
    root: Path,
    *,
    agent: str,
    session_name: str,
    backend: str,
    cwd: Path,
    command: list[str],
    raw_capture: Path | None = None,
    native_session_id: str | None = None,
    status: str = "active",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with session_registry_lock(root, exclusive=True):
        registry = _read_session_registry_unlocked(root)
        sessions = registry.setdefault("sessions", {})
        previous = sessions.get(agent) if isinstance(sessions.get(agent), dict) else {}
        record = {
            **previous,
            "agent": agent,
            "session_name": session_name,
            "backend": backend,
            "cwd": str(cwd),
            "command": command,
            "raw_capture": str(raw_capture) if raw_capture else previous.get("raw_capture"),
            "native_session_id": native_session_id if native_session_id is not None else previous.get("native_session_id"),
            "status": status,
            "updated_at": iso_timestamp(),
        }
        if "created_at" not in record:
            record["created_at"] = record["updated_at"]
        if extra:
            record.update(extra)
        record["process_status"] = session_process_status(record)
        sessions[agent] = record
        _write_session_registry_unlocked(root, registry)
        return record


def raw_capture_path(root: Path, agent: str) -> Path:
    return councli_dir(root) / "session-recordings" / f"{agent}.raw.log"


def snapshot_path(root: Path, agent: str) -> Path:
    return councli_dir(root) / "session-snapshots" / f"{utc_timestamp()}-{agent}.txt"


def ensure_councli_gitignore(root: Path) -> Path | None:
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if ".councli/" in existing or ".councli" in existing:
        return path
    lines = [*existing]
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(".councli/")
    write_text(path, "\n".join(lines).rstrip() + "\n")
    return path


def latest_task_brief(root: Path) -> Path | None:
    tasks_dir = councli_dir(root) / "tasks"
    if not tasks_dir.exists():
        return None
    candidates = [path / "brief.md" for path in tasks_dir.iterdir() if (path / "brief.md").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


@dataclass(frozen=True)
class TaskBrief:
    task_id: str
    path: Path


def write_task_brief(root: Path, *, task: str, task_id: str, run_dir: Path | None = None) -> TaskBrief:
    brief_dir = councli_dir(root) / "tasks" / task_id
    path = brief_dir / "brief.md"
    registry = read_session_registry(root)
    recent_events = read_project_events(root, limit=30)
    lines = [
        f"# councli task brief: {task_id}",
        "",
        "## User Task",
        task.strip() or "(none)",
        "",
        "## Native Assistant Sessions",
    ]
    sessions = registry.get("sessions") or {}
    if sessions:
        for agent, record in sorted(sessions.items()):
            if not isinstance(record, dict):
                continue
            lines.append(
                "- {agent}: session={session}, status={status}, cwd={cwd}, raw={raw}".format(
                    agent=agent,
                    session=record.get("session_name") or "-",
                    status=record.get("status") or "-",
                    cwd=record.get("cwd") or "-",
                    raw=record.get("raw_capture") or "-",
                )
            )
    else:
        lines.append("(none)")
    lines.extend(["", "## Recent Councli Events"])
    if recent_events:
        for event in recent_events:
            participant = event.get("participant") or "-"
            event_type = event.get("type") or "-"
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            summary = payload.get("summary") or payload.get("task") or payload.get("message") or ""
            if summary:
                summary = " - " + " ".join(str(summary).split())[:200]
            lines.append(f"- {event.get('ts')} {event_type} [{participant}]{summary}")
    else:
        lines.append("(none)")
    lines.extend(
        [
            "",
            "## Notes",
            "- Raw terminal recordings are audit/debug artifacts, not authoritative semantic transcripts.",
            "- Native assistant slash commands, permission prompts, MCP config, and session behavior remain owned by each assistant.",
            "- Use this brief as shared context only; inspect referenced artifacts when exact details matter.",
        ]
    )
    write_text(path, "\n".join(lines).rstrip() + "\n")
    if run_dir is not None:
        write_text(run_dir / "brief.md", "\n".join(lines).rstrip() + "\n")
    return TaskBrief(task_id=task_id, path=path)


def find_native_session_candidates(agent: str, root: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    home = Path.home()
    agent = agent.lower()
    patterns: list[Path] = []
    if agent == "claude":
        patterns.extend((home / ".claude" / "projects").glob("**/*.jsonl"))
    elif agent == "codex":
        patterns.extend((home / ".codex" / "sessions").glob("**/*.jsonl"))
        patterns.extend((home / ".codex" / "sessions").glob("**/*.json"))
    elif agent == "kimi":
        patterns.extend((home / ".kimi-code" / "sessions").glob("**/state.json"))
    elif agent == "codewhale":
        for base in (home / ".codewhale", home / ".local" / "share" / "codewhale"):
            if base.exists():
                patterns.extend(base.glob("**/*.jsonl"))
                patterns.extend(base.glob("**/*.json"))
    elif agent == "agy":
        for base in (home / ".config" / "antigravity", home / ".antigravity"):
            if base.exists():
                patterns.extend(base.glob("**/*.jsonl"))
                patterns.extend(base.glob("**/*.json"))

    candidates: list[dict[str, Any]] = []
    root_text = str(root.resolve())
    for path in patterns:
        try:
            stat = path.stat()
        except OSError:
            continue
        session_id = infer_session_id(path)
        score = 0
        try:
            sample = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            if root_text in sample:
                score += 10
        except OSError:
            sample = ""
        if root.name and root.name in str(path):
            score += 2
        confidence = candidate_confidence(score)
        reasons: list[str] = []
        if score >= 10:
            reasons.append("session file mentions this project root")
        if score >= 2 and root.name and root.name in str(path):
            reasons.append("session path includes project directory name")
        if not reasons:
            reasons.append("recent native session file only")
        candidates.append(
            {
                "session_id": session_id,
                "path": str(path),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "score": score,
                "confidence": confidence,
                "reason": "; ".join(reasons),
            }
        )
    candidates.sort(key=lambda item: (item["score"], item["mtime"]), reverse=True)
    return candidates[:limit]


def infer_session_id(path: Path) -> str:
    stem = path.stem
    if stem == "state":
        return path.parent.name
    match = re.search(r"[0-9a-fA-F-]{16,}", stem)
    if match:
        return match.group(0)
    return stem or path.parent.name


def candidate_confidence(score: int) -> str:
    if score >= 10:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def session_process_status(record: dict[str, Any]) -> str:
    if str(record.get("pane_dead") or "0") == "1":
        return "dead"
    current = Path(str(record.get("pane_current_command") or "")).name.lower()
    if not current:
        return "unknown"
    command = record.get("command")
    expected = ""
    if isinstance(command, list) and command:
        expected = Path(str(command[0])).name.lower()
    shells = {"bash", "sh", "zsh", "fish", "dash"}
    if current in shells and expected and expected not in shells:
        return "shell"
    return "running"

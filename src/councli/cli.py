from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import shutil
import shlex
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from councli.agents import (
    AgentRunner,
    attach_tmux_session,
    build_runners,
    capture_tmux,
    compact_terminal_prompt,
    ensure_tmux_session,
    kill_tmux_session,
    project_hash,
    send_tmux_text,
    start_tmux_raw_capture,
    tmux_attach_command,
    tmux_current_path,
    tmux,
    tmux_session_info,
    tmux_session_names,
    tmux_session_exists,
)
from councli.artifacts import new_run_dir, read_json, write_json, write_text
from councli.config import (
    ARTIFACT_CLASSES,
    ConfigTrustError,
    ProjectIdentityError,
    load_config as load_config_model,
    project_config_path,
    project_trust_path,
    trust_project_config,
    write_default_config,
)
from councli.council import (
    attach_tmux_room,
    create_visible_tmux_room,
    next_executor,
    run_blackboard_council,
    run_review_phase,
)
from councli.events import EventLedger
from councli.gitops import apply_unified_diff, create_worktree, current_commit, diff, ensure_clean_enough, require_git_repo
from councli.native import (
    append_project_event,
    raw_capture_path,
    read_project_events,
    read_session_registry,
    reconcile_session_registry,
    snapshot_path,
    upsert_session,
    write_task_brief,
    ensure_councli_gitignore,
    find_native_session_candidates,
    infer_session_id,
    latest_task_brief,
)
from councli.protocol import render_result, run_executor


app = typer.Typer(
    name="councli",
    help="A local council control plane for multiple coding CLI agents.",
    no_args_is_help=False,
    invoke_without_command=True,
)
sessions_app = typer.Typer(help="Manage native tmux-backed agent sessions.")
artifacts_app = typer.Typer(help="Inspect, redact, and prune local councli artifacts.")
app.add_typer(sessions_app, name="sessions")
app.add_typer(artifacts_app, name="artifacts")
console = Console(width=140)
NATIVE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
EXPERIMENTAL_ENV = "COUNCLI_EXPERIMENTAL"
ARTIFACT_ROOTS = {
    "raw-log": ("session-recordings",),
    "session-archive": ("session-archives",),
    "session-snapshot": ("session-snapshots",),
    "run": ("runs",),
    "task": ("tasks",),
    "project-ledger": ("ledger",),
}
COUNCLI_MASCOT = r"""
     .----------------.
   .'  codex  claude  '.
  /  agy   [ board ]   \
  \   kimi  codewhale  /
   '.      councli    .'
     '----------------'
""".strip("\n")


@dataclass(frozen=True)
class ArtifactCandidate:
    kind: str
    path: Path
    bytes: int
    modified_at: datetime
    is_dir: bool = False


def print_mascot() -> None:
    console.print(COUNCLI_MASCOT, style="cyan")


def load_config(root: Path, *, auto_init: bool = False, quiet: bool = False):
    try:
        return load_config_model(root)
    except FileNotFoundError as exc:
        if auto_init:
            path, _ = write_default_config(root, overwrite=False)
            ensure_councli_gitignore(root)
            if not quiet:
                console.print(f"[green]Created default councli config:[/] {path}")
                console.print(f"[green]Trusted generated agent control config:[/] {project_trust_path(root)}")
            return load_config_model(root)
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    except (ConfigTrustError, ProjectIdentityError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


def require_experimental(feature: str) -> None:
    if os.environ.get(EXPERIMENTAL_ENV) == "1":
        return
    console.print(f"[red]{feature} is experimental and not part of the MVP surface.[/]")
    console.print(f"Set {EXPERIMENTAL_ENV}=1 only if you intentionally want this hidden prototype.")
    raise typer.Exit(code=2)


RootOpt = Annotated[
    Path,
    typer.Option(
        "--root",
        "-C",
        help="Project root containing .councli/config.yaml.",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
]


@app.callback()
def main(ctx: typer.Context, root: RootOpt = Path.cwd()) -> None:
    """Open the interactive shell when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        chat(root=root)


@app.command()
def init(
    root: RootOpt = Path.cwd(),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing config.")] = False,
    disable_missing: Annotated[
        bool,
        typer.Option("--disable-missing", help="Set enabled: false for assistant binaries not found on PATH."),
    ] = False,
) -> None:
    """Create a project-local councli config."""
    initialize_project(root=root, force=force, disable_missing=disable_missing)


@app.command()
def setup(
    root: RootOpt = Path.cwd(),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing config.")] = False,
    disable_missing: Annotated[
        bool,
        typer.Option("--disable-missing", help="Set enabled: false for assistant binaries not found on PATH."),
    ] = False,
) -> None:
    """Create config, trust generated commands, and show detected assistant CLIs."""
    initialize_project(root=root, force=force, disable_missing=disable_missing)


def initialize_project(*, root: Path, force: bool, disable_missing: bool) -> None:
    path, wrote = write_default_config(root, overwrite=force, disable_missing=disable_missing)
    if wrote:
        console.print(f"[green]Wrote config:[/] {path}")
        console.print(f"[green]Trusted generated agent control config:[/] {project_trust_path(root)}")
    else:
        console.print(f"[green]Config already exists:[/] {path}")
        if disable_missing:
            console.print("[yellow]Existing config was not changed. Use --force with --disable-missing to rewrite it.[/]")
    gitignore = ensure_councli_gitignore(root)
    if gitignore is not None:
        console.print(f"[green]Protected local artifacts:[/] {gitignore}")
    try:
        config = load_config_model(root)
    except (ConfigTrustError, ProjectIdentityError, FileNotFoundError, ValueError) as exc:
        console.print(f"[yellow]Could not run discovery until config is trusted:[/] {exc}")
        return
    print_available_participants(build_runners(config.agents), title="Detected assistant CLIs")


@app.command()
def trust(
    root: RootOpt = Path.cwd(),
    repair_identity: Annotated[
        bool,
        typer.Option("--repair-identity", help="Accept the current path after an intentional project move/rename."),
    ] = False,
) -> None:
    """Trust assistant command and transport fields in the project-local councli config."""
    try:
        path, digest = trust_project_config(root, reason="manual", repair_identity=repair_identity)
    except (ProjectIdentityError, FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    gitignore = ensure_councli_gitignore(root)
    console.print(f"[green]Trusted config agent control fields:[/] {path}")
    console.print(f"Hash: {digest}")
    if gitignore is not None:
        console.print(f"[green]Protected local artifacts:[/] {gitignore}")


@app.command()
def doctor(
    root: RootOpt = Path.cwd(),
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable readiness JSON.")] = False,
) -> None:
    """Check configured agent availability."""
    config = load_config(root, auto_init=True, quiet=json_output)
    runners = build_runners(config.agents)
    records: list[dict[str, Any]] = []

    table = Table(title="councli doctor")
    table.add_column("Agent")
    table.add_column("Enabled")
    table.add_column("Binary")
    table.add_column("Backend")
    table.add_column("Version")
    table.add_column("Path")
    table.add_column("Status")

    for name, runner in runners.items():
        health = runner.health()
        intents = doctor_intent_readiness(runner, health)
        record = {
            "agent": name,
            "display_name": runner.config.display_name or name,
            "enabled": health.enabled,
            "binary": health.binary,
            "backend": health.backend,
            "capabilities": runner.config.capabilities,
            "version": health.version,
            "version_status": health.version_status,
            "readiness_status": health.readiness_status,
            "readiness_detail": health.readiness_detail,
            "path": health.path,
            "available": health.available,
            "reason": health.reason,
            "intents": intents,
        }
        records.append(record)
        style = "green" if health.available else "yellow"
        table.add_row(
            name,
            str(health.enabled),
            health.binary,
            health.backend,
            health.version or f"({health.version_status})",
            health.path or "-",
            f"[{style}]{health.reason}[/]",
        )

    if json_output:
        console.print_json(data={"config": str(project_config_path(root)), "agents": records})
        return
    console.print(table)
    console.print(f"Config: {project_config_path(root)}")


def doctor_intent_readiness(runner: AgentRunner, health: Any) -> dict[str, dict[str, Any]]:
    shared_command = runner.config.broadcast_command or runner.config.command
    shared_supported = any("{prompt}" in part for part in shared_command)
    broadcast_supported = bool(runner.config.broadcast_enabled and shared_supported)
    assistant_supported = bool(runner.config.start_command or runner.config.backend == "tmux")
    return {
        "chat": intent_readiness(health, supported=shared_supported and adapter_supports_intent(runner, "chat")),
        "deliberate": intent_readiness(health, supported=shared_supported and adapter_supports_intent(runner, "deliberate")),
        "vote": intent_readiness(health, supported=shared_supported and adapter_supports_intent(runner, "vote")),
        "broadcast": intent_readiness(health, supported=broadcast_supported and adapter_supports_intent(runner, "broadcast")),
        "assistant": intent_readiness(health, supported=assistant_supported and adapter_supports_intent(runner, "assistant")),
    }


def adapter_supports_intent(runner: AgentRunner, intent: str) -> bool:
    return not runner.config.capabilities or intent in runner.config.capabilities


def intent_readiness(health: Any, *, supported: bool) -> dict[str, Any]:
    if not getattr(health, "enabled", False):
        return {"ready": False, "status": "disabled", "reason": getattr(health, "reason", "disabled")}
    if not supported:
        return {"ready": False, "status": "unsupported_intent", "reason": "no command for this intent"}
    if not getattr(health, "available", False):
        reason = str(getattr(health, "reason", "") or "")
        readiness_status = str(getattr(health, "readiness_status", "") or "")
        if readiness_status and readiness_status not in {"ok", "not_configured", "not_checked"}:
            return {"ready": False, "status": readiness_status, "reason": reason}
        return {"ready": False, "status": normalize_health_status(reason), "reason": reason}
    return {"ready": True, "status": "ready", "reason": getattr(health, "reason", "available")}


def normalize_health_status(reason: str) -> str:
    lowered = reason.lower()
    if "binary not found" in lowered:
        return "missing_binary"
    if "tmux" in lowered:
        return "tmux_unavailable"
    if "disabled" in lowered:
        return "disabled"
    if "auth" in lowered or "login" in lowered:
        return "auth_required"
    if "quota" in lowered or "rate limit" in lowered or "billing" in lowered or "subscription" in lowered:
        return "quota_unavailable"
    if "model" in lowered or "provider" in lowered:
        return "model_unconfigured"
    if "readiness probe" in lowered:
        return "readiness_failed"
    return "unavailable"


@sessions_app.command("list")
def sessions_list(root: RootOpt = Path.cwd()) -> None:
    """List configured tmux sessions and whether they exist."""
    config = load_config(root)
    runners = build_runners(config.agents)
    registry = reconcile_native_sessions(root=root, runners=runners)
    records = registry.get("sessions") if isinstance(registry.get("sessions"), dict) else {}
    table = Table(title="councli tmux sessions")
    table.add_column("Agent")
    table.add_column("Backend")
    table.add_column("Session")
    table.add_column("Exists")
    table.add_column("Process")
    table.add_column("Pane Cmd")
    table.add_column("Capture")
    for name, runner in runners.items():
        session_name = runner.session_name_for(root, prefix=config.native.session_prefix)
        record = records.get(name) if isinstance(records, dict) else None
        capture_path = record.get("raw_capture") if isinstance(record, dict) else ""
        process = record.get("process_status") if isinstance(record, dict) else "-"
        pane_command = record.get("pane_current_command") if isinstance(record, dict) else "-"
        stale_reason = record.get("stale_reason") if isinstance(record, dict) else None
        if stale_reason:
            process = f"{process}: {stale_reason}"
        table.add_row(
            name,
            runner.config.backend,
            session_name if supports_native_session(runner) else "-",
            str(tmux_session_exists(session_name, socket_name=config.native.tmux_socket)) if supports_native_session(runner) else "-",
            str(process or "-"),
            str(pane_command or "-"),
            str(capture_path or "-"),
        )
    console.print(table)


@sessions_app.command("start")
def sessions_start(
    agent: Annotated[str | None, typer.Argument(help="Agent name, or omit to start all tmux agents.")] = None,
    root: RootOpt = Path.cwd(),
    instance: Annotated[str | None, typer.Option("--instance", help="Optional assistant instance name.")] = None,
) -> None:
    """Start configured tmux-backed agent sessions."""
    config = load_config(root)
    runners = build_runners(config.agents)
    selected = runners if agent is None else {agent: runners[agent]} if agent in runners else {}
    if not selected:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    for name, runner in selected.items():
        if not supports_native_session(runner):
            console.print(f"[yellow]Skipping {name}: no native start_command configured[/]")
            continue
        try:
            prepare_native_session(root=root, name=name, runner=runner, instance=instance)
        except RuntimeError as exc:
            console.print(f"[red]Could not start {name}:[/] {exc}")
            raise typer.Exit(code=2) from exc
        console.print(f"[green]Started/ready:[/] {name} -> {runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix)}")


def prepare_native_session(
    *,
    root: Path,
    name: str,
    runner: AgentRunner,
    instance: str | None = None,
    command_override: list[str] | None = None,
    allow_existing: bool = True,
) -> Path:
    if command_override is None and not supports_native_session(runner):
        raise RuntimeError(f"{name} has no native start_command configured")
    ensure_councli_gitignore(root)
    config = load_config(root)
    session_name = runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix)
    registry_key = registry_key_for(name, instance)
    command = command_override or native_start_command(runner)
    if not allow_existing and tmux_session_exists(session_name, socket_name=config.native.tmux_socket):
        raise RuntimeError(f"tmux session {session_name} already exists")
    ensure_tmux_session(
        session_name,
        command,
        root,
        detach_key=config.native.detach_key,
        socket_name=config.native.tmux_socket,
    )
    capture_path = raw_capture_path(root, registry_key).resolve()
    start_tmux_raw_capture(
        session_name,
        capture_path,
        max_bytes=config.native.raw_log_max_bytes,
        backups=config.native.raw_log_backups,
        socket_name=config.native.tmux_socket,
    )
    configure_native_tmux_hooks(root=root, agent=registry_key, session_name=session_name)
    info = tmux_session_info(session_name, socket_name=config.native.tmux_socket)
    upsert_session(
        root,
        agent=registry_key,
        session_name=session_name,
        backend=runner.config.backend,
        cwd=root,
        command=command,
        raw_capture=capture_path,
        extra=info,
    )
    append_project_event(
        root,
        "session.started",
        participant=registry_key,
        payload={
            "session_name": session_name,
            "cwd": str(root),
            "command": command,
            "tmux": info,
            "instance": instance,
        },
        refs={"raw_capture": str(capture_path)},
    )
    return capture_path


def supports_native_session(runner: AgentRunner) -> bool:
    return bool(runner.config.start_command or runner.config.backend == "tmux")


def native_start_command(runner: AgentRunner) -> list[str]:
    if runner.config.start_command:
        return runner.config.start_command
    return runner.config.command


def native_session_runner(runner: AgentRunner) -> AgentRunner:
    return AgentRunner(
        runner.name,
        runner.config.model_copy(
            update={
                "backend": "tmux",
                "command": native_start_command(runner),
            }
        ),
    )


def configure_native_tmux_hooks(*, root: Path, agent: str, session_name: str) -> None:
    config = load_config(root)
    for hook_name, event_type in (
        ("client-attached", "tmux.client-attached"),
        ("client-detached", "tmux.client-detached"),
    ):
        command = shlex.join(
            [
                sys.executable,
                "-m",
                "councli.hook",
                "--root",
                str(root),
                "--event",
                event_type,
                "--participant",
                agent,
                "--session",
                session_name,
            ]
        )
        proc = tmux(
            ["set-hook", "-t", session_name, hook_name, f"run-shell {shlex.quote(command)}"],
            socket_name=config.native.tmux_socket,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"could not install {hook_name} hook for {session_name}")
    closed_command = shlex.join(
        [
            sys.executable,
            "-m",
            "councli.hook",
            "--root",
            str(root),
            "--event",
            "tmux.session-closed",
            "--participant",
            agent,
            "--session",
            session_name,
        ]
    )
    condition = f"#{{==:#{{hook_session_name}},{session_name}}}"
    run_shell = f"run-shell {shlex.quote(closed_command)}"
    hook_index = int(hashlib.sha256(session_name.encode("utf-8")).hexdigest()[:12], 16) % 2_000_000_000
    proc = tmux(
        ["set-hook", "-g", f"session-closed[{hook_index}]", f"if -F {shlex.quote(condition)} {shlex.quote(run_shell)}"],
        socket_name=config.native.tmux_socket,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not install session-closed hook for {session_name}")


def registry_key_for(agent: str, instance: str | None = None) -> str:
    return f"{agent}:{instance}" if instance else agent


def reconcile_native_sessions(*, root: Path, runners: dict[str, AgentRunner]) -> dict[str, Any]:
    config = load_config(root)
    live: dict[str, dict[str, Any]] = {}
    for session_name in tmux_session_names(socket_name=config.native.tmux_socket):
        try:
            live[session_name] = tmux_session_info(session_name, socket_name=config.native.tmux_socket)
        except RuntimeError:
            continue
    registry = reconcile_session_registry(root, live)
    sessions = registry.setdefault("sessions", {})
    for session_name, info in live.items():
        adopted = adoption_key_for_live_session(
            session_name,
            root=root,
            runners=runners,
            prefix=config.native.session_prefix,
        )
        if adopted is None:
            continue
        key, runner = adopted
        record = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
        if not record:
            upsert_session(
                root,
                agent=key,
                session_name=session_name,
                backend=runner.config.backend,
                cwd=Path(info.get("pane_current_path") or root),
                command=native_start_command(runner) if supports_native_session(runner) else runner.config.command,
                status="active",
                extra=info,
            )
    return read_session_registry(root)


def adoption_key_for_live_session(
    session_name: str,
    *,
    root: Path,
    runners: dict[str, AgentRunner],
    prefix: str,
) -> tuple[str, AgentRunner] | None:
    project_prefix = f"{prefix}-{project_hash(root)}-"
    if not session_name.startswith(project_prefix):
        return None
    remainder = session_name[len(project_prefix) :]
    for name, runner in sorted(runners.items(), key=lambda item: len(item[0]), reverse=True):
        if not supports_native_session(runner):
            continue
        expected = runner.session_name_for(root, prefix=prefix).removeprefix(project_prefix)
        if remainder == expected:
            return name, runner
        if remainder.startswith(f"{expected}-"):
            instance = remainder[len(expected) + 1 :]
            return registry_key_for(name, instance), runner
    return None


@sessions_app.command("attach")
def sessions_attach(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    root: RootOpt = Path.cwd(),
    instance: Annotated[str | None, typer.Option("--instance", help="Optional assistant instance name.")] = None,
) -> None:
    """Attach to a native tmux-backed assistant session."""
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    try:
        attach_agent_session(root=root, name=agent, runner=runners[agent], instance=instance)
    except RuntimeError as exc:
        console.print(f"[red]Could not attach {agent}:[/] {exc}")
        raise typer.Exit(code=2) from exc


@sessions_app.command("import", hidden=True)
def sessions_import(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    root: RootOpt = Path.cwd(),
    session_id: Annotated[str | None, typer.Option("--session-id", help="Explicit native session id to record.")] = None,
    path: Annotated[Path | None, typer.Option("--path", help="Explicit native session file to record.")] = None,
    instance: Annotated[str | None, typer.Option("--instance", help="Optional assistant instance name.")] = None,
    auto: Annotated[
        bool,
        typer.Option("--auto", help="Import the best candidate only when it is a single high-confidence match."),
    ] = False,
) -> None:
    """Import a native assistant session id/path into the councli registry."""
    require_experimental("sessions import")
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    registry_key = registry_key_for(agent, instance)
    selected_id = session_id
    selected_path = str(path.resolve()) if path else None
    if selected_id is None and selected_path is None:
        candidates = find_native_session_candidates(agent, root, limit=10)
        if not candidates:
            console.print(f"[yellow]No native session candidates found for {agent}.[/]")
            raise typer.Exit(code=2)
        if auto and can_auto_select_candidate(candidates):
            selected_id = str(candidates[0]["session_id"])
            selected_path = str(candidates[0]["path"])
        else:
            print_native_session_candidates(agent, candidates)
            console.print(
                "[yellow]No session was imported.[/] Rerun with --session-id <id> or --path <file>. "
                "Use --auto only for a single high-confidence match."
            )
            raise typer.Exit(code=2)
    elif selected_id is None and path is not None:
        selected_id = infer_session_id(path)
    if selected_id is not None:
        selected_id = validate_native_session_id(selected_id)
    runner = runners[agent]
    record = upsert_session(
        root,
        agent=registry_key,
        session_name=runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix),
        backend=runner.config.backend,
        cwd=root,
        command=native_start_command(runner) if supports_native_session(runner) else runner.config.command,
        native_session_id=selected_id,
        status="imported",
        extra={"native_session_path": selected_path},
    )
    append_project_event(
        root,
        "session.imported",
        participant=registry_key,
        payload={"native_session_id": selected_id, "native_session_path": selected_path},
    )
    console.print(f"[green]Imported:[/] {registry_key} native_session_id={record.get('native_session_id')}")
    if selected_path:
        console.print(f"Path: {selected_path}")


def can_auto_select_candidate(candidates: list[dict[str, Any]]) -> bool:
    if not candidates:
        return False
    top = candidates[0]
    if top.get("confidence") != "high":
        return False
    if len(candidates) == 1:
        return True
    return int(top.get("score") or 0) > int(candidates[1].get("score") or 0)


def print_native_session_candidates(agent: str, candidates: list[dict[str, Any]]) -> None:
    table = Table(title=f"Native {agent} session candidates")
    table.add_column("#")
    table.add_column("Confidence")
    table.add_column("Session ID")
    table.add_column("Modified")
    table.add_column("Reason")
    table.add_column("Path")
    for index, candidate in enumerate(candidates, start=1):
        modified = datetime.fromtimestamp(float(candidate.get("mtime") or 0), timezone.utc).isoformat()
        table.add_row(
            str(index),
            str(candidate.get("confidence") or "-"),
            str(candidate.get("session_id") or "-"),
            modified,
            str(candidate.get("reason") or "-"),
            str(candidate.get("path") or "-"),
        )
    console.print(table)


def validate_native_session_id(session_id: str) -> str:
    if not NATIVE_SESSION_ID_PATTERN.fullmatch(session_id):
        raise typer.BadParameter("native session id may contain only letters, numbers, dot, underscore, dash, or colon")
    if session_id.startswith("-"):
        raise typer.BadParameter("native session id must not start with '-'")
    return session_id


@sessions_app.command("resume", hidden=True)
def sessions_resume(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    root: RootOpt = Path.cwd(),
    instance: Annotated[str | None, typer.Option("--instance", help="Optional assistant instance name.")] = None,
    attach: Annotated[bool, typer.Option("--attach/--no-attach", help="Attach after relaunching/resuming.")] = True,
    replace_existing: Annotated[
        bool,
        typer.Option("--replace-existing", help="Stop an existing councli tmux session before running the native resume command."),
    ] = False,
    archive_existing: Annotated[
        bool,
        typer.Option("--archive-existing/--no-archive-existing", help="Archive the existing pane before replacing it."),
    ] = True,
) -> None:
    """Relaunch a native assistant from an imported native session id when supported."""
    require_experimental("sessions resume")
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    runner = runners[agent]
    registry = read_session_registry(root)
    registry_key = registry_key_for(agent, instance)
    record = (registry.get("sessions") or {}).get(registry_key)
    if not isinstance(record, dict) or not record.get("native_session_id"):
        console.print(f"[yellow]No imported native session id for {registry_key}.[/]")
        console.print(f"Run: councli sessions import {agent} --session-id <id>")
        console.print("Or start a fresh native session and share context with: councli brief")
        raise typer.Exit(code=2)
    if not runner.config.resume_command:
        console.print(f"[yellow]{agent} has no resume_command configured.[/]")
        raise typer.Exit(code=2)
    native_session_id = validate_native_session_id(str(record["native_session_id"]))
    resume_command = [part.replace("{session_id}", native_session_id) for part in runner.config.resume_command]
    session_name = runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix)
    if tmux_session_exists(session_name, socket_name=config.native.tmux_socket):
        if not replace_existing:
            console.print(f"[red]Refusing to resume over live session:[/] {session_name}")
            console.print("Use --replace-existing to stop the live session and run the native resume command.")
            raise typer.Exit(code=2)
        stop_session(
            root=root,
            label=registry_key,
            session=session_name,
            archive=archive_existing,
            dry_run=False,
            socket_name=config.native.tmux_socket,
        )
    try:
        prepare_native_session(
            root=root,
            name=agent,
            runner=runner,
            instance=instance,
            command_override=resume_command,
            allow_existing=False,
        )
    except RuntimeError as exc:
        console.print(f"[red]Could not resume {registry_key}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    append_project_event(
        root,
        "session.resumed",
        participant=registry_key,
        payload={"native_session_id": native_session_id, "command": resume_command},
    )
    console.print(f"[green]Resumed:[/] {registry_key}")
    if attach:
        attach_agent_session(root=root, name=agent, runner=runner, instance=instance)


def attach_agent_session(*, root: Path, name: str, runner: AgentRunner, instance: str | None = None) -> None:
    config = load_config(root)
    session_name = runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix)
    registry_key = registry_key_for(name, instance)
    capture_path = prepare_native_session(root=root, name=name, runner=runner, instance=instance)
    snapshot_before = snapshot_path(root, registry_key)
    try:
        write_text(snapshot_before, capture_tmux(session_name, socket_name=config.native.tmux_socket).rstrip() + "\n")
    except RuntimeError:
        pass
    append_project_event(
        root,
        "mode.changed",
        participant=registry_key,
        payload={
            "mode": "assistant",
            "action": "attach",
            "session_name": session_name,
            "detach": config.native.detach_key,
            "nested_tmux": bool(os.environ.get("TMUX")),
        },
        refs={"raw_capture": str(capture_path), "snapshot_before": str(snapshot_before)},
    )
    console.print(f"[bold]Attaching to {registry_key}[/] ({session_name}). Press [cyan]{config.native.detach_key}[/cyan] to return to councli.")
    rc = attach_tmux_session(session_name, socket_name=config.native.tmux_socket)
    snapshot_after = snapshot_path(root, registry_key)
    try:
        write_text(snapshot_after, capture_tmux(session_name, socket_name=config.native.tmux_socket).rstrip() + "\n")
    except RuntimeError:
        pass
    append_project_event(
        root,
        "mode.changed",
        participant=registry_key,
        payload={
            "mode": "assistant",
            "action": "detach",
            "session_name": session_name,
            "return_code": rc,
        },
        refs={"raw_capture": str(capture_path), "snapshot_after": str(snapshot_after)},
        status="ok" if rc == 0 else "failed",
    )
    if rc != 0:
        raise RuntimeError(f"tmux attach exited with code {rc}")


@sessions_app.command("send", hidden=True)
def sessions_send(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    message: Annotated[str, typer.Argument(help="Message to paste into the agent session.")],
    root: RootOpt = Path.cwd(),
    instance: Annotated[str | None, typer.Option("--instance", help="Optional assistant instance name.")] = None,
    marker: Annotated[bool, typer.Option("--marker/--no-marker", help="Append the agent done marker.")] = True,
) -> None:
    """Send a message into a tmux-backed agent session."""
    require_experimental("sessions send")
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    runner = runners[agent]
    if not supports_native_session(runner):
        console.print(f"[red]{agent} has no native start_command configured[/]")
        raise typer.Exit(code=2)
    try:
        capture_path = prepare_native_session(root=root, name=agent, runner=runner, instance=instance)
    except RuntimeError as exc:
        console.print(f"[red]Could not prepare {agent}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    config = load_config(root)
    session_name = runner.session_name_for(root, instance=instance, prefix=config.native.session_prefix)
    registry_key = registry_key_for(agent, instance)
    text = message
    if marker:
        text = (
            f"{message} "
            "When finished, print exactly: "
            f"{runner.done_marker}"
        )
    if runner.config.prompt_style == "compact":
        text = compact_terminal_prompt(text)
    try:
        send_tmux_text(
            session_name,
            text,
            input_method=runner.config.input_method,
            submit_keys=runner.config.submit_keys,
            post_paste_delay_seconds=runner.config.post_paste_delay_seconds,
            socket_name=config.native.tmux_socket,
        )
    except RuntimeError as exc:
        console.print(f"[red]Could not send to {agent}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    append_project_event(
        root,
        "prompt.routed",
        participant=registry_key,
        payload={"mode": "send", "message": message, "marker": marker},
        refs={"raw_capture": str(capture_path)},
    )
    console.print(f"[green]Sent to {registry_key}[/] ({session_name})")


@sessions_app.command("ask", hidden=True)
def sessions_ask(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    message: Annotated[str, typer.Argument(help="Message to send and wait on.")],
    root: RootOpt = Path.cwd(),
    save: Annotated[
        Path | None,
        typer.Option("--save", help="Optional file path to save the captured response."),
    ] = None,
) -> None:
    """Send a message, wait for the done marker, and print the captured response."""
    require_experimental("sessions ask")
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    runner = runners[agent]
    if not supports_native_session(runner):
        console.print(f"[red]{agent} has no native start_command configured[/]")
        raise typer.Exit(code=2)

    result = native_session_runner(runner).run(message, cwd=root)
    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(result.output, encoding="utf-8")
    if result.ok:
        console.print(result.output)
        return
    console.print(f"[red]{agent} failed:[/] {result.error}")
    if result.output:
        console.print(result.output)
    raise typer.Exit(code=2)


@sessions_app.command("relay", hidden=True)
def sessions_relay(
    source: Annotated[str, typer.Argument(help="Agent that answers first.")],
    target: Annotated[str, typer.Argument(help="Agent that receives the source response.")],
    message: Annotated[str, typer.Argument(help="Initial message for the source agent.")],
    root: RootOpt = Path.cwd(),
) -> None:
    """Ask one tmux-backed agent, then relay its response to another agent."""
    require_experimental("sessions relay")
    config = load_config(root)
    runners = build_runners(config.agents)
    missing = [name for name in (source, target) if name not in runners]
    if missing:
        console.print(f"[red]Unknown agent(s):[/] {', '.join(missing)}")
        raise typer.Exit(code=2)

    source_runner = runners[source]
    target_runner = runners[target]
    for runner in (source_runner, target_runner):
        if not supports_native_session(runner):
            console.print(f"[red]{runner.name} has no native start_command configured[/]")
            raise typer.Exit(code=2)

    source_result = native_session_runner(source_runner).run(message, cwd=root)
    if not source_result.ok:
        console.print(f"[red]{source} failed:[/] {source_result.error}")
        if source_result.output:
            console.print(source_result.output)
        raise typer.Exit(code=2)

    relay_message = (
        f"Another agent ({source}) responded to this task.\n\n"
        f"Original task:\n{message}\n\n"
        f"{source} response:\n{source_result.output}\n\n"
        "Critique it, identify anything missing, and give your own revised answer."
    )
    target_result = native_session_runner(target_runner).run(relay_message, cwd=root)
    if not target_result.ok:
        console.print(f"[red]{target} failed:[/] {target_result.error}")
        if target_result.output:
            console.print(target_result.output)
        raise typer.Exit(code=2)

    console.print(f"[bold]{source} response[/]")
    console.print(source_result.output)
    console.print(f"[bold]{target} response[/]")
    console.print(target_result.output)


@sessions_app.command("capture")
def sessions_capture(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    root: RootOpt = Path.cwd(),
) -> None:
    """Print captured output from a tmux-backed agent session."""
    config = load_config(root)
    runners = build_runners(config.agents)
    if agent not in runners:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)
    runner = runners[agent]
    if not supports_native_session(runner):
        console.print(f"[red]{agent} has no native start_command configured[/]")
        raise typer.Exit(code=2)
    config = load_config(root)
    session_name = runner.session_name_for(root, prefix=config.native.session_prefix)
    try:
        console.print(capture_tmux(session_name, socket_name=config.native.tmux_socket))
    except RuntimeError as exc:
        console.print(f"[red]Could not capture {agent}:[/] {exc}")
        raise typer.Exit(code=2) from exc


@sessions_app.command("stop")
def sessions_stop(
    agent: Annotated[str | None, typer.Argument(help="Agent name, or omit to stop all configured tmux agents.")] = None,
    root: RootOpt = Path.cwd(),
    archive: Annotated[
        bool,
        typer.Option("--archive/--no-archive", help="Capture pane text before killing the session."),
    ] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be killed.")] = False,
) -> None:
    """Stop configured tmux-backed participant sessions."""
    config = load_config(root)
    runners = build_runners(config.agents)
    selected = runners if agent is None else {agent: runners[agent]} if agent in runners else {}
    if not selected:
        console.print(f"[red]Unknown agent:[/] {agent}")
        raise typer.Exit(code=2)

    stopped = 0
    for name, runner in selected.items():
        if not supports_native_session(runner):
            console.print(f"[yellow]Skipping {name}: no native start_command configured[/]")
            continue
        if stop_session(
            root=root,
            label=name,
            session=runner.session_name_for(root, prefix=config.native.session_prefix),
            archive=archive,
            dry_run=dry_run,
            socket_name=config.native.tmux_socket,
        ):
            stopped += 1
    console.print(f"[green]{'Would stop' if dry_run else 'Stopped'}:[/] {stopped} configured tmux session(s)")


@sessions_app.command("prune")
def sessions_prune(
    root: RootOpt = Path.cwd(),
    archive: Annotated[
        bool,
        typer.Option("--archive/--no-archive", help="Capture pane text before killing sessions."),
    ] = True,
    all_councli: Annotated[
        bool,
        typer.Option("--all-councli", help="Also prune any tmux session whose name starts with councli-."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be killed.")] = False,
) -> None:
    """Prune councli-managed tmux sessions and visible council rooms."""
    config = load_config(root)
    runners = build_runners(config.agents)
    existing = set(tmux_session_names(socket_name=config.native.tmux_socket))
    configured = {
        runner.session_name_for(root, prefix=config.native.session_prefix)
        for runner in runners.values()
        if supports_native_session(runner)
    }
    rooms = {name for name in existing if name.startswith("councli-room-")}
    targets = configured | rooms
    if all_councli:
        targets |= {name for name in existing if name.startswith("councli-")}
    targets = {name for name in targets if name in existing}

    if not targets:
        console.print("No councli tmux sessions to prune.")
        return

    for session in sorted(targets):
        stop_session(
            root=root,
            label=session,
            session=session,
            archive=archive,
            dry_run=dry_run,
            socket_name=config.native.tmux_socket,
        )
    console.print(f"[green]{'Would prune' if dry_run else 'Pruned'}:[/] {len(targets)} tmux session(s)")


def stop_session(*, root: Path, label: str, session: str, archive: bool, dry_run: bool, socket_name: str | None = None) -> bool:
    if not tmux_session_exists(session, socket_name=socket_name):
        console.print(f"[yellow]Missing:[/] {label} -> {session}")
        return False
    if dry_run:
        console.print(f"[cyan]Would stop:[/] {label} -> {session}")
        return True
    if archive:
        try:
            body = capture_tmux(session, socket_name=socket_name)
        except RuntimeError as exc:
            body = f"Could not capture session {session}: {exc}\n"
        path = session_archive_path(root, session)
        write_text(path, body.rstrip() + "\n")
        console.print(f"[green]Archived:[/] {session} -> {path}")
    stopped_at = datetime.now(timezone.utc)
    try:
        kill_tmux_session(session, socket_name=socket_name)
    except RuntimeError as exc:
        console.print(f"[red]Could not stop {session}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    wait_for_tmux_hook_event(root, "tmux.session-closed", session_name=session, since=stopped_at, timeout_seconds=2.0)
    append_project_event(
        root,
        "session.stopped",
        participant=label,
        payload={"session_name": session, "archived": archive},
        refs={"archive": str(path) if archive else None},
    )
    console.print(f"[green]Stopped:[/] {label} -> {session}")
    return True


def wait_for_tmux_hook_event(
    root: Path,
    event_type: str,
    *,
    session_name: str,
    since: datetime,
    timeout_seconds: float,
) -> bool:
    deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
    while datetime.now(timezone.utc).timestamp() < deadline:
        for event in read_project_events(root, limit=20):
            if not event_is_after(event, since):
                continue
            if event.get("type") != event_type:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if payload.get("session_name") == session_name:
                return True
        time.sleep(0.05)
    return False


def event_is_after(event: dict[str, Any], since: datetime) -> bool:
    try:
        value = datetime.fromisoformat(str(event.get("ts") or ""))
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value >= since


def session_archive_path(root: Path, session: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_session = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in session).strip("-") or "session"
    return root / ".councli" / "session-archives" / timestamp / f"{safe_session}.txt"


@artifacts_app.command("list")
def artifacts_list(
    root: RootOpt = Path.cwd(),
    artifact_class: Annotated[
        list[str] | None,
        typer.Option("--class", "-k", help="Artifact class to include. Repeat to choose multiple."),
    ] = None,
    older_than_days: Annotated[int | None, typer.Option("--older-than", help="Only show artifacts older than N days.")] = None,
) -> None:
    """List local councli artifact files and directories."""
    config = load_config(root)
    classes = normalize_artifact_classes(artifact_class, default=ARTIFACT_CLASSES)
    candidates = filter_artifacts_by_age(
        collect_artifact_files(root, classes=classes, max_file_bytes=None),
        older_than_days=older_than_days,
    )
    if not candidates:
        console.print("No matching artifacts.")
        return
    print_artifact_table("Councli artifacts", candidates)
    console.print(f"[dim]Total:[/] {len(candidates)} artifact(s), {format_bytes(sum(candidate.bytes for candidate in candidates))}")
    if older_than_days is not None:
        console.print(f"[dim]Retention default classes:[/] {', '.join(config.artifacts.prune_default_classes)}")


@artifacts_app.command("scrub")
def artifacts_scrub(
    root: RootOpt = Path.cwd(),
    artifact_class: Annotated[
        list[str] | None,
        typer.Option("--class", "-k", help="Artifact class to include. Repeat to choose multiple."),
    ] = None,
    write: Annotated[bool, typer.Option("--write/--dry-run", help="Rewrite matching files in place. Defaults to dry-run.")] = False,
) -> None:
    """Redact common secret-looking tokens from local councli text artifacts."""
    config = load_config(root)
    classes = normalize_artifact_classes(artifact_class, default=ARTIFACT_CLASSES)
    patterns = [re.compile(pattern) for pattern in config.artifacts.redact_patterns]
    candidates = collect_artifact_files(root, classes=classes, max_file_bytes=config.artifacts.scrub_max_file_bytes)
    changed: list[ArtifactCandidate] = []
    total_matches = 0
    skipped = 0
    for candidate in candidates:
        try:
            original = candidate.path.read_bytes()
        except OSError:
            skipped += 1
            continue
        if b"\x00" in original:
            skipped += 1
            continue
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        redacted, matches = redact_text(text, patterns=patterns, replacement=config.artifacts.redact_replacement)
        if matches == 0:
            continue
        total_matches += matches
        changed.append(candidate)
        if write:
            rewrite_text_preserve_mode(candidate.path, redacted)

    if changed:
        print_artifact_table("Artifacts to redact" if not write else "Redacted artifacts", changed)
    action = "Redacted" if write else "Would redact"
    console.print(f"[green]{action}:[/] {total_matches} match(es) in {len(changed)} file(s)")
    if skipped:
        console.print(f"[yellow]Skipped:[/] {skipped} binary, unreadable, or oversized file(s)")
    if write and changed:
        append_project_event(
            root,
            "artifacts.scrubbed",
            payload={"files": len(changed), "matches": total_matches, "classes": classes},
        )
    elif changed:
        console.print("[dim]Run again with --write to rewrite these files in place.[/]")


@artifacts_app.command("prune")
def artifacts_prune(
    root: RootOpt = Path.cwd(),
    older_than_days: Annotated[int, typer.Option("--older-than", help="Delete artifacts older than N days.")] = 30,
    artifact_class: Annotated[
        list[str] | None,
        typer.Option("--class", "-k", help="Artifact class to prune. Repeat to choose multiple."),
    ] = None,
    delete: Annotated[bool, typer.Option("--delete/--dry-run", help="Actually remove matching artifacts. Defaults to dry-run.")] = False,
) -> None:
    """Prune old local councli artifacts. Destructive deletion requires --delete."""
    config = load_config(root)
    classes = normalize_artifact_classes(artifact_class, default=config.artifacts.prune_default_classes)
    candidates = filter_artifacts_by_age(
        collect_prune_targets(root, classes=classes),
        older_than_days=older_than_days,
    )
    if not candidates:
        console.print("No matching artifacts to prune.")
        return
    print_artifact_table("Artifacts to prune" if not delete else "Pruned artifacts", candidates)
    if not delete:
        console.print("[dim]Dry run only. Run again with --delete to remove these artifacts.[/]")
        return
    for candidate in candidates:
        if candidate.is_dir:
            shutil.rmtree(candidate.path)
        else:
            candidate.path.unlink()
    append_project_event(
        root,
        "artifacts.pruned",
        payload={
            "files_or_directories": len(candidates),
            "bytes": sum(candidate.bytes for candidate in candidates),
            "classes": classes,
            "older_than_days": older_than_days,
        },
    )
    console.print(f"[green]Deleted:[/] {len(candidates)} artifact(s), {format_bytes(sum(candidate.bytes for candidate in candidates))}")


def normalize_artifact_classes(values: list[str] | None, *, default: tuple[str, ...] | list[str]) -> list[str]:
    selected = list(default if not values else values)
    invalid = sorted(set(selected) - set(ARTIFACT_CLASSES))
    if invalid:
        console.print(f"[red]Unknown artifact class(es):[/] {', '.join(invalid)}")
        console.print(f"Known classes: {', '.join(ARTIFACT_CLASSES)}")
        raise typer.Exit(code=2)
    return selected


def artifact_root(root: Path, kind: str) -> Path:
    return root / ".councli" / Path(*ARTIFACT_ROOTS[kind])


def collect_artifact_files(root: Path, *, classes: list[str], max_file_bytes: int | None) -> list[ArtifactCandidate]:
    candidates: list[ArtifactCandidate] = []
    for kind in classes:
        base = artifact_root(root, kind)
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if max_file_bytes is not None and size > max_file_bytes:
                continue
            candidates.append(artifact_candidate(kind, path))
    return candidates


def collect_prune_targets(root: Path, *, classes: list[str]) -> list[ArtifactCandidate]:
    candidates: list[ArtifactCandidate] = []
    directory_classes = {"run", "task"}
    for kind in classes:
        base = artifact_root(root, kind)
        if not base.exists():
            continue
        if kind in directory_classes:
            paths = sorted(base.iterdir())
        else:
            paths = sorted(path for path in base.rglob("*") if path.is_file())
        for path in paths:
            candidates.append(artifact_candidate(kind, path))
    return candidates


def artifact_candidate(kind: str, path: Path) -> ArtifactCandidate:
    try:
        stat = path.stat()
        size = directory_size(path) if path.is_dir() else stat.st_size
        modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    except OSError:
        size = 0
        modified_at = datetime.fromtimestamp(0, timezone.utc)
    return ArtifactCandidate(kind=kind, path=path, bytes=size, modified_at=modified_at, is_dir=path.is_dir())


def directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total


def filter_artifacts_by_age(candidates: list[ArtifactCandidate], *, older_than_days: int | None) -> list[ArtifactCandidate]:
    if older_than_days is None:
        return candidates
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    return [candidate for candidate in candidates if candidate.modified_at < cutoff]


def redact_text(text: str, *, patterns: list[re.Pattern[str]], replacement: str) -> tuple[str, int]:
    total = 0
    value = text
    for pattern in patterns:
        value, count = pattern.subn(replacement, value)
        total += count
    return value, total


def rewrite_text_preserve_mode(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def print_artifact_table(title: str, candidates: list[ArtifactCandidate]) -> None:
    table = Table(title=title, expand=False)
    table.add_column("Class", no_wrap=True)
    table.add_column("Size", no_wrap=True)
    table.add_column("Modified", no_wrap=True)
    table.add_column("Path")
    for candidate in candidates:
        table.add_row(
            candidate.kind,
            format_bytes(candidate.bytes),
            candidate.modified_at.isoformat(timespec="seconds"),
            str(candidate.path),
        )
    console.print(table)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


@app.command()
def council(
    task: Annotated[str | None, typer.Argument(help="Task or question for the blackboard council.")] = None,
    root: RootOpt = Path.cwd(),
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", "-p", help="Participant name. Repeat to choose multiple."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Write blackboard artifacts without invoking CLIs."),
    ] = False,
    visible: Annotated[
        bool,
        typer.Option("--visible", help="Create a visible tmux room for participants before running."),
    ] = False,
    attach: Annotated[
        bool,
        typer.Option("--attach", help="Attach to the visible tmux room instead of running rounds."),
    ] = False,
) -> None:
    """Run an explicit peer-aware shared council turn."""
    if task is None:
        console.print("[yellow]No council task supplied. Opening interactive councli chat instead.[/]")
        chat(root=root, participant=participant, dry_run=dry_run)
        return

    config = load_config(root)
    runners = build_runners(config.agents)
    selected_names = participant or [
        name
        for name, runner in runners.items()
        if runner.health().available
    ]
    if not selected_names:
        console.print("[red]No participants selected or available.[/]")
        console.print("Enable at least one configured agent, or pass --participant for configured entries.")
        raise typer.Exit(code=2)

    room = "councli-room-" + project_hash(root) + "-" + "-".join(selected_names)
    if visible or attach:
        visible_names = [
            name
            for name in selected_names
            if name in runners and supports_native_session(runners[name])
        ]
        if not visible_names:
            console.print("[yellow]No selected participants have a native start_command; visible room skipped.[/]")
        else:
            room = "councli-room-" + project_hash(root) + "-" + "-".join(visible_names)
            try:
                create_visible_tmux_room(
                    room=room,
                    runners=runners,
                    root=root,
                    participants=visible_names,
                    session_prefix=config.native.session_prefix,
                    socket_name=config.native.tmux_socket,
                    detach_key=config.native.detach_key,
                )
            except RuntimeError as exc:
                console.print(f"[red]Could not create visible room:[/] {exc}")
                raise typer.Exit(code=2) from exc
            console.print(f"[green]Visible room:[/] tmux attach -t {room}")
    if attach:
        if not any(name in runners and supports_native_session(runners[name]) for name in selected_names):
            raise typer.Exit(code=2)
        try:
            attach_tmux_room(room, socket_name=config.native.tmux_socket)
        except RuntimeError as exc:
            console.print(f"[red]Could not attach visible room:[/] {exc}")
            raise typer.Exit(code=2) from exc
        return

    result = run_shared_turn(
        task=task,
        intent_name="deliberate",
        root=root,
        runners=runners,
        participant=selected_names,
        dry_run=dry_run,
    )
    if result is None:
        raise typer.Exit(code=2)


@app.command(hidden=True)
def reason(
    task: Annotated[str, typer.Argument(help="Task or question for the agent council.")],
    root: RootOpt = Path.cwd(),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Write prompts and decisions without invoking agent CLIs."),
    ] = False,
) -> None:
    """Deprecated: run a deliberate shared turn."""
    require_experimental("councli reason")
    config = load_config(root)
    runners = build_runners(config.agents)
    console.print("[yellow]`councli reason` is deprecated; using the shared /deliberate engine.[/]")
    result = run_shared_turn(
        task=task,
        intent_name="deliberate",
        root=root,
        runners=runners,
        participant=None,
        dry_run=dry_run,
    )
    if result is None:
        raise typer.Exit(code=2)


@app.command()
def broadcast(
    prompt: Annotated[str, typer.Argument(help="Planning/review prompt to send to all available headless-capable assistants.")],
    root: RootOpt = Path.cwd(),
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", "-p", help="Participant name. Repeat to choose multiple."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Write artifacts without invoking assistant CLIs."),
    ] = False,
) -> None:
    """Send a planning/review prompt to available assistants through headless commands."""
    config = load_config(root)
    runners = build_runners(config.agents)
    run_dir, results = run_broadcast_round(
        root=root,
        runners=runners,
        prompt=prompt,
        participants=participant,
        dry_run=dry_run,
        min_confidence=config.consensus.min_confidence,
    )
    ok, failed, skipped = summarize_broadcast_results(results)
    console.print(f"[bold]Broadcast:[/] {run_dir}")
    console.print("Mode: headless subprocess broadcast; active tmux assistant sessions are not fed this prompt.")
    console.print("Retry policy: none. Failures and policy skips are recorded separately.")
    console.print(f"Responded: {', '.join(ok) if ok else '-'}")
    console.print(f"Failed: {', '.join(failed) if failed else '-'}")
    console.print(f"Skipped: {', '.join(skipped) if skipped else '-'}")
    console.print(f"Blackboard: {run_dir / 'blackboard.md'}")


@app.command()
def brief(
    task: Annotated[str | None, typer.Argument(help="Optional task text for a new manual brief.")] = None,
    root: RootOpt = Path.cwd(),
) -> None:
    """Print or create an inspectable councli task brief."""
    show_or_create_brief(root=root, task=task)


def show_or_create_brief(*, root: Path, task: str | None = None) -> Path | None:
    if task:
        brief = write_task_brief(root, task=task, task_id=f"manual-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        path = brief.path
    else:
        path = latest_task_brief(root)
    if path is None:
        console.print("[yellow]No task brief exists yet.[/] Run /brief <task> or start a council/broadcast run.")
        return None
    console.print(f"Brief: {path}")
    console.print(f"Paste into an assistant if needed: Read {path} before starting.")
    return path


def run_broadcast_round(
    *,
    root: Path,
    runners: dict[str, AgentRunner],
    prompt: str,
    participants: list[str] | None,
    dry_run: bool,
    min_confidence: float,
) -> tuple[Path, dict[str, Any]]:
    selected_names = participants or [
        name
        for name, runner in runners.items()
        if runner.health().available
    ]
    run_dir = new_run_dir(root, "broadcast")
    brief = write_task_brief(root, task=prompt, task_id=run_dir.name, run_dir=run_dir)
    write_text(
        run_dir / "broadcast" / "README.md",
        "\n".join(
            [
                "# Broadcast semantics",
                "",
                "This run used headless subprocess commands for planning, critique, comparison, or review.",
                "It did not inject the prompt into active tmux assistant sessions.",
                "Retry policy: none. Failures and policy skips are recorded separately.",
                "Broadcast is not a concurrent editing mode.",
                "",
            ]
        ),
    )
    ledger = EventLedger(run_dir, run_id=run_dir.name)
    ledger.append(
        "run.started",
        payload={
            "task": prompt,
            "root": str(root),
            "mode": "broadcast",
            "dry_run": dry_run,
            "min_confidence": min_confidence,
            "session_context": "headless_subprocess",
            "retry_policy": "none",
        },
        refs={"brief": str(brief.path)},
    )
    results: dict[str, Any] = {}
    runnable: dict[str, AgentRunner] = {}
    for name in selected_names:
        runner = runners.get(name)
        if runner is None:
            continue
        health = runner.health()
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
                "mode": "broadcast",
            },
        )
        if not health.available:
            continue
        if not adapter_supports_intent(runner, "broadcast"):
            results[name] = runner_unavailable_result(name, "unsupported intent: broadcast")
            continue
        try:
            runnable[name] = broadcast_runner(runner)
        except ValueError as exc:
            results[name] = runner_unavailable_result(name, str(exc))
    ledger.render()

    task_prompt = (
        "Read-only broadcast from councli. Do not edit files or run implementation commands. "
        f"User prompt: {prompt}\n\n"
        f"Shared task brief: {brief.path}\n"
        "Return a concise answer with: SUMMARY, RISKS, RECOMMENDATION."
    )
    prompts = {name: task_prompt for name in runnable}
    results.update(run_turn_round(root=root, runners=runnable, prompts=prompts, dry_run=dry_run, phase="broadcast"))

    for name in selected_names:
        result = results.get(name)
        if result is None:
            result = runner_unavailable_result(name, "not runnable")
            results[name] = result
        status = "ok" if result.ok else "skipped" if result.skipped else "failed"
        suffix = "md" if result.ok else "skipped.txt" if result.skipped else "failed.txt"
        body = result.output if result.ok else "\n\n".join(part for part in [result.error, result.output] if part)
        path = run_dir / "broadcast" / f"{name}.{suffix}"
        write_text(path, (body or "(empty)").rstrip() + "\n")
        ref = ledger.write_blob("broadcast", name, body or result.error or "(empty)", suffix="md" if result.ok else "txt")
        original_runner = runners.get(name)
        original_config = original_runner.config if original_runner is not None else None
        explicit_broadcast_command = bool(original_config and original_config.broadcast_command)
        broadcast_read_only = bool(original_config and original_config.broadcast_read_only)
        ledger.append(
            "response.received",
            phase="broadcast",
            participant=name,
            status=status,
            refs={"content" if result.ok else "error": ref},
            payload={
                "ok": result.ok,
                "mode": "broadcast",
                "command": result.command,
                "exit_code": result.exit_code,
                "error": result.error,
                "session_context": "headless_subprocess",
                "retry_policy": "none",
                "broadcast_command_explicit": explicit_broadcast_command,
                "read_only_enforced": bool(broadcast_read_only and explicit_broadcast_command),
                "broadcast_read_only": broadcast_read_only,
            },
        )
        append_project_event(
            root,
            "broadcast.sent",
            participant=name,
            status=status,
            payload={
                "run": run_dir.name,
                "prompt": prompt,
                "ok": result.ok,
                "session_context": "headless_subprocess",
                "retry_policy": "none",
            },
            refs={"run": str(run_dir), "artifact": str(path)},
        )
    ledger.append("run.completed", payload={"approved": False, "implemented": False, "mode": "broadcast"})
    ledger.render()
    return run_dir, results


def summarize_broadcast_results(results: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for name, result in results.items():
        if result.ok:
            ok.append(name)
        elif result.skipped:
            skipped.append(name)
        else:
            failed.append(name)
    return sorted(ok), sorted(failed), sorted(skipped)


def runner_unavailable_result(name: str, reason: str) -> Any:
    from councli.agents import AgentRunResult

    return AgentRunResult(
        name=name,
        ok=False,
        skipped=True,
        exit_code=None,
        output="",
        error=reason,
        command=[],
    )


def broadcast_runner(runner: AgentRunner) -> AgentRunner:
    if not runner.config.broadcast_enabled:
        raise ValueError(f"{runner.name} broadcast is disabled in config")
    command = runner.config.broadcast_command or runner.config.command
    if not any("{prompt}" in part for part in command):
        raise ValueError(f"{runner.name} has no prompt-capable broadcast command")
    timeout = runner.config.broadcast_timeout_seconds or runner.config.timeout_seconds
    return AgentRunner(
        runner.name,
        runner.config.model_copy(
            update={
                "backend": "exec",
                "command": command,
                "timeout_seconds": timeout,
            }
        ),
    )


def shared_turn_runner(runner: AgentRunner) -> AgentRunner:
    command = runner.config.broadcast_command or runner.config.command
    if not any("{prompt}" in part for part in command):
        raise ValueError(f"{runner.name} has no prompt-capable command")
    timeout = runner.config.broadcast_timeout_seconds or runner.config.timeout_seconds
    return AgentRunner(
        runner.name,
        runner.config.model_copy(update={"backend": "exec", "command": command, "timeout_seconds": timeout}),
    )


@dataclass(frozen=True)
class TurnIntent:
    name: str
    title: str
    instruction: str
    max_rounds: int = 1
    force_peer_round: bool = False
    require_vote: bool = False


TURN_INTENTS: dict[str, TurnIntent] = {
    "chat": TurnIntent(
        name="chat",
        title="Shared conversation",
        instruction=(
            "Answer the user directly as part of the council. Do not force critique, voting, "
            "executor selection, or implementation unless the user explicitly asks."
        ),
        max_rounds=2,
    ),
    "deliberate": TurnIntent(
        name="deliberate",
        title="Deliberation",
        instruction=(
            "Think with the other participants about the prompt. In the first round, give your "
            "independent view. In later rounds, react to the shared blackboard, call out useful "
            "disagreements, and converge on a practical recommendation."
        ),
        max_rounds=2,
        force_peer_round=True,
    ),
    "vote": TurnIntent(
        name="vote",
        title="Explicit vote",
        instruction=(
            "Cast a clear vote or rank the available options in response to the prompt. "
            "Use the body for reasoning, then include a vote value in the trailer."
        ),
        max_rounds=1,
        require_vote=True,
    ),
}


TRAILER_PATTERN = re.compile(r"\n?COUNCLI_TRAILER\s*\n(?P<body>.*)$", re.IGNORECASE | re.DOTALL)


def run_shared_turn(
    *,
    task: str,
    intent_name: str,
    root: Path,
    runners: dict[str, AgentRunner],
    participant: list[str] | None,
    dry_run: bool,
    session_degraded: dict[str, str] | None = None,
) -> Path | None:
    intent = TURN_INTENTS[intent_name]
    selected_names = participant or [
        name
        for name, runner in runners.items()
        if runner.health().available
    ]
    if not selected_names:
        console.print("[red]No participants selected or available.[/]")
        return None

    run_dir = new_run_dir(root, intent.name)
    brief = write_task_brief(root, task=task, task_id=run_dir.name, run_dir=run_dir)
    ledger = EventLedger(run_dir, run_id=run_dir.name)
    ledger.append(
        "run.started",
        payload={
            "task": task,
            "root": str(root),
            "mode": "shared_turn",
            "intent": intent.name,
            "dry_run": dry_run,
        },
        refs={"brief": str(brief.path)},
    )

    console.rule(f"[bold]turn {run_dir.name}[/]")
    console.print(f"[bold]Task:[/] {task}")
    console.print(f"[bold]Intent:[/] {intent.name} - {intent.title}")
    console.print(f"[bold]Asking:[/] {', '.join(selected_names)}")

    runnable: dict[str, AgentRunner] = {}
    degraded: dict[str, Any] = {}
    session_degraded = session_degraded if session_degraded is not None else {}
    for name in selected_names:
        runner = runners.get(name)
        if runner is None:
            degraded[name] = runner_unavailable_result(name, "unknown participant")
            continue
        health = runner.health()
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
                "mode": "shared_turn",
                "intent": intent.name,
            },
        )
        if name in session_degraded:
            degraded[name] = runner_unavailable_result(name, f"degraded for this session: {session_degraded[name]}")
            continue
        if not adapter_supports_intent(runner, intent.name):
            degraded[name] = runner_unavailable_result(name, f"unsupported intent: {intent.name}")
            continue
        if not health.available:
            degraded[name] = runner_unavailable_result(name, health.reason)
            continue
        try:
            runnable[name] = shared_turn_runner(runner)
        except ValueError as exc:
            degraded[name] = runner_unavailable_result(name, str(exc))
    ledger.render()

    if not runnable and degraded:
        console.print("[yellow]No prompt-capable participants are available for this turn.[/]")

    all_rounds: list[dict[str, Any]] = []
    latest_results: dict[str, Any] = dict(degraded)
    round_count = 0
    try:
        for round_number in range(1, intent.max_rounds + 1):
            round_count = round_number
            context = render_peer_context(all_rounds) if round_number > 1 else ""
            phase = f"{intent.name}.round{round_number}"
            packet_prompts = write_shared_turn_packets(
                ledger=ledger,
                run_dir=run_dir,
                participants=list(runnable.keys()),
                task=task,
                intent=intent,
                brief_path=brief.path,
                round_number=round_number,
                peer_context=context,
            )
            console.print(f"\n[bold cyan]Round {round_number}[/] asking {', '.join(runnable.keys()) if runnable else '-'}")
            round_results = run_turn_round(root=root, runners=runnable, prompts=packet_prompts, dry_run=dry_run, phase=phase)
            merged_results = dict(degraded)
            merged_results.update(round_results)
            parsed_round: dict[str, dict[str, Any]] = {}
            for name in selected_names:
                result = merged_results.get(name) or runner_unavailable_result(name, "not runnable")
                if dry_run and result.ok:
                    body, trailer = result.output, default_turn_trailer()
                else:
                    body, trailer = parse_turn_trailer(result.output if result.ok else "")
                if result.ok:
                    result = replace(result, output=body)
                latest_results[name] = result
                parsed_round[name] = {"result": result, "trailer": trailer}
                sidecar = write_shared_turn_result(
                    root=root,
                    run_dir=run_dir,
                    ledger=ledger,
                    name=name,
                    result=result,
                    intent=intent,
                    round_number=round_number,
                    trailer=trailer,
                )
                parsed_round[name]["sidecar"] = sidecar
                if is_session_degrading_failure(result):
                    session_degraded[name] = result.error or result.failure_class or "agent unavailable"
                    runnable.pop(name, None)
            ledger.render()
            all_rounds.append(parsed_round)
            print_shared_round_responses(parsed_round, selected_names, intent=intent, round_number=round_number)
            if not should_continue_shared_turn(intent=intent, round_number=round_number, parsed_round=parsed_round):
                break
    except KeyboardInterrupt:
        ledger.append(
            "turn.canceled",
            status="canceled",
            payload={"mode": "shared_turn", "intent": intent.name, "rounds": round_count},
        )
        ledger.render()
        console.print("\n[yellow]Turn canceled.[/]")
        console.print(f"[dim]Blackboard:[/] {run_dir / 'blackboard.md'}")
        return run_dir

    decision = decide_shared_vote(all_rounds[-1], selected_names) if intent.require_vote and all_rounds else None
    if decision is not None:
        write_json(run_dir / "decisions" / "vote.json", decision)
        ledger.append(
            "decision.finalized",
            phase=f"{intent.name}.decision",
            payload=decision,
            refs={"decision": "decisions/vote.json"},
        )
        ledger.render()
        print_shared_vote_decision(decision)

    final_answer = synthesize_shared_turn(
        task=task,
        intent=intent,
        root=root,
        run_dir=run_dir,
        runners=runners,
        selected_names=selected_names,
        all_rounds=all_rounds,
        dry_run=dry_run,
        ledger=ledger,
        decision=decision,
    )
    if final_answer:
        console.print("\n[bold]Councli[/]")
        console.print(final_answer.strip())
    ledger.append(
        "run.completed",
        payload={
            "mode": "shared_turn",
            "intent": intent.name,
            "responded": bool(final_answer),
            "rounds": round_count,
            "decision": decision,
        },
    )
    ledger.render()
    record_shared_turn(
        root=root,
        run_dir=run_dir,
        task=task,
        intent=intent,
        final_answer=final_answer,
        results=latest_results,
        decision=decision,
    )
    console.print(f"[dim]Blackboard:[/] {run_dir / 'blackboard.md'}")
    return run_dir


def run_conversation_turn(
    *,
    task: str,
    root: Path,
    runners: dict[str, AgentRunner],
    participant: list[str] | None,
    dry_run: bool,
    session_degraded: dict[str, str] | None = None,
) -> Path | None:
    return run_shared_turn(
        task=task,
        intent_name="chat",
        root=root,
        runners=runners,
        participant=participant,
        dry_run=dry_run,
        session_degraded=session_degraded,
    )


def write_shared_turn_packets(
    *,
    ledger: EventLedger,
    run_dir: Path,
    participants: list[str],
    task: str,
    intent: TurnIntent,
    brief_path: Path,
    round_number: int,
    peer_context: str,
) -> dict[str, str]:
    prompts: dict[str, str] = {}
    phase = f"{intent.name}.round{round_number}"
    for name in participants:
        packet = shared_turn_prompt(
            task=task,
            intent=intent,
            brief_path=brief_path,
            round_number=round_number,
            peer_context=peer_context,
            participant=name,
        )
        packet_path = ledger.write_packet(name, phase, packet)
        packet_ref = packet_path.relative_to(run_dir).as_posix()
        ledger.append(
            "packet.created",
            phase=phase,
            participant=name,
            refs={"packet": packet_ref},
            payload={"intent": intent.name, "round": round_number},
        )
        prompts[name] = shared_turn_packet_pointer(packet_path)
    ledger.render()
    return prompts


def shared_turn_packet_pointer(packet_path: Path) -> str:
    return (
        "COUNCLI_SHARED_TURN=1\n"
        f"COUNCLI_PACKET_FILE={packet_path}\n\n"
        "Read the packet file above and answer exactly according to it. "
        "Do not rely on this short pointer as the full task."
    )


def run_turn_round(
    *,
    root: Path,
    runners: dict[str, AgentRunner],
    prompts: dict[str, str],
    dry_run: bool,
    phase: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if dry_run:
        for name, runner in runners.items():
            console.print(f"[cyan]{phase}:{name} start[/]")
            results[name] = runner.run(prompts.get(name, ""), cwd=root, dry_run=True)
            print_participant_status(phase, name, results[name])
        return results
    if not runners:
        return results
    with ThreadPoolExecutor(max_workers=max(1, len(runners))) as pool:
        futures = {
            pool.submit(runner.run, prompts.get(name, ""), cwd=root, dry_run=False): name
            for name, runner in runners.items()
        }
        start_times = {name: time.monotonic() for name in runners}
        next_notice = {name: 10 for name in runners}
        for name in runners:
            console.print(f"[cyan]{phase}:{name} start[/]")
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            now = time.monotonic()
            for future in done:
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:  # pragma: no cover - adapter guard
                    results[name] = runner_unavailable_result(name, str(exc))
                print_participant_status(phase, name, results[name])
            for future in pending:
                name = futures[future]
                elapsed = int(now - start_times[name])
                if elapsed >= next_notice[name]:
                    console.print(f"[cyan]{phase}:{name} working {elapsed}s[/]")
                    next_notice[name] += 10
    return results


def shared_turn_prompt(
    *,
    task: str,
    intent: TurnIntent,
    brief_path: Path,
    round_number: int,
    peer_context: str,
    participant: str | None = None,
) -> str:
    parts = [
        "COUNCLI_SHARED_TURN=1",
        f"COUNCLI_INTENT={intent.name}",
        f"COUNCLI_PARTICIPANT={participant or 'unknown'}",
        "",
        "You are one participant in a councli shared room with other coding assistants.",
        "Councli is only the control plane: it records the blackboard and routes messages.",
        "Do not invent a fixed lifecycle. Follow the user's prompt and this turn intent.",
        "Do not edit files or run implementation commands in this turn.",
        intent.instruction,
        "",
        f"Round: {round_number}",
        f"Shared task brief: {brief_path}",
        "",
        "User prompt:",
        task,
    ]
    if peer_context:
        parts.extend(
            [
                "",
                "Visible blackboard from prior round(s):",
                peer_context,
                "",
                "React to the strongest useful points. If the council can answer now, do not ask for more rounds.",
            ]
        )
    else:
        parts.extend(
            [
                "",
                "This is the first round. Answer independently; do not claim to know what other participants think yet.",
            ]
        )
    parts.extend(
        [
            "",
            "End with this machine-readable trailer:",
            "COUNCLI_TRAILER",
            "continue: false",
            "recommend: none",
            "summary: one short line",
        ]
    )
    if intent.require_vote:
        parts.extend(["vote: your chosen option or answer", "confidence: 0.0-1.0"])
    return "\n".join(parts).rstrip() + "\n"


def parse_turn_trailer(output: str) -> tuple[str, dict[str, Any]]:
    match = TRAILER_PATTERN.search(output or "")
    if not match:
        return (output or "").strip(), default_turn_trailer()
    body = (output[: match.start()] or "").strip()
    values: dict[str, Any] = default_turn_trailer()
    for line in match.group("body").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        if key == "continue":
            values[key] = value.lower() in {"1", "true", "yes", "y"}
        elif key == "confidence":
            try:
                values[key] = float(value)
            except ValueError:
                values[key] = 0.0
        else:
            values[key] = value
    return body, values


def default_turn_trailer() -> dict[str, Any]:
    return {"continue": False, "recommend": "none", "summary": ""}


def render_peer_context(all_rounds: list[dict[str, dict[str, Any]]], *, per_participant_limit: int = 6000) -> str:
    lines: list[str] = []
    for index, round_data in enumerate(all_rounds, start=1):
        lines.append(f"## Round {index}")
        for name, data in round_data.items():
            result = data["result"]
            body = result.output if result.ok else result.error
            status = "ok" if result.ok else "skipped" if result.skipped else "failed"
            lines.extend([f"### {name} ({status})", ""])
            lines.append(bound_peer_body(body, limit=per_participant_limit))
            lines.append("")
        lines.append("")
    return "\n".join(lines).strip()


def bound_peer_body(body: str, *, limit: int) -> str:
    text = (body or "(empty)").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[truncated by councli after {limit} characters]"


def should_continue_shared_turn(*, intent: TurnIntent, round_number: int, parsed_round: dict[str, dict[str, Any]]) -> bool:
    if round_number >= intent.max_rounds:
        return False
    if intent.force_peer_round and round_number == 1:
        return True
    return any(bool(data.get("trailer", {}).get("continue")) for data in parsed_round.values())


def is_session_degrading_failure(result: Any) -> bool:
    return bool(
        not result.ok
        and not result.skipped
        and getattr(result, "failure_class", "") in {"auth_required", "model_unconfigured", "quota_unavailable"}
    )


def print_participant_status(phase: str, name: str, result: Any) -> None:
    if result.ok:
        console.print(f"[green]{phase}:{name} done[/]")
    elif result.skipped:
        console.print(f"[yellow]{phase}:{name} skipped[/]")
    else:
        console.print(f"[red]{phase}:{name} failed[/]")


def write_shared_turn_result(
    *,
    root: Path,
    run_dir: Path,
    ledger: EventLedger,
    name: str,
    result: Any,
    intent: TurnIntent,
    round_number: int,
    trailer: dict[str, Any],
) -> dict[str, Any]:
    status = "ok" if result.ok else "skipped" if result.skipped else "failed"
    suffix = "md" if result.ok else "skipped.txt" if result.skipped else "failed.txt"
    body = result.output if result.ok else "\n\n".join(part for part in [result.error, result.output] if part)
    phase = f"{intent.name}.round{round_number}"
    path = run_dir / "shared" / phase / f"{name}.{suffix}"
    write_text(path, (body or "(empty)").rstrip() + "\n")
    body_ref = path.relative_to(run_dir).as_posix()
    sidecar = build_response_sidecar(
        run_dir=run_dir,
        name=name,
        result=result,
        intent=intent,
        round_number=round_number,
        body_ref=body_ref,
        trailer=trailer,
    )
    sidecar_path = run_dir / "shared" / phase / f"{name}.response.json"
    write_json(sidecar_path, sidecar)
    sidecar_ref = sidecar_path.relative_to(run_dir).as_posix()
    ref = ledger.write_blob(phase, name, body or result.error or "(empty)", suffix="md" if result.ok else "txt")
    ledger.append(
        "response.received",
        phase=phase,
        participant=name,
        status=status,
        refs={"content" if result.ok else "error": ref, "sidecar": sidecar_ref},
        payload={
            "ok": result.ok,
            "skipped": result.skipped,
            "mode": "shared_turn",
            "intent": intent.name,
            "round": round_number,
            "command": result.command,
            "exit_code": result.exit_code,
            "error": result.error,
            "trailer": trailer,
            "sidecar": sidecar_ref,
            "failure_class": sidecar.get("failure_class"),
        },
    )
    append_project_event(
        root,
        "shared_turn.response",
        participant=name,
        status=status,
        payload={
            "run": run_dir.name,
            "ok": result.ok,
            "skipped": result.skipped,
            "error": result.error,
            "intent": intent.name,
            "round": round_number,
            "summary": trailer.get("summary"),
        },
        refs={"run": str(run_dir), "artifact": str(path)},
    )
    return sidecar


def build_response_sidecar(
    *,
    run_dir: Path,
    name: str,
    result: Any,
    intent: TurnIntent,
    round_number: int,
    body_ref: str,
    trailer: dict[str, Any],
) -> dict[str, Any]:
    status = "ok" if result.ok else "skipped" if result.skipped else "failed"
    vote_value = str(trailer.get("vote") or "").strip()
    confidence = trailer.get("confidence")
    if not isinstance(confidence, int | float):
        confidence = 0.0
    return {
        "schema_version": "councli.response.v1",
        "id": f"resp_{safe_response_id(run_dir.name)}_{safe_response_id(name)}_{intent.name}_round{round_number}",
        "request_id": run_dir.name,
        "kind": "participant.response",
        "participant": name,
        "intent": intent.name,
        "round": round_number,
        "status": status,
        "body_ref": body_ref,
        "summary": str(trailer.get("summary") or ""),
        "continue": bool(trailer.get("continue")),
        "recommend": str(trailer.get("recommend") or "none"),
        "vote": {
            "value": vote_value,
            "confidence": float(confidence),
            "valid": bool(result.ok and vote_value),
        },
        "failure_class": result.failure_class or ("" if result.ok else "skipped" if result.skipped else "launch_failed"),
        "exit_code": result.exit_code,
        "duration_seconds": round(float(result.duration_seconds or 0.0), 3),
        "command": result.command,
    }


def safe_response_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "item"


def print_shared_round_responses(
    parsed_round: dict[str, dict[str, Any]],
    selected_names: list[str],
    *,
    intent: TurnIntent,
    round_number: int,
) -> None:
    console.print(f"\n[bold]Responses[/] [dim]{intent.name} round {round_number}[/]")
    for name in selected_names:
        result = parsed_round[name]["result"]
        body = result.output if result.ok else result.error
        status = "ok" if result.ok else "skipped" if result.skipped else "failed"
        style = "dim" if result.ok else "yellow" if result.skipped else "red"
        console.print(f"  [bold]{name}[/] [{style}]{status}[/]: {compact_for_stdout(body, limit=180)}")


def synthesize_shared_turn(
    *,
    task: str,
    intent: TurnIntent,
    root: Path,
    run_dir: Path,
    runners: dict[str, AgentRunner],
    selected_names: list[str],
    all_rounds: list[dict[str, dict[str, Any]]],
    dry_run: bool,
    ledger: EventLedger,
    decision: dict[str, Any] | None,
) -> str:
    if not all_rounds:
        return "No assistant returned a usable response."
    latest_round = all_rounds[-1]
    results = {name: latest_round[name]["result"] for name in selected_names if name in latest_round}
    ok_names = [name for name in selected_names if results.get(name) and results[name].ok]
    if not ok_names:
        return "No assistant returned a usable response."
    if dry_run:
        answer = f"DRY RUN: would synthesize a unified {intent.name} response from participant answers."
        write_synthesis_artifact(
            run_dir=run_dir,
            ledger=ledger,
            body=answer,
            synthesizer="local",
            source_participants=ok_names,
            status="ok",
        )
        return answer
    if len(ok_names) == 1:
        only = ok_names[0]
        answer = f"Single-source answer from {only} (only one participant responded):\n\n{results[only].output.strip()}"
        write_synthesis_artifact(
            run_dir=run_dir,
            ledger=ledger,
            body=answer,
            synthesizer="local",
            source_participants=ok_names,
            status="ok",
        )
        return answer

    synthesizer_name = ok_names[0]
    try:
        synthesizer = shared_turn_runner(runners[synthesizer_name])
    except ValueError:
        fallback = local_shared_synthesis(intent=intent, names=ok_names, results=results, decision=decision)
        write_synthesis_artifact(
            run_dir=run_dir,
            ledger=ledger,
            body=fallback,
            synthesizer="local",
            source_participants=ok_names,
            status="ok",
        )
        return fallback

    prompt = synthesis_prompt(task=task, intent=intent, names=ok_names, all_rounds=all_rounds, decision=decision)
    result = run_turn_round(
        root=root,
        runners={synthesizer_name: synthesizer},
        prompts={synthesizer_name: prompt},
        dry_run=False,
        phase="synthesis",
    ).get(synthesizer_name, runner_unavailable_result(synthesizer_name, "synthesis did not return"))
    body, trailer = parse_turn_trailer(result.output if result.ok else "")
    if result.ok:
        result = replace(result, output=body)
    write_synthesis_artifact(
        run_dir=run_dir,
        ledger=ledger,
        body=result.output if result.ok else result.error,
        synthesizer=synthesizer_name,
        source_participants=ok_names,
        status="ok" if result.ok else "failed",
        trailer=trailer,
        result=result,
    )
    print_participant_status("synthesis", synthesizer_name, result)
    if result.ok and result.output.strip():
        return result.output.strip()
    fallback = local_shared_synthesis(intent=intent, names=ok_names, results=results, decision=decision)
    write_synthesis_artifact(
        run_dir=run_dir,
        ledger=ledger,
        body=fallback,
        synthesizer="local",
        source_participants=ok_names,
        status="ok",
    )
    return fallback


def write_synthesis_artifact(
    *,
    run_dir: Path,
    ledger: EventLedger,
    body: str,
    synthesizer: str,
    source_participants: list[str],
    status: str,
    trailer: dict[str, Any] | None = None,
    result: Any | None = None,
) -> None:
    body_text = (body or "(empty)").rstrip() + "\n"
    body_path = run_dir / "synthesis" / "synthesis.md"
    write_text(body_path, body_text)
    sidecar = {
        "schema_version": "councli.response.v1",
        "id": f"resp_{safe_response_id(run_dir.name)}_synthesis",
        "request_id": run_dir.name,
        "kind": "synthesis.response",
        "participant": f"synthesis-{synthesizer}",
        "intent": "synthesis",
        "round": 0,
        "status": status,
        "body_ref": body_path.relative_to(run_dir).as_posix(),
        "summary": str((trailer or {}).get("summary") or compact_for_stdout(body, limit=160)),
        "continue": False,
        "recommend": "none",
        "vote": {"value": "", "confidence": 0.0, "valid": False},
        "failure_class": getattr(result, "failure_class", "") if result is not None else "",
        "exit_code": getattr(result, "exit_code", None) if result is not None else None,
        "duration_seconds": round(float(getattr(result, "duration_seconds", 0.0) or 0.0), 3),
        "command": getattr(result, "command", []) if result is not None else [],
        "source_participants": source_participants,
        "synthesizer": synthesizer,
    }
    sidecar_path = run_dir / "synthesis" / "synthesis.response.json"
    write_json(sidecar_path, sidecar)
    content_ref = ledger.write_blob("synthesis", "synthesis", body_text)
    ledger.append(
        "response.received",
        phase="synthesis",
        participant=f"synthesis-{synthesizer}",
        status=status,
        refs={
            "content": content_ref,
            "sidecar": sidecar_path.relative_to(run_dir).as_posix(),
        },
        payload={
            "mode": "shared_turn",
            "intent": "synthesis",
            "source_participants": source_participants,
            "synthesizer": synthesizer,
            "sidecar": sidecar_path.relative_to(run_dir).as_posix(),
        },
    )
    ledger.render()


def synthesis_prompt(
    *,
    task: str,
    intent: TurnIntent,
    names: list[str],
    all_rounds: list[dict[str, dict[str, Any]]],
    decision: dict[str, Any] | None,
) -> str:
    parts = [
        "COUNCLI_SHARED_TURN_SYNTHESIS=1",
        f"COUNCLI_INTENT={intent.name}",
        "",
        "You are the temporary synthesizer for a councli shared room.",
        "Use the participant responses below to answer the user as one unified council voice.",
        "Do not mention lifecycle phases or implementation unless the user asked for them.",
        "Be concise and direct.",
        "Name the participants whose outputs support the answer.",
        "If participants disagree in a meaningful way, state the disagreement instead of hiding it.",
        "Do not claim consensus when the evidence only supports a partial or single-source answer.",
        "",
        f"User prompt:\n{task}",
        "",
        "Participant responses:",
    ]
    for index, round_data in enumerate(all_rounds, start=1):
        parts.append(f"\n## Round {index}")
        for name in names:
            if name not in round_data:
                continue
            result = round_data[name]["result"]
            if result.ok:
                parts.extend([f"\n### {name}", result.output.strip()])
    if decision:
        parts.extend(["", "Explicit vote result:", json.dumps(decision, sort_keys=True)])
    return "\n".join(parts).rstrip() + "\n"


def local_shared_synthesis(
    *,
    intent: TurnIntent,
    names: list[str],
    results: dict[str, Any],
    decision: dict[str, Any] | None,
) -> str:
    if decision and decision.get("winner"):
        return f"The council vote selected {decision['winner']}: {decision.get('reason') or 'majority result'}."
    lines = ["The available assistants responded with these combined points:"]
    for name in names:
        lines.append(f"- {name}: {compact_for_stdout(results[name].output, limit=180)}")
    if intent.name == "deliberate":
        lines.append("Use these points as the shared recommendation; ask for /vote only if you want a formal decision.")
    return "\n".join(lines)


def decide_shared_vote(parsed_round: dict[str, dict[str, Any]], selected_names: list[str]) -> dict[str, Any]:
    votes: dict[str, str] = {}
    abstentions: dict[str, str] = {}
    for name in selected_names:
        data = parsed_round.get(name)
        if not data:
            abstentions[name] = "no response"
            continue
        result = data["result"]
        trailer = data.get("trailer") or {}
        if not result.ok:
            abstentions[name] = result.error or "unavailable"
            continue
        sidecar = data.get("sidecar") or {}
        vote_data = sidecar.get("vote") if isinstance(sidecar, dict) else None
        vote = str((vote_data or {}).get("value") or "").strip() if isinstance(vote_data, dict) else ""
        if not vote:
            vote = str(trailer.get("vote") or "").strip()
        if not vote and str(result.output or "").lstrip().startswith("DRY RUN:"):
            abstentions[name] = "dry run"
            continue
        if not vote:
            abstentions[name] = "missing structured vote"
            continue
        if vote:
            votes[name] = vote
        else:
            abstentions[name] = "empty vote"
    counts: dict[str, int] = {}
    for vote in votes.values():
        counts[vote] = counts.get(vote, 0) + 1
    winner = None
    if counts:
        winner = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return {
        "approved": bool(winner),
        "winner": winner,
        "votes": votes,
        "counts": counts,
        "abstentions": abstentions,
        "reason": "explicit /vote result" if winner else "no usable votes",
    }


def print_shared_vote_decision(decision: dict[str, Any]) -> None:
    if decision.get("winner"):
        console.print(f"\n[bold green]Vote result:[/] {decision['winner']}")
    else:
        console.print("\n[bold yellow]Vote result:[/] no usable vote")
    if decision.get("counts"):
        console.print(f"Counts: {decision['counts']}")
    if decision.get("abstentions"):
        console.print(f"Abstentions: {decision['abstentions']}")


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def record_shared_turn(
    *,
    root: Path,
    run_dir: Path,
    task: str,
    intent: TurnIntent,
    final_answer: str,
    results: dict[str, Any],
    decision: dict[str, Any] | None,
) -> None:
    ok = sorted(name for name, result in results.items() if result.ok)
    failed = sorted(name for name, result in results.items() if not result.ok and not result.skipped)
    skipped = sorted(name for name, result in results.items() if result.skipped)
    summary = f"{run_dir.name}: {compact_for_stdout(task, limit=120)} => responded={','.join(ok) or '-'}"
    append_project_event(
        root,
        "turn.completed",
        status="ok" if ok else "failed",
        payload={
            "run": run_dir.name,
            "task": task,
            "summary": summary,
            "mode": "shared_turn",
            "intent": intent.name,
            "responded": ok,
            "failed": failed,
            "skipped": skipped,
            "final_answer": compact_for_stdout(final_answer, limit=500),
            "decision": decision,
        },
        refs={"run": str(run_dir), "blackboard": str(run_dir / "blackboard.md")},
    )


def run_visible_council_turn(
    *,
    task: str,
    root: Path,
    runners: dict[str, AgentRunner],
    participant: list[str] | None,
    dry_run: bool,
    run_kind: str = "chat",
) -> Any:
    config = load_config(root)
    selected_names = participant or [
        name
        for name, runner in runners.items()
        if runner.health().available
    ]
    if not selected_names:
        console.print("[red]No participants selected or available.[/]")
        return None

    run_dir = new_run_dir(root, run_kind)
    write_task_brief(root, task=task, task_id=run_dir.name, run_dir=run_dir)
    console.rule(f"[bold]turn {run_dir.name}[/]")
    console.print(f"[bold]Task:[/] {task}")
    console.print(f"[bold]Participants:[/] {', '.join(selected_names)}")
    result = run_blackboard_council(
        task=task,
        runners=runners,
        root=root,
        run_dir=run_dir,
        participants=selected_names,
        dry_run=dry_run,
        min_confidence=config.consensus.min_confidence,
        progress=make_turn_progress_printer(),
    )
    record_interactive_turn(root=root, run_dir=run_dir, task=task, result=result)
    console.print(f"[dim]Blackboard:[/] {run_dir / 'blackboard.md'}")
    return result


def make_turn_progress_printer():
    def progress(event: str, state: Any, payload: dict[str, Any]) -> None:
        if event == "phase_started":
            phase = str(payload.get("phase") or "")
            participants = payload.get("participants") or []
            console.print(f"\n[bold cyan]{phase.upper()}[/] asking {', '.join(str(name) for name in participants)}")
            return
        if event == "participant_response":
            phase = str(payload.get("phase") or "")
            participant = str(payload.get("participant") or "")
            ok = bool(payload.get("ok"))
            status = "done" if ok else "failed"
            style = "green" if ok else "red"
            console.print(f"[{style}]{phase}:{participant} {status}[/]")
            return
        if event == "phase_completed":
            phase = str(payload.get("phase") or "")
            print_phase_blackboard_summary(state, phase)
            return
        if event == "plans_registered":
            plan_ids = payload.get("plan_ids") or []
            if plan_ids:
                console.print(f"\n[bold]Plans:[/] {', '.join(str(plan) for plan in plan_ids)}")
            return
        if event == "decision":
            print_consensus_summary(payload.get("decision") or {})
            return

    return progress


def print_phase_blackboard_summary(state: Any, phase: str) -> None:
    items = [item for item in getattr(state, "items", []) if item.phase == phase]
    if not items:
        return
    console.print(f"[bold]{phase.title()} blackboard[/]")
    for item in items:
        body = item.content if item.ok else item.error
        prefix = "ok" if item.ok else "failed"
        style = "dim" if item.ok else "red"
        console.print(f"  [bold]{item.participant}[/] [{style}]{prefix}[/]: {compact_for_stdout(body, limit=180)}")


def print_consensus_summary(decision: dict[str, Any]) -> None:
    approved = bool(decision.get("approved"))
    status = "approved" if approved else "not approved"
    style = "green" if approved else "yellow"
    console.print(f"\n[bold {style}]Consensus {status}[/]")
    console.print(f"Selected plan: {decision.get('selected_plan') or '-'}")
    console.print(f"Executor: {decision.get('selected_executor') or '-'}")
    console.print(f"Reason: {compact_for_stdout(str(decision.get('reason') or '-'), limit=500)}")


def compact_for_stdout(value: str, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "(empty)"
    if len(text) > limit:
        return text[:limit].rstrip() + " ..."
    return text


def record_interactive_turn(*, root: Path, run_dir: Path, task: str, result: Any) -> None:
    decision = result.decision
    summary = (
        f"{run_dir.name}: {compact_for_stdout(task, limit=120)} "
        f"=> plan={decision.get('selected_plan') or '-'}, executor={decision.get('selected_executor') or '-'}, "
        f"approved={bool(decision.get('approved'))}"
    )
    append_project_event(
        root,
        "turn.completed",
        status="ok" if decision.get("approved") else "failed",
        payload={
            "run": run_dir.name,
            "task": task,
            "summary": summary,
            "selected_plan": decision.get("selected_plan"),
            "selected_executor": decision.get("selected_executor"),
            "approved": bool(decision.get("approved")),
        },
        refs={"run": str(run_dir), "blackboard": str(run_dir / "blackboard.md")},
    )


@app.command()
def chat(
    root: RootOpt = Path.cwd(),
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", "-p", help="Participant name. Repeat to choose multiple."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Write blackboard artifacts without invoking agent CLIs."),
    ] = False,
) -> None:
    """Open a small interactive councli prompt."""
    config = load_config(root, auto_init=True)
    runners = build_runners(config.agents)
    print_mascot()
    console.print("[bold]councli interactive[/]")
    console.print("Type normally for shared conversation, /deliberate or /vote for explicit governance, /help for commands.")
    print_available_participants(runners)
    session_degraded: dict[str, str] = {}

    while True:
        try:
            line = input("councli> ").strip()
        except EOFError:
            console.print("")
            return
        except KeyboardInterrupt:
            console.print("")
            return
        if not line:
            continue
        if line.startswith("//"):
            line = line[1:]
        elif line.startswith("/"):
            if handle_chat_command(
                line,
                root=root,
                runners=runners,
                participant=participant,
                dry_run=dry_run,
                session_degraded=session_degraded,
            ):
                return
            continue

        run_conversation_turn(
            task=line,
            root=root,
            runners=runners,
            participant=participant,
            dry_run=dry_run,
            session_degraded=session_degraded,
        )


@app.command(hidden=True)
def run(
    task: Annotated[str, typer.Argument(help="Implementation task for the agent council.")],
    root: RootOpt = Path.cwd(),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Exercise the workflow without invoking agent CLIs."),
    ] = False,
    allow_dirty: Annotated[
        bool,
        typer.Option("--allow-dirty", help="Allow running when the main worktree has existing changes."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Run executor even when consensus is not approved."),
    ] = False,
) -> None:
    """Deliberate, choose an executor, and run that executor in a git worktree."""
    require_experimental("councli run")
    require_git_repo(root)
    if not allow_dirty:
        ensure_clean_enough(root)

    config = load_config(root)
    runners = build_runners(config.agents)
    run_dir = new_run_dir(root, "run")
    write_task_brief(root, task=task, task_id=run_dir.name, run_dir=run_dir)
    console.print(f"[bold]Run:[/] {run_dir}")

    council_result = run_blackboard_council(
        task=task,
        runners=runners,
        root=root,
        run_dir=run_dir,
        dry_run=dry_run,
        complete_run=False,
        min_confidence=config.consensus.min_confidence,
    )
    decision = council_result.decision

    if not decision.get("approved") and not force:
        console.print("[yellow]Consensus not approved; executor not started.[/]")
        console.print(f"Reason: {decision.get('reason')}")
        console.print(f"Blackboard: {run_dir / 'blackboard.md'}")
        ledger = EventLedger(run_dir, run_id=run_dir.name)
        ledger.append("run.completed", payload={"approved": False, "implemented": False})
        ledger.render()
        raise typer.Exit(code=2)

    executor = decision.get("selected_executor")
    if not executor or executor not in runners:
        console.print("[red]No valid executor selected.[/]")
        ledger = EventLedger(run_dir, run_id=run_dir.name)
        ledger.append("run.completed", payload={"approved": decision.get("approved", False), "implemented": False})
        ledger.render()
        raise typer.Exit(code=2)

    ledger = EventLedger(run_dir, run_id=run_dir.name)
    state = read_json(run_dir / "state.json")
    selected_plan = decision.get("selected_plan")
    selected_plan_content = ""
    if selected_plan:
        selected_plan_content = ((state.get("plans") or {}).get(selected_plan) or {}).get("content", "")

    tried: set[str] = set()
    final_status = "rounds_exhausted"
    final_result_ok = False
    final_error = ""
    revision_concerns: list[str] = []
    last_worktree = None
    last_result = None
    last_diff_path = run_dir / "implementation" / "diff.patch"

    for attempt in range(1, config.consensus.max_rounds + 1):
        try:
            executor_runner = implementation_runner(runners[executor])
        except ValueError as exc:
            final_status = "executor_unavailable"
            final_error = str(exc)
            break

        try:
            worktree = create_worktree(root, run_name=f"{run_dir.name}-attempt{attempt}", executor=executor)
        except RuntimeError as exc:
            final_status = "worktree_failed"
            final_error = str(exc)
            break
        last_worktree = worktree

        transcript = (run_dir / "blackboard.md").read_text(encoding="utf-8")
        ledger.append(
            "implementation.started",
            participant=executor,
            payload={
                "attempt": attempt,
                "executor": executor,
                "selected_plan": selected_plan,
                "worktree": str(worktree.path),
                "branch": worktree.branch,
                "base_ref": worktree.base_ref,
            },
        )
        ledger.render()
        result = run_executor(
            task=task,
            runner=executor_runner,
            worktree=worktree.path,
            transcript=transcript,
            run_dir=run_dir,
            selected_plan=selected_plan,
            selected_plan_content=selected_plan_content,
            revision_concerns=revision_concerns,
            dry_run=dry_run,
        )
        last_result = result
        diff_text = diff(worktree.path, base_ref=worktree.base_ref)
        attempt_dir = run_dir / "implementation" / f"attempt-{attempt}"
        write_text(attempt_dir / "diff.patch", diff_text)
        write_text(
            attempt_dir / "worktree.txt",
            f"branch: {worktree.branch}\nbase_ref: {worktree.base_ref}\npath: {worktree.path}\n",
        )
        write_text(last_diff_path, diff_text)
        write_text(
            run_dir / "implementation" / "worktree.txt",
            f"branch: {worktree.branch}\nbase_ref: {worktree.base_ref}\npath: {worktree.path}\n",
        )
        result_ref = ledger.write_blob("implementation", f"{executor}-attempt-{attempt}-result", render_result(result))
        diff_ref = ledger.write_blob("implementation", f"attempt-{attempt}-diff", diff_text, suffix="patch")
        ledger.append(
            "implementation.diff_submitted",
            participant=executor,
            status="ok" if result.ok else "failed",
            refs={"result": result_ref, "diff": diff_ref},
            payload={
                "attempt": attempt,
                "executor": executor,
                "selected_plan": selected_plan,
                "worktree": str(worktree.path),
                "branch": worktree.branch,
                "base_ref": worktree.base_ref,
                "ok": result.ok,
            },
        )
        ledger.render()

        if not dry_run and not diff_text.strip():
            final_status = "no_changes"
            final_error = "executor produced no reviewable diff"
            break

        review_decision = run_review_phase(
            council_result.state,
            runners,
            executor=executor,
            attempt=attempt,
            selected_plan=selected_plan,
            diff_ref=diff_ref,
            result_ref=result_ref,
            dry_run=dry_run,
            min_confidence=config.consensus.min_confidence,
        )
        verdict = review_decision.get("verdict")
        if verdict == "accepted":
            final_status = "accepted"
            final_result_ok = result.ok
            break
        if verdict == "unreviewed_implementation":
            final_status = "unreviewed_implementation"
            final_result_ok = result.ok
            break
        if verdict == "replace":
            tried.add(executor)
            replacement = next_executor(
                decision.get("executor_votes", {}),
                exclude=tried,
                participants=council_result.participants,
            )
            if replacement is None:
                final_status = "no_executor_left"
                final_error = "review requested replacement, but no alternate executor is available"
                break
            executor = replacement
            revision_concerns = review_decision.get("blocking_concerns", [])
            continue
        if verdict == "revise":
            revision_concerns = review_decision.get("blocking_concerns", [])
            continue
        final_status = "needs_user"
        final_error = review_decision.get("reason", "review did not reach a deterministic outcome")
        break

    implemented = final_status in {"accepted", "unreviewed_implementation"} and final_result_ok
    ledger.append(
        "run.completed",
        payload={
            "approved": decision.get("approved", False),
            "implemented": implemented,
            "status": final_status,
            "error": final_error,
        },
    )
    ledger.render()

    if final_error:
        console.print(f"[yellow]Run ended:[/] {final_status}: {final_error}")
    console.print(f"Executor: {executor}")
    if last_worktree is not None:
        console.print(f"Worktree: {last_worktree.path}")
        console.print(f"Branch: {last_worktree.branch}")
    console.print(f"Executor status: {'ok' if last_result and last_result.ok else 'failed'}")
    console.print(f"Review status: {final_status}")
    console.print(f"Diff: {last_diff_path}")
    if not implemented:
        raise typer.Exit(code=2)


@app.command("apply", hidden=True)
def apply_run(
    run: Annotated[str, typer.Argument(help="Run id, unique prefix, or 'latest'.")] = "latest",
    root: RootOpt = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Check whether the patch applies without changing files.")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Allow applying even when the current commit differs from the run base."),
    ] = False,
    allow_unreviewed: Annotated[
        bool,
        typer.Option("--allow-unreviewed", help="Allow applying a single-participant unreviewed implementation."),
    ] = False,
) -> None:
    """Apply an implemented councli run patch to the current worktree."""
    require_experimental("councli apply")
    require_git_repo(root)
    ensure_clean_enough(root)
    run_dir = resolve_run_dir(root, run)
    state = load_run_state(run_dir)
    run_completed = state.get("run_completed") or {}
    implementation = state.get("implementation") or {}
    review_decision = state.get("review_decision") or {}

    if not run_completed.get("implemented"):
        console.print(f"[red]Run is not implemented:[/] {run_dir.name}")
        raise typer.Exit(code=2)

    status = str(run_completed.get("status") or "")
    review_verdict = str(review_decision.get("verdict") or "")
    if review_verdict != "accepted":
        if not (allow_unreviewed and status == "unreviewed_implementation"):
            console.print(
                "[red]Run is not accepted by peer review.[/] "
                "Use --allow-unreviewed only for intentional single-participant runs."
            )
            raise typer.Exit(code=2)

    base_ref = str(implementation.get("base_ref") or "")
    if not base_ref:
        console.print("[red]Run is missing implementation base_ref; cannot apply safely.[/]")
        raise typer.Exit(code=2)
    current = current_commit(root)
    if current != base_ref and not force:
        console.print(f"[red]Current commit differs from run base.[/] current={current} base={base_ref}")
        console.print("Rebase/checkout the base commit or rerun with --force after manual review.")
        raise typer.Exit(code=2)

    patch_path = run_dir / "implementation" / "diff.patch"
    try:
        patch = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not read patch:[/] {patch_path}: {exc}")
        raise typer.Exit(code=2) from exc
    if not patch.strip():
        console.print("[red]Implementation patch is empty.[/]")
        raise typer.Exit(code=2)

    check = apply_unified_diff(root, patch, check=True)
    if check.returncode != 0:
        console.print("[red]Patch does not apply cleanly.[/]")
        console.print((check.stderr or check.stdout).strip())
        raise typer.Exit(code=2)

    if dry_run:
        console.print(f"[green]Patch applies cleanly:[/] {run_dir.name}")
        console.print(f"Patch: {patch_path}")
        return

    applied = apply_unified_diff(root, patch, check=False)
    if applied.returncode != 0:
        console.print("[red]Patch apply failed.[/]")
        console.print((applied.stderr or applied.stdout).strip())
        raise typer.Exit(code=2)

    ledger = EventLedger(run_dir, run_id=run_dir.name)
    ledger.append(
        "implementation.applied",
        payload={
            "root": str(root),
            "base_ref": base_ref,
            "current_before_apply": current,
            "patch": str(patch_path),
        },
    )
    ledger.render()
    console.print(f"[green]Applied:[/] {run_dir.name}")
    console.print(f"Patch: {patch_path}")


def print_available_participants(runners: dict[str, AgentRunner], *, title: str = "Configured participants") -> None:
    table = Table(title=title, expand=False)
    table.add_column("Name")
    table.add_column("Backend")
    table.add_column("Status")
    for name, runner in runners.items():
        health = runner.health()
        table.add_row(
            name,
            health.backend,
            "available" if health.available else health.reason,
        )
    console.print(table)


def handle_chat_command(
    line: str,
    *,
    root: Path,
    runners: dict[str, AgentRunner],
    participant: list[str] | None,
    dry_run: bool,
    session_degraded: dict[str, str] | None = None,
) -> bool:
    parts = line.split()
    command = parts[0].lower()
    if command in {"/quit", "/exit"}:
        return True
    if command == "/help":
        console.print(
            "Commands: /help, /doctor, /status, /show [run|latest] [--blackboard], "
            "/sessions, /assistant <name> [instance], /broadcast <prompt>, /brief [task], "
            "/deliberate <prompt>, /vote <prompt>, /council <prompt>, /quit"
        )
        console.print("Normal lines run a shared conversation turn. Slash commands opt into stronger coordination.")
        console.print("/assistant attaches to a native session; press Ctrl-] to return.")
        return False
    if command == "/doctor":
        doctor(root=root)
        return False
    if command == "/status":
        status(root=root)
        return False
    if command == "/sessions":
        sessions_list(root=root)
        return False
    if command == "/brief":
        task = line[len(parts[0]) :].strip() or None
        show_or_create_brief(root=root, task=task)
        return False
    if command in {"/assistant", "/agent"}:
        if len(parts) < 2:
            console.print("[yellow]Usage:[/] /assistant <name>")
            return False
        name = parts[1]
        runner = runners.get(name)
        if runner is None:
            console.print(f"[red]Unknown agent:[/] {name}")
            return False
        if not adapter_supports_intent(runner, "assistant"):
            console.print(f"[red]{name} does not support native assistant attach[/]")
            return False
        if dry_run:
            config = load_config(root)
            console.print(f"[cyan]DRY RUN:[/] would attach to {name} ({runner.session_name_for(root, prefix=config.native.session_prefix)})")
            return False
        if not supports_native_session(runner):
            console.print(f"[red]{name} has no native start_command configured[/]")
            return False
        try:
            instance = parts[2] if len(parts) > 2 else None
            attach_agent_session(root=root, name=name, runner=runner, instance=instance)
        except RuntimeError as exc:
            console.print(f"[red]Could not attach {name}:[/] {exc}")
        return False
    if command == "/broadcast":
        prompt = line[len(parts[0]) :].strip()
        if not prompt:
            console.print("[yellow]Usage:[/] /broadcast <prompt>")
            return False
        config = load_config(root)
        run_dir, results = run_broadcast_round(
            root=root,
            runners=runners,
            prompt=prompt,
            participants=participant,
            dry_run=dry_run,
            min_confidence=config.consensus.min_confidence,
        )
        ok, failed, skipped = summarize_broadcast_results(results)
        console.print(f"[bold]Broadcast:[/] {run_dir}")
        console.print("Mode: headless subprocess broadcast; active tmux assistant sessions are not fed this prompt.")
        console.print("Retry policy: none. Failures and policy skips are recorded separately.")
        console.print(f"Responded: {', '.join(ok) if ok else '-'}")
        console.print(f"Failed: {', '.join(failed) if failed else '-'}")
        console.print(f"Skipped: {', '.join(skipped) if skipped else '-'}")
        console.print(f"Blackboard: {run_dir / 'blackboard.md'}")
        return False
    if command in {"/council", "/deliberate", "/vote"}:
        task = line[len(parts[0]) :].strip()
        if not task:
            console.print(f"[yellow]Usage:[/] {command} <prompt>")
            return False
        intent_name = "vote" if command == "/vote" else "deliberate"
        run_shared_turn(
            task=task,
            intent_name=intent_name,
            root=root,
            runners=runners,
            participant=participant,
            dry_run=dry_run,
            session_degraded=session_degraded,
        )
        return False
    if command == "/show":
        run_id = "latest"
        show_blackboard = False
        for part in parts[1:]:
            if part == "--blackboard":
                show_blackboard = True
            else:
                run_id = part
        show(run=run_id, root=root, blackboard=show_blackboard)
        return False
    console.print(f"[yellow]Unknown councli command:[/] {command}. Type /help.")
    return False


def implementation_runner(runner: AgentRunner) -> AgentRunner:
    if runner.config.backend != "tmux":
        return runner
    if not any("{prompt}" in part for part in runner.config.command):
        raise ValueError(
            f"{runner.name} is tmux-backed and has no non-interactive command containing {{prompt}}"
        )
    return AgentRunner(runner.name, runner.config.model_copy(update={"backend": "exec"}))


@app.command()
def status(
    root: RootOpt = Path.cwd(),
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum runs to show.")] = 20,
) -> None:
    """Show recent councli runs."""
    runs = list_run_dirs(root)
    if not runs:
        console.print("No runs yet.")
        return

    table = Table(title="Recent councli runs", expand=False)
    table.add_column("Run", no_wrap=True)
    table.add_column("Task", no_wrap=True)
    table.add_column("Participants", no_wrap=True)
    table.add_column("Decision")
    table.add_column("Review")
    table.add_column("Implemented")
    table.add_column("Applied")
    for run in runs[:limit]:
        summary = run_summary(run)
        table.add_row(
            run.name,
            truncate(summary["task"], 48),
            summary["participants"],
            summary["decision"],
            summary["review"],
            summary["implemented"],
            summary["applied"],
        )
    console.print(table)


@app.command()
def show(
    run: Annotated[str, typer.Argument(help="Run id, unique prefix, or 'latest'.")] = "latest",
    root: RootOpt = Path.cwd(),
    blackboard: Annotated[
        bool,
        typer.Option("--blackboard/--no-blackboard", help="Print the full blackboard after the summary."),
    ] = False,
) -> None:
    """Show a councli run summary and artifact paths."""
    run_dir = resolve_run_dir(root, run)
    summary = run_summary(run_dir)
    state = load_run_state(run_dir)

    console.print(f"[bold]Run:[/] {run_dir.name}")
    console.print(f"Path: {run_dir}")
    console.print(f"Task: {summary['task']}")
    console.print(f"Participants: {summary['participants']}")
    console.print(f"Decision: {summary['decision']}")
    console.print(f"Review: {summary['review']}")
    console.print(f"Implemented: {summary['implemented']}")
    console.print(f"Applied: {summary['applied']}")
    console.print(f"Blackboard: {run_dir / 'blackboard.md'}")
    console.print(f"State: {run_dir / 'state.json'}")
    console.print(f"Events: {run_dir / 'events.jsonl'}")

    implementation = state.get("implementation") or {}
    if implementation.get("worktree"):
        console.print(f"Worktree: {implementation.get('worktree')}")
    applied = implementation.get("applied") if isinstance(implementation, dict) else None
    if applied:
        console.print(f"Applied root: {applied.get('root')}")
    if (run_dir / "implementation" / "diff.patch").exists():
        console.print(f"Diff: {run_dir / 'implementation' / 'diff.patch'}")

    if blackboard:
        path = run_dir / "blackboard.md"
        try:
            console.rule("Blackboard")
            console.print(path.read_text(encoding="utf-8").rstrip())
        except OSError as exc:
            console.print(f"[red]Could not read blackboard:[/] {exc}")
            raise typer.Exit(code=2) from exc


def list_run_dirs(root: Path) -> list[Path]:
    runs_dir = root / ".councli" / "runs"
    if not runs_dir.exists():
        return []
    return sorted([path for path in runs_dir.iterdir() if path.is_dir()], reverse=True)


def resolve_run_dir(root: Path, run: str) -> Path:
    runs = list_run_dirs(root)
    if not runs:
        console.print("No runs yet.")
        raise typer.Exit(code=2)
    if run == "latest":
        return runs[0]
    exact = root / ".councli" / "runs" / run
    if exact.exists() and exact.is_dir():
        return exact
    matches = [path for path in runs if path.name.startswith(run)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(f"[red]Ambiguous run prefix:[/] {run}")
        for path in matches[:10]:
            console.print(f"- {path.name}")
        raise typer.Exit(code=2)
    console.print(f"[red]Unknown run:[/] {run}")
    raise typer.Exit(code=2)


def load_run_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "state.json"
    if path.exists():
        try:
            loaded = read_json(path)
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return {}
    decision = run_dir / "decision.json"
    if decision.exists():
        try:
            loaded = read_json(decision)
            return {"decision": loaded if isinstance(loaded, dict) else loaded}
        except (OSError, ValueError):
            return {}
    return {}


def run_summary(run_dir: Path) -> dict[str, str]:
    state = load_run_state(run_dir)
    participants = state.get("participants") or {}
    decision = state.get("decision") or {}
    review_decision = state.get("review_decision") or {}
    run_completed = state.get("run_completed") or {}
    implemented = run_completed.get("implemented")
    if implemented is None:
        implemented_text = "-"
    else:
        implemented_text = "yes" if implemented else "no"
    return {
        "task": str(state.get("task") or "(unknown)"),
        "participants": ", ".join(participants) if isinstance(participants, dict) and participants else "-",
        "decision": str(decision.get("status") or ("present" if decision else "-")),
        "review": str(review_decision.get("verdict") or "-"),
        "implemented": implemented_text,
        "applied": "yes" if isinstance(state.get("implementation"), dict) and state["implementation"].get("applied") else "no",
    }


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


if __name__ == "__main__":
    app()

from __future__ import annotations

import shutil
import shlex
import signal
import subprocess
import time
import uuid
import os
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from councli.config import AgentConfig


AUTH_ERROR_MARKERS = (
    "authentication",
    "auth",
    "api key",
    "not logged in",
    "login",
    "permission denied",
    "quota",
    "rate limit",
    "payment",
    "subscription",
)
MODEL_ERROR_MARKERS = (
    "no model",
    "model not configured",
    "default_model",
    "provider",
    "no provider",
)
QUOTA_ERROR_MARKERS = (
    "quota",
    "rate limit",
    "payment",
    "billing",
    "subscription",
)


@dataclass(frozen=True)
class AgentHealth:
    name: str
    enabled: bool
    binary: str
    path: str | None
    available: bool
    reason: str
    backend: str = "exec"
    version: str | None = None
    version_status: str = "not_checked"
    readiness_status: str = "not_configured"
    readiness_detail: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    name: str
    ok: bool
    skipped: bool
    exit_code: int | None
    output: str
    error: str
    command: list[str]
    duration_seconds: float = 0.0
    failure_class: str = ""


class AgentRunner:
    def __init__(self, name: str, config: AgentConfig) -> None:
        self.name = name
        self.config = config

    def health(self) -> AgentHealth:
        if not self.config.enabled:
            return AgentHealth(
                name=self.name,
                enabled=False,
                binary=self.config.binary,
                path=None,
                available=False,
                reason="disabled in config",
                backend=self.config.backend,
                version_status="not_checked",
            )
        if self.config.backend == "tmux" and not shutil.which("tmux"):
            return AgentHealth(
                name=self.name,
                enabled=True,
                binary=self.config.binary,
                path=None,
                available=False,
                reason="tmux not found on PATH",
                backend=self.config.backend,
                version_status="not_checked",
            )
        path = shutil.which(self.config.binary)
        if not path:
            return AgentHealth(
                name=self.name,
                enabled=True,
                binary=self.config.binary,
                path=None,
                available=False,
                reason="binary not found on PATH",
                backend=self.config.backend,
                version_status="missing_binary",
            )
        version, version_status = self.probe_version(path)
        ready, readiness_status, readiness_detail = self.probe_readiness(path)
        return AgentHealth(
            name=self.name,
            enabled=True,
            binary=self.config.binary,
            path=path,
            available=ready,
            reason="available" if ready else readiness_detail or readiness_status,
            backend=self.config.backend,
            version=version,
            version_status=version_status,
            readiness_status=readiness_status,
            readiness_detail=readiness_detail,
        )

    def probe_version(self, binary_path: str) -> tuple[str | None, str]:
        command = self.config.version_command
        if not command:
            return None, "not_configured"
        proc, status = self.run_probe(command, binary_path=binary_path, timeout_seconds=self.config.probe_timeout_seconds)
        if proc is None:
            return None, status
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            return compact_one_line(output), "failed"
        return compact_one_line(output), "ok"

    def probe_readiness(self, binary_path: str) -> tuple[bool, str, str | None]:
        command = self.config.readiness_command
        if not command:
            return True, "not_configured", None
        proc, status = self.run_probe(command, binary_path=binary_path, timeout_seconds=self.config.readiness_timeout_seconds)
        if proc is None:
            detail = f"readiness probe {status.replace('_', ' ')}"
            return False, status, detail
        output = (proc.stdout or proc.stderr or "").strip()
        compact = compact_one_line(output)
        if proc.returncode == 0:
            return True, "ok", compact
        failure_class = classify_agent_failure(output)
        if failure_class == "launch_failed":
            failure_class = "readiness_failed"
        detail = f"readiness probe failed: {compact}" if compact else "readiness probe failed"
        return False, failure_class, detail

    def run_probe(
        self,
        command: list[str],
        *,
        binary_path: str,
        timeout_seconds: int,
    ) -> tuple[subprocess.CompletedProcess[str] | None, str]:
        try:
            resolved_command = [
                binary_path if index == 0 and part == self.config.binary else part
                for index, part in enumerate(command)
            ]
            proc = subprocess.run(
                resolved_command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError:
            return None, "launch_failed"
        return proc, "ok"

    def render_command(self, prompt: str) -> list[str]:
        return [part.replace("{prompt}", prompt) for part in self.config.command]

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        dry_run: bool = False,
        output_path: Path | None = None,
    ) -> AgentRunResult:
        if self.config.backend == "tmux":
            return self._run_tmux(prompt, cwd=cwd, dry_run=dry_run, output_path=output_path)
        return self._run_exec(prompt, cwd=cwd, dry_run=dry_run)

    def _run_exec(self, prompt: str, *, cwd: Path, dry_run: bool = False) -> AgentRunResult:
        health = self.health()
        command = self.render_command(prompt)

        if not health.available:
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=True,
                exit_code=None,
                output="",
                error=health.reason,
                command=command,
            )

        if dry_run:
            return AgentRunResult(
                name=self.name,
                ok=True,
                skipped=True,
                exit_code=None,
                output=f"DRY RUN: {' '.join(command)}",
                error="",
                command=command,
            )

        start = time.monotonic()
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            if proc is not None:
                terminate_process_group(proc)
                stdout, stderr = proc.communicate()
            else:
                stdout, stderr = "", ""
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=False,
                exit_code=None,
                output=(stdout or "").strip(),
                error=f"timed out after {self.config.timeout_seconds}s\n{stderr or ''}".strip(),
                command=command,
                duration_seconds=time.monotonic() - start,
                failure_class="timeout",
            )
        except OSError as exc:
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=False,
                exit_code=None,
                output="",
                error=str(exc),
                command=command,
                duration_seconds=time.monotonic() - start,
                failure_class="launch_failed",
            )

        output = (stdout or "").strip()
        error = (stderr or "").strip()
        ok = proc.returncode == 0
        failure_class = "" if ok else classify_agent_failure(error or output)
        if not ok and failure_class == "auth_required":
            error = f"agent unavailable or unauthenticated: {error or output}"

        return AgentRunResult(
            name=self.name,
            ok=ok,
            skipped=False,
            exit_code=proc.returncode,
            output=output,
            error=error,
            command=command,
            duration_seconds=time.monotonic() - start,
            failure_class=failure_class,
        )

    def _run_tmux(
        self,
        prompt: str,
        *,
        cwd: Path,
        dry_run: bool = False,
        output_path: Path | None = None,
    ) -> AgentRunResult:
        health = self.health()
        session = self.session_name_for(cwd)
        command = self.config.start_command or self.config.command

        if not health.available:
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=True,
                exit_code=None,
                output="",
                error=health.reason,
                command=command,
            )

        marker = f"<<<COUNCLI_DONE:{self.name}:{uuid.uuid4()}>>>"
        request_marker = f"<<<COUNCLI_START:{self.name}:{uuid.uuid4()}>>>"
        prompt_end_marker = f"<<<COUNCLI_PROMPT_END:{self.name}:{uuid.uuid4()}>>>"
        full_prompt = render_tmux_prompt(
            prompt,
            request_marker=request_marker,
            prompt_end_marker=prompt_end_marker,
            done_marker=marker,
            style=self.config.prompt_style,
        )
        if dry_run:
            return AgentRunResult(
                name=self.name,
                ok=True,
                skipped=True,
                exit_code=None,
                output=f"DRY RUN TMUX: {session} <= {full_prompt}",
                error="",
                command=command,
            )

        try:
            ensure_tmux_session(session, command, cwd)
            before = capture_tmux(session)
            send_tmux_text(
                session,
                full_prompt,
                input_method=self.config.input_method,
                submit_keys=self.config.submit_keys,
                post_paste_delay_seconds=self.config.post_paste_delay_seconds,
            )
            if output_path is not None:
                output = wait_for_output_file(output_path, timeout_seconds=self.config.timeout_seconds)
                return AgentRunResult(
                    name=self.name,
                    ok=True,
                    skipped=False,
                    exit_code=0,
                    output=output.strip(),
                    error="",
                    command=command,
                )
            output = wait_for_marker(session, marker, timeout_seconds=self.config.timeout_seconds, previous=before)
            output = extract_latest_exchange(
                output,
                previous=before,
                request_marker=request_marker,
                prompt_end_marker=prompt_end_marker,
            )
            return AgentRunResult(
                name=self.name,
                ok=True,
                skipped=False,
                exit_code=0,
                output=output.strip(),
                error="",
                command=command,
            )
        except TimeoutError as exc:
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=False,
                exit_code=None,
                output=capture_tmux(session),
                error=str(exc),
                command=command,
            )
        except RuntimeError as exc:
            return AgentRunResult(
                name=self.name,
                ok=False,
                skipped=False,
                exit_code=None,
                output="",
                error=str(exc),
                command=command,
            )

    @property
    def session_name(self) -> str:
        return self.config.session_name or f"councli-{self.name}"

    def session_name_for(self, root: Path, *, instance: str | None = None, prefix: str = "councli") -> str:
        return scoped_session_name(root, self.name, instance=instance, prefix=prefix)

    @property
    def done_marker(self) -> str:
        return self.config.done_marker or f"<<<COUNCLI_DONE:{self.name}>>>"


def looks_like_auth_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def classify_agent_failure(text: str) -> str:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in QUOTA_ERROR_MARKERS):
        return "quota_unavailable"
    if any(marker in lowered for marker in AUTH_ERROR_MARKERS):
        return "auth_required"
    if any(marker in lowered for marker in MODEL_ERROR_MARKERS):
        return "model_unconfigured"
    return "launch_failed"


def terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def build_runners(agent_configs: dict[str, AgentConfig]) -> dict[str, AgentRunner]:
    return {name: AgentRunner(name, cfg) for name, cfg in agent_configs.items()}


def tmux(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
    socket_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", tmux_socket_name(socket_name), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def tmux_socket_name(socket_name: str | None = None) -> str:
    return socket_name or os.environ.get("COUNCLI_TMUX_SOCKET", "councli")


def configure_tmux_server(*, detach_key: str = "C-]", socket_name: str | None = None) -> None:
    """Install councli-level tmux defaults without touching the user's tmux server."""
    tmux(["set-option", "-g", "status", "on"], socket_name=socket_name)
    tmux(["set-option", "-g", "status-left", " councli #{session_name} "], socket_name=socket_name)
    tmux(["set-option", "-g", "status-right", f"{detach_key} detach "], socket_name=socket_name)
    tmux(["bind-key", "-n", detach_key, "detach-client"], socket_name=socket_name)


def tmux_session_exists(session: str, *, socket_name: str | None = None) -> bool:
    return tmux(["has-session", "-t", session], socket_name=socket_name).returncode == 0


def tmux_session_names(*, socket_name: str | None = None) -> list[str]:
    if not shutil.which("tmux"):
        return []
    proc = tmux(["list-sessions", "-F", "#{session_name}"], socket_name=socket_name)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def kill_tmux_session(session: str, *, socket_name: str | None = None) -> None:
    proc = tmux(["kill-session", "-t", session], socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not kill tmux session {session}")


def ensure_tmux_session(
    session: str,
    command: list[str],
    cwd: Path,
    *,
    detach_key: str = "C-]",
    socket_name: str | None = None,
) -> None:
    if tmux_session_exists(session, socket_name=socket_name):
        current = tmux_current_path(session, socket_name=socket_name)
        if current is not None and current.resolve() != cwd.resolve():
            raise RuntimeError(
                f"tmux session {session} is attached to {current}, not {cwd}. "
                "Use a project-specific session_name or kill/restart the session."
            )
        configure_tmux_server(detach_key=detach_key, socket_name=socket_name)
        return
    if not command:
        raise RuntimeError(f"no start command configured for tmux session {session}")
    shell_command = shell_join(command)
    proc = tmux(["new-session", "-d", "-s", session, "-c", str(cwd), shell_command], cwd=cwd, socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"could not start tmux session {session}")
    configure_tmux_server(detach_key=detach_key, socket_name=socket_name)
    time.sleep(1.0)


def tmux_attach_command(session: str, *, socket_name: str | None = None) -> str:
    return shlex.join(["tmux", "-L", tmux_socket_name(socket_name), "attach-session", "-t", session])


def attach_tmux_session(session: str, *, socket_name: str | None = None) -> int:
    env = os.environ.copy()
    env.pop("TMUX", None)
    proc = subprocess.run(
        ["tmux", "-L", tmux_socket_name(socket_name), "attach-session", "-t", session],
        env=env,
        check=False,
    )
    return proc.returncode


def start_tmux_raw_capture(
    session: str,
    path: Path,
    *,
    max_bytes: int = 5_000_000,
    backups: int = 3,
    socket_name: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "councli.rawlog",
            "--path",
            str(path),
            "--max-bytes",
            str(max_bytes),
            "--backups",
            str(backups),
        ]
    )
    proc = tmux(["pipe-pane", "-o", "-t", session, command], socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not start raw capture for {session}")


def stop_tmux_raw_capture(session: str, *, socket_name: str | None = None) -> None:
    proc = tmux(["pipe-pane", "-t", session], socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not stop raw capture for {session}")


def tmux_session_info(session: str, *, socket_name: str | None = None) -> dict[str, str]:
    proc = tmux(
        [
            "display-message",
            "-p",
            "-t",
            session,
            "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_dead}",
        ],
        socket_name=socket_name,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not inspect tmux session {session}")
    parts = proc.stdout.rstrip("\n").split("\t")
    while len(parts) < 6:
        parts.append("")
    return {
        "session_name": parts[0],
        "pane_id": parts[1],
        "pane_pid": parts[2],
        "pane_current_path": parts[3],
        "pane_current_command": parts[4],
        "pane_dead": parts[5],
        "tmux_socket": tmux_socket_name(socket_name),
    }


def capture_tmux(session: str, *, last_lines: int = 4000, socket_name: str | None = None) -> str:
    proc = tmux(["capture-pane", "-p", "-J", "-S", f"-{last_lines}", "-t", session], socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not capture tmux session {session}")
    return proc.stdout


def tmux_current_path(session: str, *, socket_name: str | None = None) -> Path | None:
    proc = tmux(["display-message", "-p", "-t", session, "#{pane_current_path}"], socket_name=socket_name)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return Path(value) if value else None


def send_tmux_text(
    session: str,
    text: str,
    *,
    input_method: str = "paste",
    submit_keys: list[str] | None = None,
    post_paste_delay_seconds: float = 0.5,
    socket_name: str | None = None,
) -> None:
    if input_method == "type":
        send_tmux_literal_text(session, text, socket_name=socket_name)
        if post_paste_delay_seconds > 0:
            time.sleep(post_paste_delay_seconds)
        for key in submit_keys or ["Enter"]:
            proc = tmux(["send-keys", "-t", session, key], socket_name=socket_name)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"could not send {key} to {session}")
        return

    # paste-buffer preserves multiline prompts more reliably than send-keys text.
    buffer_name = f"councli-{session}-{uuid.uuid4()}"
    proc = subprocess.run(
        ["tmux", "-L", tmux_socket_name(socket_name), "load-buffer", "-b", buffer_name, "-"],
        input=text,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "could not load tmux buffer")
    proc = tmux(["paste-buffer", "-b", buffer_name, "-t", session], socket_name=socket_name)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"could not paste prompt into {session}")
    tmux(["delete-buffer", "-b", buffer_name], socket_name=socket_name)
    if post_paste_delay_seconds > 0:
        time.sleep(post_paste_delay_seconds)
    for key in submit_keys or ["Enter"]:
        proc = tmux(["send-keys", "-t", session, key], socket_name=socket_name)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"could not send {key} to {session}")


def send_tmux_literal_text(session: str, text: str, *, chunk_size: int = 350, socket_name: str | None = None) -> None:
    for index in range(0, len(text), chunk_size):
        chunk = text[index : index + chunk_size]
        proc = tmux(["send-keys", "-t", session, "-l", "--", chunk], socket_name=socket_name)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"could not type prompt into {session}")
        time.sleep(0.02)


def wait_for_marker(session: str, marker: str, *, timeout_seconds: int, previous: str = "") -> str:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        last = capture_tmux(session)
        new_text = last[len(previous) :] if previous and last.startswith(previous) else last
        if marker in new_text:
            return last
        time.sleep(1.0)
    raise TimeoutError(f"timed out waiting for marker {marker} in tmux session {session}")


def wait_for_output_file(path: Path, *, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if path.exists() and path.stat().st_size > 0:
                return path.read_text(encoding="utf-8")
        except OSError as exc:
            last_error = str(exc)
        time.sleep(1.0)
    detail = f": {last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for output file {path}{detail}")


def extract_latest_exchange(output: str, *, previous: str, request_marker: str, prompt_end_marker: str) -> str:
    if prompt_end_marker in output:
        return output.split(prompt_end_marker, 1)[1].lstrip()
    if request_marker in output:
        return output.split(request_marker, 1)[1].lstrip()
    if previous and output.startswith(previous):
        return output[len(previous) :].lstrip()
    return output


def render_tmux_prompt(
    prompt: str,
    *,
    request_marker: str,
    prompt_end_marker: str,
    done_marker: str,
    style: str = "compact",
) -> str:
    spaced_marker = " ".join(done_marker)
    if style == "verbatim":
        return (
            f"{request_marker}\n"
            f"{prompt}\n\n"
            "When finished, print exactly the following token with all spaces removed:\n"
            f"{spaced_marker}\n"
            f"{prompt_end_marker}"
        )
    body = compact_terminal_prompt(prompt)
    return (
        f"{request_marker} "
        f"{body} "
        "When finished, print exactly the following token with all spaces removed: "
        f"{spaced_marker} "
        f"{prompt_end_marker}"
    )


def compact_terminal_prompt(text: str) -> str:
    return " ".join(text.split())


def compact_one_line(text: str, *, limit: int = 200) -> str | None:
    value = " ".join((text or "").split())
    if not value:
        return None
    if len(value) > limit:
        return value[:limit].rstrip() + " ..."
    return value


def shell_join(command: list[str]) -> str:
    return shlex.join(command)


def scoped_session_name(root: Path, agent: str, *, instance: str | None = None, prefix: str = "councli") -> str:
    digest = project_hash(root)
    parts = [safe_tmux_name(prefix), digest, safe_tmux_name(agent)]
    if instance:
        parts.append(safe_tmux_name(instance))
    return "-".join(part for part in parts if part)


def safe_tmux_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip("-") or "session"


def project_hash(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:10]


def rotate_file(path: Path, *, max_bytes: int, backups: int) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    if backups <= 0:
        try:
            path.unlink()
        except OSError:
            pass
        return
    oldest = path.with_name(f"{path.name}.{backups}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            try:
                src.replace(dst)
            except OSError:
                pass
    try:
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass

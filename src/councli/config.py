from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal
from datetime import datetime, timezone

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


CONFIG_DIR = ".councli"
CONFIG_FILE = "config.yaml"
PROJECT_ID_FILE = "project.json"
TRUST_FILE = "trust.json"
EXECUTABLE_AGENT_FIELDS = (
    "enabled",
    "backend",
    "binary",
    "display_name",
    "capabilities",
    "command_capabilities",
    "broadcast_capabilities",
    "start_capabilities",
    "resume_capabilities",
    "broadcast_policy",
    "version_command",
    "readiness_command",
    "probe_timeout_seconds",
    "readiness_timeout_seconds",
    "command",
    "broadcast_command",
    "broadcast_enabled",
    "broadcast_read_only",
    "broadcast_timeout_seconds",
    "resume_command",
    "session_name",
    "start_command",
    "done_marker",
    "prompt_style",
    "input_method",
    "submit_keys",
    "post_paste_delay_seconds",
    "timeout_seconds",
)
TRUSTED_NATIVE_FIELDS = (
    "tmux_socket",
    "detach_key",
    "raw_log_max_bytes",
    "raw_log_backups",
    "session_prefix",
)
TMUX_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
TMUX_KEY_PATTERN = re.compile(
    r"^(?:"
    r"C-[A-Za-z0-9@\[\]\\^_?]"
    r"|M-[A-Za-z0-9]"
    r"|F(?:[1-9]|1[0-2])"
    r"|Enter|Escape|Esc|Space|Tab|BSpace|Delete"
    r"|Up|Down|Left|Right|Home|End|PageUp|PageDown"
    r")$"
)
COMMAND_CAPABILITIES = (
    "planning_only",
    "reads_workspace",
    "writes_workspace",
    "runs_tools",
    "network_access",
    "full_permission",
)


class ConfigTrustError(ValueError):
    pass


class ProjectIdentityError(ValueError):
    pass


class AgentConfig(BaseModel):
    enabled: bool = True
    backend: Literal["exec", "tmux"] = "exec"
    binary: str
    display_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    command_capabilities: list[str] = Field(default_factory=lambda: ["reads_workspace", "runs_tools"])
    broadcast_capabilities: list[str] = Field(default_factory=list)
    start_capabilities: list[str] = Field(
        default_factory=lambda: ["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"]
    )
    resume_capabilities: list[str] = Field(
        default_factory=lambda: ["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"]
    )
    broadcast_policy: Literal["safe_only", "allow_full_permission"] = "safe_only"
    version_command: list[str] | None = None
    readiness_command: list[str] | None = None
    probe_timeout_seconds: int = Field(default=3, ge=1, le=30)
    readiness_timeout_seconds: int = Field(default=10, ge=1, le=120)
    command: list[str]
    broadcast_command: list[str] | None = None
    broadcast_enabled: bool = True
    broadcast_read_only: bool = True
    broadcast_timeout_seconds: int | None = Field(default=None, ge=1)
    resume_command: list[str] | None = None
    session_name: str | None = None
    start_command: list[str] | None = None
    done_marker: str | None = None
    prompt_style: Literal["compact", "verbatim"] = "compact"
    input_method: Literal["paste", "type"] = "paste"
    submit_keys: list[str] = Field(default_factory=lambda: ["Enter"])
    post_paste_delay_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    timeout_seconds: int = Field(default=900, ge=1)

    @field_validator("command", "broadcast_command")
    @classmethod
    def validate_prompt_placeholder(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for part in value:
            if "{prompt}" in part and part != "{prompt}":
                raise ValueError("{prompt} must be a standalone argv token")
        return value

    @field_validator("version_command", "readiness_command")
    @classmethod
    def validate_probe_command(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any("{prompt}" in part for part in value):
            raise ValueError("probe commands must not contain {prompt}")
        return value

    @field_validator(
        "command_capabilities",
        "broadcast_capabilities",
        "start_capabilities",
        "resume_capabilities",
    )
    @classmethod
    def validate_command_capabilities(cls, value: list[str]) -> list[str]:
        invalid = sorted(set(value) - set(COMMAND_CAPABILITIES))
        if invalid:
            raise ValueError(f"unknown command capability/capabilities: {', '.join(invalid)}")
        return value


class ConsensusConfig(BaseModel):
    max_rounds: int = Field(default=2, ge=1, le=5)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)


ARTIFACT_CLASSES = (
    "raw-log",
    "session-archive",
    "session-snapshot",
    "run",
    "task",
    "project-ledger",
)


class ArtifactConfig(BaseModel):
    prune_default_classes: list[str] = Field(default_factory=lambda: ["raw-log", "session-archive", "session-snapshot"])
    redact_patterns: list[str] = Field(
        default_factory=lambda: [
            r"sk-proj-[A-Za-z0-9_-]{20,}",
            r"sk-[A-Za-z0-9_-]{20,}",
            r"gh[pousr]_[A-Za-z0-9_]{20,}",
            r"glpat-[A-Za-z0-9_-]{20,}",
            r"xox[baprs]-[A-Za-z0-9-]{20,}",
            r"AKIA[0-9A-Z]{16}",
            r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s'\"]{8,}",
        ]
    )
    redact_replacement: str = "[REDACTED]"
    scrub_max_file_bytes: int = Field(default=10_000_000, ge=1024)

    @field_validator("prune_default_classes")
    @classmethod
    def validate_prune_default_classes(cls, value: list[str]) -> list[str]:
        invalid = sorted(set(value) - set(ARTIFACT_CLASSES))
        if invalid:
            raise ValueError(f"unknown artifact class(es): {', '.join(invalid)}")
        return value

    @field_validator("redact_patterns")
    @classmethod
    def validate_redact_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid redaction regex {pattern!r}: {exc}") from exc
        return value


class ContextConfig(BaseModel):
    peer_context_latest_rounds: int = Field(default=2, ge=1, le=10)
    peer_context_per_participant_chars: int = Field(default=6000, ge=1, le=200_000)
    peer_context_total_chars: int = Field(default=24000, ge=1, le=500_000)
    peer_context_include_failures: Literal["summary", "full", "omit"] = "summary"


class NativeConfig(BaseModel):
    tmux_socket: str = "councli"
    detach_key: str = "C-]"
    raw_log_max_bytes: int = Field(default=5_000_000, ge=1024)
    raw_log_backups: int = Field(default=3, ge=0, le=20)
    session_prefix: str = "councli"

    @field_validator("tmux_socket", "session_prefix")
    @classmethod
    def validate_tmux_name(cls, value: str) -> str:
        if not TMUX_NAME_PATTERN.fullmatch(value):
            raise ValueError("must contain only letters, numbers, dot, underscore, or dash")
        return value

    @field_validator("detach_key")
    @classmethod
    def validate_detach_key(cls, value: str) -> str:
        if not TMUX_KEY_PATTERN.fullmatch(value):
            raise ValueError("must be a simple tmux key chord such as C-], C-a, M-x, F1, or Escape")
        return value


class CouncliConfig(BaseModel):
    agents: dict[str, AgentConfig]
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    native: NativeConfig = Field(default_factory=NativeConfig)


DEFAULT_CONFIG = CouncliConfig(
    agents={
        "codex": AgentConfig(
            backend="exec",
            binary="codex",
            display_name="Codex CLI",
            capabilities=["chat", "deliberate", "vote", "broadcast", "assistant"],
            command_capabilities=["planning_only", "reads_workspace"],
            broadcast_capabilities=["planning_only", "reads_workspace"],
            start_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            resume_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            version_command=["codex", "--version"],
            readiness_command=["codex", "doctor"],
            command=[
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "{prompt}",
            ],
            broadcast_command=[
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "{prompt}",
            ],
            resume_command=["codex", "resume", "{session_id}"],
            start_command=["codex", "--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"],
            timeout_seconds=900,
        ),
        "claude": AgentConfig(
            backend="exec",
            binary="claude",
            display_name="Claude Code",
            capabilities=["chat", "deliberate", "vote", "broadcast", "assistant"],
            command_capabilities=["planning_only", "reads_workspace"],
            broadcast_capabilities=["planning_only", "reads_workspace"],
            start_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            resume_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            version_command=["claude", "--version"],
            readiness_command=["claude", "auth", "status"],
            command=["claude", "--permission-mode", "plan", "-p", "{prompt}"],
            broadcast_command=["claude", "--permission-mode", "plan", "-p", "{prompt}"],
            resume_command=["claude", "--resume", "{session_id}"],
            start_command=["claude", "--dangerously-skip-permissions"],
            timeout_seconds=900,
        ),
        "agy": AgentConfig(
            backend="exec",
            binary="agy",
            display_name="AGY",
            capabilities=["chat", "deliberate", "vote", "broadcast", "assistant"],
            command_capabilities=["planning_only", "reads_workspace"],
            broadcast_capabilities=["planning_only", "reads_workspace"],
            start_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            resume_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            version_command=["agy", "--version"],
            readiness_command=["agy", "models"],
            command=["agy", "--sandbox", "--print", "{prompt}"],
            broadcast_command=["agy", "--sandbox", "--print", "{prompt}"],
            resume_command=["agy", "--conversation", "{session_id}"],
            start_command=["agy", "--dangerously-skip-permissions"],
            timeout_seconds=900,
        ),
        "codewhale": AgentConfig(
            backend="exec",
            binary="codewhale",
            display_name="CodeWhale",
            capabilities=["chat", "deliberate", "vote", "broadcast", "assistant"],
            command_capabilities=["reads_workspace", "runs_tools"],
            broadcast_capabilities=["reads_workspace", "runs_tools"],
            start_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            resume_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "network_access", "full_permission"],
            version_command=["codewhale", "--version"],
            readiness_command=["codewhale", "doctor"],
            command=["codewhale", "exec", "{prompt}"],
            broadcast_command=["codewhale", "exec", "{prompt}"],
            resume_command=["codewhale", "--resume", "{session_id}"],
            start_command=["codewhale", "--yolo"],
            timeout_seconds=900,
        ),
        "kimi": AgentConfig(
            backend="exec",
            binary="kimi",
            display_name="Kimi",
            capabilities=["chat", "deliberate", "vote", "broadcast", "assistant"],
            command_capabilities=["reads_workspace", "runs_tools"],
            broadcast_capabilities=["reads_workspace", "runs_tools"],
            start_capabilities=["reads_workspace", "writes_workspace", "runs_tools"],
            resume_capabilities=["reads_workspace", "writes_workspace", "runs_tools"],
            version_command=["kimi", "--version"],
            readiness_command=["kimi", "doctor"],
            command=["kimi", "--prompt", "{prompt}"],
            broadcast_command=["kimi", "--prompt", "{prompt}"],
            resume_command=["kimi", "--session", "{session_id}"],
            start_command=["kimi"],
            timeout_seconds=900,
        ),
    }
)


def project_config_path(root: Path) -> Path:
    return root / CONFIG_DIR / CONFIG_FILE


def project_identity_path(root: Path) -> Path:
    return root / CONFIG_DIR / PROJECT_ID_FILE


def project_trust_path(root: Path) -> Path:
    return councli_state_dir() / "trust" / f"{project_identity(root)['hash']}-{TRUST_FILE}"


def councli_state_dir() -> Path:
    override = os.environ.get("COUNCLI_STATE_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "councli"
    return Path.home() / ".local" / "state" / "councli"


def ensure_project_dir(root: Path) -> Path:
    path = root / CONFIG_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_for_environment(*, disable_missing: bool = False) -> CouncliConfig:
    config = DEFAULT_CONFIG.model_copy(deep=True)
    if not disable_missing:
        return config

    agents: dict[str, AgentConfig] = {}
    for name, agent in config.agents.items():
        agents[name] = agent.model_copy(update={"enabled": shutil.which(agent.binary) is not None})
    return config.model_copy(update={"agents": agents})


def write_default_config(root: Path, *, overwrite: bool = False, disable_missing: bool = False) -> tuple[Path, bool]:
    ensure_project_dir(root)
    path = project_config_path(root)
    if path.exists() and not overwrite:
        return path, False
    config = default_config_for_environment(disable_missing=disable_missing)
    path.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False),
        encoding="utf-8",
    )
    trust_project_config(root, reason="init", repair_identity=True)
    return path, True


def load_config(root: Path) -> CouncliConfig:
    path = project_config_path(root)
    if not path.exists():
        raise FileNotFoundError(f"No councli config at {path}. Run: councli init")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config at {path}: expected a YAML mapping")

    ensure_project_identity(root, initialize=True)
    assert_project_config_trusted(root, raw)

    try:
        return CouncliConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid config at {path}:\n{exc}") from exc


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)


def trust_project_config(
    root: Path,
    *,
    reason: str = "manual",
    repair_identity: bool = False,
) -> tuple[Path, str]:
    path = project_config_path(root)
    if not path.exists():
        raise FileNotFoundError(f"No councli config at {path}. Run: councli init")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config at {path}: expected a YAML mapping")
    try:
        CouncliConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid config at {path}:\n{exc}") from exc

    identity = ensure_project_identity(root, initialize=True, repair=repair_identity)
    digest = executable_config_hash(raw)
    trust = {
        "version": 1,
        "trusted_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "project": identity,
        "config": {
            "path": str(path.resolve()),
            "executable_hash": digest,
            "executable_fields": list(EXECUTABLE_AGENT_FIELDS),
            "native_fields": list(TRUSTED_NATIVE_FIELDS),
            "binaries": resolved_agent_binaries(raw),
        },
    }
    trust_path = project_trust_path(root)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(json.dumps(trust, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return trust_path, digest


def assert_project_config_trusted(root: Path, raw: dict[str, Any]) -> None:
    expected = executable_config_hash(raw)
    trust_path = project_trust_path(root)
    try:
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigTrustError(
            f"Project config contains assistant control fields but is not trusted: {project_config_path(root)}\n"
            "Review the config, then run: councli trust"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigTrustError(
            f"Could not read councli trust pin at {trust_path}. Review config and run: councli trust"
        ) from exc

    actual = ((trust.get("config") or {}).get("executable_hash")) if isinstance(trust, dict) else None
    if actual != expected:
        raise ConfigTrustError(
            f"Project config trusted agent fields changed: {project_config_path(root)}\n"
            "Review assistant commands and transport settings, then run: councli trust"
        )
    assert_binary_paths_trusted(root, raw, trust)


def assert_binary_paths_trusted(root: Path, raw: dict[str, Any], trust: dict[str, Any]) -> None:
    trusted = ((trust.get("config") or {}).get("binaries")) if isinstance(trust, dict) else None
    if not isinstance(trusted, dict):
        return
    current = resolved_agent_binaries(raw)
    path_drifts: list[str] = []
    hash_drifts: list[str] = []
    for name, info in current.items():
        if not info.get("enabled", True):
            continue
        pinned = trusted.get(name)
        if not isinstance(pinned, dict) or "path" not in pinned:
            continue
        if pinned.get("path") != info.get("path"):
            path_drifts.append(
                "{name}: trusted={trusted} current={current}".format(
                    name=name,
                    trusted=pinned.get("path") or "(missing)",
                    current=info.get("path") or "(missing)",
                )
            )
            continue
        pinned_hash = pinned.get("sha256")
        current_hash = info.get("sha256")
        if pinned_hash and current_hash and pinned_hash != current_hash:
            hash_drifts.append(
                "{name}: path={path} trusted_sha256={trusted_hash} current_sha256={current_hash}".format(
                    name=name,
                    path=info.get("path") or "(missing)",
                    trusted_hash=pinned_hash,
                    current_hash=current_hash,
                )
            )
    if path_drifts:
        detail = "\n".join(f"- {line}" for line in path_drifts)
        raise ConfigTrustError(
            "Trusted assistant binary path changed after config trust:\n"
            f"{detail}\n"
            "Review installed assistant binaries and PATH, then run: councli trust"
        )
    if hash_drifts:
        detail = "\n".join(f"- {line}" for line in hash_drifts)
        raise ConfigTrustError(
            "Trusted assistant binary content changed after config trust:\n"
            f"{detail}\n"
            "Review installed assistant binaries and rerun: councli trust"
        )


def executable_config_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(executable_config_payload(raw), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def executable_config_payload(raw: dict[str, Any]) -> dict[str, Any]:
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    native = raw.get("native") if isinstance(raw.get("native"), dict) else {}
    payload: dict[str, Any] = {
        "agents": {},
        "native": {
            field: native.get(field)
            for field in TRUSTED_NATIVE_FIELDS
            if field in native
        },
    }
    for name, value in sorted(agents.items()):
        if not isinstance(value, dict):
            continue
        payload["agents"][str(name)] = {
            field: value.get(field)
            for field in EXECUTABLE_AGENT_FIELDS
            if field in value
        }
    return payload


def resolved_agent_binaries(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for name, value in sorted(agents.items()):
        if not isinstance(value, dict):
            continue
        binary = str(value.get("binary") or "")
        if not binary:
            continue
        resolved = shutil.which(binary)
        result[str(name)] = {
            "binary": binary,
            "enabled": value.get("enabled", True) is not False,
            "path": str(Path(resolved).resolve()) if resolved else None,
            "sha256": file_sha256(Path(resolved).resolve()) if resolved else None,
        }
    return result


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def project_identity(root: Path) -> dict[str, str]:
    resolved = str(root.resolve())
    return {
        "root": resolved,
        "hash": hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10],
    }


def ensure_project_identity(
    root: Path,
    *,
    initialize: bool = False,
    repair: bool = False,
) -> dict[str, str]:
    current = project_identity(root)
    path = project_identity_path(root)
    if not path.exists():
        if initialize:
            ensure_project_dir(root)
            path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return current

    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectIdentityError(f"Could not read project identity at {path}") from exc
    if not isinstance(recorded, dict):
        raise ProjectIdentityError(f"Invalid project identity at {path}")

    if recorded.get("hash") == current["hash"] and recorded.get("root") == current["root"]:
        return current
    if repair:
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return current

    raise ProjectIdentityError(
        "This .councli directory appears to belong to a different project path.\n"
        f"Recorded root: {recorded.get('root') or '-'}\n"
        f"Current root:  {current['root']}\n"
        "If this move/rename is intentional, review the config and run: councli trust --repair-identity"
    )

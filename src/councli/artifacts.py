from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from councli.config import CONFIG_DIR


def new_run_dir(root: Path, prefix: str = "run") -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / CONFIG_DIR / "runs" / f"{timestamp}-{prefix}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(to_jsonable(data), indent=2, sort_keys=True) + "\n")


def atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def to_jsonable(data: Any) -> Any:
    if is_dataclass(data):
        return to_jsonable(asdict(data))
    if isinstance(data, dict):
        return {str(k): to_jsonable(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [to_jsonable(v) for v in data]
    if isinstance(data, Path):
        return str(data)
    return data

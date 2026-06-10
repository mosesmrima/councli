from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def rotate_file(path: Path, *, backups: int) -> None:
    if backups <= 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
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
                dst.chmod(0o600)
            except OSError:
                pass
    try:
        path.replace(path.with_name(f"{path.name}.1"))
        path.with_name(f"{path.name}.1").chmod(0o600)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def open_private_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def write_rotating_stream(path: Path, *, max_bytes: int, backups: int) -> None:
    handle = open_private_append(path)
    try:
        while True:
            chunk = sys.stdin.buffer.read(8192)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                try:
                    current_size = path.stat().st_size
                except OSError:
                    current_size = 0
                remaining_capacity = max_bytes - current_size if max_bytes > 0 else len(chunk) - offset
                if max_bytes > 0 and remaining_capacity <= 0:
                    handle.close()
                    rotate_file(path, backups=backups)
                    handle = open_private_append(path)
                    continue
                write_size = min(len(chunk) - offset, remaining_capacity)
                handle.write(chunk[offset : offset + write_size])
                offset += write_size
    finally:
        try:
            handle.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write tmux pipe-pane output to a private rotating raw log.")
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--backups", type=int, default=3)
    args = parser.parse_args(argv)
    write_rotating_stream(Path(args.path), max_bytes=args.max_bytes, backups=args.backups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

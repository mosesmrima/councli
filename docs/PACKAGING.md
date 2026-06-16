# Packaging and release notes

This project uses `hatchling` as the build backend and exposes the console
entry point:

```toml
[project.scripts]
councli = "councli.cli:app"
```

## Local build

```bash
uv run python -m compileall -q src
uv run pytest -q
uv build
```

Expected artifacts:

```text
dist/councli-<version>.tar.gz
dist/councli-<version>-py3-none-any.whl
```

The wheel must include `councli/schemas/*.schema.json`; `councli verify` and
schema validation depend on those package resources.

Check wheel contents:

```bash
python - <<'PY'
import zipfile
from pathlib import Path

wheel = sorted(Path("dist").glob("councli-*.whl"))[-1]
with zipfile.ZipFile(wheel) as z:
    for name in z.namelist():
        if "schemas" in name:
            print(name)
PY
```

## Test a built wheel

```bash
tmpdir="$(mktemp -d)"
python -m venv "$tmpdir/venv"
"$tmpdir/venv/bin/python" -m pip install dist/councli-*.whl
"$tmpdir/venv/bin/councli" --help
"$tmpdir/venv/bin/python" - <<'PY'
from councli.schema import load_schema

print(load_schema("event")["$id"])
PY
```

On Windows PowerShell, use:

```powershell
py -m venv .packaging-test
.\.packaging-test\Scripts\python.exe -m pip install .\dist\councli-*.whl
.\.packaging-test\Scripts\councli.exe --help
```

## Versioning

Before release, update both:

- `pyproject.toml`
- `src/councli/__init__.py`

Keep them aligned until automated versioning is introduced.

## Release checklist

1. Confirm the worktree is clean except intentional release changes.
2. Run `uv run pytest -q`.
3. Run `uv build`.
4. Inspect wheel contents for schema JSON files.
5. Test install the wheel in a fresh virtual environment.
6. Tag the release, for example `v0.1.0`.
7. Publish to GitHub Releases and/or PyPI.

PyPI upload, once credentials are configured:

```bash
python -m pip install --upgrade twine
python -m twine check dist/*
python -m twine upload dist/*
```

## Platform support

The package is pure Python and declares `Operating System :: OS Independent`.
That means installation should work on Linux, macOS, Windows, and WSL.

Feature support still depends on external binaries:

- Core exec-mode council turns: Linux, macOS, Windows, WSL.
- Native tmux-backed assistant sessions: Linux, macOS, WSL.
- Assistant availability: only when that assistant CLI is installed,
  authenticated, and on `PATH` in the same shell where `councli` runs.

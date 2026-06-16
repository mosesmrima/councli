# Contributing to councli

`councli` is an alpha-stage terminal tool. The main design rule is that it
coordinates existing coding agent CLIs; it should not replace their native
harnesses, auth, slash commands, permission prompts, or tool ecosystems.

## Development setup

Use Python 3.11 or newer.

```bash
git clone https://github.com/mosesmrima/councli.git
cd councli
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

If you use `uv`:

```bash
uv sync
uv run councli --help
```

## Verify changes

Run the full local checks before opening a pull request:

```bash
uv run python -m compileall -q src
uv run pytest -q
uv build
```

If you are not using `uv`, install the project editable with test tooling in
your virtual environment and run the equivalent `python -m compileall`,
`pytest`, and `python -m build` commands.

## Safety expectations

- Do not commit `.councli/`, local run artifacts, raw terminal recordings,
  assistant transcripts, API keys, or private project data.
- Keep changes to trusted command fields explicit. Any config field that can
  launch or control an assistant is intentionally pinned by `councli trust`.
- Prefer adapter changes that preserve each tool's native CLI behavior.
- Use read-only/planning commands for shared chat, deliberation, vote, and
  synthesis paths unless the user explicitly opts into stronger execution.

## Pull request guidance

Small, focused changes are easiest to review. Include:

- What user workflow changed.
- Any platform assumptions.
- Tests or manual verification.
- Any changes to assistant command templates, trust behavior, or artifact
  retention.

For architecture context, start with `docs/ARCHITECTURE.md`,
`docs/PROTOCOL_DESIGN.md`, and `docs/SECURITY_MODEL.md`.

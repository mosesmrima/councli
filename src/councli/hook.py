from __future__ import annotations

from pathlib import Path

import typer

from councli.native import append_project_event


app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def main(
    root: Path = typer.Option(..., "--root", file_okay=False, dir_okay=True, resolve_path=True),
    event: str = typer.Option(..., "--event"),
    participant: str = typer.Option(..., "--participant"),
    session: str = typer.Option(..., "--session"),
) -> None:
    append_project_event(
        root,
        event,
        participant=participant,
        payload={"session_name": session, "source": "tmux-hook"},
    )


if __name__ == "__main__":
    app()

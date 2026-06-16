# Install councli

`councli` is packaged as a normal Python command-line application. The
recommended install method is `pipx`, because it creates an isolated Python
environment and exposes the `councli` shell command on your `PATH`.

## Requirements

- Python 3.11 or newer.
- At least one supported coding assistant CLI on `PATH`, such as `codex`,
  `claude`, `agy`, `codewhale`, or `kimi`.
- Each assistant must already be installed and authenticated in its own native
  way. `councli` does not manage provider API keys or model subscriptions.
- `tmux` is optional. It is needed for native attach/session features, not for
  basic non-interactive council turns.

## Install from GitHub

```bash
pipx install "git+https://github.com/mosesmrima/councli.git"
councli --help
```

Upgrade:

```bash
pipx upgrade councli
```

Uninstall:

```bash
pipx uninstall councli
```

## Install from a local checkout

```bash
git clone https://github.com/mosesmrima/councli.git
cd councli
pipx install .
councli --help
```

During development, reinstall after edits:

```bash
pipx install . --force
```

## Linux

Install Python and `pipx` with your system package manager or `pip`:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Restart the terminal if `pipx ensurepath` asks you to. Optional native attach
support needs `tmux`:

```bash
sudo apt install tmux
```

Use your distribution's equivalent command on Fedora, Arch, Nix, or other
Linux distributions.

## macOS

With Homebrew:

```bash
brew install pipx
pipx ensurepath
brew install tmux
```

Then install `councli`:

```bash
pipx install "git+https://github.com/mosesmrima/councli.git"
```

The `tmux` step is optional unless you want native assistant sessions through
`/assistant` or `councli sessions`.

## Windows

Native Windows can run the core `exec` backend: shared chat, `/deliberate`,
`/vote`, `/broadcast`, `/doctor`, `/status`, and artifact inspection.

Install Python 3.11+ from python.org, the Microsoft Store, or `winget`, then:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install "git+https://github.com/mosesmrima/councli.git"
councli --help
```

`tmux` is not a native Windows dependency. For native attach/session features,
use WSL and install `councli`, `tmux`, and your assistant CLIs inside the WSL
distribution.

## WSL

WSL is the recommended Windows path if you want the full terminal/session
feature set.

Inside WSL:

```bash
sudo apt update
sudo apt install python3 python3-pip pipx tmux
pipx ensurepath
pipx install "git+https://github.com/mosesmrima/councli.git"
```

Install and authenticate assistant CLIs inside WSL as well; Windows-installed
agent binaries may not behave the same way inside a Linux shell.

## First project setup

From the repository or project you want assistants to inspect:

```bash
councli setup
councli doctor
councli
```

`councli setup` creates `.councli/config.yaml`, writes local artifact ignore
rules, trusts generated assistant command templates, and detects installed
assistant CLIs.

Inside the interactive shell:

```text
/agents
/enable claude
/disable kimi
what can you all do?
/deliberate compare sqlite and postgres for this app
/quit
```

## Shell completion

Typer exposes completion helpers:

```bash
councli --install-completion
```

Restart the shell after installing completion.

## Troubleshooting

`councli doctor` is the first command to run when something looks wrong. Common
states:

- `disabled in config`: run `/enable <agent>` in `councli`, or edit
  `.councli/config.yaml` and run `councli trust`.
- `binary not found on PATH`: install that assistant CLI or disable it.
- `auth_required`: launch the assistant directly and sign in with its native
  login flow.
- `model_unconfigured`: configure the assistant's model in its native CLI.
- `tmux not found on PATH`: install `tmux` or use exec-mode commands only.
- `trusted agent fields changed`: review `.councli/config.yaml`, then run
  `councli trust`.

`councli` stores local run artifacts under `.councli/`. Do not commit that
directory; it may contain transcripts, prompts, project details, and raw
terminal captures.
